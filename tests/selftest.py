#!/usr/bin/env python3
"""Self-test: exercises the real streaming code path against a local SSE server.

The `--mock` flag in rig.py bypasses the network layer entirely, so it would not
catch a broken SSE parser or bad TTFT math. This does: it speaks OpenAI-style
chat-completions streaming over real sockets, then asserts on what rig measured.

    .venv/bin/python tests/selftest.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tps import core as rig  # noqa: E402

TOKENS = 32
FIRST_DELAY = 0.05
TOKEN_DELAY = 0.01


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.headers.get("Authorization") == "Bearer bad-key":
            payload = b'{"error":{"message":"rate limited","code":429}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        if body.get("stream") is False:
            self._non_streaming(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-openrouter-provider", "MockUpstream")
        self.end_headers()

        time.sleep(FIRST_DELAY)
        # role-only chunk: must NOT count as the first token
        self._emit({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        for i in range(TOKENS):
            time.sleep(TOKEN_DELAY)
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
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _emit(self, obj: dict) -> None:
        try:
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise  # client hung up (timeout test); let the handler unwind

    def _non_streaming(self, body: dict) -> None:
        """Deterministic greedy completion, for the fingerprint tests.

        `test-model` and `other-model` agree for 40 tokens then diverge, which
        mimics 'same base, different quantization' well enough to assert on.
        """
        model = body.get("model", "")
        if model == "text-only-model" and body.get("logprobs"):
            payload = {
                "error": {
                    "message": "logprobs are not supported for this model",
                    "code": 400,
                }
            }
            data = json.dumps(payload).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
                "content": [
                    {"token": t, "logprob": -0.01, "top_logprobs": []} for t in tokens
                ]
            }
        payload = {
            "choices": [choice],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": len(tokens),
                "total_tokens": 11 + len(tokens),
            },
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    """Broken pipes are expected here: the timeout test disconnects on purpose."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve() -> tuple[Server, str]:
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


async def main() -> int:
    server, base = serve()
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "ok  " if condition else "FAIL"
        print(f"  [{status}] {name}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(name)

    cfg = rig.RunConfig(targets=[], prompts={}, reps=1, concurrency=[1])
    target = rig.Target(
        name="local",
        base_url=base,
        model="test-model",
        api_key="good-key",
        price_in=1.0,
        price_out=2.0,
    )

    try:
        async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
            print("streaming (usage reported):")
            sample = await rig.stream_once(
                client, target, "hello", "prose-256", 64, cfg, 1, 0, True
            )
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
            check(
                "provider attribution",
                sample.served_by == "MockUpstream",
                str(sample.served_by),
            )
            expected_tps = TOKENS / (TOKENS * TOKEN_DELAY)
            check(
                "out tok/s within 20% of truth",
                sample.out_tps is not None
                and abs(sample.out_tps - expected_tps) / expected_tps < 0.2,
                f"{sample.out_tps:.1f} vs {expected_tps:.1f}",
            )
            check(
                "itl ~ token delay",
                sample.itl_p50_ms is not None
                and 0.8 * TOKEN_DELAY * 1000
                <= sample.itl_p50_ms
                <= 3 * TOKEN_DELAY * 1000,
                f"{sample.itl_p50_ms}",
            )

            print("streaming (no usage block -> chars/4 fallback):")
            fallback = await rig.stream_once(
                client, target, "hello", "prose-256", 64, cfg, 1, 1, False
            )
            check("ok", fallback.ok, str(fallback.error))
            check(
                "fallback flagged",
                fallback.count_source == "chars/4",
                fallback.count_source,
            )
            check(
                "fallback token estimate plausible",
                fallback.out_tokens is not None and fallback.out_tokens > 0,
                str(fallback.out_tokens),
            )

            print("429 handling:")
            bad = rig.Target(name="bad", base_url=base, model="m", api_key="bad-key")
            denied = await rig.stream_once(
                client, bad, "hello", "prose-256", 64, cfg, 1, 2, True
            )
            check("not ok", not denied.ok)
            check("status 429", denied.status == 429, str(denied.status))
            check(
                "tagged rate_limited",
                (denied.error or "").startswith("rate_limited"),
                str(denied.error),
            )

            print("timeout handling:")
            slow = rig.Target(name="slow", base_url=base, model="m", api_key="good-key")
            async with httpx.AsyncClient(
                http2=False, timeout=httpx.Timeout(0.02)
            ) as impatient:
                hung = await rig.stream_once(
                    impatient, slow, "hello", "prose-256", 64, cfg, 1, 3, True
                )
            check("timeout recorded", hung.error == "timeout", str(hung.error))

        print("report pipeline:")
        run_dir = ROOT / "runs" / "_selftest"
        run_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            sample.as_dict(),
            fallback.as_dict(),
            denied.as_dict(),
        ]
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
        samples, meta = rig.load_samples(run_dir)
        report = rig.write_report(run_dir, samples, meta)
        check("report has table row", "| local |" in report)
        check("report shows error count", "rate_limited" in report)

        print("fingerprint (same weights vs different artifact):")
        fp_cfg = rig.RunConfig(
            targets=[], prompts={"prose-256": dict(rig.DEFAULT_PROMPTS["prose-256"])}
        )
        alpha = rig.Target(name="alpha", base_url=base, model="test-model", api_key="k")
        beta = rig.Target(name="beta", base_url=base, model="other-model", api_key="k")
        gamma = rig.Target(
            name="gamma", base_url=base, model="text-only-model", api_key="k"
        )
        async with httpx.AsyncClient(http2=False, timeout=30.0) as fp_client:
            prints = []
            for target in (alpha, alpha, beta, beta, gamma, gamma):
                prints.append(
                    await rig.fingerprint_once(
                        fp_client, target, "probe", "prose-256", 120, fp_cfg, 0
                    )
                )
        a0, a1, b0, b1, g0, g1 = prints
        check("streamed tokens parsed", a0.ok and a0.tokens[:3] == ["t0", "t1", "t2"])
        check("self-agreement is exact", a0.tokens == a1.tokens)
        frac_cross, first_cross = rig.token_agreement(
            a0.tokens, b0.tokens, "tokens", "tokens"
        )
        check(
            "cross agreement 40/120",
            abs(frac_cross - 40 / 120) < 1e-9,
            str(frac_cross),
        )
        check("first divergence at 40", first_cross == 40, str(first_cross))
        check(
            "logprobs-unsupported falls back to text mode",
            g0.ok and g0.mode == "text",
            f"{g0.mode} {g0.error}",
        )
        check(
            "text mode self-agreement",
            rig.compare_prints(g0, g1)[0] == 1.0,
        )

        fp_report = rig.fingerprint_reports(
            prints, ["alpha", "beta", "gamma"], ["prose-256"]
        )
        check("two weight clusters", fp_report.count("**group") == 2, fp_report[:200])
        check("alpha+gamma cluster together", "alpha, gamma" in fp_report or "gamma, alpha" in fp_report)

        print("discover (offline fixture):")
        fixture = ROOT / "tests" / "fixtures" / "openrouter-endpoints.sample.json"
        out = ROOT / "runs" / "_selftest" / "targets.toml"
        rig.discover(
            "deepseek/deepseek-v3.2", out, None, fixture, None
        )
        loaded = rig.load_config(out)
        check("generated toml parses", len(loaded.targets) > 0, str(len(loaded.targets)))
        check(
            "every target is pinned",
            all(t.extra_body.get("provider", {}).get("only") for t in loaded.targets),
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
