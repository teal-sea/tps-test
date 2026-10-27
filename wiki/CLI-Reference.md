# CLI Reference

```
tps [--json] [--no-color] [-q] [-v] <command> [options]
```

Global flags work before or after the subcommand.

| Flag | Effect |
|---|---|
| `--json` | one JSON object on stdout; human output suppressed |
| `--no-color` | no ANSI (also honours `NO_COLOR`, `TERM=dumb`, non-TTY) |
| `-q`, `--quiet` | errors only |
| `-v`, `--verbose` | traceback on unexpected errors |
| `--version` | print version |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage / configuration error |
| 2 | missing credentials |
| 3 | provider or API failure |
| 4 | run completed but every request failed |
| 130 | interrupted |

Partial failure does **not** change the exit code. A run where 3 of 70 requests
failed exits 0 and reports `"failed": 3`. Read `results[].ok/n`.

## Commands

### `tps doctor`

```bash
tps doctor [-c FILE] [--network]
```

Python, `httpx`, `h2`, config parse, credentials for every target. `--network`
probes base URLs (5s, any HTTP status counts as reachable). Exit 2 when a critical
check fails.

### `tps init`

```bash
tps init [--model SLUG] [--out targets.toml] [--force]
```

Starter config, or a full pinned config when `--model` is given (needs network).
Refuses to overwrite without `--force`.

### `tps discover`

```bash
tps discover <author>/<slug> [--out FILE] [--api-key KEY]
             [--from-json FILE] [--json-out FILE]
```

Endpoint list with price, quantization, context. `--out` writes a runnable config.
`--from-json` parses a recorded payload offline.

### `tps run`

```bash
tps run [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4,16]
        [--prompts name,other] [--mock] [--no-warmup]
```

| Flag | Default |
|---|---|
| `-c` | `targets.toml`, then `targets.gateway.toml` |
| `-o` | `runs/<UTC stamp>` |
| `--reps` | from config (7) |
| `--concurrency` | from config (`[1]`) |
| `--prompts` | from config |
| `--mock` | off — synthetic samples, no network, flagged everywhere |

Requests = targets × prompts × reps × Σ(concurrency).

### `tps report`

```bash
tps report <run_dir>
```

Rebuilds `report.md`/`summary.csv` from `raw.jsonl`. Exits 1 if the directory has
no `raw.jsonl`.

### `tps fingerprint`

```bash
tps fingerprint [-c FILE] [-o DIR] [--reps 2] [--prompt-count 2]
                [--max-tokens 160] [--mock]
```

Greedy-decode agreement test. See [[Fingerprinting]].

## JSON contract

Every command prints one object:

```json
{"schema_version": 1, "command": "run", "ok": true, "...": "..."}
```

Errors print to **stdout** in JSON mode (so pipes see them) and exit non-zero:

```json
{"schema_version": 1, "command": "run", "ok": false,
 "error": {"code": 2, "message": "missing credentials for: deepinfra — set OPENROUTER_API_KEY"}}
```

`run` adds `results[]` with `ttft_p50_s`, `ttft_p90_s`, `out_tps_p50`,
`out_tps_p90`, `itl_p50_ms`, `e2e_p50_s`, `out_tokens_median`, `ok`, `n`,
`errors{}`, `aggregate_tps{}`, `price_*_per_1m`; plus `run_dir`, `failed`, `total`
and `artifacts{}`.

`discover` adds `endpoints[]`; `fingerprint` adds `clusters[][]`,
`self_agreement{}`, `pairs[]`, `prompt_tokens{}`; `doctor` adds `checks[]`.

Full reference: [docs/cli.md](https://github.com/teal-sea/tps-test/blob/main/docs/cli.md).

## Environment

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | `discover`, `init --model`, matching targets |
| `NO_COLOR` | disables colour |
| any `api_key_env` from a config | that target's auth header |
