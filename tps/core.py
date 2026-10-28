"""Engine for tps: config, transport, metrics, reporting, discovery."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import statistics
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__

try:  # HTTP/2 is a nice-to-have; HTTP/1.1 is fine and universally supported
    import h2  # noqa: F401

    HTTP2 = True
except ImportError:  # pragma: no cover
    HTTP2 = False


# --------------------------------------------------------------------------
# errors (stable exit codes for scripts and agents)
# --------------------------------------------------------------------------


class TpsError(Exception):
    """Base error carrying the exit code the CLI should use."""

    exit_code = 1

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UsageError(TpsError):
    """Bad config, bad flags, model not found."""

    exit_code = 1


class MissingKeyError(TpsError):
    """Credential for a target is not set."""

    exit_code = 2


class ProviderError(TpsError):
    """Upstream/API failure while talking to a provider."""

    exit_code = 3


# identify benchmark traffic honestly to the providers being measured
USER_AGENT = f"tps/{__version__} (+https://github.com/teal-sea/tpsbench)"

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
APIS = ("openai", "anthropic")
# Claude API stop reasons in OpenAI's vocabulary, so one report covers both
ANTHROPIC_STOP = {
    "max_tokens": "length",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}
# request fields tps sets itself; overriding them in extra_body would silently
# change what is measured (and what --dry-run budgets for)
RESERVED_BODY = ("model", "messages", "stream", "max_tokens", "max_completion_tokens")


SECRET_HEADER_HINTS = ("key", "token", "auth", "secret", "password")


def redact(text: str, secrets: str | list[str]) -> str:
    """Providers sometimes echo the submitted key in error bodies; never store it.

    Redact the *full* text before truncating it, so a key that straddles the
    cut can't leave its prefix behind."""
    for secret in [secrets] if isinstance(secrets, str) else secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    """Turn a prompt spec into the string actually sent.

    An explicit `document`/`document_file` is included once by default; the
    built-in filler document is only used when `repeat` asks for it.
    """
    text = spec.get("text", "")
    source = spec.get("document_file")
    if source:
        try:
            doc = Path(source).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(
                f"prompt document_file not readable: {source} ({exc.strerror})"
            ) from exc
        default_repeat = 1
    elif "document" in spec:
        doc = str(spec["document"])
        default_repeat = 1
    else:
        doc = PAD_DOC
        default_repeat = 0

    repeat = int(spec.get("repeat", default_repeat) or 0)
    parts = [text] if text else []
    if repeat > 0:
        parts.append("\n\n".join([doc] * repeat))
    if not parts:
        raise UsageError("prompt spec needs `text`, `document` or `document_file`")
    return "\n\n---\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


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
    # "openai" = /chat/completions; "anthropic" = the native Claude Messages
    # API, the only one that accepts Claude subscription (OAuth) tokens
    api: str = "openai"

    def url(self) -> str:
        base = self.base_url.rstrip("/")
        path = "/messages" if self.api == "anthropic" else "/chat/completions"
        return base if base.endswith(path) else base + path

    def auth_headers(self) -> dict[str, str]:
        key = self.key()
        if self.api == "anthropic":
            # API keys go in x-api-key; OAuth / session tokens are bearer tokens,
            # and a Claude subscription OAuth token also needs the oauth beta
            auth = {"x-api-key": key} if key.startswith("sk-ant-api") else {}
            if key and not auth:
                auth = {"Authorization": f"Bearer {key}"}
            if key.startswith("sk-ant-oat"):
                auth["anthropic-beta"] = ANTHROPIC_OAUTH_BETA
            return {"anthropic-version": ANTHROPIC_VERSION, **auth}
        return {"Authorization": f"Bearer {key}"} if key else {}

    def key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        return ""

    def secrets(self) -> list[str]:
        """The API key, credential-looking header values (x-api-key, …) and any
        password embedded in the base URL."""
        values = [self.key()]
        for name, value in self.headers.items():
            if isinstance(value, str) and any(h in name.lower() for h in SECRET_HEADER_HINTS):
                values.append(value)
                values.append(value.removeprefix("Bearer ").strip())
        try:
            values.append(urlparse(self.base_url).password or "")
        except ValueError:
            pass
        return [v for v in values if v]

    def needs_key(self) -> bool:
        """True when a real request would go out unauthenticated to a remote host."""
        return not self.key() and not is_local(self.base_url)

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
    # None = each API's default (0 for OpenAI-compatible, omitted for the Claude
    # API, which rejects it); "omit" = never send it; a number = always send it
    temperature: float | str | None = None
    max_tokens_field: str = "max_tokens"
    stream_options: bool = True
    baseline: str | None = None
    # every prompt that may be selected by name (built-ins + [prompts.*])
    available_prompts: dict[str, dict[str, Any]] = field(default_factory=dict)


def validate(cfg: RunConfig, where: str = "config") -> None:
    """Reject runs that would measure nothing or crash halfway through."""
    if cfg.reps < 1:
        raise UsageError(f"{where}: reps must be at least 1 (got {cfg.reps})")
    if not cfg.concurrency or any(c < 1 for c in cfg.concurrency):
        raise UsageError(
            f"{where}: concurrency levels must be integers ≥ 1 (got {cfg.concurrency})"
        )
    if cfg.warmup < 0:
        raise UsageError(f"{where}: warmup cannot be negative")
    if not math.isfinite(cfg.timeout_s) or cfg.timeout_s <= 0:
        raise UsageError(f"{where}: timeout_s must be a positive number of seconds")
    cfg.temperature = parse_temperature(cfg.temperature, where)
    if not cfg.prompts:
        raise UsageError(f"{where}: no prompts selected")
    for name, spec in cfg.prompts.items():
        try:
            capped = int(spec.get("max_tokens", 256)) >= 1
        except (TypeError, ValueError):
            capped = False
        if not capped:
            raise UsageError(f"{where}: prompt {name!r}: max_tokens must be an integer ≥ 1")
    names = [t.name for t in cfg.targets]
    if cfg.baseline and cfg.baseline not in names:
        raise UsageError(
            f"{where}: baseline {cfg.baseline!r} is not a target name; have {', '.join(names)}"
        )


def parse_temperature(value: Any, where: str) -> float | str | None:
    if value is None or value == "omit":
        return value
    if isinstance(value, str) and value.strip().lower() in ("omit", "none"):
        return "omit"
    try:
        number = float(value) if not isinstance(value, bool) else -1.0
    except (TypeError, ValueError):
        number = -1.0
    if not math.isfinite(number) or number < 0:
        raise UsageError(f'{where}: temperature must be a number ≥ 0, or "omit" (got {value!r})')
    return number


def request_temperature(cfg: RunConfig, target: Target) -> float | None:
    """The temperature to send this target, or None to leave the field out.

    Claude 4.7+ rejects any sampling parameter, so the Claude API default is
    to omit it; OpenAI-compatible targets default to 0 for comparability.
    """
    if cfg.temperature == "omit":
        return None
    if cfg.temperature is None:
        return None if target.api == "anthropic" else 0.0
    return float(cfg.temperature)


def sent_temperature(cfg: RunConfig, target: Target) -> Any:
    """What a request to this target actually carries: extra_body is applied
    last and may set (or null) temperature itself."""
    if "temperature" in target.extra_body:
        return target.extra_body["temperature"]
    return request_temperature(cfg, target)


def _table(entry: dict[str, Any], key: str, label: str, path: Path) -> dict[str, Any]:
    value = entry.get(key) or {}
    if not isinstance(value, dict):
        raise UsageError(f"{path}: {label}: `{key}` must be a table, e.g. {key} = {{ … }}")
    return value


