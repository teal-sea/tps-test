#!/usr/bin/env python3
"""tps-test

Measure how the *same model* performs when served by different providers:
TTFT, inter-token latency (ITL), per-stream output tokens/sec, aggregate
throughput under concurrency, error/429 rate, and cost per request.

Design rules that make the numbers honest:

  * streaming only  -> TTFT and ITL are observable at all
  * exact token counts from `usage` (chars/4 fallback, flagged in output)
  * targets are measured round-robin, interleaved per rep, so a transient
    provider slowdown does not silently land on one row of the table
  * warm-up requests are discarded (connection pool + provider cold start)
  * provider pinning lives in the target config (`extra_body`), so you are
    measuring an upstream, not a router's mood
  * no automatic retries on 429/5xx by default -- retries hide exactly the
    tail behaviour you are trying to measure

Usage:
    python rig.py discover deepseek/deepseek-chat --out targets.toml
    python rig.py run -c targets.toml --reps 7 --concurrency 1,4
    python rig.py report runs/2026-10-27T05-30-00Z
    python rig.py run -c targets.example.toml --mock     # no API keys needed
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

try:  # HTTP/2 is a nice-to-have; HTTP/1.1 is fine and universally supported
    import h2  # noqa: F401

    HTTP2 = True
except ImportError:  # pragma: no cover
    HTTP2 = False


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

# ~170 tokens of filler used to build a prefill-heavy prompt without shipping
# a big fixture file. Repeat it `repeat` times to reach the target context size.
PAD_DOC = (
    "Distributed systems fail in ways that are rarely captured by a single "
    "component's unit tests. A request crosses a load balancer, an "
    "authentication service, a queue, two stateless workers and a shard of a "
    "relational database before a byte reaches the client. Each hop has its "
    "own timeout budget, its own retry policy and its own notion of what "
    "idempotency means. When one hop slows down, the others do not simply "
    "wait: they retry, they queue, they amplify. The classic failure mode is "
    "metastable collapse, where a service that is technically healthy keeps "
    "failing because its queues never drain. Operator intuition tends to "
    "underestimate this because the system looks fine at every individual "
    "dashboard while the aggregate is on fire."
)

DEFAULT_PROMPTS: dict[str, dict[str, Any]] = {
    "prose-256": {
        "text": (
            "Write a continuous technical explanation of how TCP congestion "
            "control works, covering slow start, congestion avoidance, fast "
            "retransmit and CUBIC. Do not use bullet points, headers or lists. "
            "Write flowing paragraphs of at least 500 words."
        ),
        "max_tokens": 256,
    },
    "code-256": {
        "text": (
            "Write a complete, runnable Python implementation of a red-black "
            "tree with insert, delete and search, including type hints and "
            "docstrings. Output only code, at least 120 lines."
        ),
        "max_tokens": 256,
    },
    "long-context": {
        "text": "Summarise the document below in about 150 words.",
        "repeat": 40,
        "max_tokens": 128,
    },
}


def build_prompt(spec: dict[str, Any]) -> str:
    """Turn a prompt spec into the string actually sent."""
    text = spec.get("text", "")
    source = spec.get("document_file")
    if source:
        doc = Path(source).expanduser().read_text(encoding="utf-8")
    else:
        doc = spec.get("document", PAD_DOC)

    repeat = int(spec.get("repeat", 0) or 0)
    parts = [text] if text else []
    if repeat > 0:
        parts.append("\n\n".join([doc] * repeat))
    if not parts:
        raise SystemExit("prompt spec needs `text`, `document` or `document_file`")
    return "\n\n---\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


@dataclass
class Target:
    name: str
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    price_in: float | None = None  # USD per 1M input tokens
    price_out: float | None = None  # USD per 1M output tokens
    max_tokens_field: str | None = None
    send_stream_options: bool = True
    mock: dict[str, Any] | None = None

    def url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        return ""

    def note(self) -> str:
        pin = self.extra_body.get("provider")
        if isinstance(pin, dict):
            only = pin.get("only") or pin.get("order")
            if only:
                return f"pinned:{','.join(only)}"
        return ""


@dataclass
class RunConfig:
    targets: list[Target]
    prompts: dict[str, dict[str, Any]]
    reps: int = 7
    concurrency: list[int] = field(default_factory=lambda: [1])
    warmup: int = 2
    timeout_s: float = 180.0
    temperature: float = 0.0
    max_tokens_field: str = "max_tokens"
    stream_options: bool = True


def load_config(path: Path) -> RunConfig:
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)

    run = raw.get("run", {}) or {}
    target_dicts = raw.get("targets") or []
    if not target_dicts:
        raise SystemExit(f"{path}: no [[targets]] defined")

    targets: list[Target] = []
    for entry in target_dicts:
        price = entry.get("price", {}) or {}
        targets.append(
            Target(
                name=entry.get("name") or entry["model"],
                base_url=entry["base_url"],
                model=entry["model"],
                api_key_env=entry.get("api_key_env"),
                api_key=entry.get("api_key"),
                headers=entry.get("headers", {}) or {},
                extra_body=entry.get("extra_body", {}) or {},
                price_in=price.get("in_per_1m"),
                price_out=price.get("out_per_1m"),
                max_tokens_field=entry.get("max_tokens_field"),
                send_stream_options=bool(entry.get("stream_options", True)),
                mock=entry.get("mock"),
            )
        )

    prompt_defs: dict[str, dict[str, Any]] = {}
    prompt_table = raw.get("prompts", {}) or {}
    for name, spec in prompt_table.items():
        merged = dict(DEFAULT_PROMPTS.get(name, {}))
        merged.update(spec)
        merged.setdefault("max_tokens", 256)
        prompt_defs[name] = merged

    selected = run.get("prompts") or list(prompt_defs) or list(DEFAULT_PROMPTS)[:2]
    if not prompt_defs:
        prompt_defs = {n: dict(DEFAULT_PROMPTS[n]) for n in selected}
    missing = [n for n in selected if n not in prompt_defs]
    if missing:
        raise SystemExit(f"unknown prompt(s): {missing}; have {sorted(prompt_defs)}")

    conc = run.get("concurrency", [1])
    if isinstance(conc, int):
        conc = [conc]

    return RunConfig(
        targets=targets,
        prompts={n: prompt_defs[n] for n in selected},
        reps=int(run.get("reps", 7)),
        concurrency=[int(c) for c in conc],
        warmup=int(run.get("warmup", 2)),
        timeout_s=float(run.get("timeout_s", 180)),
        temperature=float(run.get("temperature", 0.0)),
        max_tokens_field=run.get("max_tokens_field", "max_tokens"),
        stream_options=bool(run.get("stream_options", True)),
    )


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


@dataclass
class Sample:
    target: str
    prompt: str
    concurrency: int
    batch: int
    ok: bool
    status: int | None = None
    error: str | None = None
    ttft_s: float | None = None
    e2e_s: float | None = None
    decode_s: float | None = None
    out_tokens: int | None = None
    in_tokens: int | None = None
    out_tps: float | None = None
    itl_p50_ms: float | None = None
    itl_p90_ms: float | None = None
    chunks: int = 0
    count_source: str = "usage"
    served_by: str | None = None
    batch_wall_s: float | None = None
    wall_clock: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def percentile(values: list[float], q: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


async def stream_once(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
    send_stream_options: bool,
) -> Sample:
    body: dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": cfg.temperature,
        "stream": True,
    }
    field_name = target.max_tokens_field or cfg.max_tokens_field
    body[field_name] = max_tokens
    if send_stream_options:
        body["stream_options"] = {"include_usage": True}
    # extra_body last: it is how provider pinning / vendor flags get applied
    body.update(target.extra_body)

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = target.key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(target.headers)

    sample = Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=False,
        wall_clock=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    t0 = time.perf_counter()
    first = last = None
    gaps: list[float] = []
    chars = 0
    usage: dict[str, Any] | None = None
    served: str | None = None

    try:
        async with client.stream(
            "POST", target.url(), json=body, headers=headers
        ) as response:
            sample.status = response.status_code
            if response.status_code >= 400:
                raw = await response.aread()
                snippet = raw[:600].decode("utf-8", "replace").strip()
                # Some OpenAI-compatible servers reject stream_options outright.
                # That is a compatibility issue, not a performance result, so
                # retry once without it rather than reporting a failed row.
                if (
                    send_stream_options
                    and response.status_code in (400, 422)
                    and "stream_options" in snippet
                ):
                    return await stream_once(
                        client,
                        target,
                        prompt_text,
                        prompt_name,
                        max_tokens,
                        cfg,
                        concurrency,
                        batch,
                        False,
                    )
                sample.error = f"http_{response.status_code}: {snippet}"
                if response.status_code == 429:
                    sample.error = "rate_limited: " + snippet
                sample.e2e_s = time.perf_counter() - t0
                return sample

            served = response.headers.get("x-openrouter-provider") or response.headers.get(
                "x-provider"
            )

            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                if obj.get("provider") and not served:
                    served = str(obj["provider"])
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if not piece and not reasoning:
                    continue
                now = time.perf_counter()
                if first is None:
                    first = now
                    sample.ttft_s = now - t0
                elif piece:
                    gaps.append(now - last if last is not None else 0.0)
                last = now
                sample.chunks += 1
                chars += len(piece)

    except httpx.TimeoutException:
        sample.error = "timeout"
        sample.e2e_s = time.perf_counter() - t0
        return sample
    except (httpx.HTTPError, OSError) as exc:
        sample.error = f"transport: {type(exc).__name__}: {exc}"
        sample.e2e_s = time.perf_counter() - t0
        return sample

    sample.e2e_s = time.perf_counter() - t0
    sample.served_by = served

    if usage:
        sample.out_tokens = usage.get("completion_tokens") or usage.get(
            "output_tokens"
        )
        sample.in_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
        sample.count_source = "usage"
    if sample.out_tokens is None:
        sample.out_tokens = max(1, round(chars / 4))
        sample.count_source = "chars/4"

    if first is not None and last is not None and last > first:
        sample.decode_s = last - first
        if sample.out_tokens:
            sample.out_tps = sample.out_tokens / sample.decode_s
        if gaps:
            sample.itl_p50_ms = percentile(gaps, 0.5) * 1000
            sample.itl_p90_ms = percentile(gaps, 0.9) * 1000
    elif first is not None and sample.e2e_s:
        sample.decode_s = max(sample.e2e_s - sample.ttft_s, 1e-3)
        if sample.out_tokens:
            sample.out_tps = sample.out_tokens / sample.decode_s

    sample.ok = bool(sample.chunks and sample.out_tokens)
    if not sample.ok and not sample.error:
        sample.error = "empty_stream"
    return sample


async def run_target_batch(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
) -> list[Sample]:
    if target.mock is not None:
        batch = [
            mock_sample(target, prompt_name, concurrency, batch, max_tokens, i)
            for i in range(concurrency)
        ]
        wall = max(s.e2e_s or 0.0 for s in batch)
        for sample in batch:
            sample.batch_wall_s = wall
        return batch

    async def one() -> Sample:
        return await stream_once(
            client,
            target,
            prompt_text,
            prompt_name,
            max_tokens,
            cfg,
            concurrency,
            batch,
            target.send_stream_options and cfg.stream_options,
        )

    started = time.perf_counter()
    samples = await asyncio.gather(*(one() for _ in range(concurrency)))
    wall = time.perf_counter() - started
    for sample in samples:
        sample.batch_wall_s = wall
    return samples


def mock_sample(
    target: Target,
    prompt_name: str,
    concurrency: int,
    batch: int,
    max_tokens: int,
    index: int = 0,
) -> Sample:
    """Synthesise a plausible sample. No network, no sleeping."""
    import random

    prof = target.mock or {}
    rng = random.Random(f"{target.name}:{prompt_name}:{batch}:{index}")
    ttft = rng.uniform(*prof.get("ttft", (0.35, 0.8)))
    itl = rng.uniform(*prof.get("itl", (0.012, 0.028)))
    tokens = rng.randint(int(max_tokens * 0.85), max_tokens)
    # concurrency degrades per-stream ITL, as real serving stacks do
    itl *= 1 + 0.06 * (concurrency - 1)
    decode = itl * tokens
    return Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=True,
        status=200,
        ttft_s=ttft,
        e2e_s=ttft + decode,
        decode_s=decode,
        out_tokens=tokens,
        in_tokens=estimate_tokens(DEFAULT_PROMPTS.get(prompt_name, {}).get("text", "")),
        out_tps=tokens / decode,
        itl_p50_ms=itl * 1000,
        itl_p90_ms=itl * 1000 * 1.4,
        chunks=tokens,
        count_source="mock",
        served_by=target.note() or "mock",
        batch_wall_s=ttft + decode,
        wall_clock=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def rotate(items: list[Any], n: int) -> list[Any]:
    if not items:
        return items
    n %= len(items)
    return items[n:] + items[:n]


async def execute(cfg: RunConfig, outdir: Path, mock: bool) -> list[Sample]:
    samples: list[Sample] = []
    started = time.time()

    limits = httpx.Limits(max_connections=256, max_keepalive_connections=64)
    async with httpx.AsyncClient(
        http2=HTTP2,
        timeout=httpx.Timeout(cfg.timeout_s, connect=15.0),
        limits=limits,
        follow_redirects=True,
    ) as client:
        # warm-up: establish TLS/HTTP2, let the provider's cache settle
        for target in cfg.targets:
            for _ in range(cfg.warmup):
                await run_target_batch(
                    client,
                    target,
                    build_prompt(next(iter(cfg.prompts.values()))),
                    next(iter(cfg.prompts)),
                    int(next(iter(cfg.prompts.values())).get("max_tokens", 64)),
                    cfg,
                    1,
                    -1,
                )

        for prompt_name, spec in cfg.prompts.items():
            prompt_text = build_prompt(spec)
            max_tokens = int(spec.get("max_tokens", 256))
            for concurrency in cfg.concurrency:
                for rep in range(cfg.reps):
                    # rotate the order every rep so nobody always runs "first"
                    for target in rotate(cfg.targets, rep):
                        batch_samples = await run_target_batch(
                            client,
                            target,
                            prompt_text,
                            prompt_name,
                            max_tokens,
                            cfg,
                            concurrency,
                            rep,
                        )
                        samples.extend(batch_samples)
                        ok = sum(1 for s in batch_samples if s.ok)
                        rate = [
                            s.out_tps for s in batch_samples if s.out_tps
                        ]
                        med = statistics.median(rate) if rate else 0.0
                        print(
                            f"[{time.time() - started:7.1f}s] {prompt_name:<13}"
                            f" c={concurrency:<3} rep={rep:<3} {target.name:<24}"
                            f" ok={ok}/{concurrency:<3} tok/s={med:7.1f}",
                            file=sys.stderr,
                            flush=True,
                        )

    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "raw.jsonl", "w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample.as_dict()) + "\n")
    meta = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mock": mock,
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "temperature": cfg.temperature,
        "targets": [
            {
                "name": t.name,
                "model": t.model,
                "base_url": t.base_url,
                "pin": t.extra_body.get("provider"),
                "price_in": t.price_in,
                "price_out": t.price_out,
            }
            for t in cfg.targets
        ],
        "prompts": list(cfg.prompts),
        "http2": HTTP2,
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return samples


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def load_samples(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = run_dir / "raw.jsonl"
    if not raw.exists():
        raise SystemExit(f"{raw} not found")
    samples = [
        json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines() if line
    ]
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return samples, meta


def bar(value: float, peak: float, width: int = 28) -> str:
    if not peak or not value:
        return ""
    filled = max(1, round(width * value / peak))
    return "█" * filled


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Group by (prompt, target) and by (prompt, target, concurrency).

    The latency table uses the *lowest* concurrency configured for each prompt
    (normally 1), because mixing c=1 and c=4 samples into one percentile is how
    people accidentally invent a 30% regression that never happened.
    """
    per_stream: dict[tuple[str, str], dict[str, Any]] = {}
    aggregate: dict[tuple[str, str, int], dict[str, Any]] = {}
    min_conc: dict[str, int] = {}

    for s in samples:
        if s["batch"] == -1:  # warm-up
            continue
        min_conc[s["prompt"]] = min(min_conc.get(s["prompt"], 10**6), s["concurrency"])

    for s in samples:
        if s["batch"] == -1:  # warm-up
            continue
        key = (s["prompt"], s["target"])
        slot = per_stream.setdefault(
            key,
            {
                "ttft": [],
                "e2e": [],
                "tps": [],
                "itl": [],
                "in": [],
                "out": [],
                "ok": 0,
                "n": 0,
                "conc": 1,
                "errors": {},
                "served": set(),
                "count_source": set(),
            },
        )
        if s["concurrency"] == min_conc[s["prompt"]]:
            # only the lowest-concurrency samples feed the per-stream table
            slot["n"] += 1
            slot["conc"] = s["concurrency"]
            slot["served"].add(s.get("served_by"))
            slot["count_source"].add(s.get("count_source"))
            if s["ok"]:
                slot["ok"] += 1
                slot["ttft"].append(s["ttft_s"])
                slot["e2e"].append(s["e2e_s"])
                slot["tps"].append(s["out_tps"])
                slot["itl"].append(s["itl_p50_ms"])
                slot["out"].append(s["out_tokens"])
                slot["in"].append(s["in_tokens"])
            else:
                err = (s.get("error") or "unknown").split(":")[0]
                slot["errors"][err] = slot["errors"].get(err, 0) + 1

        bkey = (s["prompt"], s["target"], s["concurrency"])
        agg = aggregate.setdefault(bkey, {"batches": [], "ok": 0, "n": 0, "errors": {}})
        agg["n"] += 1
        if s["ok"]:
            agg["ok"] += 1
        else:
            err = (s.get("error") or "unknown").split(":")[0]
            agg["errors"][err] = agg["errors"].get(err, 0) + 1

    # aggregate throughput per batch (sum of tokens / wall clock of the batch)
    batch_tokens: dict[tuple, float] = {}
    for s in samples:
        if s["batch"] == -1 or not s["ok"]:
            continue
        bkey = (s["prompt"], s["target"], s["concurrency"], s["batch"])
        batch_tokens[bkey] = batch_tokens.get(bkey, 0) + (s["out_tokens"] or 0)
    batch_wall: dict[tuple, float] = {}
    for s in samples:
        if s["batch"] == -1:
            continue
        bkey = (s["prompt"], s["target"], s["concurrency"], s["batch"])
        batch_wall[bkey] = max(batch_wall.get(bkey, 0.0), s.get("batch_wall_s") or 0.0)

    for bkey, tokens in batch_tokens.items():
        prompt, target, conc, _ = bkey
        wall = batch_wall.get(bkey) or 0.0
        if wall > 0:
            aggregate[(prompt, target, conc)]["batches"].append(tokens / wall)

    return {"per_stream": per_stream, "aggregate": aggregate}


