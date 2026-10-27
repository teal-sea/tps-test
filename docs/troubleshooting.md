# Troubleshooting

## `missing credentials for: X — set Y` (exit 2)

The env var named in the target's `api_key_env` is empty in this shell.
`tps doctor` shows which targets are affected. Docker/CI: `-e OPENROUTER_API_KEY=...`.

## `no config found — run tps init` (exit 1)

`tps run` looks for `targets.toml`, then `targets.gateway.toml`, in the current
directory. Either `cd` to where the config is, or pass `-c path/to/file.toml`.

## `HTTP 400 ... stream_options`

Some OpenAI-compatible servers reject `stream_options`. The rig retries once
automatically without it. If it still fails, set in the target:

```toml
stream_options = false
```

## `HTTP 404` on a pinned target

The provider slug or model id changed. Re-run:

```bash
tps discover <model> --out targets.toml
```

Pins must be full tag slugs for tiers (`baseten/fast`), not base slugs.

## Every request times out

- `tps doctor --network` to test egress.
- Corporate proxy / VPN: `HTTPS_PROXY` is honoured by httpx.
- Check the model id — a wrong id can produce a hanging connection on some
  gateways rather than a clean 404.

## `ok` is much lower than `n` at high concurrency

That is the finding, not a bug. 429s at c=16 mean the provider is rate-limiting
you. The report counts them per class; there is no retry by design.

## Numbers look impossibly good

Check, in this order:

1. Is the report marked `MOCK DATA`, or `meta.json` `"mock": true`?
2. Is `out tok` lower than the other rows? (early stop ⇒ fast-looking)
3. Is the target pinned, or did the router send you somewhere cheap and different?
4. Is the quantization different (fp4 vs fp8)?
5. Did `serve by` show a provider you did not expect?

## Two runs disagree by 30%

Expected. Provider throughput moves with load and time of day. Run both twice,
same hour, ≥7 reps, and compare medians of medians. Then check whether the
difference survives.

## `tps report` says "not a tps run directory"

Pass the directory printed by `tps run` (contains `raw.jsonl`), not its parent.

## Fingerprint says everything is one group

That is a real result: at 160 tokens the endpoints were indistinguishable. It does
not prove identical weights. Increase `--max-tokens` and `--prompt-count` to probe
harder, or accept that speed differences come from serving, not weights.

## Fingerprint says a provider is non-deterministic

`self_agreement` < 98% at temperature 0. That endpoint varies run to run — often
batch-dependent kernels or a router fanning out between backends. Its
cross-provider numbers are not interpretable; exclude it or investigate.

## Import error: `No module named tps`

You are running `python -m tps` outside the repo, or the package is not installed.
Either `pipx install .` from the repo, or `cd` into it.

## Colour codes in piped output

They should not appear — colour is disabled when stdout is not a TTY. If a wrapper
forces a TTY, pass `--no-color` or set `NO_COLOR=1`.

## `error: unrecognized arguments`

Flags must go after the subcommand (`tps run --json`), not between two commands.
Global flags are accepted in both positions but a typo will not be treated kindly.

## Still stuck

```bash
tps -v run -c targets.toml --mock     # full traceback on unexpected errors
cat runs/<stamp>/meta.json            # exactly what was measured
head -1 runs/<stamp>/raw.jsonl | jq   # one request in full, errors included
```
