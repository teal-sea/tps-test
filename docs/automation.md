# Automation

`tps` is built to be driven by scripts, cron and agents: one JSON object per
command, stable exit codes, no interactive prompts.

## The contract

```bash
tps run -c targets.toml --json --reps 5 > run.json
echo $?          # 0 ok · 1 usage · 2 credentials · 3 provider · 4 all failed
```

- stdout carries **only** the JSON object in `--json` mode.
- errors are `<stdout JSON>` in `--json` mode, stderr text otherwise.
- `schema_version` is 1 and only changes on breaking shapes.
- `meta.json` in the run directory records everything needed to reproduce:
  model, pins, prices, reps, concurrency, `mock` flag.

## jq recipes

```bash
# ranked by throughput
tps run -c targets.toml --json | jq -r '.results | sort_by(-.out_tps_p50)
  | .[] | "\(.target)\t\(.out_tps_p50|round)\t\(.ttft_p50_s*1000|round)ms"'

# anything that failed
tps run -c targets.toml --json | jq '.results[] | select(.errors != {}) | {target, errors}'

# cheapest five providers of a model
tps discover deepseek/deepseek-v4.1-flash --json \
  | jq '.endpoints | sort_by(.price_out_per_1m) | .[:5]
        | .[] | {provider, quantization, price_out_per_1m}'

# did the fingerprint split the field?
tps fingerprint -c targets.toml --json | jq -c '.clusters'
```

## CI: a performance regression gate

```yaml
- name: throughput
  env:
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  run: |
    tps run -c ci/targets.toml --json --reps 5 > run.json
    tps run -c ci/targets.toml --json --reps 5 > run2.json   # two snapshots, always
    jq -e '[.results[].out_tps_p50] | min > 20' run.json
```

Two runs, not one: provider speed moves severalfold with load. Gate on a floor you
know is safe, never on a precise number — otherwise the pipeline fails for
reasons unrelated to your code.

Keep a small `ci/targets.toml` (2–3 targets, 1 prompt, `reps = 3`); the full
config is for humans.

## Cron: tracking a provider over time

```bash
0 */6 * * * cd /srv/tps && /usr/local/bin/tps run -c targets.toml --json \
  >> /var/log/tps/$(date +\%Y-\%m-\%d).jsonl
```

Then diff `summary.csv` between runs, or load `raw.jsonl` into anything that reads
JSON lines. Time of day is a variable — keep the schedule fixed or you are
measuring the sun.

## Agent usage

Give an agent [AGENTS.md](https://github.com/teal-sea/tps-test/blob/main/AGENTS.md) rather than the prose docs. The short
version:

1. `tps doctor --json` → if exit 2, ask the human for the missing env var; never
   invent a key.
2. `tps discover <model> --json` → choose targets by policy (price, quant).
3. `tps run --json` → read `results[]`, never scrape the table.
4. `tps fingerprint --json` → refuse to present a cheap row as equivalent when
   `clusters` has more than one group.

Rules an agent must respect: no retries, no `--mock` numbers in a report, no
conclusions below ~7 reps, and always include the date and your region.

## Cost control

Requests = `targets × prompts × reps × Σ(concurrency)`.

| Run | Requests | Time | Tokens |
|---|---|---|---|
| 11 targets, 1 prompt, 7 reps, c=1,4 | 385 | ~20 min | ~120k |
| 41 targets, 2 prompts, 7 reps, c=1 | 574 | ~45 min | ~180k |
| fingerprint, 11 targets, 2 prompts | 44 | ~1 min | ~8k |

Start with `--reps 3 --concurrency 1` to catch config errors, then scale up.

## Exit-code handling in shell

```bash
set +e
tps run -c targets.toml --json > out.json
code=$?
case $code in
  0) echo "measured" ;;
  2) echo "no credentials; skipping" ; exit 0 ;;
  3|4) echo "provider problem; retry later" >&2 ; exit 1 ;;
  *) echo "usage error" >&2 ; exit 1 ;;
esac
```