def fmt(value: float | None, digits: int = 1, scale: float = 1.0) -> str:
    if value is None:
        return "—"
    return f"{value * scale:.{digits}f}"


def write_report(
    run_dir: Path, samples: list[dict[str, Any]], meta: dict[str, Any]
) -> str:
    stats = summarize(samples)
    per_stream = stats["per_stream"]
    aggregate = stats["aggregate"]
    targets = [t["name"] for t in meta.get("targets", [])]
    prompts = list(dict.fromkeys(p for (p, _t) in per_stream))
    tag = " (MOCK DATA)" if meta.get("mock") else ""

    lines: list[str] = []
    lines.append(f"# Provider throughput rig — {run_dir.name}{tag}")
    lines.append("")
    model = next((t["model"] for t in meta.get("targets", [])), "?")
    lines.append(
        f"- model: `{model}` · reps {meta.get('reps')} · "
        f"concurrency {meta.get('concurrency')} · temp {meta.get('temperature')} · "
        f"http2 {meta.get('http2')}"
    )
    lines.append(f"- captured: {meta.get('created')}")
    lines.append("")
    lines.append(
        "TTFT = time to first content token. ITL = gap between content chunks "
        "(lower is smoother). tok/s = per-stream decode rate "
        "(`completion_tokens / decode_window`). Aggregate = sum of tokens over "
        "batch wall clock."
    )
    lines.append("")

    for prompt in prompts:
        lines.append(f"## `{prompt}` — single stream")
        lines.append("")
        lines.append(
            "| provider | TTFT p50 | TTFT p90 | out tok/s p50 | out tok/s p90 | "
            "ITL p50 ms | E2E p50 | out tok | ok |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        rows = []
        for target in targets:
            slot = per_stream.get((prompt, target))
            if not slot:
                continue
            rows.append((target, slot))
        for target, slot in rows:
            lines.append(
                "| {t} | {a} | {b} | {c} | {d} | {e} | {f} | {g} | {h}/{i} |".format(
                    t=target,
                    a=fmt(percentile(slot["ttft"], 0.5), 2),
                    b=fmt(percentile(slot["ttft"], 0.9), 2),
                    c=fmt(percentile(slot["tps"], 0.5)),
                    d=fmt(percentile(slot["tps"], 0.9)),
                    e=fmt(percentile(slot["itl"], 0.5)),
                    f=fmt(percentile(slot["e2e"], 0.5), 2),
                    g=fmt(statistics.median(slot["out"]) if slot["out"] else None, 0),
                    h=slot["ok"],
                    i=slot["n"],
                )
            )
        lines.append("")

        peak = max(
            (
                percentile(per_stream[(prompt, t)]["tps"], 0.5) or 0
                for t in targets
                if (prompt, t) in per_stream
            ),
            default=0.0,
        )
        lines.append("```")
        for target in targets:
            slot = per_stream.get((prompt, target))
            if not slot:
                continue
            p50 = percentile(slot["tps"], 0.5) or 0
            extra = ""
            if slot["errors"]:
                extra = "  " + " ".join(f"[{k}×{v}]" for k, v in slot["errors"].items())
            lines.append(f"{target:<24} {bar(p50, peak)} {p50:7.1f} tok/s{extra}")
        lines.append("```")
        lines.append("")

        concs = sorted(
            {c for (p, _t, c) in aggregate if p == prompt}, reverse=False
        )
        if len(concs) > 1 or (concs and concs[0] != 1):
            lines.append(f"## `{prompt}` — aggregate throughput (tokens/sec)")
            lines.append("")
            header = "| provider | " + " | ".join(f"c={c}" for c in concs) + " |"
            lines.append(header)
            lines.append("|---" * (len(concs) + 1) + "|")
            for target in targets:
                cells = []
                for c in concs:
                    agg = aggregate.get((prompt, target, c))
                    val = statistics.median(agg["batches"]) if agg and agg["batches"] else None
                    cells.append(fmt(val))
                lines.append(f"| {target} | " + " | ".join(cells) + " |")
            lines.append("")

    # cost + provenance
    lines.append("## Cost & provenance")
    lines.append("")
    lines.append(
        "| provider | $/1M in | $/1M out | est. $/1k requests | cost of 1M generated tokens | served by | token source |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    price_by_name = {t["name"]: t for t in meta.get("targets", [])}
    for target in targets:
        info = price_by_name.get(target, {})
        pin = info.get("pin") or {}
        served = set()
        source = set()
        cost_per_1k = None
        cost_per_mtok = None
        for (p, t), slot in per_stream.items():
            if t != target:
                continue
            served |= {x for x in slot["served"] if x}
            source |= {x for x in slot["count_source"] if x}
        median_in = statistics.median(
            [
                v
                for (_p, t), s in per_stream.items()
                if t == target
                for v in s["in"]
                if v is not None
            ]
            or [0]
        )
        median_out = statistics.median(
            [
                v
                for (_p, t), s in per_stream.items()
                if t == target
                for v in s["out"]
                if v is not None
            ]
            or [0]
        )
        if info.get("price_in") is not None and info.get("price_out") is not None:
            per_request = (
                median_in * info["price_in"] + median_out * info["price_out"]
            ) / 1e6
            cost_per_1k = per_request * 1000
            # fully-loaded cost of a million output tokens, incl. its prompt
            if median_out:
                cost_per_mtok = per_request * (1e6 / median_out)
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                target,
                fmt(info.get("price_in"), 3),
                fmt(info.get("price_out"), 3),
                fmt(cost_per_1k, 3),
                fmt(cost_per_mtok, 2),
                ", ".join(sorted(served))
                or ("pin:" + ",".join(pin.get("only", [])) if pin else "—"),
                ", ".join(sorted(source)),
            )
        )
    lines.append("")
    lines.append(
        "> Provider attribution comes from the response (`provider` field or "
        "`x-openrouter-provider` header) when the gateway exposes it. If that "
        "column is empty you were routed by a gateway, not pinned to an upstream."
    )

    # failures -- counted across every concurrency level, not just c=1
    totals: dict[str, int] = {}
    errors: dict[str, dict[str, int]] = {}
    for s in samples:
        if s.get("batch", 0) == -1:
            continue
        name = s["target"]
        totals[name] = totals.get(name, 0) + 1
        if not s.get("ok"):
            code = (s.get("error") or "unknown").split(":")[0]
            errors.setdefault(name, {})
            errors[name][code] = errors[name].get(code, 0) + 1
    if errors:
        lines.append("")
        lines.append("## Failures")
        lines.append("")
        lines.append("| provider | failed / total | error classes |")
        lines.append("|---|---|---|")
        for name in sorted(errors):
            failed = sum(errors[name].values())
            detail = ", ".join(f"{k} × {v}" for k, v in sorted(errors[name].items()))
            lines.append(f"| {name} | {failed} / {totals.get(name, 0)} | {detail} |")
        lines.append("")
        lines.append(
            "Retries are intentionally off: a provider that 429s at concurrency 16 "
            "is telling you something a retry would erase."
        )

    report = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(report, encoding="utf-8")

    # csv
    with open(run_dir / "summary.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "prompt",
                "provider",
                "concurrency",
                "ttft_p50_s",
                "ttft_p90_s",
                "out_tps_p50",
                "out_tps_p90",
                "itl_p50_ms",
                "e2e_p50_s",
                "aggregate_tps",
                "out_tokens_median",
                "ok",
                "n",
                "errors",
            ]
        )
        for (prompt, target), slot in sorted(per_stream.items()):
            concs = sorted(
                {c for (p, t, c) in aggregate if p == prompt and t == target}
            )
            for conc in concs:
                agg = aggregate.get((prompt, target, conc), {})
                agg_tps = (
                    statistics.median(agg["batches"]) if agg.get("batches") else None
                )
                writer.writerow(
                    [
                        prompt,
                        target,
                        conc,
                        fmt(percentile(slot["ttft"], 0.5), 3),
                        fmt(percentile(slot["ttft"], 0.9), 3),
                        fmt(percentile(slot["tps"], 0.5)),
                        fmt(percentile(slot["tps"], 0.9)),
                        fmt(percentile(slot["itl"], 0.5)),
                        fmt(percentile(slot["e2e"], 0.5), 3),
                        fmt(agg_tps),
                        fmt(statistics.median(slot["out"]) if slot["out"] else None, 0),
                        slot["ok"],
                        slot["n"],
                        "; ".join(f"{k}×{v}" for k, v in slot["errors"].items()),
                    ]
                )

    return report