def _price(value: Any, key: str, label: str, path: Path) -> float | None:
    """USD per 1M tokens; a quoted number is accepted, anything else is an error
    now rather than a crash after every request has been sent."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise UsageError(f"{path}: {label}: `{key}` must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UsageError(f"{path}: {label}: `{key}` must be a number, got {value!r}") from None
    if not math.isfinite(number):  # TOML allows inf/nan; JSON output doesn't
        raise UsageError(f"{path}: {label}: `{key}` must be a finite number, got {value!r}")
    return number


def load_config(path: Path) -> RunConfig:
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: invalid TOML — {exc}") from exc
    return config_from_dict(raw, path)


def config_from_text(text: str, label: str) -> RunConfig:
    """A config built in memory (`--base-url` mode), validated like a file."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{label}: can't build a valid config from these flags — {exc}") from exc
    return config_from_dict(raw, Path(label))


def config_from_dict(raw: dict[str, Any], path: Path) -> RunConfig:
    run = raw.get("run", {}) or {}
    target_dicts = raw.get("targets") or []
    if not target_dicts:
        raise UsageError(f"{path}: no [[targets]] defined")

    targets: list[Target] = []
    for index, entry in enumerate(target_dicts, start=1):
        label = entry.get("name") or f"target #{index}"
        for key in ("base_url", "model"):
            if not entry.get(key):
                raise UsageError(f"{path}: {label} is missing `{key}`")
        price = _table(entry, "price", label, path)
        api = str(entry.get("api", "openai"))
        if api not in APIS:
            raise UsageError(f"{path}: {label}: `api` must be one of {', '.join(APIS)}")
        extra_body = _table(entry, "extra_body", label, path)
        # the token-cap field can be renamed per target or for the whole run
        cap_fields = {entry.get("max_tokens_field"), run.get("max_tokens_field")}
        reserved = sorted(k for k in extra_body if k in RESERVED_BODY or k in cap_fields)
        if reserved:
            raise UsageError(
                f"{path}: {label}: extra_body can't set {', '.join(reserved)} — tps sends "
                "those itself (token caps: a prompt's max_tokens, --max-tokens, or "
                "max_tokens_field for the field name)"
            )
        targets.append(
            Target(
                name=str(entry.get("name") or entry["model"]),
                base_url=entry["base_url"],
                model=entry["model"],
                api_key_env=entry.get("api_key_env"),
                api_key=entry.get("api_key"),
                headers=_table(entry, "headers", label, path),
                extra_body=extra_body,
                price_in=_price(price.get("in_per_1m"), "price.in_per_1m", label, path),
                price_out=_price(price.get("out_per_1m"), "price.out_per_1m", label, path),
                max_tokens_field=entry.get("max_tokens_field"),
                send_stream_options=bool(entry.get("stream_options", True)),
                mock=_table(entry, "mock", label, path) if "mock" in entry else None,
                api=api,
            )
        )

    names = [t.name for t in targets]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise UsageError(
            f"{path}: duplicate target name(s): {', '.join(dupes)} — every statistic is "
            "keyed by name, so give each target a unique `name`"
        )

    available = {name: dict(spec) for name, spec in DEFAULT_PROMPTS.items()}
    prompt_table = raw.get("prompts", {}) or {}
    for name, spec in prompt_table.items():
        merged = dict(available.get(name, {}))
        merged.update(spec)
        merged.setdefault("max_tokens", 256)
        source = merged.get("document_file")
        if source and not Path(source).expanduser().is_absolute():
            # relative to the config file, so `-c elsewhere/targets.toml` works
            merged["document_file"] = str((Path(path).parent / source).resolve())
        available[name] = merged

    selected = run.get("prompts") or list(prompt_table) or list(DEFAULT_PROMPTS)[:2]
    if isinstance(selected, str):
        selected = [selected]
    missing = [n for n in selected if n not in available]
    if missing:
        raise UsageError(f"{path}: unknown prompt(s) {missing}; have {sorted(available)}")

    try:
        conc = run.get("concurrency", [1])
        if isinstance(conc, int):
            conc = [conc]
        cfg = RunConfig(
            targets=targets,
            prompts={n: available[n] for n in selected},
            reps=int(run.get("reps", 7)),
            concurrency=[int(c) for c in conc],
            warmup=int(run.get("warmup", 2)),
            timeout_s=float(run.get("timeout_s", 180)),
            temperature=parse_temperature(run.get("temperature"), str(path)),
            max_tokens_field=run.get("max_tokens_field", "max_tokens"),
            stream_options=bool(run.get("stream_options", True)),
            baseline=run.get("baseline"),
            available_prompts=available,
        )
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{path}: [run] has a value of the wrong type — {exc}") from exc
    validate(cfg, str(path))
    return cfg


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
    first_content_s: float | None = None
    e2e_s: float | None = None
    decode_s: float | None = None
    out_tokens: int | None = None
    in_tokens: int | None = None
    out_tps: float | None = None
    itl_p50_ms: float | None = None
    itl_p90_ms: float | None = None
    # A median gap far below the mean means chunks arrived in bursts (a proxy
    # flushing several at once): the ITL then measures delivery, not the model.
    itl_mean_ms: float | None = None
    itl_pace_ms: float | None = None  # mean gap without the longest few (stalls)
    tokens_per_chunk: float | None = None
    chunks: int = 0
    count_source: str = "usage"
    served_by: str | None = None
    batch_wall_s: float | None = None
    wall_clock: str = ""
    phase: str = "measure"  # "warmup" rows are discarded by every statistic
    finish_reason: str | None = None  # "length" = stopped by max_tokens
    reasoning_tokens: int | None = None  # from usage: OpenAI or Claude token details
    hidden_reasoning_tokens: int = 0  # billed and generated, but never streamed

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def percentile(values: list[float | None], q: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def p50(values: list[float | None]) -> float | None:
    return percentile(values, 0.5)


def p90(values: list[float | None], higher_is_better: bool = False) -> float | None:
    """The tail. For latencies that's the 90th percentile; for rates (where
    higher is better) it's the 10th — the rate 90% of requests met or beat."""
    return percentile(values, 0.1 if higher_is_better else 0.9)


def median(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return statistics.median(xs) if xs else None


def _first_present(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


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
    wants_usage = send_stream_options and target.api == "openai"
    extra = {"stream_options": {"include_usage": True}} if wants_usage else None
    body = request_body(target, cfg, prompt_text, max_tokens, stream=True, extra=extra)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    headers.update(target.auth_headers())
    headers.update(target.headers)

    sample = Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=False,
        wall_clock=now_iso(),
    )

    t0 = time.perf_counter()
    first = last = None
    gaps: list[float] = []
    chars = 0  # content + reasoning: the chars/4 fallback and first-chunk share
    reasoning_chars = 0
    first_chars = 0
    usage: dict[str, Any] = {}
    served: str | None = None
    stream_error: str | None = None
    stopped = False  # Claude ends every complete stream with message_stop

    try:
        async with client.stream("POST", target.url(), json=body, headers=headers) as response:
            sample.status = response.status_code
            if response.status_code >= 400:
                raw = await response.aread()
                snippet = redact(raw.decode("utf-8", "replace"), target.secrets())[:600].strip()
                # Some OpenAI-compatible servers reject stream_options outright.
                # That is a compatibility issue, not a performance result: resend
                # without it, and remember so later requests skip the doomed one.
                if (
                    send_stream_options
                    and response.status_code in (400, 422)
                    and "stream_options" in snippet
                ):
                    target.send_stream_options = False
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
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    # gateways report mid-stream upstream failures in-band
                    err = obj["error"]
                    message = err.get("message") if isinstance(err, dict) else err
                    stream_error = redact(f"stream_error: {message}", target.secrets())[:300]
                    break
                event = parse_event(obj, target.api)
                stopped = stopped or event["stop"]
                usage.update(event["usage"])
                if event["provider"] and not served:
                    served = event["provider"]
                if event["finish"]:
                    sample.finish_reason = event["finish"]
                piece, reasoning = event["piece"], event["reasoning"]
                if not piece and not reasoning:
                    continue
                now = time.perf_counter()
                size = len(piece) + len(reasoning)
                if first is None:
                    first = now
                    first_chars = size
                    sample.ttft_s = now - t0
                elif piece and last is not None:
                    gaps.append(now - last)
                if piece and sample.first_content_s is None:
                    sample.first_content_s = now - t0
                last = now
                sample.chunks += 1
                chars += size
                reasoning_chars += len(reasoning)

    except httpx.TimeoutException:
        sample.error = "timeout"
        sample.e2e_s = time.perf_counter() - t0
        return sample
    except (httpx.HTTPError, OSError) as exc:
        sample.error = redact(f"transport: {type(exc).__name__}: {exc}", target.secrets())
        sample.e2e_s = time.perf_counter() - t0
        return sample

    sample.e2e_s = time.perf_counter() - t0
    sample.served_by = served
    if not stream_error and target.api == "anthropic" and not stopped:
        # cut off mid-stream (a proxy closing early): its counts can't be trusted
        stream_error = "incomplete_stream: ended without message_stop"
    if stream_error:
        sample.error = stream_error
        return sample

    estimated = False
    if usage:
        sample.out_tokens = _first_present(
            usage.get("completion_tokens"), usage.get("output_tokens")
        )
        sample.in_tokens = _first_present(usage.get("prompt_tokens"), usage.get("input_tokens"))
        for key, name in (
            ("completion_tokens_details", "reasoning_tokens"),  # OpenAI-compatible
            ("output_tokens_details", "thinking_tokens"),  # Claude API
        ):
            details = usage.get(key)
            if isinstance(details, dict) and details.get(name) is not None:
                sample.reasoning_tokens = details[name]
    out_from_usage = sample.out_tokens is not None
    if sample.out_tokens is None and chars:
        sample.out_tokens = max(1, round(chars / 4))
        estimated = True
    if sample.in_tokens is None:
        # without it the cost columns would silently drop input spend
        sample.in_tokens = estimate_tokens(prompt_text)
        estimated = True
    sample.count_source = "chars/4" if estimated else "usage"

    # Reasoning that was counted in usage but never streamed (Claude's omitted
    # thinking, o-series reasoning) happened before the window opened: crediting
    # it to the window would inflate tok/s by the thinking share. When only a
    # summary of it streamed, the unstreamed remainder is hidden too — that
    # split rests on chars/4, so the rate is flagged as estimated.
    hidden = 0
    if out_from_usage and sample.reasoning_tokens:
        shown = round(reasoning_chars / 4)
        if not reasoning_chars:
            hidden = sample.reasoning_tokens
        elif sample.reasoning_tokens > 2 * shown:
            hidden = sample.reasoning_tokens - shown
            sample.count_source = "usage+chars/4"
    sample.hidden_reasoning_tokens = hidden
    streamed = (sample.out_tokens or 0) - hidden
    if first is not None and last is not None and last > first and streamed > 0:
        sample.decode_s = last - first
        # Tokens in the first chunk were generated before the window opened.
        # Estimate them by that chunk's share of the text, so providers that
        # send big chunks aren't credited with tokens they made "for free".
        first_tokens = max(1.0, streamed * first_chars / chars) if chars else 1.0
        if streamed > first_tokens:
            sample.out_tps = (streamed - first_tokens) / sample.decode_s
        if gaps:
            sample.itl_p50_ms = percentile(gaps, 0.5) * 1000
            sample.itl_p90_ms = percentile(gaps, 0.9) * 1000
            sample.itl_mean_ms = sum(gaps) / len(gaps) * 1000
            pace = itl_pace(gaps)
            sample.itl_pace_ms = None if pace is None else pace * 1000
    if sample.chunks and streamed > 0:
        sample.tokens_per_chunk = streamed / sample.chunks
    # A single chunk has no decode window: the rate is unknown, not infinite.

    finish = sample.finish_reason
    if finish in ("content_filter", "refusal"):
        # the provider declined; whatever streamed is not an answer to time
        sample.error = f"{finish}: finish_reason={finish}"
    elif sample.chunks:
        sample.ok = bool(sample.out_tokens)
    elif finish == "length" and sample.reasoning_tokens:
        # Nothing streamed, but the cap was spent on billed reasoning: a model
        # that only thought (Claude's hidden thinking) — a result, flagged as such.
        sample.ok = True
    elif finish or sample.out_tokens:
        sample.error = f"no_content: nothing streamed (finish_reason={finish})"
    if not sample.ok and not sample.error:
        sample.error = "empty_stream"
    return sample


def request_body(
    target: Target,
    cfg: RunConfig,
    prompt_text: str,
    max_tokens: int,
    stream: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON body for one request. `extra` holds the fields tps adds for
    the command (stream_options); extra_body still wins over them."""
    body: dict[str, Any] = {
        "model": target.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": stream,
    }
    temperature = request_temperature(cfg, target)
    if temperature is not None:
        body["temperature"] = temperature
    if target.api == "anthropic":
        body["max_tokens"] = max_tokens
    else:
        body[target.max_tokens_field or cfg.max_tokens_field] = max_tokens
    body.update(extra or {})
    # extra_body last: it is how provider pinning / vendor flags get applied
    body.update(target.extra_body)
    return body


def parse_event(obj: dict[str, Any], api: str) -> dict[str, Any]:
    """One streamed JSON event, reduced to what the timing loop needs."""
    event: dict[str, Any] = {
        "usage": {},
        "provider": None,
        "finish": None,
        "piece": "",
        "reasoning": "",
        "stop": False,
    }
    if api == "anthropic":
        kind = obj.get("type")
        if kind == "message_start":
            usage = (obj.get("message") or {}).get("usage") or {}
            # output_tokens here is a placeholder (≈1); only message_delta's
            # count is final, so never let a cut-off stream keep it
            event["usage"] = {k: v for k, v in usage.items() if k != "output_tokens"}
        elif kind == "message_stop":
            event["stop"] = True
        elif kind == "message_delta":
            event["usage"] = obj.get("usage") or {}
            stop = (obj.get("delta") or {}).get("stop_reason")
            event["finish"] = ANTHROPIC_STOP.get(stop, stop)
        elif kind == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                event["piece"] = delta.get("text") or ""
            elif delta.get("type") == "thinking_delta":
                event["reasoning"] = delta.get("thinking") or ""  # "" when omitted
    else:
        if isinstance(obj.get("usage"), dict):
            event["usage"] = obj["usage"]
        if obj.get("provider"):
            event["provider"] = str(obj["provider"])
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason"):
                event["finish"] = str(choices[0]["finish_reason"])
            delta = choices[0].get("delta") or {}
            event["piece"] = delta.get("content") or ""
            event["reasoning"] = delta.get("reasoning_content") or delta.get("reasoning") or ""
    for key in ("piece", "reasoning"):
        if not isinstance(event[key], str):
            event[key] = ""
    return event


async def run_target_batch(
    client: httpx.AsyncClient,
    target: Target,
    prompt_text: str,
    prompt_name: str,
    max_tokens: int,
    cfg: RunConfig,
    concurrency: int,
    batch: int,
    mock: bool = False,
) -> list[Sample]:
    if mock:
        samples = [
            mock_sample(target, prompt_name, prompt_text, concurrency, batch, max_tokens, i)
            for i in range(concurrency)
        ]
        wall = max(s.e2e_s or 0.0 for s in samples)
        for sample in samples:
            sample.batch_wall_s = wall
        return samples

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
    return list(samples)


def mock_sample(
    target: Target,
    prompt_name: str,
    prompt_text: str,
    concurrency: int,
    batch: int,
    max_tokens: int,
    index: int = 0,
) -> Sample:
    """Synthesise a plausible sample. No network, no sleeping."""
    import random

    prof = target.mock or {}
    rng = random.Random(f"{target.name}:{prompt_name}:{concurrency}:{batch}:{index}")
    ttft = rng.uniform(*prof.get("ttft", (0.35, 0.8)))
    itl = rng.uniform(*prof.get("itl", (0.012, 0.028)))
    tokens = rng.randint(int(max_tokens * 0.85), max_tokens)
    # concurrency degrades per-stream ITL and queues the first token, as real
    # serving stacks do; a lower `scaling` makes a provider that saturates early
    scaling = float(prof.get("scaling", 0.94))
    itl *= 1 + (1 - scaling) * (concurrency - 1)
    ttft *= 1 + 0.04 * (concurrency - 1)
    decode = itl * (tokens - 1)
    return Sample(
        target=target.name,
        prompt=prompt_name,
        concurrency=concurrency,
        batch=batch,
        ok=True,
        status=200,
        ttft_s=ttft,
        first_content_s=ttft,
        e2e_s=ttft + decode,
        decode_s=decode,
        out_tokens=tokens,
        in_tokens=estimate_tokens(prompt_text),
        out_tps=(tokens - 1) / decode,
        itl_p50_ms=itl * 1000,
        itl_p90_ms=itl * 1000 * 1.4,
        itl_mean_ms=itl * 1000 * 1.1,  # a steady stream: one token per chunk
        itl_pace_ms=itl * 1000 * 1.05,
        tokens_per_chunk=1.0,
        chunks=tokens,
        count_source="mock",
        served_by=target.note() or "mock",
        batch_wall_s=ttft + decode,
        wall_clock=now_iso(),
        finish_reason="length" if tokens == max_tokens else "stop",
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def rotate(items: list[Any], n: int) -> list[Any]:
    if not items:
        return items
    n %= len(items)
    return items[n:] + items[:n]


# Warm-ups only open connections and warm the provider's prompt cache, and are
# discarded; generating a full-length answer for them would just cost money.
WARMUP_MAX_TOKENS = 16


def planned_requests(cfg: RunConfig) -> int:
    """targets × prompts × reps × Σ(concurrency), plus warm-ups."""
    measured = len(cfg.targets) * len(cfg.prompts) * cfg.reps * sum(cfg.concurrency)
    return measured + len(cfg.targets) * cfg.warmup


# `--dry-run` turns requests into minutes at an assumed per-stream rate. Real
# rates differ 10x between providers: the result is a budget, not a forecast.
PLAN_TOK_S = 40.0
PLAN_TTFT_S = 1.0
# chat-template tokens a provider wraps around one user message; a provider
# that injects its own system prompt can exceed this, and nothing can see that
PLAN_TEMPLATE_TOKENS = 32


def max_prompt_tokens(text: str) -> int:
    """An upper bound on a prompt's input tokens, whatever the tokenizer.

    Every BPE/SentencePiece token covers at least one UTF-8 byte, so a prompt
    can't tokenize to more tokens than it has bytes. chars/4 is the typical
    count for English but undercounts CJK text or dense code up to 4x.
    """
    return len(text.encode("utf-8")) + PLAN_TEMPLATE_TOKENS


def plan_run(cfg: RunConfig, mock: bool = False) -> dict[str, Any]:
    """What `execute` would send — counted the same way, nothing sent.

    `cost_max_usd` is a true upper bound: output at the max_tokens cap, input
    at `max_prompt_tokens`. `in_tokens_est` (chars/4) is the typical count.
    """
    warm_spec = next(iter(cfg.prompts.values()))
    warm_out = min(int(warm_spec.get("max_tokens", 256)), WARMUP_MAX_TOKENS)
    warm_text = build_prompt(warm_spec)
    measured_per_target = cfg.reps * sum(cfg.concurrency) * len(cfg.prompts)
    in_tokens = cfg.warmup * estimate_tokens(warm_text)
    in_tokens_max = cfg.warmup * max_prompt_tokens(warm_text)
    out_tokens = cfg.warmup * warm_out
    seconds = cfg.warmup * (PLAN_TTFT_S + warm_out / PLAN_TOK_S)
    for spec in cfg.prompts.values():
        cap = int(spec.get("max_tokens", 256))
        requests = cfg.reps * sum(cfg.concurrency)
        text = build_prompt(spec)
        in_tokens += requests * estimate_tokens(text)
        in_tokens_max += requests * max_prompt_tokens(text)
        out_tokens += requests * cap
        # batches run one after another; a concurrent batch lasts ~one request
        seconds += cfg.reps * len(cfg.concurrency) * (PLAN_TTFT_S + cap / PLAN_TOK_S)

    targets = []
    for target in cfg.targets:
        priced = target.price_in is not None and target.price_out is not None
        cost = (
            (in_tokens_max * target.price_in + out_tokens * target.price_out) / 1e6
            if priced
            else None
        )
        targets.append(
            {
                "target": target.name,
                "requests": measured_per_target + cfg.warmup,
                "cost_max_usd": 0.0 if mock else cost,
            }
        )
    costs = [t["cost_max_usd"] for t in targets if t["cost_max_usd"] is not None]
    return {
        "requests": planned_requests(cfg),
        "measured_requests": measured_per_target * len(cfg.targets),
        "warmup_requests": cfg.warmup * len(cfg.targets),
        "in_tokens_est": in_tokens * len(cfg.targets),
        "in_tokens_max": in_tokens_max * len(cfg.targets),
        "out_tokens_max": out_tokens * len(cfg.targets),
        "cost_max_usd": sum(costs) if costs else None,
        "unpriced": [t["target"] for t in targets if t["cost_max_usd"] is None],
        "est_minutes": round(seconds * len(cfg.targets) / 60, 1),
        "assumed": {
            "tok_s": PLAN_TOK_S,
            "ttft_s": PLAN_TTFT_S,
            "template_tokens": PLAN_TEMPLATE_TOKENS,
        },
        "targets": targets,
    }


def run_meta(cfg: RunConfig, mock: bool) -> dict[str, Any]:
    return {
        "created": now_iso(),
        "tps_version": __version__,
        "mock": mock,
        "complete": False,
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "warmup": cfg.warmup,
        "temperature": cfg.temperature,
        "max_tokens": {
            name: int(spec.get("max_tokens", 256)) for name, spec in cfg.prompts.items()
        },
        "timeout_s": cfg.timeout_s,
        "targets": [
            {
                "name": t.name,
                "model": t.model,
                "base_url": redact_url(t.base_url),  # meta, JSON and report are shared
                "pin": t.extra_body.get("provider"),
                "price_in": t.price_in,
                "price_out": t.price_out,
                "api": t.api,
                "temperature": sent_temperature(cfg, t),  # null = not sent
            }
            for t in cfg.targets
        ],
        "prompts": list(cfg.prompts),
        "baseline": cfg.baseline,
        "http2": HTTP2,
        "planned_requests": planned_requests(cfg),  # includes the warm-ups
        "planned_warmup": len(cfg.targets) * cfg.warmup,
    }


async def execute(
    cfg: RunConfig, outdir: Path, mock: bool, on_batch: Callable[..., None] | None = None
) -> list[Sample]:
    """Run every batch, interleaved, streaming results to disk as they land.

    raw.jsonl is appended batch by batch and meta.json is written up front, so
    an interrupted run keeps everything measured so far (`tps report` renders
    it, flagged PARTIAL).
    """
    outdir.mkdir(parents=True, exist_ok=True)
    meta = run_meta(cfg, mock)
    meta_path = outdir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    samples: list[Sample] = []
    total = planned_requests(cfg)
    done = 0
    limits = httpx.Limits(max_connections=256, max_keepalive_connections=64)
    async with httpx.AsyncClient(
        http2=HTTP2,
        timeout=httpx.Timeout(cfg.timeout_s, connect=15.0),
        limits=limits,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        with open(outdir / "raw.jsonl", "w", encoding="utf-8") as raw:

            def record(batch_samples: list[Sample], **info: Any) -> None:
                nonlocal done
                for sample in batch_samples:
                    sample.phase = info["phase"]
                    raw.write(json.dumps(sample.as_dict()) + "\n")
                raw.flush()
                samples.extend(batch_samples)
                done += len(batch_samples)
                if on_batch is not None:
                    rates = [s.out_tps for s in batch_samples if s.out_tps]
                    on_batch(
                        done=done,
                        total=total,
                        ok=sum(1 for s in batch_samples if s.ok),
                        n=len(batch_samples),
                        tps=statistics.median(rates) if rates else None,
                        **info,
                    )

            # warm-up: establish TLS/HTTP2, let the provider's cache settle
            warm_name, warm_spec = next(iter(cfg.prompts.items()))
            warm_text = build_prompt(warm_spec)
            for target in cfg.targets:
                for _ in range(cfg.warmup):
                    record(
                        await run_target_batch(
                            client,
                            target,
                            warm_text,
                            warm_name,
                            min(int(warm_spec.get("max_tokens", 256)), WARMUP_MAX_TOKENS),
                            cfg,
                            1,
                            -1,
                            mock,
                        ),
                        phase="warmup",
                        target=target.name,
                        prompt=warm_name,
                        concurrency=1,
                        rep=-1,
                    )

            for prompt_name, spec in cfg.prompts.items():
                prompt_text = build_prompt(spec)
                max_tokens = int(spec.get("max_tokens", 256))
                for concurrency in cfg.concurrency:
                    for rep in range(cfg.reps):
                        # rotate the order every rep so nobody always runs "first"
                        for target in rotate(cfg.targets, rep):
                            record(
                                await run_target_batch(
                                    client,
                                    target,
                                    prompt_text,
                                    prompt_name,
                                    max_tokens,
                                    cfg,
                                    concurrency,
                                    rep,
                                    mock,
                                ),
                                phase="measure",
                                target=target.name,
                                prompt=prompt_name,
                                concurrency=concurrency,
                                rep=rep,
                            )

    meta["complete"] = True
    meta["finished"] = now_iso()
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return samples


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def load_samples(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = run_dir / "raw.jsonl"
    if not raw.exists():
        raise UsageError(
            f"{run_dir} is not a tps run directory (no raw.jsonl) — "
            "pass the folder printed by `tps run`"
        )
    samples = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a run killed mid-write can leave one torn line
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if not meta.get("targets"):
        names = list(dict.fromkeys(s["target"] for s in samples))
        meta["targets"] = [{"name": n} for n in names]
    return samples, meta


def error_class(sample: dict[str, Any]) -> str:
    return (sample.get("error") or "unknown").split(":")[0]


def is_estimated(count_sources: list[str]) -> bool:
    """True when any token count behind a row came from chars/4."""
    return any("chars/4" in source for source in count_sources)


def is_warmup(sample: dict[str, Any]) -> bool:
    # runs recorded before `phase` existed mark warm-ups with batch -1 only
    return sample.get("phase") == "warmup" or sample.get("batch") == -1


def _bucket() -> dict[str, Any]:
    return {
        "ttft": [],
        "first_content": [],
        "e2e": [],
        "tps": [],
        "itl": [],
        "tpc": [],
        "in": [],
        "out": [],
        "batches": [],
        "ok": 0,
        "n": 0,
        "truncated": 0,
        "reasoning_only": 0,
        "bursty": 0,
        "hidden": [],
        "errors": {},
        "served": set(),
        "count_source": set(),
    }


_FIELDS = (
    ("ttft", "ttft_s"),
    ("first_content", "first_content_s"),
    ("e2e", "e2e_s"),
    ("tps", "out_tps"),
    ("itl", "itl_p50_ms"),
    ("tpc", "tokens_per_chunk"),
    ("in", "in_tokens"),
    ("out", "out_tokens"),
)

# A request whose median chunk gap is under this share of its typical gap got
# its chunks in bursts. Comparing the stream with itself needs no token counts.
BURST_RATIO = 0.2


def itl_pace(gaps: list[float]) -> float | None:
    """The mean chunk gap with the longest few left out.

    One long stall in a steady stream drags the plain mean up and would make
    an honest median look like a burst; clumped delivery has a long gap per
    clump, so dropping just the top 2% (at least two) keeps it visible.
    """
    drop = max(2, math.ceil(len(gaps) * 0.02))
    if len(gaps) <= drop + 2:
        return None  # too few gaps to tell a burst from a pause
    kept = sorted(gaps)[:-drop]
    return sum(kept) / len(kept)


def is_bursty(sample: dict[str, Any]) -> bool:
    p50_gap = sample.get("itl_p50_ms")
    # rows recorded before itl_pace_ms existed fall back to the plain mean
    pace = sample["itl_pace_ms"] if "itl_pace_ms" in sample else sample.get("itl_mean_ms")
    return bool(p50_gap is not None and pace and p50_gap < BURST_RATIO * pace)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Group measured samples by (prompt, target, concurrency).

    `per_stream` is the bucket at the *lowest* concurrency configured for each
    prompt (normally 1), because mixing c=1 and c=4 samples into one percentile
    is how people accidentally invent a 30% regression that never happened.
    `totals` counts every request for a (prompt, target), all concurrency
    levels included — that is the reliability signal.
    """
    buckets: dict[tuple[str, str, int], dict[str, Any]] = {}
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    batch_tokens: dict[tuple, float] = {}
    batch_wall: dict[tuple, float] = {}

    for s in samples:
        if is_warmup(s):
            continue
        key = (s["prompt"], s["target"], s["concurrency"])
        bucket = buckets.setdefault(key, _bucket())
        total = totals.setdefault(
            (s["prompt"], s["target"]),
            {"ok": 0, "n": 0, "truncated": 0, "reasoning_only": 0, "errors": {}},
        )
        bucket["n"] += 1
        total["n"] += 1
        bkey = (*key, s["batch"])
        batch_wall[bkey] = max(batch_wall.get(bkey, 0.0), s.get("batch_wall_s") or 0.0)
        if s.get("ok"):
            bucket["ok"] += 1
            total["ok"] += 1
            bucket["served"].add(s.get("served_by"))
            bucket["count_source"].add(s.get("count_source"))
            truncated = s.get("finish_reason") == "length"
            # every chunk was reasoning: there is no answer to time
            reasoning_only = s.get("first_content_s") is None
            bucket["truncated"] += truncated
            bucket["reasoning_only"] += reasoning_only
            # counted at every level, like ok/n, so `reasoning_only == ok` holds
            total["truncated"] += truncated
            total["reasoning_only"] += reasoning_only
            bucket["hidden"].append(s.get("hidden_reasoning_tokens") or 0)
            bucket["bursty"] += is_bursty(s)
            for name, source in _FIELDS:
                bucket[name].append(s.get(source))
            batch_tokens[bkey] = batch_tokens.get(bkey, 0) + (s.get("out_tokens") or 0)
        else:
            cls = error_class(s)
            bucket["errors"][cls] = bucket["errors"].get(cls, 0) + 1
            total["errors"][cls] = total["errors"].get(cls, 0) + 1

    # aggregate throughput per batch: sum of tokens / wall clock of the batch
    for bkey, tokens in batch_tokens.items():
        wall = batch_wall.get(bkey) or 0.0
        if wall > 0:
            buckets[bkey[:3]]["batches"].append(tokens / wall)

    min_conc: dict[str, int] = {}
    for prompt, _target, conc in buckets:
        min_conc[prompt] = min(min_conc.get(prompt, conc), conc)
    per_stream = {(p, t): b for (p, t, c), b in buckets.items() if c == min_conc[p]}
    return {"buckets": buckets, "per_stream": per_stream, "totals": totals, "min_conc": min_conc}


def bucket_stats(bucket: dict[str, Any]) -> dict[str, Any]:
    """The numbers every output (terminal, report, CSV, JSON) is built from."""
    return {
        "ttft_p50_s": p50(bucket["ttft"]),
        "ttft_p90_s": p90(bucket["ttft"]),
        "first_content_p50_s": p50(bucket["first_content"]),
        "out_tps_p50": p50(bucket["tps"]),
        "out_tps_p90": p90(bucket["tps"], higher_is_better=True),
        "itl_p50_ms": p50(bucket["itl"]),
        # how many samples each median actually came from: reasoning-only and
        # single-chunk responses have no first answer / no ITL to contribute
        "itl_n": sum(v is not None for v in bucket["itl"]),
        "first_content_n": sum(v is not None for v in bucket["first_content"]),
        "tokens_per_chunk_median": median(bucket["tpc"]),
        "bursty": bucket["bursty"],
        "e2e_p50_s": p50(bucket["e2e"]),
        "out_tokens_median": median(bucket["out"]),
        "in_tokens_median": median(bucket["in"]),
        "aggregate_tps": median(bucket["batches"]),
        "ok": bucket["ok"],
        "n": bucket["n"],
        "truncated": bucket["truncated"],
        "reasoning_only": bucket["reasoning_only"],
        "hidden_reasoning_tokens_median": median(bucket["hidden"]) or None,
        "errors": dict(bucket["errors"]),
        "count_source": sorted(x for x in bucket["count_source"] if x),
        "served_by": sorted(x for x in bucket["served"] if x),
    }


def reasoning_note(st: dict[str, Any]) -> str | None:
    """Why first-answer and ITL are empty: the model never got to an answer.

    Its TTFT and tok/s are still real, but they time the thinking — say so
    instead of printing a bare "—" that looks like a broken tool.
    """
    ok, only = st["ok"], st["reasoning_only"]
    if not only:
        return None
    if only < ok:
        return (
            f"{only}/{ok} responses were reasoning only (no answer text); first answer "
            f"is from {st['first_content_n']} and ITL from {st['itl_n']} of {ok}"
        )
    if st["truncated"] == ok:
        return (
            f"hit the token cap while reasoning ({ok}/{ok} finish_reason=length, no answer "
            "text) — any TTFT or tok/s here measured thinking, not answering; "
            "raise --max-tokens"
        )
    return (
        f"no answer text in {ok}/{ok} responses (reasoning only) — any TTFT or tok/s "
        "here measured thinking, not answering"
    )


def row_notes(st: dict[str, Any]) -> list[str]:
    """Everything a reader needs to interpret a row's numbers correctly."""
    notes = [note] if (note := reasoning_note(st)) else []
    hidden = st["hidden_reasoning_tokens_median"]
    if hidden and st["reasoning_only"] < st["ok"]:
        notes.append(
            f"TTFT includes ~{hidden:.0f} thinking tokens (median) that the API doesn't "
            "stream; tok/s counts only the streamed answer"
        )
    if st["bursty"]:
        tpc = st["tokens_per_chunk_median"]
        notes.append(
            f"chunks arrived in bursts in {st['bursty']}/{st['itl_n']} responses (median gap "
            f"under {BURST_RATIO:.0%} of the typical gap) — ITL measures how the stream was "
            "flushed, not the model's pacing" + (f" ({tpc:.1f} tokens/chunk)" if tpc else "")
        )
    return notes


def request_cost(
    in_tokens: float | None,
    out_tokens: float | None,
    price_in: float | None,
    price_out: float | None,
) -> tuple[float | None, float | None]:
    """(USD per 1k requests, USD per 1M generated tokens incl. their prompts)."""
    if price_in is None or price_out is None or not out_tokens:
        return None, None
    per_request = ((in_tokens or 0) * price_in + out_tokens * price_out) / 1e6
    return per_request * 1000, per_request * (1e6 / out_tokens)


def fmt(value: float | None, digits: int = 1, scale: float = 1.0) -> str:
    if value is None:
        return "—"
    return f"{value * scale:.{digits}f}"


def bar(value: float, peak: float, width: int = 28) -> str:
    if not peak or not value:
        return ""
    filled = max(1, round(width * value / peak))
    return "█" * filled


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _ordered_targets(meta: dict[str, Any], keys) -> list[str]:
    names = [t["name"] for t in meta.get("targets", [])]
    for key in keys:
        if key not in names:
            names.append(key)
    return names


def write_report(run_dir: Path, samples: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    stats = summarize(samples)
    buckets = stats["buckets"]
    per_stream = stats["per_stream"]
    min_conc = stats["min_conc"]
    targets = _ordered_targets(meta, [t for (_p, t) in per_stream])
    prompts = [p for p in dict.fromkeys([*(meta.get("prompts") or []), *min_conc]) if p in min_conc]
    price_by_name = {t["name"]: t for t in meta.get("targets", [])}

    flags = []
    if meta.get("mock"):
        flags.append("MOCK DATA")
    if meta.get("complete") is False:
        flags.append("PARTIAL")
    tag = f" ({', '.join(flags)})" if flags else ""

    lines: list[str] = [f"# tps report — {run_dir.name}{tag}", ""]
    if meta.get("mock"):
        lines += [
            "> **MOCK DATA** — synthetic numbers from `--mock`. Not a measurement; never publish them.",
            "",
        ]
    if meta.get("complete") is False:
        lines += [
            "> **PARTIAL** — the run was interrupted; tables cover only the requests that finished.",
            "",
        ]
    model = next((t.get("model") for t in meta.get("targets", []) if t.get("model")), "?")
    caps = sorted(set((meta.get("max_tokens") or {}).values()))
    cap = f" · max_tokens {'/'.join(map(str, caps))}" if caps else ""
    targets_meta = meta.get("targets", [])
    if targets_meta and all("temperature" in t for t in targets_meta):
        temps = {t["temperature"] for t in targets_meta}
        temp = "/".join(sorted("not sent" if v is None else f"{v:g}" for v in temps))
    else:  # runs recorded before per-target temperatures
        temp = str(meta.get("temperature"))
    lines.append(
        f"- model: `{model}` · reps {meta.get('reps')} · "
        f"concurrency {meta.get('concurrency')} · temp {temp}{cap} · "
        f"http2 {meta.get('http2')}"
    )
    version = f" · tps {meta['tps_version']}" if meta.get("tps_version") else ""
    lines.append(f"- captured: {meta.get('created')}{version}")
    lines.append("")
    lines.append(
        "TTFT = time to the first generated token (content or reasoning). "
        "tok/s = per-stream decode rate across the streaming window. ITL = median gap "
        "between content chunks; tok/chunk shows how many tokens each chunk carried, and "
        "† marks streams that arrived in bursts, where ITL reflects flushing rather than "
        "the model. **p90 is always the slow tail** (for tok/s, the rate 90% "
        "of requests met or beat). Aggregate = all tokens of a concurrent batch ÷ its wall "
        "clock. Definitions: `docs/metrics.md`."
    )
    lines.append("")

    estimated_any = False
    for prompt in prompts:
        conc = min_conc[prompt]
        label = "single stream" if conc == 1 else f"lowest concurrency (c={conc})"
        lines.append(f"## `{prompt}` — {label}")
        lines.append("")
        rows = [(t, per_stream[(prompt, t)]) for t in targets if (prompt, t) in per_stream]

        # Optional delta columns against a named baseline (usually the no-gateway
        # direct endpoint): this turns "which is faster" into "what did the
        # gateway hop cost me".
        base_name = meta.get("baseline")
        base = (
            bucket_stats(per_stream[(prompt, base_name)])
            if (prompt, base_name) in per_stream
            else None
        )
        show_delta = bool(base and base["ttft_p50_s"] is not None and base["out_tps_p50"])
        show_first = any(
            fc is not None and tt is not None and fc - tt > 1e-6
            for _t, b in rows
            for fc, tt in zip(b["first_content"], b["ttft"], strict=False)
        )

        header = ["provider", "TTFT p50", "TTFT p90"]
        if show_first:
            header.append("first answer p50")
        header += ["tok/s p50", "tok/s p90", "ITL p50 ms", "tok/chunk", "E2E p50", "out tok", "ok"]
        if show_delta:
            header += [f"Δ TTFT vs {base_name}", f"Δ tok/s vs {base_name}"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))

        for target, bucket in rows:
            st = bucket_stats(bucket)
            estimated = is_estimated(st["count_source"])
            estimated_any |= estimated
            mark = " ≈" if estimated else ""
            cells = [target, fmt(st["ttft_p50_s"], 2), fmt(st["ttft_p90_s"], 2)]
            if show_first:
                cells.append(fmt(st["first_content_p50_s"], 2))
            cells += [
                fmt(st["out_tps_p50"]) + mark,
                fmt(st["out_tps_p90"]) + mark,
                fmt(st["itl_p50_ms"]) + (" †" if st["bursty"] else ""),
                fmt(st["tokens_per_chunk_median"]),
                fmt(st["e2e_p50_s"], 2),
                fmt(st["out_tokens_median"], 0),
                f"{st['ok']}/{st['n']}",
            ]
            if show_delta:
                if target == base_name:
                    cells += ["—", "—"]
                else:
                    d_ttft = (
                        st["ttft_p50_s"] - base["ttft_p50_s"]
                        if st["ttft_p50_s"] is not None
                        else None
                    )
                    d_tps = (
                        (st["out_tps_p50"] - base["out_tps_p50"]) / base["out_tps_p50"] * 100
                        if st["out_tps_p50"] is not None
                        else None
                    )
                    cells += [
                        "—" if d_ttft is None else f"{d_ttft:+.2f}s",
                        "—" if d_tps is None else f"{d_tps:+.0f}%",
                    ]
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        lines.append("")
        notes = [(t, note) for t, b in rows for note in row_notes(bucket_stats(b))]
        for target, note in notes:
            lines.append(f"> ⚠ **{target}**: {note}.")
        if notes:
            lines.append("")

        peak = max((p50(b["tps"]) or 0 for _t, b in rows), default=0.0)
        lines.append("```")
        width = max((len(t) for t, _b in rows), default=10)
        for target, bucket in rows:
            rate = p50(bucket["tps"])
            extra = ""
            if bucket["errors"]:
                extra = "  " + " ".join(f"[{k}×{v}]" for k, v in bucket["errors"].items())
            shown = "      — tok/s" if rate is None else f"{rate:7.1f} tok/s"
            lines.append(f"{target:<{width}}  {bar(rate or 0, peak):<28} {shown}{extra}")
        lines.append("```")
        lines.append("")

        concs = sorted({c for (p, _t, c) in buckets if p == prompt})
        if len(concs) > 1 or concs[0] != 1:
            lines.append(f"## `{prompt}` — under load")
            lines.append("")
            lines.append(
                "| provider | c | aggregate tok/s | vs c="
                + str(concs[0])
                + " | per-stream tok/s p50 "
                "| TTFT p50 | ok | errors |"
            )
            lines.append("|---|---|---|---|---|---|---|---|")
            for target in targets:
                first_agg = None
                for c in concs:
                    bucket = buckets.get((prompt, target, c))
                    if not bucket:
                        continue
                    st = bucket_stats(bucket)
                    agg = st["aggregate_tps"]
                    if c == concs[0]:
                        first_agg = agg
                    scale = f"{agg / first_agg:.1f}×" if agg and first_agg else "—"
                    errors = ", ".join(f"{k} × {v}" for k, v in sorted(st["errors"].items()))
                    lines.append(
                        f"| {target} | {c} | {fmt(agg)} | {scale} | {fmt(st['out_tps_p50'])} | "
                        f"{fmt(st['ttft_p50_s'], 2)} | {st['ok']}/{st['n']} | {errors or '—'} |"
                    )
            lines.append("")
            lines.append(
                "Aggregate rising roughly with `c` means the provider scales; flat aggregate "
                "with collapsing per-stream tok/s means it queues."
            )
            lines.append("")

    if estimated_any:
        lines.append(
            "> ≈ token counts were estimated as chars/4 — the provider sent no `usage` "
            "block, or (`usage+chars/4`) only a summary of its billed reasoning streamed "
            "and the streamed/hidden split was estimated. Treat those rates as approximate."
        )
        lines.append("")

    # cost + provenance
    lines.append("## Cost & provenance")
    lines.append("")
    lines.append(
        "| provider | $/1M in | $/1M out | est. $/1k requests | cost of 1M generated tokens "
        "| served by | token source |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for target in targets:
        info = price_by_name.get(target, {})
        pin = info.get("pin") or {}
        pooled = [b for (_p, t), b in per_stream.items() if t == target]
        served = sorted({x for b in pooled for x in b["served"] if x})
        source = sorted({x for b in pooled for x in b["count_source"] if x})
        median_in = median([v for b in pooled for v in b["in"]])
        median_out = median([v for b in pooled for v in b["out"]])
        per_1k, per_mtok = request_cost(
            median_in, median_out, info.get("price_in"), info.get("price_out")
        )
        pinned = ",".join(pin.get("only", [])) if isinstance(pin, dict) else ""
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                target,
                fmt(info.get("price_in"), 3),
                fmt(info.get("price_out"), 3),
                fmt(per_1k, 3),
                fmt(per_mtok, 2),
                ", ".join(served) or (f"pin:{pinned}" if pinned else "—"),
                ", ".join(source) or "—",
            )
        )
    lines.append("")
    lines.append(
        "> Cost uses the median request across all prompts. Provider attribution comes from "
        "the response (`provider` field or `x-openrouter-provider` header) when the gateway "
        "exposes it; if the column is empty you were routed by a gateway, not pinned."
    )

    # failures — counted across every concurrency level, not just the lowest
    totals: dict[str, int] = {}
    errors: dict[str, dict[str, int]] = {}
    for s in samples:
        if is_warmup(s):
            continue
        name = s["target"]
        totals[name] = totals.get(name, 0) + 1
        if not s.get("ok"):
            code = error_class(s)
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

    # csv: one row per prompt × provider × concurrency, each with its own stats
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
                "first_content_p50_s",
                "count_source",
                "served_by",
                "truncated",
                "reasoning_only",
                "itl_n",
                "tokens_per_chunk_median",
                "bursty",
            ]
        )
        for (prompt, target, conc), bucket in sorted(buckets.items()):
            st = bucket_stats(bucket)
            writer.writerow(
                [
                    prompt,
                    target,
                    conc,
                    fmt(st["ttft_p50_s"], 3),
                    fmt(st["ttft_p90_s"], 3),
                    fmt(st["out_tps_p50"]),
                    fmt(st["out_tps_p90"]),
                    fmt(st["itl_p50_ms"]),
                    fmt(st["e2e_p50_s"], 3),
                    fmt(st["aggregate_tps"]),
                    fmt(st["out_tokens_median"], 0),
                    st["ok"],
                    st["n"],
                    "; ".join(f"{k}×{v}" for k, v in st["errors"].items()),
                    fmt(st["first_content_p50_s"], 3),
                    ";".join(st["count_source"]),
                    ";".join(st["served_by"]),
                    st["truncated"],
                    st["reasoning_only"],
                    st["itl_n"],
                    fmt(st["tokens_per_chunk_median"], 2),
                    st["bursty"],
                ]
            )

    return report


