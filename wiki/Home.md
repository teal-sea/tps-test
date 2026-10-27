# tps-test

**Measure one model across many providers and gateways.**

Time-to-first-token, inter-token latency, tokens/sec, throughput under load,
failures, cost — plus a fingerprint mode that tests whether those providers are
even serving the same weights.

```
$ tps run -c targets.toml

prose-256 — single stream   ttft = first token · tok/s = per-stream decode · itl = gap between chunks
provider        ttft  tok/s  spread                     itl   ok  $/1M out
-------------  -----  -----  ------------------------  ----  ---  --------
deepinfra       0.42s   78.3  ████████████████████████  12ms  7/7      0.42
together        0.61s   51.9  ████████████████          18ms  7/7      1.20
```

## Quick start

```bash
pipx install git+https://github.com/teal-sea/tps-test

tps init --model deepseek/deepseek-v4.1-flash   # who serves it, at what price
tps doctor                                       # env + config + credentials
export OPENROUTER_API_KEY=...
tps run -c targets.toml                          # the measurement
tps fingerprint -c targets.toml                  # same weights, or same name?
```

No key yet? `tps run -c targets.mock.toml --mock` renders the full report shape
with synthetic numbers, offline and instantly.

## Pages

| Page | What |
|---|---|
| [[Getting Started]] | install → first measurement → reading the output |
| [[CLI Reference]] | commands, flags, exit codes, JSON |
| [[Configuration]] | `targets.toml` anatomy and pinning |
| [[Metrics]] | exact definition of every number |
| [[Methodology]] | how not to produce a misleading comparison |
| [[Gateways]] | measuring the gateway itself |
| [[Fingerprinting]] | same weights or just the same name |
| [[Automation]] | CI, cron, JSON pipelines, agents |
| [[Troubleshooting]] | the errors you will actually hit |

## What it answers

| Question | Command |
|---|---|
| Who serves this model, at what price and quantization? | `tps discover <model>` |
| Which is fastest, snappiest, cheapest? | `tps run` |
| Does it scale or just queue under load? | `tps run --concurrency 1,16` |
| What did the gateway hop cost? | `tps run` with `baseline =` |
| Are these providers the same weights? | `tps fingerprint` |
| Did a provider get worse since last week? | `tps run` twice, diff `summary.csv` |

## Source

<https://github.com/teal-sea/tps-test> · docs in [`docs/`](https://github.com/teal-sea/tps-test/tree/main/docs) ·
agent contract in [`AGENTS.md`](https://github.com/teal-sea/tps-test/blob/main/AGENTS.md).
