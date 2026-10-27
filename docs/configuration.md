# Configuration

A config is TOML with one `[run]` table and any number of `[[targets]]` tables.
`tps run` picks `targets.toml` then `targets.gateway.toml` if `-c` is omitted.

## `[run]`

| Key | Default | Meaning |
|---|---|---|
| `reps` | `7` | repetitions per prompt × concurrency × target |
| `concurrency` | `[1]` | list of parallel-request levels |
| `warmup` | `2` | discarded requests per target (TLS, cold start) |
| `temperature` | `0.0` | keep at 0 for comparability |
| `timeout_s` | `180` | per-request read timeout |
| `prompts` | first two built-ins | names defined under `[prompts]` or built-in |
| `baseline` | none | target name to compute Δ columns against |
| `max_tokens_field` | `max_tokens` | use `max_completion_tokens` for GPT-5/o-series |
| `stream_options` | `true` | send `include_usage`; disable if a provider 400s |

## `[[targets]]`

| Key | Required | Notes |
|---|---|---|
| `name` | yes | shows up in reports; keep it stable across runs |
| `base_url` | yes | OpenAI-compatible root; `/chat/completions` is appended |
| `model` | yes | provider's model id, not your alias |
| `api_key_env` | no* | env var holding the key |
| `api_key` | no* | literal key (avoid; use `api_key_env`) |
| `headers` | no | extra request headers |
| `extra_body` | no | merged into the JSON body — this is where pinning lives |
| `price` | no | `{ in_per_1m, out_per_1m }` for cost columns |
| `max_tokens_field` | no | per-target override |
| `stream_options` | no | per-target override |
| `mock` | no | synthetic profile for `--mock` runs |

\* One of them, unless the endpoint is on localhost.

## Pinning an upstream

A gateway target is meaningless unless you pin it, otherwise you measure the
router's choice:

```toml
extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }
```

**Use the full tag slug for variants and tiers.** OpenRouter's routing rules say a
base slug matches every region of the standard endpoint but *never* matches a tier
endpoint:

```toml
# standard endpoint
extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }
# flex tier — pinning "deepinfra" here would silently measure the standard one
extra_body.provider = { only = ["deepinfra/fp8/flex"], allow_fallbacks = false }
# priority tier
extra_body.provider = { only = ["baseten/fast"], allow_fallbacks = false }
```

Other routing fields worth knowing: `sort = "throughput" | "latency" | "price"`,
`quantizations = ["fp8"]`, `ignore = [...]`, `data_collection = "deny"`,
`zdr = true`.

## Prompts

Built-ins: `prose-256` (long prose), `code-256` (code generation),
`long-context` (~7k-token prefill, 128-token output).

```toml
[prompts.summary]
text = "Summarise the document below in 150 words."
repeat = 40            # repeats the built-in ~170-token filler document
max_tokens = 128

[prompts.mine]
text = "Explain TCP congestion control."
max_tokens = 256
# document = "inline text"          # or
# document_file = "docs/big.txt"    # or
```

`repeat` is how you build a prefill-heavy prompt without shipping a fixture file.

## Prices

`price` is only used for the cost columns; it never affects measurement.

```toml
price = { in_per_1m = 0.14, out_per_1m = 0.42 }
```

`tps discover --out` fills these in from the provider list. They drift — re-run
`discover` when they matter.

## Baseline deltas

Name a target and the report grows Δ TTFT / Δ tok/s columns:

```toml
[run]
baseline = "together-direct"
```

Use it for the gateway-hop question: same upstream direct vs through the gateway.

## Ready-made configs in this repo

| File | Targets | Use |
|---|---|---|
| `targets.mock.toml` | 3 fake | offline demo, no keys |
| `targets.v4.1-flash.toml` | 11 curated | DeepSeek-V4.1-Flash, ~20 min |
| `targets.v4.1-flash.full.toml` | 41 | everything OpenRouter lists |
| `targets.gateway.toml` | 3 + commented | hop overhead / routing choice |
| `targets.example.toml` | mixed | direct vendors, pinned and unpinned gateways |

## Validating a config without spending money

```bash
tps run -c my.toml --mock --reps 1
```

The report is flagged `MOCK DATA`, and `meta.json` records `"mock": true`. If a
target's `extra_body` is malformed you'll see it here, not after 400 requests.