# --------------------------------------------------------------------------
# discover (OpenRouter)
# --------------------------------------------------------------------------


def discover(
    model_slug: str,
    out_path: Path | None,
    api_key: str | None,
    from_json: Path | None,
    json_out: Path | None,
) -> None:
    if from_json:
        payload = json.loads(from_json.read_text(encoding="utf-8"))
    else:
        url = f"https://openrouter.ai/api/v1/models/{model_slug.strip('/')}/endpoints"
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
        if response.status_code >= 400:
            raise SystemExit(
                f"GET {url} -> {response.status_code}\n{response.text[:500]}\n"
                "Pass --api-key or set OPENROUTER_API_KEY for private models."
            )
        payload = response.json()

    if json_out:
        json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    data = payload.get("data", payload)
    if isinstance(data, list):
        data = data[0] if data else {}
    endpoints = data.get("endpoints") or []
    if not endpoints:
        raise SystemExit("no endpoints in response — dump it with --json and inspect")

    canonical = data.get("id") or data.get("slug") or model_slug
    print(f"{canonical}: {len(endpoints)} endpoint(s)\n", file=sys.stderr)
    print(
        f"{'provider':<20} {'slug':<16} {'quant':<7} {'$/1M in':>8} {'$/1M out':>8} "
        f"{'p50 t/s*':>8} {'p50 ttft*':>9} {'status':>6}"
    )
    for endpoint in endpoints:
        pricing = endpoint.get("pricing") or {}
        print(
            f"{_text(endpoint.get('provider_name'), 20):<20} "
            f"{provider_slug(endpoint):<16} "
            f"{_text(endpoint.get('quantization'), 7):<7} "
            f"{fmt(_per_million(pricing.get('prompt')), 3):>8} "
            f"{fmt(_per_million(pricing.get('completion')), 3):>8} "
            f"{fmt(_stat(endpoint.get('throughput_last_30m'), 'p50'), 1):>8} "
            f"{fmt(_stat(endpoint.get('latency_last_30m'), 'p50'), 2):>9} "
            f"{_text(endpoint.get('status'), 6):>6}"
        )
    print(
        "\n* provider-reported rolling stats (30m), not your measurement; "
        "often null without an API key.",
        file=sys.stderr,
    )

    if out_path:
        blocks = [
            "# generated by: rig.py discover " + canonical,
            "# each target pins ONE upstream: allow_fallbacks = false means a",
            "# failure shows up as an error row instead of silently rerouting.",
            "",
            "[run]",
            "reps = 7",
            "concurrency = [1, 4]",
            'prompts = ["prose-256", "code-256"]',
            "",
        ]
        # Pin the *full* tag slug (e.g. `baseten/fast`, `sail-research/fp4/us`).
        # A base slug like `baseten` matches every endpoint of that provider but
        # explicitly does NOT match tier endpoints, so pinning `baseten` while
        # labelling the row "priority" would silently measure the wrong thing.
        groups: dict[str, list[dict[str, Any]]] = {}
        for endpoint in endpoints:
            groups.setdefault(str(endpoint.get("tag") or provider_slug(endpoint)), []).append(
                endpoint
            )

        collapsed = 0
        labels: dict[str, int] = {}
        for tag, group in groups.items():
            group.sort(
                key=lambda e: (
                    _per_million((e.get("pricing") or {}).get("prompt")) or 1e9
                )
                + (_per_million((e.get("pricing") or {}).get("completion")) or 1e9)
            )
            endpoint = group[0]
            collapsed += len(group) - 1

            quant = endpoint.get("quantization")
            if quant in (None, "unknown"):
                quant = None
            label = _text(endpoint.get("provider_name"), 40) or tag
            if quant:
                label = f"{label} ({quant})"
            tier = endpoint.get("service_tier")
            if tier:
                label = f"{label} [{tier}]"
            labels[label] = labels.get(label, 0) + 1
            if labels[label] > 1:
                label = f"{label} #{labels[label]}"

            pricing = endpoint.get("pricing") or {}
            price_in = _per_million(pricing.get("prompt"))
            price_out = _per_million(pricing.get("completion"))

            blocks.append("[[targets]]")
            blocks.append(f'name = "{label}"')
            if len(group) > 1:
                blocks.append(f"# {len(group)} endpoints share this pin; cheapest kept")
            blocks.append('base_url = "https://openrouter.ai/api/v1"')
            blocks.append(f'model = "{canonical}"')
            blocks.append('api_key_env = "OPENROUTER_API_KEY"')
            pin: dict[str, Any] = {"only": [tag], "allow_fallbacks": False}
            if quant:
                pin["quantizations"] = [quant]
            blocks.append("extra_body.provider = " + _toml_inline(pin))
            price = {}
            if price_in:
                price["in_per_1m"] = round(price_in, 4)
            if price_out:
                price["out_per_1m"] = round(price_out, 4)
            if price:
                blocks.append("price = " + _toml_inline(price))
            blocks.append("")
        out_path.write_text("\n".join(blocks), encoding="utf-8")
        note = f", collapsed {collapsed} indistinguishable endpoint(s)" if collapsed else ""
        print(
            f"\nwrote {out_path} ({len(groups)} pinned targets{note})", file=sys.stderr
        )


