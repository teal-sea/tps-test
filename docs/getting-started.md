# Getting started

Ten minutes from zero to a real comparison.

## 1. Install

```bash
git clone https://github.com/teal-sea/tps-test
cd tps-test
pipx install .
```

`pipx` keeps it isolated and puts `tps` on your PATH. Alternatives:

```bash
uv tool install .        # same result, uv-native
python -m tps --help     # no install; run from the repo checkout
```

Check it:

```bash
tps --version
tps doctor
```

`doctor` inspects Python, `httpx`, HTTP/2, your config, and credentials. Red rows
are blocking; the ⚠ rows are optional. Add `--network` to also probe each
`base_url`.

## 2. Get a provider list for your model

```bash
tps discover deepseek/deepseek-v4.1-flash --out targets.toml
```

This asks OpenRouter which upstreams serve that model and prints their price,
quantization and context. With `--out` it writes a config containing **one pinned
target per endpoint**:

```toml
[[targets]]
name = "DeepInfra (fp8)"
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "OPENROUTER_API_KEY"
extra_body.provider = { only = ["deepinfra/fp8"], allow_fallbacks = false, quantizations = ["fp8"] }
price = { in_per_1m = 0.14, out_per_1m = 0.42 }
```

Already know your endpoints? `tps init` writes a commented starter you can edit.

## 3. Set a key

```bash
export OPENROUTER_API_KEY=sk-or-...
tps doctor          # credentials row should go green
```

Any OpenAI-compatible endpoint works — the key just has to match `api_key_env` in
the target. OpenRouter is convenient because one key reaches every upstream.

## 4. Measure

```bash
tps run -c targets.toml
```

You get a table per prompt, plus rankings:

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

Artifacts land in `runs/<timestamp>/`:

| File | Contents |
|---|---|
| `report.md` | full tables, aggregate throughput, cost, failures |
| `summary.csv` | one row per prompt × provider × concurrency |
| `raw.jsonl` | every request; re-analyse without spending tokens |
| `meta.json` | what was configured (model, pins, prices, mock flag) |

Re-render anytime: `tps report runs/2026-10-27T12-45-39Z`.

## 5. Sanity-check the weights

Prices that look impossible usually mean fewer bits. Before trusting a cheap row:

```bash
tps fingerprint -c targets.toml
```

Output groups providers by token-level agreement under greedy decoding. Different
groups = different artifact (quantization, revision, or another model). See
[fingerprinting](fingerprinting.md).

## Reading the numbers

- **Compare `tok/s` at equal `ttft`.** A provider that returns one token instantly
  and then crawls is not fast.
- **Check `ok`.** 6/7 means one failure; at concurrency 16, `ok` falling off is
  rate limiting, not slowness.
- **Check `out tok`.** A provider that stops early looks fast. The report prints
  the median; compare it before comparing speed.
- **Check your own noise first.** Two consecutive runs of the same config tell you
  more than any single p90.

## Next

- [Configuration](configuration.md) — concurrency sweeps, prompts, baselines
- [Methodology](methodology.md) — the nine ways a provider comparison lies
- [Automation](automation.md) — running this on a schedule or in CI