# --------------------------------------------------------------------------
# discover (OpenRouter)
# --------------------------------------------------------------------------


def fetch_endpoints(
    model_slug: str,
    api_key: str | None = None,
    from_json: Path | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Return (canonical_slug, endpoints, raw_payload) for one model."""
    if from_json:
        try:
            payload = json.loads(from_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise UsageError(f"cannot read {from_json}: {exc.strerror}") from exc
        except json.JSONDecodeError as exc:
            raise UsageError(f"{from_json} is not JSON: {exc}") from exc
    else:
        url = f"https://openrouter.ai/api/v1/models/{model_slug.strip('/')}/endpoints"
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not reach OpenRouter: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            raise UsageError(
                f"model not found on OpenRouter: {model_slug} — use the author/slug "
                "shown on openrouter.ai/models"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"GET {url} -> {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"GET {url} returned non-JSON") from exc

    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        data = data[0] if data else {}
    endpoints = data.get("endpoints") or []
    if not endpoints:
        raise ProviderError("no endpoints in response — dump it with --json-out and inspect")
    canonical = data.get("id") or data.get("slug") or model_slug
    return canonical, endpoints, payload


def normalize_endpoints(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat, JSON-safe rows — the same shape the CLI emits and renders."""
    rows = []
    for endpoint in endpoints:
        pricing = endpoint.get("pricing") or {}
        rows.append(
            {
                "provider": endpoint.get("provider_name") or endpoint.get("name"),
                "slug": provider_slug(endpoint),
                "tag": endpoint.get("tag"),
                "quantization": endpoint.get("quantization"),
                "service_tier": endpoint.get("service_tier"),
                "context_length": endpoint.get("context_length"),
                "price_in_per_1m": _per_million(pricing.get("prompt")),
                "price_out_per_1m": _per_million(pricing.get("completion")),
                "reported_p50_throughput": _stat(endpoint.get("throughput_last_30m"), "p50"),
                "reported_p50_latency_s": _stat(endpoint.get("latency_last_30m"), "p50"),
            }
        )
    return rows


def render_endpoint_table(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'provider':<20} {'slug':<20} {'quant':<8} {'$/1M in':>8} "
        f"{'$/1M out':>9} {'p50 t/s*':>9} {'p50 ttft*':>10}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{_text(row['provider'], 20):<20} "
            f"{_text(row['slug'], 20):<20} "
            f"{_text(row['quantization'], 8):<8} "
            f"{fmt(row['price_in_per_1m'], 3):>8} "
            f"{fmt(row['price_out_per_1m'], 3):>9} "
            f"{fmt(row['reported_p50_throughput'], 1):>9} "
            f"{fmt(row['reported_p50_latency_s'], 2):>10}"
        )
    return "\n".join(lines)


