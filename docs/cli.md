# CLI reference

```
tps [--json] [--no-color] [-q] [-v] <command> [options]
```

Global flags may be given before or after the subcommand.

| Flag | Effect |
|---|---|
| `--json` | print exactly one JSON object on stdout; suppress human output |
| `--no-color` | disable ANSI colour (also honours `NO_COLOR`, `TERM=dumb`, non-TTY) |
| `-q`, `--quiet` | suppress everything except errors (progress included) |
| `-v`, `--verbose` | full traceback on unexpected errors |
| `--version` | print version |

`FORCE_COLOR=1` forces colour when stdout is not a TTY (`NO_COLOR` still wins).
Progress is drawn on stderr, only when stderr is a terminal.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage or configuration error — bad flag, bad config, unknown prompt, model not found, bad run directory |
| 2 | missing credentials |
| 3 | provider/API failure (OpenRouter unreachable or erroring during discovery) |
| 4 | the command ran but **every** request failed |
| 130 | interrupted (Ctrl-C) — whatever finished is kept on disk |
| 141 | stdout closed early (e.g. piped into `head`) |

Bad flags exit **1**, not argparse's usual 2, so they can never be mistaken for
"missing credentials"; with `--json` they still produce an error object.

Partial failures do **not** change the exit code: a run where 3 of 70 requests
failed exits 0 and reports `"failed": 3`. Treat `results[].ok/n` as the signal.

---

## `tps doctor`

```bash
tps doctor [-c FILE] [--network]
```

Checks Python ≥ 3.11, `httpx`, `h2`, config parse, and whether every target that
needs a key has one (targets on `localhost`/`127.0.0.1`/`::1` don't; a mock-only
config doesn't). `--network` additionally probes each `base_url` (5s timeout) —
any HTTP status counts as reachable, any connection error or timeout does not.

Exit 0 when every critical check passes, 2 when only credentials are missing,
1 for anything else (no config, invalid config, Python too old).

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

Lists every endpoint serving the model with price, quantization, and context,
sorted by output price.

| Flag | Use |
|---|---|
| `--out FILE` | write a runnable config, one pinned target per endpoint tag |
| `--json-out FILE` | save the raw API payload for debugging |
| `--from-json FILE` | parse a recorded payload offline (used by tests) |
| `--api-key` | defaults to `OPENROUTER_API_KEY` |

An unknown model exits 1; OpenRouter being unreachable exits 3.

## `tps run`

```bash
tps run [-c FILE] [-o DIR] [--reps N] [--concurrency 1,4,16]
        [--prompts name,other] [--mock] [--no-warmup]
```

| Flag | Default | Notes |
|---|---|---|
| `-c` | `targets.toml` → `targets.gateway.toml` | first one that exists |
| `-o` | `runs/<UTC stamp>` | created if missing |
| `--reps` | from config (7) | reps per prompt × concurrency × target, ≥ 1 |
| `--concurrency` | from config (`[1]`) | comma list of integers ≥ 1; each level adds a batch per rep |
| `--prompts` | from config | any built-in or `[prompts.*]` name |
| `--mock` | off | synthesises samples; no network; flagged in every output |
| `--no-warmup` | off | skips discarded warm-up requests |

Cost model: requests = targets × prompts × reps × Σ(concurrency), plus
`warmup` per target. A curated 11-target run at 1 prompt, 7 reps, concurrency
1+4 is 385 measured requests.

`raw.jsonl` is written as batches finish and `meta.json` up front (with
`"complete": false` until the end). **Ctrl-C keeps everything measured so far**,
writes a report flagged `PARTIAL`, and exits 130.

## `tps report`

```bash
tps report <run_dir> [--markdown]
```

Re-reads `raw.jsonl` + `meta.json`, rewrites `report.md`/`summary.csv`, and prints
the same tables `run` prints. `--markdown` prints `report.md` instead. A missing
`raw.jsonl` exits 1.

Use it to analyse a run again after changing `tps`, to recover a deleted report,
or to render an interrupted run.

## `tps fingerprint`

```bash
tps fingerprint [-c FILE] [-o DIR] [--reps 2] [--prompt-count 2] [--max-tokens 160] [--mock]
```

Sends the same prompt `--reps` times per target at temperature 0, non-streaming,
with `logprobs`, and compares token sequences. Writes `fingerprints.jsonl` +
`fingerprint.md`. Exits 4 if no request succeeded. See
[fingerprinting](fingerprinting.md).

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
 "error": {"code": 2, "message": "missing credentials for: deepinfra — set OPENROUTER_API_KEY (or run with --mock)"}}
