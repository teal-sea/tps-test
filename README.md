# tps-test

**Measure one model across many providers and gateways.** Time-to-first-token,
inter-token latency, tokens/sec, throughput under load, failures, cost — plus a
fingerprint mode that tests whether those providers are even serving the same
weights.

[![selftest](https://github.com/teal-sea/tps-test/actions/workflows/selftest.yml/badge.svg)](https://github.com/teal-sea/tps-test/actions/workflows/selftest.yml)

```
$ tps run -c targets.toml

prose-256 — single stream   ttft = first token · tok/s = per-stream decode · itl = gap between chunks
provider        ttft  tok/s  spread                     itl   ok  $/1M out
-------------  -----  -----  ------------------------  ----  ---  --------
deepinfra       0.42s   78.3  ████████████████████████  12ms  7/7      0.42
together        0.61s   51.9  ████████████████          18ms  7/7      1.20
or-unpinned     0.55s   63.4  ███████████████████       16ms  7/7         —

  fastest  deepinfra (78.3 tok/s)
  snappiest deepinfra (0.42s to first token)
  cheapest deepinfra ($0.42/1M out)
```

## Install

```bash
git clone https://github.com/teal-sea/tps-test
cd tps-test
pipx install .            # gives you the `tps` command
# or: uv tool install .   ·   or: python -m tps (no install, run from the repo)
```

Requires Python 3.11+. One dependency (`httpx`); `httpx[http2]` recommended.

## 60-second tour

```bash
tps init --model deepseek/deepseek-v4.1-flash   # provider list -> pinned config
tps doctor                                       # env, config, credentials, reachability
export OPENROUTER_API_KEY=...
tps run -c targets.toml                          # the measurement
tps fingerprint -c targets.toml                  # same weights, or just the same name?
```

No key yet? `tps run -c targets.mock.toml --mock` produces the full report shape
with synthetic numbers, instantly and offline.

## What it answers

| Question | Command |
|---|---|
| Who serves this model, at what price and quantization? | `tps discover <model>` |
| Which is fastest? Which is snappiest? Which is cheapest? | `tps run` |
| Does it scale, or just queue, when I send 16 at once? | `tps run --concurrency 1,16` |
| What did the gateway hop cost me? | `tps run` with `baseline =` |
| Are these providers serving the same weights? | `tps fingerprint` |
| Did that provider get worse since last week? | `tps run` twice, diff the CSV |

## Documentation

| Doc | What's in it |
|---|---|
| [Getting started](docs/getting-started.md) | install → first measurement → reading the output |
| [CLI reference](docs/cli.md) | every command and flag, exit codes, JSON schema |
| [Configuration](docs/configuration.md) | `targets.toml` anatomy, pinning, prices, prompts |
| [Metrics](docs/metrics.md) | exact definition of every number |
| [Methodology](docs/methodology.md) | what makes a comparison fair, and what silently invalidates one |
| [Gateways](docs/gateways.md) | hop overhead vs routing choice vs gateway-vs-gateway |
| [Fingerprinting](docs/fingerprinting.md) | detecting different weights behind the same model name |
| [Automation](docs/automation.md) | CI, cron, `--json` contracts, agent usage |
| [Troubleshooting](docs/troubleshooting.md) | the errors you will actually hit |
| [FAQ](docs/faq.md) | short answers |

**Wiki**: <https://github.com/teal-sea/tps-test/wiki> — the same material in
browsable form. If the wiki is empty, enable *Settings → Features → Wikis* and run
`scripts/publish-wiki.sh` (content lives in [`wiki/`](wiki/), so it's reviewable and
versioned with the code).

Building an agent on top? Read [AGENTS.md](AGENTS.md) instead — it's the same
contracts in a denser form.

## Why not an existing benchmark

| Tool | Gives you | Gap |
|---|---|---|
| [Artificial Analysis](https://artificialanalysis.ai/api-reference) | Independent medians per model | One row per **model**, not per **upstream** |
| [OpenRouter routing](https://openrouter.ai/docs/guides/routing/provider-selection.md) | Provider-reported p50/p90, pinning | Self-reported, 5-min window, only their endpoints |
| [LLMPerf](https://github.com/ray-project/llmperf) | Real load generation | Validates *an* endpoint; N vendors = N harnesses |
| [GenAI-Perf](https://developer.nvidia.com/blog/llm-performance-benchmarking-measuring-nvidia-nim-performance-with-genai-perf/) | Serving benchmark | Aimed at NIM/Triton, heavyweight for comparing APIs |

None of them hold the model constant, swap the upstream, and **interleave** the
measurements so a 30-second blip doesn't get attributed to whoever ran first.

## Project layout

```
tps/            # package: core.py (engine), cli.py, ui.py
tests/          # selftest.py — 28 checks, local fake server, no keys
docs/           # the documentation above
targets.*.toml  # ready-made and example configs
runs/           # measurement output (gitignored)
```

## Limits

- Chat-completions SSE only — no Responses API, embeddings, images, audio.
- No retries by design: 429s and 5xx are data, not noise to be smoothed away.
- `fingerprint` can fail to disprove identical weights; it cannot prove them.
- Numbers are from *your* network position. Latency includes your RTT.

MIT licensed.
