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

`doctor` inspects Python, `httpx`, HTTP/2, your config, and credentials. ✗ rows
are blocking; `!` rows are optional. Add `--network` to also probe each
`base_url`.

No key yet? See the whole pipeline offline first:

```bash
tps run -c targets.mock.toml --mock      # synthetic numbers, flagged MOCK DATA
```

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

A progress bar with an ETA runs while it measures; then you get a table per
prompt, an under-load table if you configured more than one concurrency, and the
rankings (shown here on the offline demo):

![tps run output — demo data](assets/screenshot-run.svg)

Best values are highlighted; `≈` marks a rate whose token count had to be
estimated; `Δ` columns appear when the config names a `baseline`.

Artifacts land in `runs/<timestamp>/`:

| File | Contents |
|---|---|
| `report.md` | full tables, aggregate throughput, cost, failures |
| `summary.csv` | one row per prompt × provider × concurrency, each with its own stats |
| `raw.jsonl` | every request, written as it lands; re-analyse without spending tokens |
| `meta.json` | what was configured (model, pins, prices, mock flag, completion) |

Re-render anytime: `tps report runs/2026-10-27T12-45-39Z`. Interrupted with
Ctrl-C? The finished requests are kept and the report is flagged `PARTIAL`.

## 5. Sanity-check the weights

Prices that look impossible usually mean fewer bits. Before trusting a cheap row:

```bash
tps fingerprint -c targets.toml
```

Output groups providers by token-level agreement under greedy decoding and ends
in a verdict. `split` = different artifacts (quantization, revision, or another
model). Providers whose requests failed are listed as *no data*, never as
different. See [fingerprinting](fingerprinting.md).

## Reading the numbers

- **Compare `tok/s` at equal `ttft`.** A provider that returns one token instantly
  and then crawls is not fast.
- **Check `ok`.** 6/7 means one failure; at concurrency 16, `ok` falling off is
  rate limiting, not slowness — the under-load table shows it per level.
- **Check `out tok`.** A provider that stops early looks fast. The report prints
  the median; compare it before comparing speed.
- **Check your own noise first.** Two consecutive runs of the same config tell you
  more than any single p90.

## Next

- [Configuration](configuration.md) — concurrency sweeps, prompts, baselines
- [Methodology](methodology.md) — the nine ways a provider comparison lies
- [Automation](automation.md) — running this on a schedule or in CI