def provider_slug(endpoint: dict[str, Any]) -> str:
    """`tag` looks like `deepinfra/fp4/flex`; the router's pin key is the first part."""
    tag = endpoint.get("tag")
    if isinstance(tag, str) and tag:
        return tag.split("/")[0]
    name = endpoint.get("provider_name") or endpoint.get("name") or "unknown"
    return str(name).lower().replace(" ", "-")


def _text(value: Any, width: int) -> str:
    text = "—" if value is None else str(value)
    return text[:width]


def _stat(value: Any, key: str) -> float | None:
    if isinstance(value, dict):
        for candidate in (key, "p50", "mean", "value"):
            if value.get(candidate) is not None:
                try:
                    return float(value[candidate])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def _toml_inline(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_inline(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)


# --------------------------------------------------------------------------
# fingerprint: are they even serving the same weights?
# --------------------------------------------------------------------------
#
# Same model slug does not mean same artifact. A cheaper endpoint may serve a
# different quantization (fp4 vs fp8), a distilled sibling, an older revision,
# or a different model entirely. Price and speed cannot tell you which.
#
# Signal used here: greedy decoding at temperature 0 is a near-deterministic
# function of the weights. Identical weights + identical kernels reproduce the
# same tokens; fp4 vs fp8 diverges within a few dozen tokens; a distilled
# model diverges almost immediately. Two confounders are handled explicitly:
#
#   1. batching/numerics make *any* endpoint non-deterministic, so each target
#      is sampled twice and its self-agreement is the noise floor. Cross-target
#      agreement is only meaningful relative to that floor.
#   2. tokenizer/template differences change `usage.prompt_tokens` for the same
#      input string, which is cheap to read and impossible to explain away.
#
# This is evidence, not proof. Treat the clusters as "worth investigating".


@dataclass
class Fingerprint:
    target: str
    prompt: str
    rep: int
    ok: bool
    error: str | None = None
    text: str = ""
    tokens: list[str] = field(default_factory=list)
    logprobs: list[float] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    mode: str = "tokens"  # "tokens" | "text" (no logprobs support)


async def fingerprint_once(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    rep: int,
    use_logprobs: bool = True,
) -> Fingerprint:
    field_name = target.max_tokens_field or cfg.max_tokens_field
    body: dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.0,
        "stream": False,
        field_name: max_tokens,
    }
    if use_logprobs:
        body["logprobs"] = True
        body["top_logprobs"] = 5
    body.update(target.extra_body)

    headers = {"Content-Type": "application/json"}
    if target.key():
        headers["Authorization"] = f"Bearer {target.key()}"
    headers.update(target.headers)

    result = Fingerprint(target=target.name, prompt=prompt_name, rep=rep, ok=False)
    try:
        response = await client.post(target.url(), json=body, headers=headers)
        if response.status_code >= 400:
            text = response.text[:600]
            if (
                use_logprobs
                and response.status_code in (400, 422)
                and "logprob" in text.lower()
            ):
                # provider doesn't implement logprobs; exact-text mode still
                # detects "different model" but not "same model, different quant"
                return await fingerprint_once(
                    client, target, prompt_text, prompt_name, max_tokens, cfg, rep, False
                )
            result.error = f"http_{response.status_code}: {text}"
            return result
        payload = response.json()
    except (httpx.HTTPError, OSError) as exc:
        result.error = f"transport: {type(exc).__name__}: {exc}"
        return result
    except json.JSONDecodeError:
        result.error = "non_json_response"
        return result

    usage = payload.get("usage") or {}
    result.prompt_tokens = usage.get("prompt_tokens")
    result.completion_tokens = usage.get("completion_tokens")

    choices = payload.get("choices") or []
    if not choices:
        result.error = "no_choices"
        return result
    choice = choices[0]
    message = choice.get("message") or {}
    result.text = message.get("content") or ""
    logprob_block = choice.get("logprobs") or {}
    items = logprob_block.get("content") or []
    for item in items:
        token = item.get("token")
        if token is None:
            continue
        result.tokens.append(token)
        result.logprobs.append(item.get("logprob"))
    if not result.tokens:
        result.mode = "text"
    result.ok = bool(result.text or result.tokens)
    if not result.ok and not result.error:
        result.error = "empty_response"
    return result


