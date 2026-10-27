# CLI reference

```
tps [--json] [--no-color] [-q] [-v] <command> [options]
```

Global flags may be given before or after the subcommand.

| Flag | Effect |
|---|---|
| `--json` | print exactly one JSON object on stdout; suppress human output |
| `--no-color` | disable ANSI colour (also honours `NO_COLOR`, `TERM=dumb`, non-TTY) |
| `-q`, `--quiet` | suppress everything except errors |
| `-v`, `--verbose` | full traceback on unexpected errors |
| `--version` | print version |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage or configuration error (bad flag, missing config, bad run directory) |
| 2 | missing credentials |
| 3 | provider/API failure (discovery failed, upstream unreachable) |
| 4 | the run completed but **every** request failed |
| 130 | interrupted (Ctrl-C) |

Partial failures do **not** change the exit code: a run where 3 of 70 requests
failed exits 0 and reports `"failed": 3`. Treat `results[].ok/n` as the signal.

---

## `tps doctor`

```bash
tps doctor [-c FILE] [--network]
```

Checks Python ≥ 3.11, `httpx`, `h2`, config parse, and whether every target's
`api_key_env` is set. `--network` additionally probes each `base_url` (5s
timeout) — any HTTP status counts as reachable.

Exit 0 when all critical checks pass, otherwise 2.

## `tps init`

```bash
tps init [--model SLUG] [--out targets.toml] [--force]
```

Without `--model`: writes a commented starter config.
With `--model`: behaves like `discover --out`, generating one pinned target per
endpoint (needs network; uses `OPENROUTER_API_KEY` if set).

Refuses to overwrite without `--force`.

## `tps discover`

```bash
tps discover <author>/<slug> [--out FILE] [--api-key KEY]
             [--from-json FILE] [--json-out FILE]
```

Lists every endpoint serving the model with price, quantization, and context.

| Flag | Use |
|---|---|
| `--out FILE` | write a runnable config, one pinned target per endpoint tag |
| `--json-out FILE` | save the raw API payload for debugging |
| `--from-json FILE` | parse a recorded payload offline (used by tests) |
| `--api-key` | defaults to `OPENROUTER_API_KEY` |

## `tps run`

```bash
tps run [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4,16]
        [--prompts name,other] [--mock] [--no-warmup]
```

| Flag | Default | Notes |
|---|---|---|
| `-c` | `targets.toml` → `targets.gateway.toml` | first one that exists |
| `-o` | `runs/<UTC stamp>` | created if missing |
| `--reps` | from config (7) | reps per prompt × concurrency × target |
| `--concurrency` | from config (`[1]`) | comma list; each level adds a batch per rep |
| `--prompts` | from config | must exist in the config or the built-ins |
| `--mock` | off | synthesises samples; no network; flagged in outputs |
| `--no-warmup` | off | skips discarded warm-up requests |

Cost model: requests = targets × prompts × reps × Σ(concurrency). A curated
11-target run at 1 prompt, 7 reps, concurrency 1+4 is 385 requests.

## `tps report`

```bash
tps report <run_dir>
```

Re-reads `raw.jsonl` + `meta.json` and rewrites `report.md`/`summary.csv`. Printing
the markdown does not change the exit code; a missing `raw.jsonl` exits 1.

Use this to analyse a run with different grouping after the fact, or to recover a
report you deleted.

## `tps fingerprint`

```bash
tps fingerprint [-c FILE] [-o DIR] [--reps 2] [--prompt-count 2] [--max-tokens 160] [--mock]
```

Sends the same prompt N times per target at temperature 0, non-streaming, with
`logprobs`, and compares token sequences. Writes `fingerprints.jsonl` +
`fingerprint.md`. See [fingerprinting](fingerprinting.md).

---

## JSON output

Every command prints one object:

```json
{"schema_version": 1, "command": "run", "ok": true, "...": "..."}
```

Errors go to **stdout** in JSON mode (so a pipe sees them) and to stderr in human
mode:

```json
{"schema_version": 1, "command": "run", "ok": false,
 "error": {"code": 2, "message": "missing credentials for: deepinfra — set OPENROUTER_API_KEY"}}
```

### `run`

```json
{
  "schema_version": 1, "command": "run", "ok": true,
  "run_dir": "runs/2026-10-27T12-00-00Z",
  "config": "targets.toml",
  "model": "deepseek/deepseek-v4.1-flash",
  "reps": 7, "concurrency": [1, 4],
  "results": [{
    "prompt": "prose-256", "target": "deepinfra", "model": "...",
    "base_url": "https://openrouter.ai/api/v1", "pin": {"only": ["deepinfra/fp8"]},
    "ttft_p50_s": 0.42, "ttft_p90_s": 0.55,
    "out_tps_p50": 78.3, "out_tps_p90": 70.1,
    "itl_p50_ms": 12.0, "e2e_p50_s": 3.2, "out_tokens_median": 240,
    "ok": 7, "n": 7, "errors": {},
    "aggregate_tps": {"1": 78.1, "4": 240.5},
    "price_in_per_1m": 0.14, "price_out_per_1m": 0.42
  }],
  "failed": 0, "total": 77,
  "artifacts": {"report": "...", "csv": "...", "raw": "..."}
}
```

### `discover`

```json
{"schema_version":1,"command":"discover","ok":true,
 "model":"deepseek/deepseek-v4.1-flash",
 "endpoints":[{"provider":"DeepInfra","slug":"deepinfra","tag":"deepinfra/fp8",
   "quantization":"fp8","service_tier":null,"context_length":1048576,
   "price_in_per_1m":0.14,"price_out_per_1m":0.42,
   "reported_p50_throughput":null,"reported_p50_latency_s":null}],
 "config":"targets.toml","targets":41}
```

### `fingerprint`

```json
{"schema_version":1,"command":"fingerprint","ok":true,
 "clusters":[["deepinfra","together"],["decart"]],
 "self_agreement":{"deepinfra":1.0,"together":0.99},
 "pairs":[{"a":"deepinfra","b":"together","agreement":0.997,"first_divergence":142}],
 "prompt_tokens":{"deepinfra":171,"together":171},
 "prompt_used":"prose-256"}
```

### `doctor`

```json
{"schema_version":1,"command":"doctor","ok":false,
 "checks":[{"check":"credentials","ok":false,"detail":"missing for 1 target(s): deepinfra","critical":true}]}
```

## Environment variables

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | `discover`, `init --model`, any target with that `api_key_env` |
| `NO_COLOR` | disables colour everywhere |
| any `api_key_env` in your config | that target's `Authorization` header |
