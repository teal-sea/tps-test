# Metrics

Every number, defined. Code is the final authority.

## TTFT — time to first token

```
t_send → t_first_content_chunk
```

Stops at the first chunk carrying **content**. Role-only deltas are ignored —
counting them would let a provider fake a fast start with a preamble.

Includes DNS, TLS, RTT, queueing and prefill: it measures *your* experience, not
the model in isolation.

## ITL — inter-token latency

Median and p90 gap between consecutive content chunks.

This is what makes streaming feel smooth. A provider can have a great TTFT and
terrible ITL, or the reverse.

Caveat: many providers coalesce several tokens into one SSE chunk, so this is
chunk inter-arrival — which is what a user perceives anyway, but it is not the
per-token ITL you would measure from raw logits.

## out tok/s — per-stream decode rate

```
completion_tokens / (t_last_chunk − t_first_chunk)
```

Decode only, prefill excluded, so a slow start can't hide in the average. Token
counts come from `usage.completion_tokens`; the chars/4 fallback is flagged in the
report's token-source column.

## aggregate tok/s — throughput under load

```
Σ completion_tokens over a batch / wall clock of that batch
```

Median across batches, per concurrency level. Where rate limits show up: 80 tok/s
at c=1 and 90 tok/s at c=16 means queueing, not scaling.

## ok / n

2xx **and** a non-empty stream counts as ok. Errors are classified
(`rate_limited`, `timeout`, `http_400`, `transport`, `empty_stream`) and tabulated.
No retries: a 429 at c=16 is information.

## out tok (median)

The sanity check nobody runs. Early truncation looks fast. A row with 90 median
tokens next to rows with 240 is measuring a shorter job, not a faster one.

## Cost

| Column | Formula |
|---|---|
| `$/1M in`, `$/1M out` | from config `price` |
| `est. $/1k requests` | `(median_in × p_in + median_out × p_out) × 1000 / 1e6` |
| `cost of 1M generated tokens` | per-request cost scaled to 1M output tokens |

The last one includes the prompt tokens that produced those outputs, which is the
honest cross-provider comparison. For input-heavy workloads (RAG, long context)
the ranking flips — hence the per-request column.

## Percentiles

Linear interpolation over the group. 7 reps gives a usable p50 and an indicative
p90; p99 needs 100+ samples. Never quote a p99 from seven requests.

## Not measured

Quality. Cold start beyond warm-up. Cancellation. Tool calling, JSON mode, vision,
embeddings. Prompt-cache effects are *included* if the provider does them — realistic
for repeated workloads, flattering for first-call latency.
