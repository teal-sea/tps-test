# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [0.1.0] — unreleased

First public release.

### Added

- `tps run`: streaming TTFT, first-answer time, ITL, per-stream tok/s, aggregate
  throughput under concurrency, failures and cost, interleaved across targets.
- `tps fingerprint`: greedy-decode agreement matrix, noise floors, tokenizer
  signature, clusters and a `split` / `consistent` / `inconclusive` verdict.
- `tps discover` / `tps init --model`: one pinned target per OpenRouter endpoint.
- `tps doctor`, `tps report` (terminal tables or `--markdown`).
- `--json` on every command with stable exit codes; partial runs survive Ctrl-C.
- Redesigned terminal output: boxed tables, best-value highlights, smooth bars,
  heatmap, live progress with ETA. `NO_COLOR` / `FORCE_COLOR` honoured.
- Docs site (mkdocs-material), wiki generated from `docs/`, README images
  rendered from real CLI output.
- `tps run --base-url URL --model ID` and `tps init --endpoint URL --model ID`:
  measure any OpenAI-compatible endpoint without OpenRouter or a hand-written
  config; `--price IN,OUT` fills the cost columns.
- `tps run --dry-run`: requests, token budget, worst-case cost and rough time,
  with nothing sent.
- `tps run --max-tokens / --temperature / --timeout` (and `fingerprint
  --timeout`) override the config.
- `finish_reason` and reasoning-token counts are recorded; results carry
  `truncated`, `reasoning_only` and `notes`.

### Fixed before release

- A fingerprint target whose request failed was reported as a different artifact.
- A noisy or unmeasured fingerprint target could bridge two distinguishable
  groups and turn a `split` verdict into `consistent`.
- A reasoning model that spent its whole `max_tokens` thinking produced a row
  of unexplained `—`; it is now flagged as *measured thinking, not answering*.
- Warm-ups generated a full-length answer that was thrown away; they are now
  capped at 16 tokens.
- `raw.jsonl` didn't say which rows were warm-ups (only `batch: -1`); every row
  now has `phase`, and run JSON labels `total` / `warmup_requests` /
  `planned_requests`.
- The selftest's no-buffering check used a wall-clock budget that a cold first
  connection could exceed; it now compares against the same request's end. A
  missing dependency prints setup steps instead of a traceback.
- `summary.csv` repeated the lowest-concurrency latency on every concurrency row.
- `results[].ok/n` hid failures at higher concurrency; now counts every level.
- A `mock` profile in a real config produced synthetic rows without `--mock`.
- Single-chunk responses reported near-infinite tok/s; mid-stream SSE errors
  counted as successes; first-chunk tokens inflated tok/s for chunky providers.
- `out_tps_p90` was the fast end; p90 is now the slow tail for every metric.
- Bad flags exited 2 ("missing credentials"); they exit 1 with a JSON error.
- Input cost was dropped when a provider omitted `usage`.
- Duplicate target names silently pooled their samples.
- The fingerprint verdict counted targets with no measured noise floor
  (`--reps 1`, or a failed second rep) as deterministic, so it could say
  `split` without evidence.
- API keys could survive in stored errors when sent via custom headers or cut
  mid-key by truncation; both are now redacted.
- A quoted price crashed the run after every request had been sent.

[0.1.0]: https://github.com/teal-sea/tpsbench
