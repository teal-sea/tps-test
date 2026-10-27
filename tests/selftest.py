#!/usr/bin/env python3
"""Self-test: exercises the real streaming code path against a local SSE server.

`--mock` bypasses the network layer entirely, so it would not catch a broken
SSE parser or bad TTFT math. This does: it speaks OpenAI-style chat-completions
streaming over real sockets (including the ways real providers misbehave), then
asserts on what was measured — and drives the CLI as a subprocess to hold the
exit-code and JSON contracts in place. No network egress, no API keys.

    python tests/selftest.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tps import core  # noqa: E402

TOKENS = 32
FIRST_DELAY = 0.05
TOKEN_DELAY = 0.01
BIG_CHUNK = 32  # tokens per chunk for the "big-chunks" model
BIG_DELAY = 0.02

INFLIGHT = {"n": 0}
INFLIGHT_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):  # silence
        pass

    def do_POST(self):
        with INFLIGHT_LOCK:
            INFLIGHT["n"] += 1
            inflight = INFLIGHT["n"]
        try:
            self._handle(inflight)
        finally:
            with INFLIGHT_LOCK:
                INFLIGHT["n"] -= 1

    def _json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, inflight: int) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "")

        if model == "echo-key":  # some providers echo the submitted key in errors
            # header key near the start; the bearer key straddles the 600-char cut
            auth = self.headers.get("Authorization")
            message = f"hdr={self.headers.get('x-api-key')} " + "x" * 523 + f" invalid: {auth}"
            self._json(401, {"error": {"message": message}})
            return
        if self.headers.get("Authorization") == "Bearer bad-key":
            self._json(429, {"error": {"message": "rate limited", "code": 429}})
            return
        if model == "rl-conc" and inflight > 1:
            self._json(429, {"error": {"message": "concurrency limit", "code": 429}})
            return

        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if body.get("stream") is False:
            self._non_streaming(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-openrouter-provider", "MockUpstream")
        self.end_headers()

        if model == "single-chunk":
            time.sleep(FIRST_DELAY)
            self._emit({"choices": [{"delta": {"content": "all of it at once " * 10}}]})
            self._emit({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 40}})
            self._done()
            return
        if model in ("reasoning", "reasoning-nousage"):
            time.sleep(FIRST_DELAY)
            for i in range(5):
                self._emit({"choices": [{"delta": {"reasoning": f"thinking step {i} "}}]})
                time.sleep(TOKEN_DELAY)
            if model == "reasoning":
                time.sleep(0.1)
                for i in range(10):
                    self._emit({"choices": [{"delta": {"content": f"answer{i} "}}]})
                    time.sleep(TOKEN_DELAY)
                if want_usage:
                    self._emit(
                        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 15}}
                    )
            self._done()
            return
        if model == "midstream-error":
            for i in range(5):
                self._emit({"choices": [{"delta": {"content": f"tok{i} "}}]})
            self._emit({"error": {"message": "upstream died mid-stream", "code": 502}})
            self._done()
            return
        if model == "big-chunks":
            time.sleep(FIRST_DELAY)
            for _ in range(8):
                self._emit({"choices": [{"delta": {"content": "word " * BIG_CHUNK}}]})
                time.sleep(BIG_DELAY)
            self._emit(
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 8 * BIG_CHUNK}}
            )
            self._done()
            return

        delay = 0.05 if model == "slow" else TOKEN_DELAY
        time.sleep(FIRST_DELAY)
        # role-only chunk: must NOT count as the first token
        self._emit({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        for i in range(TOKENS):
            time.sleep(delay)
            self._emit(
                {
                    "choices": [
                        {"delta": {"content": f"tok{i} "}, "index": 0, "finish_reason": None}
                    ],
                    "provider": "MockUpstream",
                }
            )
        if want_usage:
            self._emit(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": TOKENS,
                        "total_tokens": 11 + TOKENS,
                    },
                }
            )
        self._done()

    def _emit(self, obj: dict) -> None:
        try:
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise  # client hung up (timeout test); let the handler unwind

    def _done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _non_streaming(self, body: dict) -> None:
        """Deterministic greedy completion, for the fingerprint tests.

        `test-model` and `other-model` agree for 40 tokens then diverge, which
        mimics 'same base, different quantization' well enough to assert on.
        """
        model = body.get("model", "")
        if model == "fp-fail":
            self._json(500, {"error": {"message": "upstream down"}})
            return
        if model == "fp-slow":
            time.sleep(0.4)
        if model == "text-only-model" and body.get("logprobs"):
            self._json(
                400,
                {"error": {"message": "logprobs are not supported for this model", "code": 400}},
            )
            return

        tokens = [f"t{i}" for i in range(120)]
        if model == "other-model":
            tokens = tokens[:40] + [f"x{i}" for i in range(80)]
        content = " ".join(tokens)
        choice: dict = {
            "message": {"role": "assistant", "content": content},
            "finish_reason": "length",
        }
        if body.get("logprobs"):
            choice["logprobs"] = {
                "content": [{"token": t, "logprob": -0.01, "top_logprobs": []} for t in tokens]
            }
        self._json(
            200,
            {
                "choices": [choice],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": len(tokens),
                    "total_tokens": 11 + len(tokens),
                },
            },
        )


class Server(ThreadingHTTPServer):
    """Broken pipes are expected here: the timeout test disconnects on purpose."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve() -> tuple[Server, str]:
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


