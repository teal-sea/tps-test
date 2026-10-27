# Gateways

A gateway — OpenRouter, Portkey, LiteLLM, your own nginx — is not plumbing here.
It is a row in the table, and the same row can hide three different questions.

## The three questions

| Question | Row pair | What the delta actually measures |
|---|---|---|
| What does the hop cost? | same upstream, direct vs through the gateway | proxy overhead: TLS, an extra RTT, request rewriting, streaming relay |
| What does the router's choice cost me? | unpinned vs your best pin | convenience, paid for in latency and/or money |
| Is the gateway the bottleneck? | gateway A vs gateway B, same pins | the proxy itself, independent of any provider |

Only the first is a clean measurement, and it requires the vendor to be reachable
**both** ways. If a provider exists only behind a gateway, the "direct vs gateway"
delta silently becomes hop + different upstream + different routing, and no
careful reading will separate them.

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

`tps run -c targets.gateway.toml` then prints:

```
Δ vs together-direct:
  together-via-or      +0.08s ttft   -6% tok/s
```

That number is the hop. If it is ~0.05s, the gateway is a routing layer. If it is
0.5s, the gateway is buffering your stream and you should know that before
building on it.

## The second question: routing choice

```toml
[[targets]]
name = "or-unpinned"
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "OPENROUTER_API_KEY"
# no provider block: the router decides, weighted by price
```

Compare it against your best pin. If unpinned is 20% slower, the router is
optimising for someone else — usually price, or uptime.

## The third question: gateway vs gateway

Any OpenAI-compatible proxy is a target. A local LiteLLM in front of a vendor:

```toml
[[targets]]
name = "litellm-proxy"
base_url = "http://localhost:4000/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "LITELLM_MASTER_KEY"
extra_body.provider = { only = ["together"], allow_fallbacks = false }
```

The rig does not care that it is on localhost; note in your writeup that the hop
is local (no WAN RTT), which makes it a *lower bound* on real gateway overhead.

## Things that look like gateway overhead but are not

- **Different upstream.** Check the `served by` column first.
- **Different quantization.** Two rows on the same gateway can be fp4 and fp8.
- **Fallbacks firing.** If `allow_fallbacks` is true, a slow primary silently
  becomes a different provider mid-run.
- **Regional routing.** `sail-research/fp4/us` and `sail-research/fp4` are
  different endpoints; a base pin lets the router pick either.
- **Stream buffering.** Some proxies buffer SSE; TTFT looks fine but ITL collapses
  into a few giant chunks. Watch `itl` and `chunks`, not just tok/s.

## Practical advice

- Measure the hop once, in your region, and record it. It is stable enough to keep
  quoting for a quarter.
- Prefer a gateway for **breadth** (many models, one key) and direct endpoints for
  **latency-critical** paths, unless the hop measures under ~20ms.
- Always pin, even on a gateway you trust: an unpinned row is not a measurement of
  anything you can reproduce next week.
