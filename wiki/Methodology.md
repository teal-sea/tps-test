# Methodology

The rig handles mechanics. These are the things that quietly invalidate a
provider comparison, and what the tool does about each.

## 1. Pin the upstream

An unpinned gateway row changes provider between requests. `allow_fallbacks =
false` + explicit `only` makes a failure visible instead of silently rerouted. The
report reads back who actually answered (`served by`) — empty means you were
routed, not pinned.

## 2. Same quantization

fp4 vs fp8 changes speed *and* quality. `discover` carries the quant into the pin.
Compare within a class, or state the difference out loud.

## 3. Interleave, don't batch

All-of-A then all-of-B measures 03:00 vs 03:10. The run loop **rotates target order
every rep** so a slow period spreads across providers instead of landing on
whoever went first.

## 4. Warm up, discard

Two requests per target establish TLS, HTTP/2 and any cold start, and are excluded
from statistics.

## 5. Run it twice a day

Provider throughput moves 2–3× with load. One 7-rep burst is a snapshot, not a
property.

## 6. Concurrency is not latency

c=1 measures the serving stack. c=16 measures rate limits and queue depth. Both are
real; never report one as the other.

## 7. Note your network position

TTFT contains your RTT. Bogotá and Frankfurt will disagree, correctly. Record
where you ran it.

## 8. Distrust small samples

7 reps gives a usable p50 and a shaky p90. Within ~15%, you have not measured a
difference yet.

## 9. Watch output length

Early stopping looks fast. Compare `out tok` before `tok/s`.

## 10. The model name is not the artifact

Different quantization, a distilled sibling, an older revision, another model.
`tps fingerprint` tests it — see [[Fingerprinting]].

---

## A defensible run

- identical `model`, prompt text and `max_tokens`
- `temperature = 0`
- every target pinned, `allow_fallbacks = false`
- one quantization class, or explicitly separated
- ≥ 7 reps, order rotated
- ≥ 2 concurrency levels if load matters
- date + region + p50 and p90 reported
- repeated twice before deciding

## A run to distrust

- one rep, measured in sequence
- unpinned rows labelled with provider names
- 240-token median vs 90-token median
- p99 from 7 samples
- no quantization noted across a 30× price spread
- numbers from a `--mock` run pasted into a decision

## Tool limits

No retries (tail behaviour is the measurement). Chat-completions SSE only.
`discover`'s provider stats are *their* numbers. `fingerprint` can fail to disprove
identity; it cannot prove it.
