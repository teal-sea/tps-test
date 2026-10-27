# Getting Started

Ten minutes from zero to a real comparison.

## 1. Install

```bash
pipx install git+https://github.com/teal-sea/tps-test
# or, from a clone:
git clone https://github.com/teal-sea/tps-test && cd tps-test && pipx install .
# or, without installing anything:
python -m tps --help
```

Requires Python 3.11+. One dependency (`httpx`); `httpx[http2]` recommended.

```bash
tps --version
tps doctor
```

`doctor` checks Python, `httpx`, HTTP/2, your config and your credentials. Red
rows block; ⚠ rows are optional. Add `--network` to probe each `base_url`.

## 2. Find out who serves your model

```bash
tps discover deepseek/deepseek-v4.1-flash --out targets.toml
```

Prints every endpoint with price, quantization and context, and writes one
**pinned** target per endpoint. Pinning matters: without it you measure the
router's choice, not the provider.

## 3. Set a key

```bash
export OPENROUTER_API_KEY=sk-or-...
tps doctor          # credentials row goes green
```

Any OpenAI-compatible endpoint works — the key must match `api_key_env` in the
target. OpenRouter is just the cheapest way to reach many upstreams with one key.

## 4. Measure

```bash
tps run -c targets.toml
```

```
prose-256 — single stream   ttft = first token · tok/s = per-stream decode · itl = gap between chunks
provider        ttft  tok/s  spread                     itl   ok  $/1M out
-------------  -----  -----  ------------------------  ----  ---  --------
deepinfra       0.42s   78.3  ████████████████████████  12ms  7/7      0.42
together        0.61s   51.9  ████████████████          18ms  7/7      1.20

  fastest  deepinfra (78.3 tok/s)
  snappiest deepinfra (0.42s to first token)
  cheapest deepinfra ($0.42/1M out)

  ✓ wrote runs/2026-10-27T12-45-39Z/report.md
```

Artifacts in `runs/<timestamp>/`:

| File | Contents |
|---|---|
| `report.md` | full tables, aggregate throughput, cost, failures |
| `summary.csv` | one row per prompt × provider × concurrency |
| `raw.jsonl` | every request; re-analyse without spending tokens |
| `meta.json` | what was configured (model, pins, prices, mock flag) |

Re-render anytime with `tps report runs/<timestamp>`.

## 5. Sanity-check the weights

Impossible prices usually mean fewer bits:

```bash
tps fingerprint -c targets.toml
```

Providers are grouped by token-level agreement under greedy decoding. Different
groups = different artifact. See [[Fingerprinting]].

## Reading the numbers

- **Compare `tok/s` at equal `ttft`** — a provider that returns one token instantly
  then crawls is not fast.
- **Check `ok`.** 6/7 means a failure; at c=16, falling `ok` is rate limiting.
- **Check `out tok`.** Early stopping looks fast; compare work done before speed.
- **Check your own noise.** Two runs of the same config teach you more than one p90.

## Next

- [[Configuration]] — concurrency sweeps, prompts, baselines
- [[Methodology]] — the ten ways a provider comparison lies
- [[Automation]] — cron, CI, agents
