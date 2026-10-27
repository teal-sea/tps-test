# tps-test

Measure the **same model** served by **different providers**: time-to-first-token,
inter-token latency, per-stream output tokens/sec, aggregate throughput under
concurrency, failures/429s, and cost per request — on one interleaved run. Plus a
fingerprint mode that tests whether those providers are even serving the same
weights.

![selftest](https://github.com/teal-sea/tps-test/actions/workflows/selftest.yml/badge.svg)

```
$ python rig.py run -c targets.mock.toml --mock

## `prose-256` — single stream

| provider      | TTFT p50 | TTFT p90 | out tok/s p50 | out tok/s p90 | ITL p50 ms | E2E p50 | out tok | ok  |
|---------------|----------|----------|---------------|---------------|------------|---------|---------|-----|
| fast-provider | 0.25     | 0.30     | 119.7         | 135.9         | 8.4        | 2.13    | 228     | 6/6 |
| mid-provider  | 0.53     | 0.59     | 53.0          | 60.1          | 18.9       | 5.18    | 236     | 6/6 |
| slow-provider | 1.11     | 1.29     | 34.0          | 37.1          | 29.4       | 8.19    | 231     | 6/6 |
```

Sample output without running anything: [throughput report](examples/demo-throughput-report.md)
· [fingerprint report](examples/demo-fingerprint-report.md) (both mock data).

## Do you need to build this? What already exists

| Tool | Gives you | Why it isn't quite the thing |
|---|---|---|
| [Artificial Analysis](https://artificialanalysis.ai/) ([free API](https://artificialanalysis.ai/api-reference)) | Model-level median output tok/s + TTFT, independently measured, 1k req/day | One row per **model**, not per **upstream**. Great baseline, can't answer "DeepInfra vs GMICloud" |
| OpenRouter [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection.md) + `sort: "throughput"` | Provider-reported p50/p90 latency & throughput, pinning via `provider.only`, `preferred_min_throughput` | Provider-**self**-reported, rolling 5-min window, only for OpenRouter-listed endpoints. It's a router, not a measurement |
| [LLMPerf (ray-project)](https://github.com/ray-project/llmperf) | Proper load generator: concurrency sweeps, token throughput, success rate | Built to validate *an* endpoint under load. Comparing N vendors means N configs and no interleaving/routing-pin model |
| [NVIDIA GenAI-Perf](https://developer.nvidia.com/blog/llm-performance-benchmarking-measuring-nvidia-nim-performance-with-genai-perf/) | Industry-standard LLM serving benchmark (TTFT/ITL/throughput) | Aimed at Triton/NIM/your own endpoint; heavyweight for "which of these 6 APIs should I call" |
| `vllm bench serve` | Same, for vLLM you run yourself | Only useful if you host it |

None of them do the one thing that matters here: **hold the model constant while
swapping the upstream, and interleave the measurements** so a transient blip
doesn't get permanently attributed to whichever provider ran first.

So: ~1,700 lines, stdlib + `httpx`, no framework, no dependency on a gateway
(OpenRouter is just a convenient way to reach many providers with one key).

## Quickstart

```bash
# 1. one-time
python -m venv .venv && .venv/bin/pip install "httpx[http2]"
# (or: uv venv .venv && VIRTUAL_ENV=.venv uv pip install "httpx[http2]")

# 2. see the report shape with fake data (no keys, instant)
.venv/bin/python rig.py run -c targets.mock.toml --mock

# 3. enumerate every provider serving a model, with pricing + quantization,
#    and emit a ready-to-run config with one pinned target per provider
.venv/bin/python rig.py discover deepseek/deepseek-v4.1-flash --out targets.toml

# 4. measure for real
export OPENROUTER_API_KEY=...
.venv/bin/python rig.py run -c targets.toml --reps 7 --concurrency 1,4

# 5. re-render / re-analyse without re-spending money
.venv/bin/python rig.py report runs/2026-10-27T05-30-00Z
```

`discover` on `deepseek/deepseek-v4.1-flash` finds **41 endpoints** — $0.02 to
$0.60 per 1M input, $0.18 to $2.40 per 1M output, across fp4 / fp8 / mxfp4 /
unreported quantization, plus flex / priority / spot service tiers. One model
slug, roughly 30x price spread and genuinely different serving stacks behind it.
That is the comparison worth measuring.

Ready-made configs: [`targets.v4.1-flash.toml`](targets.v4.1-flash.toml) (curated
11, ~20 min) and [`targets.v4.1-flash.full.toml`](targets.v4.1-flash.full.toml)
(all 41).

## What is measured

| Metric | Definition |
|---|---|
| `TTFT` | request sent → first chunk carrying **content**. Role-only chunks are ignored (they'd fake a fast start). |
| `ITL p50/p90` | gap between consecutive content chunks. The metric that decides whether streaming *feels* smooth. |
| `out tok/s` | `completion_tokens / (last_chunk − first_chunk)` — decode phase only, so a slow prefill can't hide in it. |
| aggregate tok/s | `Σ completion_tokens / batch wall clock` at a given concurrency. Shows whether a provider scales or just queues. |
| `ok` | 2xx + non-empty stream. 429s and 5xx are recorded, never retried. |
| cost | optional per-target `price = { in_per_1m, out_per_1m }` → est. $/1k requests and fully-loaded $/1M generated tokens. |

Token counts come from `usage` (`stream_options.include_usage`). If a provider
omits it, the rig falls back to chars/4 and **flags the row** in the report's
`token source` column — never silently.

## Making the numbers mean something

The rig handles mechanics. These are the things that quietly invalidate a
provider comparison, and what to do about them:

1. **Pin the upstream.** `extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }`.
   Without it you are benchmarking a router's load balancer, and your "provider"
   column changes under you mid-run. The `served by` column in the report comes
   from the response itself — if it's empty, you were routed, not pinned.
   Use the **full tag slug** for variants and tiers (`deepinfra/fp8/flex`,
   `baseten/fast`): a base slug like `deepinfra` matches every region of the
   standard endpoint but never matches a tier endpoint, so a loosely pinned
   "priority tier" row silently measures the standard one.
2. **Same quantization.** fp4 vs fp8 changes speed *and* quality. `discover`
   carries the quant into the pin; compare within a quant class, or state it.
3. **Interleave, don't batch.** The rig rotates target order every rep. Running
   all of A then all of B measures 03:00 vs 03:10, not A vs B.
4. **Run it twice a day.** Provider throughput moves by 2–3× with time-of-day
   and load. One 7-rep burst is a snapshot, not a property.
5. **Concurrency ≠ latency.** At c=1 you measure the serving stack. At c=16 you
   measure rate limits and queue depth. Both are real; don't report one as the other.
6. **Note your network position.** TTFT contains your RTT to the provider. A rig
   in Bogotá and a rig in Frankfurt will disagree, correctly.
7. **Distrust single-digit samples.** 7 reps gives a usable p50 and a shaky p90.
   If two providers are within 15%, you haven't measured a difference yet.
8. **Watch output length.** A provider that stops early looks fast. The `out tok`
   column is there to catch it — compare that before comparing tok/s.

## Are they even serving the same weights?

Probably not, and a throughput table can't tell you. The same model slug can be
a different quantization (fp4 vs fp8), a distilled sibling, an older revision, or
a different model with a similar name. `deepseek-v4.1-flash` on one router spans
$0.02–$0.60/1M input across fp4, fp8, mxfp4 and *unreported* quantization — those
are not the same artifact, and a 3× speed difference often just means "fewer bits".

`fingerprint` tests it instead of guessing:

```bash
.venv/bin/python rig.py fingerprint -c targets.v4.1-flash.toml
```

Greedy decoding (temp 0) is nearly a function of the weights, so each target is
asked the same prompt twice and compared token-by-token:

| Observation | Reading |
|---|---|
| `self` agreement < 98% | that endpoint isn't deterministic run-to-run; its cross-provider numbers mean nothing |
| pair ≥ 99% | same weights, same kernels (or indistinguishable at this length) |
| pair 90–99%, first divergence deep | same base model, different quantization or kernels |
| pair < 90%, divergence early | **different artifact** — distilled, different revision, different model |
| `prompt_tokens` differs for identical input | different tokenizer or chat template — not the same serving stack, full stop |

Output is `fingerprint.md`: tokenizer signature, self-agreement noise floor, a
pairwise agreement matrix, and clusters. Cost is trivial — 11 providers × 2
prompts × 2 reps × 160 tokens is a few thousand tokens.

Two honest limits: it can *fail to disprove* identity but never prove it (a
distilled model can survive 150 tokens), and a provider without `logprobs`
support degrades to whole-text equality, which only catches gross differences.
Pair it with a task eval before trusting a cheaper endpoint.

## Gateways are targets too

A gateway (OpenRouter, Portkey, LiteLLM, your own proxy) is not plumbing here —
it's a row in the table, and it hides three separate questions:

| Question | Row pair | What the delta means |
|---|---|---|
| What does the hop cost? | same upstream, direct vs through the gateway | pure proxy overhead: TLS, an extra hop, request rewriting |
| What does the router's choice cost me? | unpinned vs your best pin | you're paying for convenience in latency and/or money |
| Is the gateway the bottleneck? | gateway A vs gateway B, same pins | the proxy itself, independent of any provider |

The first one is the only clean measurement, and it needs the vendor reachable
**both** ways (e.g. Fireworks direct vs OpenRouter pinned to `fireworks`). If a
provider only exists behind a gateway, "direct vs gateway" quietly becomes three
variables at once.

Set `baseline = "<target name>"` in `[run]` and the report grows Δ TTFT and Δ
tok/s columns:

```
| provider               | TTFT p50 | out tok/s p50 | Δ TTFT | Δ tok/s |
| or-pinned-together     | 0.56     | 43.2          | —      | —       |
| or-unpinned-price      | 0.40     | 47.2          | -0.16  | +9%     |
| or-unpinned-throughput | 0.68     | 50.7          | +0.12  | +17%    |
```

Ready to edit: [`targets.gateway.toml`](targets.gateway.toml).

## Files

| Path | What |
|---|---|
| `rig.py` | the whole tool: `run`, `report`, `discover`, `fingerprint` |
| `targets.v4.1-flash.toml` | curated 11-provider run for DeepSeek-V4.1-Flash |
| `targets.v4.1-flash.full.toml` | all 41 endpoints, generated by `discover` |
| `targets.gateway.toml` | gateway overhead / routing-choice comparison, with `baseline` deltas |
| `targets.example.toml` | direct-vendor + pinned-gateway + unpinned-control targets |
| `targets.mock.toml` | offline demo config |
| `tests/selftest.py` | checks the real SSE path, TTFT/ITL math, 429 + timeout handling, logprobs extraction, fingerprint clustering, report + `discover` |
| `tests/fixtures/` | recorded OpenRouter endpoints response for offline `discover` |
| `runs/<stamp>/` | `raw.jsonl`, `meta.json`, `report.md`, `summary.csv` |
| `runs/<stamp>-fingerprint/` | `fingerprints.jsonl`, `fingerprint.md` (same-weights check) |
| `examples/` | committed mock reports so you can see the output shape |
| `.github/workflows/selftest.yml` | CI: runs the selftest on push, no keys needed |

```bash
.venv/bin/python tests/selftest.py   # 28 checks, no network, no keys
```

## Limits

- Chat-completions SSE only (that's what every vendor speaks). No Responses API,
  no embeddings, no image/audio.
- Embeddings/batch endpoints would need a different load model.
- No retry/backoff logic by design — failures are data.
- `max_tokens` is the field name; set `max_tokens_field = "max_completion_tokens"`
  in `[run]` for GPT-5 / o-series targets.