def targets_toml(canonical: str, endpoints: list[dict[str, Any]]) -> tuple[str, int, int]:
    """Build a config with one pinned target per endpoint tag.

    Returns (toml_text, target_count, collapsed_count).
    """
    blocks = [
        f"# generated by: tps discover {canonical}",
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
        groups.setdefault(str(endpoint.get("tag") or provider_slug(endpoint)), []).append(endpoint)

    def price_key(e: dict[str, Any]) -> float:
        pricing = e.get("pricing") or {}
        price_in = _per_million(pricing.get("prompt"))
        price_out = _per_million(pricing.get("completion"))
        return (1e9 if price_in is None else price_in) + (1e9 if price_out is None else price_out)

    collapsed = 0
    labels: dict[str, int] = {}
    for tag, group in groups.items():
        group.sort(key=price_key)
        endpoint = group[0]
        collapsed += len(group) - 1

        quant = endpoint.get("quantization")
        if quant in (None, "unknown"):
            quant = None
        label = _text(endpoint.get("provider_name"), 40) if endpoint.get("provider_name") else tag
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
        blocks.append(f"name = {_toml_inline(label)}")
        if len(group) > 1:
            blocks.append(f"# {len(group)} endpoints share this pin; cheapest kept")
        blocks.append('base_url = "https://openrouter.ai/api/v1"')
        blocks.append(f"model = {_toml_inline(canonical)}")
        blocks.append('api_key_env = "OPENROUTER_API_KEY"')
        pin: dict[str, Any] = {"only": [tag], "allow_fallbacks": False}
        if quant:
            pin["quantizations"] = [quant]
        blocks.append("extra_body.provider = " + _toml_inline(pin))
        price = {}
        if price_in is not None:
            price["in_per_1m"] = round(price_in, 4)
        if price_out is not None:
            price["out_per_1m"] = round(price_out, 4)
        if price:
            blocks.append("price = " + _toml_inline(price))
        blocks.append("")
    return "\n".join(blocks), len(groups), collapsed


def redact_url(url: str) -> str:
    """The URL without its user:password@ part, for anything that gets shared."""
    try:
        parsed = urlparse(url)
        if not (parsed.username or parsed.password):
            return url
        host = parsed.hostname or ""
        host = f"[{host}]" if ":" in host else host
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return url
    return parsed._replace(netloc=f"***@{host}{port}").geturl()


def _comment(text: str) -> str:
    # a newline in a TOML comment would end it and turn the rest into config
    return "".join(c if c.isprintable() else "?" for c in text)


def run_toml(cfg: RunConfig) -> str:
    """A [run] section (plus any overridden built-in prompt caps) that
    reproduces cfg's effective settings, flags included."""
    lines = [
        "[run]",
        f"reps = {cfg.reps}",
        f"concurrency = {_toml_inline(cfg.concurrency)}",
        f"warmup = {cfg.warmup}",
        f"timeout_s = {_toml_inline(cfg.timeout_s)}",
        f"prompts = {_toml_inline(list(cfg.prompts))}",
    ]
    if cfg.temperature is not None:
        lines.append(f"temperature = {_toml_inline(cfg.temperature)}")
    if cfg.max_tokens_field != "max_tokens":
        lines.append(f"max_tokens_field = {_toml_inline(cfg.max_tokens_field)}")
    for name, spec in cfg.prompts.items():
        cap = int(spec.get("max_tokens", 256))
        if cap != int(DEFAULT_PROMPTS.get(name, {}).get("max_tokens", -1)):
            lines += ["", f"[prompts.{_toml_key(name)}]", f"max_tokens = {cap}"]
    return "\n".join(lines)


DEFAULT_RUN_TOML = '[run]\nreps = 7\nconcurrency = [1]\nprompts = ["prose-256"]'


def endpoint_toml(
    base_url: str,
    model: str,
    name: str | None = None,
    api_key_env: str | None = None,
    price: tuple[float, float] | None = None,
    source: str = "tps init --endpoint",
    api: str = "openai",
    run_block: str = DEFAULT_RUN_TOML,
) -> str:
    """A one-target config for any OpenAI-compatible endpoint, no gateway needed.

    `tps init --endpoint` writes it; `tps run --base-url` builds it in memory
    and saves a copy, with the run's effective settings, in the run directory
    so the run can be repeated.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UsageError(f"not an http(s) URL: {base_url!r} — e.g. https://api.example.com/v1")
    # never the netloc: a user:password@ in the URL must not become the name
    default_name = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    lines = [
        _comment(f"# generated by: {source} {redact_url(base_url)} --model {model}"),
        "# add more [[targets]] blocks to compare providers side by side",
        "",
        run_block,
        "",
        "[[targets]]",
        f"name = {_toml_inline(name or default_name)}",
        f"base_url = {_toml_inline(base_url)}",
        f"model = {_toml_inline(model)}",
    ]
    if api != "openai":
        lines.append(f"api = {_toml_inline(api)}")
    if api_key_env:
        lines.append(f"api_key_env = {_toml_inline(api_key_env)}")
    if price:
        lines.append("price = " + _toml_inline({"in_per_1m": price[0], "out_per_1m": price[1]}))
    return "\n".join(lines) + "\n"


def discover(
    model_slug: str,
    out_path: Path | None,
    api_key: str | None,
    from_json: Path | None,
    json_out: Path | None,
) -> None:
    """Library convenience wrapper: print the table, optionally write a config."""
    canonical, endpoints, payload = fetch_endpoints(model_slug, api_key, from_json)
    if json_out:
        json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    rows = normalize_endpoints(endpoints)
    print(f"{canonical}: {len(endpoints)} endpoint(s)\n", file=sys.stderr)
    print(render_endpoint_table(rows))
    print(
        "\n* provider-reported rolling stats (30m), not your measurement; "
        "often null without an API key.",
        file=sys.stderr,
    )
    if out_path:
        text, count, collapsed = targets_toml(canonical, endpoints)
        out_path.write_text(text, encoding="utf-8")
        note = f", collapsed {collapsed} indistinguishable endpoint(s)" if collapsed else ""
        print(f"\nwrote {out_path} ({count} pinned targets{note})", file=sys.stderr)


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
        inner = ", ".join(f"{_toml_key(k)} = {_toml_inline(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # JSON escapes are valid TOML basic-string escapes, but ensure_ascii's
        # surrogate pairs (🚀) are not: keep non-ASCII literal, and
        # escape DEL, the one control character JSON leaves alone
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    return str(value)


def _toml_key(key: Any) -> str:
    key = str(key)
    bare = key and all(c.isascii() and (c.isalnum() or c in "-_") for c in key)
    return key if bare else _toml_inline(key)