```

`schema_version` only changes on breaking changes; new keys may be added.

### `run`

```json
{
  "schema_version": 1, "command": "run", "ok": true, "mock": false,
  "run_dir": "runs/2026-10-27T12-00-00Z",
  "config": "targets.toml",
  "model": "deepseek/deepseek-v4.1-flash",
  "reps": 7, "concurrency": [1, 4],
  "results": [{
    "prompt": "prose-256", "target": "deepinfra", "model": "...",
    "base_url": "https://openrouter.ai/api/v1", "pin": {"only": ["deepinfra/fp8"]},
    "concurrency": 1,
    "ttft_p50_s": 0.42, "ttft_p90_s": 0.55, "first_content_p50_s": 0.42,
    "out_tps_p50": 78.3, "out_tps_p90": 70.1,
    "itl_p50_ms": 12.0, "e2e_p50_s": 3.2, "out_tokens_median": 240,
    "ok": 34, "n": 35, "errors": {"rate_limited": 1},
    "aggregate_tps": {"1": 78.1, "4": 240.5},
    "by_concurrency": {
      "1": {"ttft_p50_s": 0.42, "out_tps_p50": 78.3, "aggregate_tps": 78.1, "ok": 7, "n": 7, "errors": {}},
      "4": {"ttft_p50_s": 0.51, "out_tps_p50": 61.0, "aggregate_tps": 240.5, "ok": 27, "n": 28, "errors": {"rate_limited": 1}}
    },
    "count_source": ["usage"], "served_by": ["DeepInfra"],
    "price_in_per_1m": 0.14, "price_out_per_1m": 0.42
  }],
  "failed": 1, "total": 385,
  "artifacts": {"report": "...", "csv": "...", "raw": "..."}
}
```

Latency and rate fields describe the lowest concurrency (`concurrency`);
`ok`/`n`/`errors` count every level; `by_concurrency` breaks both down.
`out_tps_p90` is the slow tail (see [metrics](metrics.md#percentiles)).

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
{"schema_version":1,"command":"fingerprint","ok":true,"mock":false,
 "verdict":"split",
 "clusters":[["deepinfra","together"],["decart"]],
 "no_data":{"nebius":"http_401: ..."},
 "noisy":[],
 "noise_floor_measured":true,
 "self_agreement":{"deepinfra":1.0,"together":0.99,"decart":1.0},
 "self_first_divergence":{"deepinfra":null,"together":142,"decart":null},
 "pairs":[{"a":"deepinfra","b":"together","agreement":0.997,"first_divergence":142}],
 "prompt_tokens":{"deepinfra":171,"together":171,"decart":171,"nebius":null},
 "prompt_used":"prose-256"}
```

`verdict` is `split` (two or more distinguishable groups among deterministic
targets), `consistent`, or `inconclusive` (fewer than two deterministic targets
returned data). Targets that errored are in `no_data` and never in `clusters`.

### `doctor`

```json
{"schema_version":1,"command":"doctor","ok":false,"exit_code":2,
 "checks":[{"check":"credentials","ok":false,"detail":"missing for 1 target(s): deepinfra — set OPENROUTER_API_KEY","critical":true}]}
```

## Environment variables

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | `discover`, `init --model`, any target with that `api_key_env` |
| `NO_COLOR` | disables colour everywhere |
| `FORCE_COLOR` | forces colour when not on a TTY |
| `HTTPS_PROXY` | honoured by httpx for all requests |
| any `api_key_env` in your config | that target's `Authorization` header |
