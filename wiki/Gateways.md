# Gateways

A gateway is a row in the table, not plumbing. The same row hides three questions.

| Question | Row pair | What the delta measures |
|---|---|---|
| What does the hop cost? | same upstream, direct vs gateway | proxy overhead: TLS, extra RTT, rewriting, stream relay |
| What does the router's choice cost me? | unpinned vs your best pin | convenience, paid in latency and/or money |
| Is the gateway the bottleneck? | gateway A vs gateway B, same pins | the proxy itself |

Only the first is clean, and it needs the vendor reachable **both** ways. If a
provider exists only behind a gateway, "direct vs gateway" silently becomes hop +
different upstream + routing.

## Setup

```toml
[run]
baseline = "together-direct"

[[targets]]
name = "together-direct"
base_url = "https://api.together.xyz/v1"
model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
api_key_env = "TOGETHER_API_KEY"

[[targets]]
name = "together-via-or"
base_url = "https://openrouter.ai/api/v1"
model = "meta-llama/llama-3.3-70b-instruct"
api_key_env = "OPENROUTER_API_KEY"
extra_body.provider = { only = ["together"], allow_fallbacks = false }
```

```
Δ vs together-direct:
  together-via-or      +0.08s ttft   -6% tok/s
```

That is the hop. ~50ms means a routing layer; 500ms means the proxy is buffering
your stream, and you want to know that before building on it.

## Routing choice

```toml
[[targets]]
name = "or-unpinned"
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "OPENROUTER_API_KEY"
# no provider block — the router decides, weighted by price
```

Compare against your best pin. 20% slower unpinned means the router is optimising
for someone else.

## Gateway vs gateway

Any OpenAI-compatible proxy is a target:

```toml
[[targets]]
name = "litellm-proxy"
base_url = "http://localhost:4000/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "LITELLM_MASTER_KEY"
extra_body.provider = { only = ["together"], allow_fallbacks = false }
```

A local proxy has no WAN RTT, so treat its overhead as a *lower bound*.

## False positives

- **Different upstream** — check `served by` first.
- **Different quantization** — two rows on one gateway can be fp4 and fp8.
- **Fallbacks firing** — `allow_fallbacks = true` turns a slow primary into a
  different provider mid-run.
- **Regional routing** — `sail-research/fp4/us` ≠ `sail-research/fp4`.
- **Stream buffering** — some proxies buffer SSE: TTFT looks fine, ITL collapses
  into a few giant chunks. Watch `itl`, not just tok/s.

## Advice

Measure the hop once, in your region, record it. Prefer a gateway for breadth, a
direct endpoint for latency-critical paths — unless the hop is under ~20ms. And
always pin: an unpinned row is not reproducible next week.
