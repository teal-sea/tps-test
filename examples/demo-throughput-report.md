# tps report — demo (MOCK DATA)

> **MOCK DATA** — synthetic numbers from `--mock`. Not a measurement; never publish them.

- model: `demo-model` · reps 7 · concurrency [1, 4] · temp 0 · max_tokens 256 · http2 True
- captured: (demo run — regenerate with scripts/render_assets.py)

TTFT = time to the first generated token (content or reasoning). tok/s = per-stream decode rate across the streaming window. ITL = median gap between content chunks; tok/chunk shows how many tokens each chunk carried, and † marks streams that arrived in bursts, where ITL reflects flushing rather than the model. **p90 is always the slow tail** (for tok/s, the rate 90% of requests met or beat). Aggregate = all tokens of a concurrent batch ÷ its wall clock. Definitions: `docs/metrics.md`.

## `prose-256` — single stream

| provider | TTFT p50 | TTFT p90 | tok/s p50 | tok/s p90 | ITL p50 ms | tok/chunk | E2E p50 | out tok | ok | Δ TTFT vs mid-provider | Δ tok/s vs mid-provider |
|---|---|---|---|---|---|---|---|---|---|---|---|
| fast-provider | 0.32 | 0.34 | 99.7 | 94.3 | 10.0 | 1.0 | 2.54 | 242 | 7/7 | -0.13s | +89% |
| mid-provider | 0.45 | 0.60 | 52.8 | 49.0 | 18.9 | 1.0 | 4.98 | 231 | 7/7 | — | — |
| slow-provider | 1.13 | 1.28 | 29.3 | 26.8 | 34.1 | 1.0 | 9.40 | 244 | 7/7 | +0.69s | -45% |

```
fast-provider  ████████████████████████████    99.7 tok/s
mid-provider   ███████████████                 52.8 tok/s
slow-provider  ████████                        29.3 tok/s
```

## `prose-256` — under load

| provider | c | aggregate tok/s | vs c=1 | per-stream tok/s p50 | TTFT p50 | ok | errors |
|---|---|---|---|---|---|---|---|
| fast-provider | 1 | 88.4 | 1.0× | 99.7 | 0.32 | 7/7 | — |
| fast-provider | 4 | 312.6 | 3.5× | 96.9 | 0.32 | 28/28 | — |
| mid-provider | 1 | 48.1 | 1.0× | 52.8 | 0.45 | 7/7 | — |
| mid-provider | 4 | 122.7 | 2.6× | 36.9 | 0.55 | 28/28 | — |
| slow-provider | 1 | 25.9 | 1.0× | 29.3 | 1.13 | 7/7 | — |
| slow-provider | 4 | 35.1 | 1.4× | 10.7 | 1.26 | 28/28 | — |

Aggregate rising roughly with `c` means the provider scales; flat aggregate with collapsing per-stream tok/s means it queues.

## Cost & provenance

| provider | $/1M in | $/1M out | est. $/1k requests | cost of 1M generated tokens | served by | token source | est. $ this run |
|---|---|---|---|---|---|---|---|
| fast-provider | 0.260 | 0.380 | 0.108 | 0.44 | mock | mock | 0.0037 |
| mid-provider | 0.280 | 0.420 | 0.114 | 0.49 | mock | mock | 0.0041 |
| slow-provider | 0.350 | 0.600 | 0.167 | 0.69 | mock | mock | 0.0057 |

Estimated spend for this run: **$0.0135** — successful requests only, warm-ups excluded, at the configured prices.

> Cost uses the median request across all prompts. Provider attribution comes from the response (`provider` field or `x-openrouter-provider` header) when the gateway exposes it; if the column is empty you were routed by a gateway, not pinned.