def token_agreement(a: list[str], b: list[str], mode_a: str, mode_b: str) -> tuple[float, int | None]:
    """Fraction of aligned token positions that match, and first divergence."""
    if mode_a == "text" or mode_b == "text":
        # coarse: whole-string equality when token ids aren't available
        if not a or not b:
            return 0.0, None
        return (1.0 if a == b else 0.0), (None if a == b else 0)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0, None
    same = 0
    first = None
    for i in range(n):
        if a[i] == b[i]:
            same += 1
        elif first is None:
            first = i
    return same / n, first


def compare_prints(a: Fingerprint, b: Fingerprint) -> tuple[float, int | None]:
    """Agreement between two fingerprints, degrading to text equality when a
    provider couldn't return token-level logprobs."""
    if a.mode == "text" or b.mode == "text":
        if not a.text or not b.text:
            return 0.0, None
        same = a.text == b.text
        return (1.0 if same else 0.0), (None if same else 0)
    return token_agreement(a.tokens, b.tokens, a.mode, b.mode)


def fingerprint_reports(
    prints: list[Fingerprint], targets: list[str], prompts: list[str]
) -> str:
    by_key: dict[tuple[str, str, int], Fingerprint] = {}
    for item in prints:
        by_key[(item.target, item.prompt, item.rep)] = item

    lines: list[str] = ["# Fingerprint: same weights or just the same name?", ""]
    lines.append(
        "Greedy decode (temp 0). `self` = the provider's agreement with itself "
        "across reps — its noise floor. `vs` agreement below the floor is not "
        "evidence of different weights."
    )
    lines.append("")

    # 1. tokenizer / template signature
    lines.append("## Tokenizer & chat-template signature")
    lines.append("")
    lines.append("| provider | prompt_tokens (same input) | completion_tokens | logprobs |")
    lines.append("|---|---|---|---|")
    signatures: dict[str, set[int]] = {}
    for target in targets:
        value = None
        mode = "?"
        for prompt in prompts:
            item = by_key.get((target, prompt, 0))
            if item and item.prompt_tokens is not None:
                value = item.prompt_tokens
                break
        any_item = next(
            (by_key.get((target, p, 0)) for p in prompts if by_key.get((target, p, 0))),
            None,
        )
        if any_item:
            mode = any_item.mode
        if value is not None:
            signatures.setdefault(str(value), set()).add(target)
        lines.append(f"| {target} | {value if value is not None else '—'} | "
                     f"{any_item.completion_tokens if any_item else '—'} | {mode} |")
    lines.append("")
    if len(signatures) > 1:
        groups = "; ".join(
            f"{count} → {', '.join(sorted(names))}" for count, names in
            ((len(v), v) for v in signatures.values())
        )
        lines.append(
            f"⚠ {len(signatures)} distinct prompt-token counts for identical input. "
            f"Different tokenizer or chat template: {groups}."
        )
    else:
        lines.append(
            "All providers tokenized the probe identically — consistent with one "
            "tokenizer/template, which is necessary but not sufficient."
        )
    lines.append("")

    # 2. self-agreement noise floor
    lines.append("## Self-agreement (noise floor)")
    lines.append("")
    lines.append("| provider | token agreement rep0 vs rep1 | first divergence |")
    lines.append("|---|---|---|")
    self_agree: dict[str, float] = {}
    for target in targets:
        best: float | None = None
        first_div: int | None = None
        for prompt in prompts:
            a = by_key.get((target, prompt, 0))
            b = by_key.get((target, prompt, 1))
            if not a or not b or not a.ok or not b.ok:
                continue
            frac, first = compare_prints(a, b)
            if best is None or frac < best:
                best = frac
                first_div = first
        if best is not None:
            self_agree[target] = best
        lines.append(
            f"| {target} | {'—' if best is None else f'{best * 100:.1f}%'} | "
            f"{'—' if first_div is None else first_div} |"
        )
    lines.append("")
    noisy = [t for t, v in self_agree.items() if v < 0.98]
    if noisy:
        lines.append(
            f"⚠ Non-deterministic at temp 0: {', '.join(noisy)}. Their cross-provider "
            "agreement cannot be interpreted — the endpoint itself varies run to run."
        )
    else:
        lines.append("Every provider reproduced itself near-exactly at temp 0.")
    lines.append("")

    # 3. pairwise agreement, first prompt
    prompt = prompts[0]
    lines.append(f"## Pairwise agreement — `{prompt}`")
    lines.append("")
    if len(targets) <= 12:
        lines.append("| | " + " | ".join(f"`{t[:14]}`" for t in targets) + " |")
        lines.append("|---" * (len(targets) + 1) + "|")
        for a_name in targets:
            cells = []
            for b_name in targets:
                if a_name == b_name:
                    value = self_agree.get(a_name)
                    cells.append("—" if value is None else f"**{value * 100:.0f}**")
                    continue
                a = by_key.get((a_name, prompt, 0))
                b = by_key.get((b_name, prompt, 0))
                if not a or not b or not a.ok or not b.ok:
                    cells.append("·")
                    continue
                frac, _ = compare_prints(a, b)
                cells.append(f"{frac * 100:.0f}")
            lines.append(f"| **{a_name}** | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("Diagonal = self-agreement. Values are % of aligned tokens identical.")
        lines.append("")
    else:
        lines.append(
            "Too many targets for a matrix; showing each provider's nearest neighbours."
        )
        lines.append("")
        lines.append("| provider | closest peers (≥99%) | nearest other |")
        lines.append("|---|---|---|")
        for a_name in targets:
            a = by_key.get((a_name, prompt, 0))
            scored = []
            if a and a.ok:
                for b_name in targets:
                    if b_name == a_name:
                        continue
                    b = by_key.get((b_name, prompt, 0))
                    if not b or not b.ok:
                        continue
                    frac, _ = compare_prints(a, b)
                    scored.append((frac, b_name))
            scored.sort(reverse=True)
            peers = [n for f, n in scored if f >= 0.99]
            nearest = f"{scored[0][1]} ({scored[0][0] * 100:.0f}%)" if scored else "—"
            lines.append(f"| {a_name} | {', '.join(peers) or '—'} | {nearest} |")
        lines.append("")

    # 4. clusters
    parent: dict[str, str] = {t: t for t in targets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a_name in enumerate(targets):
        for b_name in targets[i + 1:]:
            a = by_key.get((a_name, prompt, 0))
            b = by_key.get((b_name, prompt, 0))
            if not a or not b or not a.ok or not b.ok:
                continue
            frac, _ = compare_prints(a, b)
            floor = min(self_agree.get(a_name, 1.0), self_agree.get(b_name, 1.0))
            if frac >= max(0.98, floor - 0.01):
                union(a_name, b_name)

    groups: dict[str, list[str]] = {}
    for target in targets:
        groups.setdefault(find(target), []).append(target)

    lines.append("## Clusters")
    lines.append("")
    for index, members in enumerate(
        sorted(groups.values(), key=len, reverse=True), start=1
    ):
        lines.append(f"- **group {index}** ({len(members)}): {', '.join(members)}")
    lines.append("")
    if len(groups) == 1:
        lines.append(
            "One group: indistinguishable at this resolution. Consistent with the "
            "same weights, but token-level agreement can't see small quality "
            "differences — a distilled model sometimes survives 150 tokens."
        )
    else:
        lines.append(
            f"{len(groups)} groups. Members of different groups are *not* the same "
            "artifact: expect different quantization, revision, or model. Confirm "
            "with a task eval before assuming the cheaper one is equivalent."
        )
    lines.append("")
    lines.append(
        "> Limits: a provider without logprobs support is compared on whole-text "
        "equality only (coarse). Two runs never prove identical weights — they "
        "only fail to disprove it."
    )
    return "\n".join(lines) + "\n"


def mock_fingerprint(target: Target, prompt: str, rep: int) -> Fingerprint:
    """Three synthetic 'weight families' so the clustering demo has structure."""
    import hashlib

    family = int(hashlib.sha256(target.name.encode()).hexdigest(), 16) % 3
    tokens = [f"tok{i}_{family}" for i in range(150)]
    # roughly one target in eleven is non-deterministic, so the demo shows the
    # "its own noise floor is too high to judge" warning instead of hiding it
    if rep == 1 and int(hashlib.sha256(target.name.encode()).hexdigest(), 16) % 11 == 0:
        tokens[70:] = [f"alt{i}" for i in range(80)]
    text = " ".join(tokens)
    return Fingerprint(
        target=target.name,
        prompt=prompt,
        rep=rep,
        ok=True,
        text=text,
        tokens=tokens,
        logprobs=[-0.1] * len(tokens),
        prompt_tokens=len(prompt) // 4,
        completion_tokens=len(tokens),
        mode="tokens",
    )


def run_fingerprint(
    cfg: RunConfig, outdir: Path, max_tokens: int, reps: int, prompt_count: int, mock: bool
) -> str:
    prompt_names = list(cfg.prompts)[:prompt_count]
    prints: list[Fingerprint] = []

    async def go() -> None:
        async with httpx.AsyncClient(
            http2=HTTP2, timeout=httpx.Timeout(cfg.timeout_s, connect=15.0)
        ) as client:
            for prompt_name in prompt_names:
                text = build_prompt(cfg.prompts[prompt_name])
                for rep in range(reps):
                    for target in cfg.targets:
                        if mock:
                            prints.append(mock_fingerprint(target, prompt_name, rep))
                            continue
                        print(
                            f"  fingerprint {prompt_name} rep{rep} {target.name}",
                            file=sys.stderr,
                        )
                        prints.append(
                            await fingerprint_once(
                                client, target, text, prompt_name, max_tokens, cfg, rep
                            )
                        )

    asyncio.run(go())

    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "fingerprints.jsonl", "w", encoding="utf-8") as handle:
        for item in prints:
            handle.write(json.dumps(item.__dict__) + "\n")

    report = fingerprint_reports(prints, [t.name for t in cfg.targets], prompt_names)
    if mock:
        report = (
            "> **MOCK** — synthetic data for pipeline testing, not measurements.\n\n"
            + report
        )
    (outdir / "fingerprint.md").write_text(report, encoding="utf-8")
    return report


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rig.py",
        description="Measure throughput/latency of one model across many providers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute a measurement run")
    run_p.add_argument("-c", "--config", required=True, type=Path)
    run_p.add_argument("-o", "--out", type=Path, default=None)
    run_p.add_argument("--reps", type=int, default=None)
    run_p.add_argument("--concurrency", default=None, help="e.g. 1,4,16")
    run_p.add_argument("--prompts", default=None, help="comma separated prompt names")
    run_p.add_argument("--mock", action="store_true", help="synthesise samples")
    run_p.add_argument("--no-warmup", action="store_true")

    rep_p = sub.add_parser("report", help="rebuild report from a run directory")
    rep_p.add_argument("run_dir", type=Path)

    dis_p = sub.add_parser("discover", help="list OpenRouter endpoints for a model")
    dis_p.add_argument("model", help="e.g. deepseek/deepseek-chat")
    dis_p.add_argument("--out", type=Path, default=None, help="write targets.toml")
    dis_p.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY"))
    dis_p.add_argument("--from-json", type=Path, default=None)
    dis_p.add_argument("--json", dest="json_out", type=Path, default=None)

    fp_p = sub.add_parser(
        "fingerprint", help="test whether targets serve the same weights"
    )
    fp_p.add_argument("-c", "--config", required=True, type=Path)
    fp_p.add_argument("-o", "--out", type=Path, default=None)
    fp_p.add_argument("--max-tokens", type=int, default=160)
    fp_p.add_argument("--reps", type=int, default=2, help="reps per target (self-agreement)")
    fp_p.add_argument("--prompts", type=int, default=2, help="how many prompts to probe")
    fp_p.add_argument("--mock", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "report":
        samples, meta = load_samples(args.run_dir)
        text = write_report(args.run_dir, samples, meta)
        print(text)
        return 0

    if args.command == "discover":
        discover(args.model, args.out, args.api_key, args.from_json, args.json_out)
        return 0

    if args.command == "fingerprint":
        cfg = load_config(args.config)
        if args.mock:
            for target in cfg.targets:
                if target.mock is None:
                    target.mock = {}
        else:
            missing = [
                t.name
                for t in cfg.targets
                if not t.key()
                and "localhost" not in t.base_url
                and "127.0.0.1" not in t.base_url
            ]
            if missing:
                print(
                    f"missing API keys for: {', '.join(missing)}\n"
                    "(set the api_key_env variables, or pass --mock)",
                    file=sys.stderr,
                )
                return 2
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        outdir = args.out or Path("runs") / f"{stamp}-fingerprint"
        report = run_fingerprint(
            cfg, outdir, args.max_tokens, args.reps, args.prompts, args.mock
        )
        print(report)
        print(f"\nsaved: {outdir / 'fingerprint.md'}", file=sys.stderr)
        return 0

    cfg = load_config(args.config)
    if args.reps is not None:
        cfg.reps = args.reps
    if args.concurrency:
        cfg.concurrency = [int(c) for c in args.concurrency.split(",") if c]
    if args.prompts:
        wanted = [p.strip() for p in args.prompts.split(",") if p.strip()]
        cfg.prompts = {
            p: (cfg.prompts[p] if p in cfg.prompts else dict(DEFAULT_PROMPTS[p]))
            for p in wanted
        }
    if args.no_warmup:
        cfg.warmup = 0
    if args.mock:
        # --mock turns *any* config into an offline dry run of the whole pipeline
        for target in cfg.targets:
            if target.mock is None:
                target.mock = {}

    if not args.mock:
        missing = [
            t.name
            for t in cfg.targets
            if not t.key() and "localhost" not in t.base_url and "127.0.0.1" not in t.base_url
        ]
        if missing:
            print(
                f"missing API keys for: {', '.join(missing)}\n"
                "(set the api_key_env variables, or pass --mock to dry-run)",
                file=sys.stderr,
            )
            return 2

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    outdir = args.out or Path("runs") / stamp
    samples = asyncio.run(execute(cfg, outdir, args.mock))
    meta = json.loads((outdir / "meta.json").read_text())
    report = write_report(outdir, [s.as_dict() for s in samples], meta)
    print(report)
    print(f"\nraw samples: {outdir / 'raw.jsonl'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
