# FAQ

**Is this a benchmark of model quality?**
No. Speed and cost. Quality needs your own eval.

**Why so many reps?**
Provider throughput swings 2–3× with load. Fewer than ~7 samples and you are
measuring the moment, not the provider.

**Do I need OpenRouter?**
No. Any OpenAI-compatible endpoint works — set `base_url` and `api_key_env`.
OpenRouter is convenient because one key reaches dozens of upstreams and exposes
pinning plus the provider list.

**Why is there no retry?**
Retries hide exactly the tail behaviour you are trying to measure. A 429 at
concurrency 16 is a result.

**Can I compare two different models?**
You can run it, but the numbers stop being a provider comparison — model
differences dominate. Keep `model` identical across targets in one run.

**Why does TTFT differ from provider dashboards?**
Theirs is measured from their side, on their prompt distribution, often at a
different concurrency, and reported over a rolling window. Yours includes your
network.

**Does the gateway add latency?**
Sometimes nothing, sometimes hundreds of ms. Measure it: `docs/gateways.md`. Watch
ITL too — a buffering proxy can keep TTFT fine while destroying smoothness.

**Can I use it for embeddings / images / audio?**
No — chat-completions SSE only.

**Does it store my prompts?**
`runs/` contains the prompts you configure and the completions received. It is
gitignored on purpose. Nothing is sent anywhere except the providers you configure.

**Can I run it against a local server (vLLM, llama.cpp, Ollama)?**
Yes. `base_url = "http://localhost:8000/v1"`, no key required for localhost. It's a
good sanity check before spending money on a paid provider.

**What's the smallest useful run?**
3 targets × 1 prompt × 5 reps at c=1: 15 requests, under a minute, a cent.

**How do I compare a gateway against direct?**
Set `baseline` to the direct target. It only works cleanly when the same upstream
is reachable both ways — see `docs/gateways.md`.

**Where did `rig.py` go?**
It still works as a deprecated shim. The supported entry point is `tps` (or
`python -m tps`).
