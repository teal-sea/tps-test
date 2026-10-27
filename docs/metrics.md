# Metrics

Every number in the report, defined precisely. If a definition here disagrees
with your intuition, this file wins — and the code is the final authority.

## TTFT — time to first token

```
t_send → t_first_content_chunk
```

`t_send` is immediately before the HTTP request is issued. The clock stops at the
first streaming chunk that carries **content**. Role-only deltas
(`{"delta":{"role":"assistant"}}`) are ignored — counting them would let any
provider fake a fast start by sending a preamble.

TTFT includes your DNS, TLS, network RTT, queueing, and prefill. It is therefore a
measure of *your* experience, not a property of the model alone.

## ITL — inter-token latency

Median (and p90) gap between consecutive content-bearing chunks:

```
itl_i = t_chunk_i − t_chunk_{i−1}
```

This is the metric that decides whether streaming *feels* smooth. A provider can
have a great TTFT and a terrible ITL, or the reverse.

Caveat: many providers coalesce several tokens into one SSE chunk. A chunk is not
a token, so ITL here is really "chunk inter-arrival", which is what a user
perceives anyway. It is not comparable to a per-token ITL measured from raw
logits.

## out tok/s — per-stream decode rate

```
completion_tokens / (t_last_chunk − t_first_chunk)
```

Decode phase only: prefill is excluded, so a slow start can't hide inside the
average. Uses `usage.completion_tokens` when the provider reports it; otherwise
chars/4, flagged as `chars/4` in the report's token-source column.

## aggregate tok/s — throughput under concurrency

```
Σ completion_tokens over a batch / wall clock of that batch
```

Measured per (prompt, target, concurrency) batch. The report shows the median
across batches. This is where rate limits live: if c=1 gives 80 tok/s and c=16
gives 90 tok/s, the provider is queueing, not scaling.

## ok / n

`ok` counts requests that returned 2xx **and** a non-empty stream. `n` is the
total attempted at that concurrency level. Errors are classified
(`rate_limited`, `timeout`, `http_400`, `transport`, `empty_stream`) and counted
in the Failures table.

There are no retries: a 429 at concurrency 16 is information.

## out tok (median completion tokens)

The sanity check nobody runs. A provider that truncates early looks fast. If one
row has a median of 90 tokens while the others have 240, its tok/s is measuring a
shorter job, not a faster one.

## Cost columns

| Column | Formula |
|---|---|
| `$/1M in`, `$/1M out` | from `price` in the target config |
| `est. $/1k requests` | `(median_in × price_in + median_out × price_out) × 1000 / 1e6` |
| `cost of 1M generated tokens` | the same per-request cost scaled to 1M output tokens |

`cost of 1M generated tokens` is the honest cross-provider number, because it
includes the prompt tokens that produced those outputs. If your workload is
input-heavy (RAG, long context), the ranking can flip entirely — hence the
per-request column.

## Percentiles

Computed over all samples in the group with linear interpolation. With 7 reps, p50
is meaningful and p90 is indicative; p99 needs 100+ samples. Do not quote a p99
from a 7-rep run.

## What is *not* measured

- **Quality.** Run an eval; speed and correctness are different questions.
- **Cold start** beyond the discarded warm-up requests.
- **Cancellation behaviour** — the rig always drains the stream.
- **Prompt-cache effects** — each request uses the same prompt, so cache hits are
  included if the provider does them. That is realistic for repeated workloads but
  flattering for first-call latency.
- **Tool calling, JSON mode, vision** — chat text only.
