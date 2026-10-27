# Methodology

The rig handles mechanics. These are the things that quietly invalidate a
provider comparison, and what this tool does about each.

## 1. Pin the upstream, or you are benchmarking a load balancer

An unpinned gateway row changes provider between requests. `allow_fallbacks =
false` plus an explicit `only` pin makes a failure visible instead of silently
rerouted.

The report reads back the provider that actually answered (`served by`, from the
response body or `x-openrouter-provider`) — if that column is empty, you were
routed, not pinned.

## 2. Same quantization

fp4 vs fp8 changes speed *and* quality. `tps discover` carries the quant into the
pin; compare within a quant class, or state the difference out loud.

## 3. Interleave, don't batch

Running all of A, then all of B measures 03:00 vs 03:10, not A vs B. The run loop
**rotates target order every rep**, so the slow period is spread across providers
instead of landing on whoever went first.

## 4. Warm up, then discard

Two warm-up requests per target establish TLS, HTTP/2 and any provider-side cold
start, and are excluded from the statistics.

## 5. Run it twice a day

Provider throughput moves 2–3× with time-of-day and load. One 7-rep burst is a
snapshot, not a property. If you only run it once, say so.

## 6. Concurrency is not latency

At c=1 you measure the serving stack. At c=16 you measure rate limits and queue
depth. Both are real; never report one as the other.

## 7. Note your network position

TTFT contains your RTT to the provider. A rig in Bogotá and a rig in Frankfurt
will disagree, correctly. Record where you ran it.

## 8. Distrust single-digit samples

7 reps gives a usable p50 and a shaky p90. If two providers are within ~15%, you
have not measured a difference yet — run more reps before writing a conclusion.

## 9. Watch output length

A provider that stops early looks fast. Compare the `out tok` column before
comparing tok/s.

## 10. The model name is not the artifact

Different quantization, a distilled sibling, an older revision, or a different
model entirely can sit behind the same slug. `tps fingerprint` tests it; the
[cheap-price smell test](fingerprinting.md) explains how to read the result.

---

## What "fair" looks like, concretely

A defensible run:

- identical `model`, identical prompt text, identical `max_tokens`
- `temperature = 0`
- every target pinned to a specific upstream, `allow_fallbacks = false`
- same quantization class, or explicitly separated
- ≥ 7 reps, order rotated
- ≥ 2 concurrency levels if you care about load
- reported with date, region, and both p50 and p90
- repeated at least twice before a decision

A run that should not be trusted:

- one rep, all targets measured in sequence
- unpinned gateway rows labelled with provider names
- comparing a 240-token median against a 90-token median
- a p99 quote from 7 samples
- no quantization noted when the spread is 30×
- numbers from a `--mock` run (the report says `MOCK DATA`; don't paste it)

## Limits of the tool

- No retries: tail behaviour is measured, not smoothed.
- Chat-completions SSE only.
- Provider-reported stats in `discover` are *their* numbers, not yours.
- `fingerprint` can fail to disprove identical weights; it cannot prove them.
