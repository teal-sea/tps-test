# AGENTS.md

Instructions for coding agents using this repository. Human docs are in
[`docs/`](docs/index.md); this file is the dense, contract-oriented version.

## What this is

`tps` benchmarks **one model across many providers/gateways**. It is a CLI, not a
library API — drive it as a subprocess and parse `--json`.

## Invariants you must not break

1. **Never add automatic retries** for 429/5xx. Failures are data. (The one
   resend is a compatibility fallback: a 400/422 that names `stream_options`
   is resent once without it, and that target stops sending it.)
2. **Never batch targets sequentially by provider.** The run loop rotates target
   order per rep; that interleaving is the point.
3. **`--json` must print exactly one JSON object on stdout.** Human text goes to
   stderr or is suppressed. Exit codes must match the table below.
4. **Token counts come from `usage`**, with a chars/4 fallback that must stay
   flagged in the output (`count_source`). Don't silently estimate.
5. Provider pins use the **full tag slug** (`baseten/fast`). A base slug never
   matches a tier endpoint — see `docs/configuration.md`.
6. Tests must not require network or API keys. `tests/selftest.py` runs against a
   local SSE server and a recorded fixture.
7. **Mock data never leaks.** `mock = {…}` profiles apply only under `--mock`;
   every mock output is flagged (`"mock": true`, `MOCK DATA`). README images come
   from `scripts/render_assets.py` with fictional providers only.
8. **A failed request is not evidence.** Fingerprint targets with no data are
   reported in `no_data`, never clustered as "different weights"; targets
   without a measured, deterministic noise floor never decide the verdict.

## Commands

```bash
tps doctor      [-c FILE] [--network]              # environment + credentials
tps init        [--model SLUG] [--out FILE] [--force]
tps discover    MODEL [--out FILE] [--from-json F] [--json-out F]
tps run         [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4] [--prompts a,b] [--mock]
tps report      RUN_DIR [--markdown]
tps fingerprint [-c FILE] [--reps N] [--prompt-count N] [--mock]
```

Global: `--json`, `--no-color`, `-q`, `-v`, `--version`. May appear before or
after the subcommand. Config defaults to `targets.toml`, then
`targets.gateway.toml`.

## Exit codes

| Code | Meaning | Agent action |
|---|---|---|
| 0 | success | continue |
| 1 | usage/config error | fix the invocation; do not retry |
| 2 | missing credentials | ask the human for the env var |
| 3 | provider/API failure | safe to retry later, not immediately |
| 4 | every request failed | check network/model id before retrying |
| 130 | interrupted | partial results are in `run_dir`; `tps report` renders them |
| 141 | stdout closed early | — |

Bad flags exit **1** (not argparse's 2) and still emit a JSON error object.

## JSON contract (schema_version 1)

Every command emits:

```json
{ "schema_version": 1, "command": "run", "ok": true, "...": "command-specific" }
```

Errors emit `{"schema_version":1,"command":"...","ok":false,"error":{"code":2,"message":"..."}}`
on **stdout** and exit non-zero.

`run` adds:

```json
{
  "run_dir": "runs/2026-10-27T12-00-00Z",
  "results": [{
    "target": "deepinfra", "prompt": "prose-256", "model": "deepseek/deepseek-v4.1-flash",
    "concurrency": 1,
    "ttft_p50_s": 0.42, "ttft_p90_s": 0.55, "first_content_p50_s": 0.42,
    "out_tps_p50": 78.3, "out_tps_p90": 70.1,
    "itl_p50_ms": 12.0, "e2e_p50_s": 3.2, "out_tokens_median": 240,
    "ok": 35, "n": 35, "errors": {},
    "aggregate_tps": {"1": 78.1, "4": 240.5},
    "by_concurrency": {"1": {"ok": 7, "n": 7, "...": "..."}, "4": {"ok": 28, "n": 28, "...": "..."}},
    "count_source": ["usage"], "served_by": ["DeepInfra"],
    "price_in_per_1m": 0.14, "price_out_per_1m": 0.42
  }],
  "failed": 0, "total": 385, "mock": false,
  "artifacts": {"report": "...", "csv": "...", "raw": "..."}
}
```

`discover` adds `model`, `endpoints[]`, `config`, `targets`.
`fingerprint` adds `verdict` (`split`|`consistent`|`inconclusive`), `clusters[][]`,
`no_data{target: error}`, `noisy[]`, `unmeasured[]`, `distinguishable_groups`,
`noise_floor_measured`, `self_agreement{}`,
`pairs[]`, `prompt_tokens{}`.
`doctor` adds `checks[]` with `{check, ok, detail, critical}`.

Treat `results[].ok/n` as the reliability signal — they count every concurrency
level; `by_concurrency` shows where failures happened. `errors` is a map of
class → count. Latency/rate fields describe the lowest concurrency. **p90 is
always the slow tail**: `out_tps_p90 ≤ out_tps_p50`.

## Typical agent sequences

**"Is provider X actually better?"**
```bash
tps discover <model> --out /tmp/t.toml --json | jq '.endpoints | sort_by(.price_out_per_1m)[:5]'
tps run -c /tmp/t.toml --reps 7 --json | jq '.results | sort_by(-.out_tps_p50) | .[:3] | .[] | {target, out_tps_p50, ttft_p50_s}'
```

**"Is the cheap one the same model?"**
```bash
tps fingerprint -c /tmp/t.toml --json | jq '.verdict, .clusters, .no_data'
```

## Cost and time

`run` with 11 targets, 1 prompt, 7 reps, concurrency 1 and 4 ≈ 385 requests ≈
20 min ≈ $0.05 of tokens. `fingerprint` ≈ 44 requests ≈ 1 min. Always estimate
before running a 41-target config.

## Failure modes you will hit

- `missing credentials` (2) — the env var in `api_key_env` isn't set.
- `HTTP 404` on a pin — the provider slug is wrong or the model moved; re-run `discover`.
- Everything timing out — check egress/proxy; `tps doctor --network`.
- Mock data — `"mock": true` in `meta.json`; never report it as a measurement.
- `only defines mock targets` (1) — the demo configs need `--mock`.
- `duplicate target name(s)` (1) — names key every statistic; make them unique.