def tps(
    args: list[str], cwd: Path, timeout: float = 120, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "tps", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def target_block(name: str, base: str, model: str, extra: str = "") -> str:
    return f'[[targets]]\nname = "{name}"\nbase_url = "{base}"\nmodel = "{model}"\n{extra}\n'


async def main() -> int:
    server, base = serve()
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "ok  " if condition else "FAIL"
        print(f"  [{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(name)

    cfg = core.RunConfig(targets=[], prompts={}, reps=1, concurrency=[1])

    def tgt(model: str, name: str = "local", **kw) -> core.Target:
        return core.Target(
            name=name, base_url=base, model=model, api_key=kw.pop("api_key", "good-key"), **kw
        )

    target = tgt("test-model", price_in=1.0, price_out=2.0)

    try:
        async with httpx.AsyncClient(http2=False, timeout=30.0) as client:

            async def once(
                t: core.Target, batch: int = 0, stream_options: bool = True
            ) -> core.Sample:
                return await core.stream_once(
                    client, t, "hello", "prose-256", 64, cfg, 1, batch, stream_options
                )

            print("streaming (usage reported):")
            sample = await once(target)
            check("ok", sample.ok, str(sample.error))
            check(
                "ttft >= role-chunk delay",
                sample.ttft_s is not None and sample.ttft_s >= FIRST_DELAY * 0.9,
                f"ttft={sample.ttft_s}",
            )
            check(
                "ttft < 3x delay (no buffering)",
                sample.ttft_s < FIRST_DELAY * 3,
                f"ttft={sample.ttft_s}",
            )
            check("token count from usage", sample.out_tokens == TOKENS, str(sample.out_tokens))
            check("prompt tokens from usage", sample.in_tokens == 11, str(sample.in_tokens))
            check("count source", sample.count_source == "usage", sample.count_source)
            check("provider attribution", sample.served_by == "MockUpstream", str(sample.served_by))
            expected_tps = 1 / TOKEN_DELAY
            check(
                "out tok/s within 20% of truth",
                sample.out_tps is not None
                and abs(sample.out_tps - expected_tps) / expected_tps < 0.2,
                f"{sample.out_tps} vs {expected_tps:.1f}",
            )
            check(
                "itl ~ token delay",
                sample.itl_p50_ms is not None
                and 0.8 * TOKEN_DELAY * 1000 <= sample.itl_p50_ms <= 3 * TOKEN_DELAY * 1000,
                f"{sample.itl_p50_ms}",
            )

            print("streaming (no usage block -> chars/4 fallback):")
            fallback = await once(target, 1, stream_options=False)
            check("ok", fallback.ok, str(fallback.error))
            check("fallback flagged", fallback.count_source == "chars/4", fallback.count_source)
            check(
                "fallback token estimate plausible",
                fallback.out_tokens is not None and fallback.out_tokens > 0,
                str(fallback.out_tokens),
            )
            check(
                "input tokens estimated too (cost needs them)",
                fallback.in_tokens is not None and fallback.in_tokens > 0,
                str(fallback.in_tokens),
            )

            print("429 handling:")
            denied = await once(tgt("m", name="bad", api_key="bad-key"), 2)
            check("not ok", not denied.ok)
            check("status 429", denied.status == 429, str(denied.status))
            check(
                "tagged rate_limited",
                (denied.error or "").startswith("rate_limited"),
                str(denied.error),
            )

            echoed = await once(
                tgt(
                    "echo-key",
                    api_key="sk-selftest-secret-123456",
                    headers={"x-api-key": "sk-header-SECRET-0123456789"},
                )
            )
            check(
                "echoed keys redacted (header keys, and across the truncation cut)",
                "sk-" not in (echoed.error or "") and "[redacted]" in (echoed.error or ""),
                str(echoed.error)[-120:],
            )

            print("timeout handling:")
            async with httpx.AsyncClient(http2=False, timeout=httpx.Timeout(0.02)) as impatient:
                hung = await core.stream_once(
                    impatient, tgt("m", name="slow"), "hello", "prose-256", 64, cfg, 1, 3, True
                )
            check("timeout recorded", hung.error == "timeout", str(hung.error))

            print("misbehaving providers:")
            single = await once(tgt("single-chunk"))
            check("single chunk: ok (tokens arrived)", single.ok, str(single.error))
            check("single chunk: no invented tok/s", single.out_tps is None, str(single.out_tps))
            midstream = await once(tgt("midstream-error"))
            check("in-band stream error is a failure", not midstream.ok, str(midstream.error))
            check(
                "in-band stream error classified",
                (midstream.error or "").startswith("stream_error"),
                str(midstream.error),
            )
            big = await once(tgt("big-chunks"))
            # exact, timing-independent: the window is credited with the 7 chunks
            # that arrived inside it (224 tokens), not all 256
            credited = big.out_tps * big.decode_s if big.out_tps and big.decode_s else None
            check(
                "big chunks: first chunk not credited to the window",
                credited is not None and abs(credited - 7 * BIG_CHUNK) < 1,
                f"credited {credited} tokens, expected {7 * BIG_CHUNK}",
            )
            reasoning = await once(tgt("reasoning"))
            check("reasoning: ok", reasoning.ok, str(reasoning.error))
            check(
                "reasoning: first answer token recorded after ttft",
                reasoning.first_content_s is not None
                and reasoning.ttft_s is not None
                and reasoning.first_content_s - reasoning.ttft_s > 0.08,
                f"ttft={reasoning.ttft_s} first_content={reasoning.first_content_s}",
            )
            thinking = await once(tgt("reasoning-nousage"))
            check(
                "reasoning-only stream: chars/4 counts reasoning text",
                thinking.out_tokens is not None
                and thinking.out_tokens > 5
                and thinking.count_source == "chars/4",
                f"{thinking.out_tokens} {thinking.count_source}",
            )

        print("report pipeline:")
        run_dir = Path(tempfile.mkdtemp(prefix="tps-selftest-"))
        rows = [sample.as_dict(), fallback.as_dict(), denied.as_dict()]
        for row in rows:
            row["batch_wall_s"] = row.get("e2e_s")
        (run_dir / "raw.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "created": "selftest",
                    "mock": False,
                    "reps": 2,
                    "concurrency": [1],
                    "temperature": 0.0,
                    "targets": [
                        {
                            "name": "local",
                            "model": "test-model",
                            "base_url": base,
                            "pin": None,
                            "price_in": 1.0,
                            "price_out": 2.0,
                        }
                    ],
                    "prompts": ["prose-256"],
                    "http2": False,
                }
            ),
            encoding="utf-8",
        )
        samples, meta = core.load_samples(run_dir)
        report = core.write_report(run_dir, samples, meta)
        check("report has table row", "| local |" in report)
        check("report shows error count", "rate_limited" in report)
        check("report flags estimated counts", "≈" in report)
        check(
            "p90 tok/s is the slow tail", core.p90([10, 20, 30, 40, 50], higher_is_better=True) < 20
        )

        print("fingerprint (same weights vs different artifact):")
        fp_cfg = core.RunConfig(
            targets=[], prompts={"prose-256": dict(core.DEFAULT_PROMPTS["prose-256"])}
        )
        alpha = tgt("test-model", name="alpha", api_key="k")
        beta = tgt("other-model", name="beta", api_key="k")
        gamma = tgt("text-only-model", name="gamma", api_key="k")
        down = tgt("fp-fail", name="down", api_key="k")
        async with httpx.AsyncClient(http2=False, timeout=30.0) as fp_client:
            prints = []
            for t in (alpha, alpha, beta, beta, gamma, gamma):
                prints.append(
                    await core.fingerprint_once(fp_client, t, "probe", "prose-256", 120, fp_cfg, 0)
                )
            failed_prints = [
                await core.fingerprint_once(fp_client, down, "probe", "prose-256", 120, fp_cfg, rep)
                for rep in (0, 1)
            ]
        a0, a1, b0, b1, g0, g1 = prints
        check("streamed tokens parsed", a0.ok and a0.tokens[:3] == ["t0", "t1", "t2"])
        check("self-agreement is exact", a0.tokens == a1.tokens)
        frac_cross, first_cross = core.token_agreement(a0.tokens, b0.tokens, "tokens", "tokens")
        check("cross agreement 40/120", abs(frac_cross - 40 / 120) < 1e-9, str(frac_cross))
        check("first divergence at 40", first_cross == 40, str(first_cross))
        frac_short, first_short = core.token_agreement(
            a0.tokens[:10], a0.tokens, "tokens", "tokens"
        )
        check(
            "early stop is not a perfect match",
            frac_short < 0.1 and first_short == 10,
            f"{frac_short} {first_short}",
        )
        check(
            "logprobs-unsupported falls back to text mode",
            g0.ok and g0.mode == "text",
            f"{g0.mode} {g0.error}",
        )
        check("text mode self-agreement", core.compare_prints(g0, g1)[0] == 1.0)

        fp_report = core.fingerprint_reports(prints, ["alpha", "beta", "gamma"], ["prose-256"])
        check("two weight clusters", fp_report.count("**group") == 2, fp_report[:200])
        check(
            "alpha+gamma cluster together",
            "alpha, gamma" in fp_report or "gamma, alpha" in fp_report,
        )
        check("no reps -> noise floor reported as not measured", "Not measured" in fp_report)

        # a target that errored is absence of evidence, never "different weights"
        same = [
            core.Fingerprint("a", "p", rep, True, tokens=a0.tokens, text=a0.text, prompt_tokens=11)
            for rep in (0, 1)
        ] + [
            core.Fingerprint("b", "p", rep, True, tokens=a0.tokens, text=a0.text, prompt_tokens=11)
            for rep in (0, 1)
        ]
        for f in failed_prints:
            f.target, f.prompt = "down", "p"
        summary = core.fingerprint_summary(same + failed_prints, ["a", "b", "down"], ["p"])
        check(
            "failed target not clustered",
            summary["clusters"] == [["a", "b"]],
            str(summary["clusters"]),
        )
        check(
            "failed target listed as no data", "down" in summary["no_data"], str(summary["no_data"])
        )
        check(
            "verdict consistent despite failure",
            summary["verdict"] == "consistent",
            summary["verdict"],
        )
        none = core.fingerprint_summary(failed_prints, ["down"], ["p"])
        check("all failed -> inconclusive", none["verdict"] == "inconclusive", none["verdict"])
        one_rep = core.fingerprint_summary(
            [
                core.Fingerprint("a", "p", 0, True, tokens=a0.tokens),
                core.Fingerprint("b", "p", 0, True, tokens=b0.tokens),
            ],
            ["a", "b"],
            ["p"],
        )
        check(
            "no noise floor (--reps 1) -> inconclusive, never split",
            one_rep["verdict"] == "inconclusive" and not one_rep["noise_floor_measured"],
            one_rep["verdict"],
        )
        second_failed = same + [
            core.Fingerprint("c", "p", 0, True, tokens=b0.tokens, text=b0.text),
            core.Fingerprint("c", "p", 1, False, error="rate_limited: 429"),
        ]
        partial = core.fingerprint_summary(second_failed, ["a", "b", "c"], ["p"])
        check(
            "target without a floor is unmeasured and can't force a split",
            partial["verdict"] == "consistent" and partial["unmeasured"] == ["c"],
            f"{partial['verdict']} {partial['unmeasured']} {partial['clusters']}",
        )
        # x and y differ at 2 of 100 tokens (98%: split at a perfect floor); u
        # is 99% from each, so in one union-find it would bridge them
        ref = [f"t{i}" for i in range(100)]
        x_toks, y_toks, u_toks = ref, list(ref), list(ref)
        y_toks[10], y_toks[20], u_toks[10] = "y10", "y20", "y10"
        firm = [
            core.Fingerprint(name, "p", rep, True, tokens=toks)
            for name, toks in (("x", x_toks), ("y", y_toks))
            for rep in (0, 1)
        ]
        u0 = core.Fingerprint("u", "p", 0, True, tokens=u_toks)
        bridged = {
            "unmeasured": [u0, core.Fingerprint("u", "p", 1, False, error="rate_limited: 429")],
            "noisy": [u0, core.Fingerprint("u", "p", 1, True, tokens=u_toks[:50] + ref[:50])],
        }
        for kind, u_prints in bridged.items():
            bridge = core.fingerprint_summary(firm + u_prints, ["x", "y", "u"], ["p"])
            check(
                f"{kind} target can't bridge two groups into consistent",
                bridge["verdict"] == "split"
                and bridge["distinguishable_groups"] == 2
                and bridge["clusters"] == [["x"], ["y"], ["u"]]
                and bridge[kind] == ["u"],
                f"{bridge['verdict']} {bridge['clusters']}",
            )
        joined = core.fingerprint_summary(
            same + [core.Fingerprint("u", "p", 0, True, tokens=a0.tokens)],
            ["a", "b", "u"],
            ["p"],
        )
        check(
            "unmeasured target joins the one group it matches",
            joined["verdict"] == "consistent" and joined["clusters"] == [["a", "b", "u"]],
            f"{joined['verdict']} {joined['clusters']}",
        )

        print("discover (offline fixture):")
        fixture = ROOT / "tests" / "fixtures" / "openrouter-endpoints.sample.json"
        out = run_dir / "targets.toml"
        core.discover("deepseek/deepseek-v3.2", out, None, fixture, None)
        loaded = core.load_config(out)
        check("generated toml parses", len(loaded.targets) > 0, str(len(loaded.targets)))
        check(
            "every target is pinned",
            all(t.extra_body.get("provider", {}).get("only") for t in loaded.targets),
        )
        tricky = [
            {
                "provider_name": 'Quote "Labs"',
                "tag": "quote",
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
        text, _count, _collapsed = core.targets_toml('vendor/model"x', tricky)
        tricky_path = run_dir / "tricky.toml"
        tricky_path.write_text(text, encoding="utf-8")
        reloaded = core.load_config(tricky_path).targets[0]
        check("names with quotes stay valid TOML", reloaded.name == 'Quote "Labs"', reloaded.name)
        check("free endpoints keep a 0 price", reloaded.price_out == 0.0, str(reloaded.price_out))

        print("cli contract (subprocess):")
        work = Path(tempfile.mkdtemp(prefix="tps-cli-"))
        (work / "local.toml").write_text(
            '[run]\nreps = 2\nconcurrency = [1, 4]\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block(
                "good", base, "test-model", "price = { in_per_1m = 0.1, out_per_1m = 0.2 }"
            )
            + target_block("limited", base, "rl-conc"),
            encoding="utf-8",
        )
        result = tps(["run", "-c", "local.toml", "-o", "out", "--json"], work)
        check("run exits 0", result.returncode == 0, result.stderr[-400:])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        check(
            "stdout is exactly one JSON object",
            payload.get("schema_version") == 1,
            result.stdout[:200],
        )
        rows = {r["target"]: r for r in payload.get("results", [])}
        limited = rows.get("limited", {})
        check(
            "ok/n counts every concurrency level",
            limited.get("n") == 2 * 1 + 2 * 4 and limited.get("ok", 99) < limited.get("n", 0),
            f"{limited.get('ok')}/{limited.get('n')}",
        )
        check(
            "by_concurrency shows where it failed",
            limited.get("by_concurrency", {}).get("4", {}).get("errors", {}).get("rate_limited", 0)
            > 0,
            str(limited.get("by_concurrency")),
        )
        with open(work / "out" / "summary.csv", encoding="utf-8") as handle:
            csv_rows = [r for r in csv.DictReader(handle) if r["provider"] == "good"]
        by_c = {r["concurrency"]: r for r in csv_rows}
        check(
            "csv rows carry their own concurrency's counts",
            by_c.get("1", {}).get("n") == "2" and by_c.get("4", {}).get("n") == "8",
            str({c: r.get("n") for c, r in by_c.items()}),
        )
        out_report = (work / "out" / "report.md").read_text(encoding="utf-8")
        load_rows = [
            line for line in out_report.splitlines() if line.startswith("| good | ") and "×" in line
        ]
        check("under-load table: one row per level", len(load_rows) == 2, str(load_rows))
        meta = json.loads((work / "out" / "meta.json").read_text())
        check("meta records completion", meta.get("complete") is True, str(meta.get("complete")))

        narrow = tps(["report", "out"], work, env_extra={"COLUMNS": "50"})
        widest = max(
            (len(line) for line in narrow.stdout.splitlines() if line[:1] in "│╭├╰"), default=0
        )
        check(
            "narrow terminal: tables fit by hiding optional columns",
            widest <= 50,
            f"widest line {widest}",
        )
        report_run = tps(["report", "out"], work)
        check(
            "report re-renders",
            report_run.returncode == 0 and "good" in report_run.stdout,
            report_run.stderr[-300:],
        )
        markdown = tps(["report", "out", "--markdown"], work)
        check(
            "report --markdown prints markdown",
            markdown.stdout.startswith("# tps report"),
            markdown.stdout[:80],
        )

        bad_flag = tps(["run", "--json", "--reps", "abc"], work)
        check("bad flag exits 1 (not 2)", bad_flag.returncode == 1, str(bad_flag.returncode))
        check("bad flag still emits JSON", '"ok": false' in bad_flag.stdout, bad_flag.stdout[:120])
        check(
            "JSON error names the command",
            json.loads(bad_flag.stdout or "{}").get("command") == "run",
            bad_flag.stdout[:200],
        )
        version = tps(["--json", "--version"], work)
        check(
            "--json --version is a JSON object",
            json.loads(version.stdout or "{}").get("version") is not None,
            version.stdout[:80],
        )
        no_cmd = tps(["--json"], work)
        check(
            "--json with no command is an error object",
            no_cmd.returncode == 1 and '"ok": false' in no_cmd.stdout,
        )
        bad_prompt = tps(["run", "-c", "local.toml", "--prompts", "nope"], work)
        check(
            "unknown --prompts is a usage error",
            bad_prompt.returncode == 1 and "unknown prompt" in bad_prompt.stderr,
            bad_prompt.stderr[-200:],
        )
        zero = tps(["run", "-c", "local.toml", "--concurrency", "0"], work)
        check("--concurrency 0 rejected", zero.returncode == 1, zero.stderr[-200:])

        (work / "dup.toml").write_text(
            target_block("same", base, "test-model") + target_block("same", base, "other-model"),
            encoding="utf-8",
        )
        (work / "typed.toml").write_text(
            '[run]\nprompts = "prose-256"\n\n'
            + target_block(
                "t", base, "test-model", 'price = { in_per_1m = "0.14", out_per_1m = 0.42 }'
            ),
            encoding="utf-8",
        )
        typed = core.load_config(work / "typed.toml")
        check(
            "quoted price and a string `prompts` are accepted",
            typed.targets[0].price_in == 0.14 and list(typed.prompts) == ["prose-256"],
            f"{typed.targets[0].price_in} {list(typed.prompts)}",
        )
        (work / "badprice.toml").write_text(
            target_block(
                "t", base, "test-model", 'price = { in_per_1m = "cheap", out_per_1m = 1 }'
            ),
            encoding="utf-8",
        )
        bad_price = tps(["run", "-c", "badprice.toml"], work)
        check(
            "non-numeric price rejected before any request",
            bad_price.returncode == 1 and "number" in bad_price.stderr,
            bad_price.stderr[-200:],
        )
        dup = tps(["run", "-c", "dup.toml"], work)
        check(
            "duplicate target names rejected",
            dup.returncode == 1 and "duplicate" in dup.stderr,
            dup.stderr[-200:],
        )

        mock_only = tps(["run", "-c", str(ROOT / "targets.mock.toml")], work)
        check(
            "mock-only config needs --mock",
            mock_only.returncode == 1 and "--mock" in mock_only.stderr,
        )
        mock_run = tps(
            [
                "run",
                "-c",
                str(ROOT / "targets.mock.toml"),
                "--mock",
                "--reps",
                "2",
                "-o",
                "m",
                "--json",
            ],
            work,
        )
        mock_payload = json.loads(mock_run.stdout or "{}")
        check(
            "--mock run flagged in JSON",
            mock_payload.get("mock") is True and mock_run.returncode == 0,
        )

        doctor_missing = tps(["doctor", "-c", "nope.toml"], work)
        check(
            "doctor: missing config exits 1",
            doctor_missing.returncode == 1,
            str(doctor_missing.returncode),
        )
        (work / "keyed.toml").write_text(
            target_block(
                "remote",
                "https://api.example.invalid/v1",
                "m",
                'api_key_env = "TPS_SELFTEST_UNSET_KEY"',
            ),
            encoding="utf-8",
        )
        doctor_keys = tps(["doctor", "-c", "keyed.toml"], work)
        check(
            "doctor: missing key exits 2", doctor_keys.returncode == 2, str(doctor_keys.returncode)
        )
        doctor_local = tps(["doctor", "-c", "local.toml"], work)
        check(
            "doctor: 127.0.0.1 needs no key",
            doctor_local.returncode == 0,
            doctor_local.stdout[-300:],
        )

        (work / "fp.toml").write_text(
            '[run]\nprompts = ["prose-256"]\n\n'
            + target_block("alpha", base, "test-model")
            + target_block("beta", base, "test-model")
            + target_block("down", base, "fp-fail"),
            encoding="utf-8",
        )
        fp = tps(["fingerprint", "-c", "fp.toml", "-o", "fp", "--json"], work)
        fp_payload = json.loads(fp.stdout or "{}")
        check(
            "fingerprint: failed target is no_data, verdict consistent",
            fp_payload.get("verdict") == "consistent" and "down" in fp_payload.get("no_data", {}),
            fp.stdout[:300],
        )
        (work / "fp_down.toml").write_text(target_block("down", base, "fp-fail"), encoding="utf-8")
        fp_down = tps(["fingerprint", "-c", "fp_down.toml", "-o", "fpd", "--json"], work)
        check(
            "fingerprint: every request failed exits 4",
            fp_down.returncode == 4,
            str(fp_down.returncode),
        )

        print("interrupt keeps partial data:")
        (work / "slow.toml").write_text(
            '[run]\nreps = 50\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("slow", base, "slow"),
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "tps", "run", "-c", "slow.toml", "-o", "partial"],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        raw_path = work / "partial" / "raw.jsonl"
        deadline = time.time() + 30
        while time.time() < deadline and (not raw_path.exists() or raw_path.stat().st_size == 0):
            time.sleep(0.1)
        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        _out, err = proc.communicate(timeout=30)
        check("ctrl-c exits 130", proc.returncode == 130, f"{proc.returncode} {err[-200:]}")
        kept = (
            [line for line in raw_path.read_text().splitlines() if line.strip()]
            if raw_path.exists()
            else []
        )
        check("finished requests kept on disk", len(kept) >= 1, str(len(kept)))
        partial_meta = json.loads((work / "partial" / "meta.json").read_text())
        check("meta marks the run partial", partial_meta.get("complete") is False)
        partial_report = work / "partial" / "report.md"
        check(
            "partial report written and flagged",
            partial_report.exists() and "PARTIAL" in partial_report.read_text(),
        )
        (work / "fp_slow.toml").write_text(
            '[run]\nprompts = ["prose-256"]\n\n' + target_block("slow", base, "fp-slow"),
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tps",
                "fingerprint",
                "-c",
                "fp_slow.toml",
                "-o",
                "fpartial",
                "--reps",
                "30",
            ],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fp_path = work / "fpartial" / "fingerprints.jsonl"
        deadline = time.time() + 30
        while time.time() < deadline and (not fp_path.exists() or fp_path.stat().st_size == 0):
            time.sleep(0.1)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=30)
        kept_fp = (
            [line for line in fp_path.read_text().splitlines() if line.strip()]
            if fp_path.exists()
            else []
        )
        fp_md = work / "fpartial" / "fingerprint.md"
        check(
            "fingerprint ctrl-c: exits 130, keeps data, partial report",
            proc.returncode == 130
            and len(kept_fp) >= 1
            and fp_md.exists()
            and "PARTIAL" in fp_md.read_text(),
            f"{proc.returncode} {len(kept_fp)}",
        )

        print("report provenance:")
        (work / "unknown_rate.toml").write_text(
            '[run]\nreps = 1\nwarmup = 0\nprompts = ["prose-256"]\n\n'
            + target_block("single", base, "single-chunk")
            + target_block("down", base, "test-model", 'api_key = "bad-key"'),
            encoding="utf-8",
        )
        tps(["run", "-c", "unknown_rate.toml", "-o", "ur"], work)
        ur_report = (work / "ur" / "report.md").read_text(encoding="utf-8")
        chart = [line for line in ur_report.splitlines() if line.startswith("single ")]
        check("unknown rate shows as —, not 0.0", bool(chart) and "— tok/s" in chart[0], str(chart))
        cost_section = ur_report.split("## Cost & provenance", 1)[-1]
        cost_row = next(
            (line for line in cost_section.splitlines() if line.startswith("| down |")), ""
        )
        check(
            "an all-failed target claims no token source",
            cost_row.rstrip(" |").endswith("—"),
            cost_row,
        )
    finally:
        server.shutdown()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
