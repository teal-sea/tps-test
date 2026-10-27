# Troubleshooting

## `missing credentials for: X — set Y` (exit 2)

The env var in the target's `api_key_env` is empty in this shell. `tps doctor`
lists every affected target. In Docker/CI, pass `-e OPENROUTER_API_KEY=...`.

## `no config found — run tps init` (exit 1)

`tps run` looks for `targets.toml`, then `targets.gateway.toml`, in the current
directory. `cd` to the config or pass `-c path/to/file.toml`.

## `HTTP 400 ... stream_options`

Some servers reject `stream_options`; the rig retries once without it. If it still
fails, set `stream_options = false` on that target.

## `HTTP 404` on a pinned target

The slug or model id moved. Re-run `tps discover <model> --out targets.toml`. Tiers
need full tag slugs (`baseten/fast`).

## Everything times out

- `tps doctor --network` to test egress.
- Proxy/VPN: `HTTPS_PROXY` is honoured by httpx.
- A wrong model id can hang some gateways instead of returning a clean 404.

## `ok` much lower than `n` at high concurrency

That is the finding. 429s at c=16 mean rate limiting, not a bug. Counts are
tabulated per class; there are no retries by design.

## Numbers look impossibly good

1. Report marked `MOCK DATA`, or `meta.json` `"mock": true`?
2. `out tok` lower than other rows (early stop ⇒ fast-looking)?
3. Target pinned, or did the router send you somewhere else?
4. Different quantization (fp4 vs fp8)?
5. `served by` showing an unexpected provider?

## Two runs disagree by 30%

Expected. Throughput moves with load and time of day. Run both twice, same hour,
≥7 reps, compare medians of medians — then see if the difference survives.

## `tps report` says "not a tps run directory"

Pass the directory printed by `tps run` (contains `raw.jsonl`), not its parent.

## Fingerprint: everything in one group

A real result: at 160 tokens the endpoints were indistinguishable. It does not
prove identical weights. Raise `--max-tokens` and `--prompt-count` to probe harder.

## Fingerprint: a provider is non-deterministic

`self_agreement` < 98% at temperature 0. That endpoint varies run to run; its
cross-provider numbers are uninterpretable. Exclude it or investigate.

## `No module named tps`

You ran `python -m tps` outside the repo without installing. `pipx install .`, or
`cd` into the checkout.

## Colour codes in a pipe

Shouldn't happen — colour is off when stdout is not a TTY. If a wrapper forces a
TTY, use `--no-color` or `NO_COLOR=1`.

## Still stuck

```bash
tps -v run -c targets.toml --mock     # traceback on unexpected errors
cat runs/<stamp>/meta.json            # exactly what was measured
head -1 runs/<stamp>/raw.jsonl | jq   # one request in full, errors included
```
