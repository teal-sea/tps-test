# Automation

One JSON object per command, stable exit codes, no prompts.

## Contract

```bash
tps run -c targets.toml --json --reps 5 > run.json
echo $?    # 0 ok · 1 usage · 2 credentials · 3 provider · 4 all failed
```

stdout carries only the JSON object in `--json` mode. Errors are JSON on stdout
there, stderr text otherwise. `schema_version` is 1. `meta.json` records model,
pins, prices, reps, concurrency and the `mock` flag.

## jq recipes

```bash
# ranked by throughput
tps run -c targets.toml --json | jq -r '.results | sort_by(-.out_tps_p50)
  | .[] | "\(.target)\t\(.out_tps_p50|round)\t\(.ttft_p50_s*1000|round)ms"'

# failures
tps run -c targets.toml --json | jq '.results[] | select(.errors != {}) | {target, errors}'

# cheapest five
tps discover deepseek/deepseek-v4.1-flash --json \
  | jq '.endpoints | sort_by(.price_out_per_1m) | .[:5]
        | .[] | {provider, quantization, price_out_per_1m}'

# fingerprint verdict
tps fingerprint -c targets.toml --json | jq -c '.clusters'
```

## CI gate

```yaml
- name: throughput
  env:
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  run: |
    tps run -c ci/targets.toml --json --reps 5 > run.json
    tps run -c ci/targets.toml --json --reps 5 > run2.json   # two snapshots, always
    jq -e '[.results[].out_tps_p50] | min > 20' run.json
```

Gate on a floor you know is safe, never a precise number: provider speed varies
more than your code does. Keep `ci/targets.toml` small (2–3 targets, 1 prompt,
`reps = 3`).

## Cron

```bash
0 */6 * * * cd /srv/tps && /usr/local/bin/tps run -c targets.toml --json \
  >> /var/log/tps/$(date +\%Y-\%m-\%d).jsonl
```

Keep the schedule fixed or you measure the sun along with the provider.

## Agents

Hand an agent [AGENTS.md](https://github.com/teal-sea/tps-test/blob/main/AGENTS.md),
not prose. The short version:

1. `tps doctor --json` → exit 2 means ask the human; never invent a key.
2. `tps discover <model> --json` → choose targets by policy.
3. `tps run --json` → read `results[]`, never scrape the table.
4. `tps fingerprint --json` → refuse to call a cheap row equivalent when
   `clusters` has more than one group.

Rules: no retries, no `--mock` numbers in a report, no conclusions under ~7 reps,
always state date and region.

## Cost

| Run | Requests | Time |
|---|---|---|
| 11 targets, 1 prompt, 7 reps, c=1,4 | 385 | ~20 min |
| 41 targets, 2 prompts, 7 reps, c=1 | 574 | ~45 min |
| fingerprint, 11 targets, 2 prompts | 44 | ~1 min |

Start with `--reps 3 --concurrency 1` to catch config errors, then scale.
