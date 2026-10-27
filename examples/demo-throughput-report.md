# Provider throughput rig — demo (MOCK DATA)

- model: `demo-model` · reps 6 · concurrency [1, 4] · temp 0.0 · http2 True
- captured: 2026-10-27T05:27:16+00:00

TTFT = time to first content token. ITL = gap between content chunks (lower is smoother). tok/s = per-stream decode rate (`completion_tokens / decode_window`). Aggregate = sum of tokens over batch wall clock.

## `prose-256` — single stream

| provider | TTFT p50 | TTFT p90 | out tok/s p50 | out tok/s p90 | ITL p50 ms | E2E p50 | out tok | ok |
|---|---|---|---|---|---|---|---|---|
| fast-provider | 0.25 | 0.30 | 119.7 | 135.9 | 8.4 | 2.13 | 228 | 6/6 |
| mid-provider | 0.53 | 0.59 | 53.0 | 60.1 | 18.9 | 5.18 | 236 | 6/6 |
| slow-provider | 1.11 | 1.29 | 34.0 | 37.1 | 29.4 | 8.19 | 231 | 6/6 |

```
fast-provider            ████████████████████████████   119.7 tok/s
mid-provider             ████████████    53.0 tok/s
slow-provider            ████████    34.0 tok/s
```

## `prose-256` — aggregate throughput (tokens/sec)

| provider | c=1 | c=4 |
|---|---|---|
| fast-provider | 106.4 | 292.9 |
| mid-provider | 47.4 | 152.2 |
| slow-provider | 29.0 | 80.1 |

## Cost & provenance

| provider | $/1M in | $/1M out | est. $/1k requests | cost of 1M generated tokens | served by | token source |
|---|---|---|---|---|---|---|
| fast-provider | 0.260 | 0.380 | 0.102 | 0.45 | mock | mock |
| mid-provider | 0.280 | 0.420 | 0.116 | 0.49 | mock | mock |
| slow-provider | 0.350 | 0.600 | 0.160 | 0.69 | mock | mock |

> Provider attribution comes from the response (`provider` field or `x-openrouter-provider` header) when the gateway exposes it. If that column is empty you were routed by a gateway, not pinned to an upstream.
