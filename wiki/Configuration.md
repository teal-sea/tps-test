# Configuration

TOML: one `[run]` table, any number of `[[targets]]`. `tps run` picks
`targets.toml`, then `targets.gateway.toml`.

## `[run]`

| Key | Default | Meaning |
|---|---|---|
| `reps` | `7` | repetitions per prompt × concurrency × target |
| `concurrency` | `[1]` | parallel-request levels |
| `warmup` | `2` | discarded requests per target |
| `temperature` | `0.0` | keep at 0 |
| `timeout_s` | `180` | per-request read timeout |
| `prompts` | first two built-ins | names from `[prompts]` or built-ins |
| `baseline` | none | target name for Δ columns |
| `max_tokens_field` | `max_tokens` | `max_completion_tokens` for GPT-5/o-series |
| `stream_options` | `true` | `include_usage`; disable if a provider 400s |

## `[[targets]]`

| Key | Required | Notes |
|---|---|---|
| `name` | yes | keep stable across runs; it's the report's row label |
| `base_url` | yes | OpenAI-compatible root; `/chat/completions` appended |
| `model` | yes | the provider's id, not your alias |
| `api_key_env` | no* | env var holding the key |
| `api_key` | no* | literal key — avoid |
| `headers` | no | extra request headers |
| `extra_body` | no | merged into the body; pinning lives here |
| `price` | no | `{ in_per_1m, out_per_1m }` |
| `max_tokens_field` | no | per-target override |
| `stream_options` | no | per-target override |
| `mock` | no | synthetic profile for `--mock` |

\* one of them, unless the endpoint is on localhost.

## Pinning

```toml
extra_body.provider = { only = ["deepinfra"], allow_fallbacks = false }
```

**Use the full tag slug for variants and tiers.** A base slug matches every region
of the standard endpoint but *never* matches a tier endpoint — pinning
`"deepinfra"` while labelling the row "flex" silently measures the standard one:

```toml
extra_body.provider = { only = ["deepinfra/fp8/flex"],     allow_fallbacks = false }
extra_body.provider = { only = ["baseten/fast"],           allow_fallbacks = false }
```

Other useful fields: `sort = "throughput" | "latency" | "price"`,
`quantizations = ["fp8"]`, `ignore`, `data_collection = "deny"`, `zdr = true`.

## Prompts

Built-ins: `prose-256`, `code-256`, `long-context` (~7k-token prefill, 128 out).

```toml
[prompts.summary]
text = "Summarise the document below in 150 words."
repeat = 40            # repeats the built-in ~170-token filler
max_tokens = 128

[prompts.mine]
text = "Explain TCP congestion control."
max_tokens = 256
# document = "inline"   |   document_file = "docs/big.txt"
```

## Prices and baselines

```toml
price = { in_per_1m = 0.14, out_per_1m = 0.42 }   # cost columns only

[run]
baseline = "together-direct"                       # adds Δ TTFT / Δ tok/s
```

Prices drift — regenerate with `tps discover --out`. Baseline is how you answer
"what did the gateway hop cost?"; see [[Gateways]].

## Validate without spending money

```bash
tps run -c my.toml --mock --reps 1
```

The report is stamped `MOCK DATA` and `meta.json` records `"mock": true`. Broken
`extra_body` shows up here, not after 400 requests.

## Ready-made configs

| File | Targets |
|---|---|
| `targets.mock.toml` | 3 fake, offline demo |
| `targets.v4.1-flash.toml` | 11 curated |
| `targets.v4.1-flash.full.toml` | all 41 |
| `targets.gateway.toml` | hop / routing comparison |
| `targets.example.toml` | direct vendors, pinned and unpinned |
