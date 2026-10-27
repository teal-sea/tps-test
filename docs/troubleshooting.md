# Troubleshooting

## `missing credentials for: X — set Y` (exit 2)

The env var named in the target's `api_key_env` is empty in this shell.
`tps doctor` shows which targets are affected. Docker/CI: `-e OPENROUTER_API_KEY=...`.

## `no config found — run tps init` (exit 1)

`tps run` looks for `targets.toml`, then `targets.gateway.toml`, in the current
directory. Either `cd` to where the config is, or pass `-c path/to/file.toml`.

## `HTTP 400 ... stream_options`

Some OpenAI-compatible servers reject `stream_options`. `tps` resends that
request once without it — a compatibility fallback, not a retry of a failure —
and stops sending it to that target for the rest of the run. To skip the probe
entirely, set in the target:

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
5. Did `served by` show a provider you did not expect?
6. Is `tok/s` marked `≈`? The provider sent no `usage`, so tokens were estimated.

## Two runs disagree by 30%

Expected. Provider throughput moves with load and time of day. Run both twice,
same hour, ≥7 reps, and compare medians of medians. Then check whether the
difference survives.

## I pressed Ctrl-C halfway through

Nothing measured is lost. `raw.jsonl` is written as batches finish, the report
is rendered from what completed and flagged `PARTIAL`, and the exit code is 130.
Re-render any time with `tps report runs/<stamp>`.

## `tps report` says "not a tps run directory"

Pass the directory printed by `tps run` (contains `raw.jsonl`), not its parent.

## Fingerprint says everything is one group

That is a real result: at 160 tokens the endpoints were indistinguishable. It does
not prove identical weights. Increase `--max-tokens` and `--prompt-count` to probe
harder, or accept that speed differences come from serving, not weights.

## Fingerprint says a provider is non-deterministic

`self_agreement` < 98% at temperature 0. That endpoint varies run to run — often
batch-dependent kernels or a router fanning out between backends. Its
cross-provider numbers are not interpretable, so it never decides the verdict;
exclude it or investigate.

## Fingerprint lists a provider under "no data"

Its request failed (auth, 5xx, timeout — the reason is printed). That is not
evidence of different weights; fix the error and re-run.

## Import error: `No module named tps`

You are running `python -m tps` outside the repo, or the package is not installed.
Either `pipx install .` from the repo, or `cd` into it.

## Colour codes in piped output

They should not appear — colour is disabled when stdout is not a TTY. If a wrapper
forces a TTY, pass `--no-color` or set `NO_COLOR=1`.

## `error: unrecognized arguments` (exit 1)

Subcommand flags go after the subcommand (`tps run --reps 3`). Global flags
(`--json`, `--no-color`, `-q`, `-v`) are accepted in both positions.

## `duplicate target name(s)` (exit 1)

Every statistic is keyed by `name`, so two targets with the same name (or two
unnamed targets with the same `model`) would be silently pooled. Give each one a
unique `name`.

## `only defines mock targets — add --mock` (exit 1)

`targets.mock.toml` and `examples/fingerprint-demo.toml` are synthetic demos; run
them with `--mock`.

## Still stuck

```bash
tps -v run -c targets.toml --mock     # full traceback on unexpected errors
cat runs/<stamp>/meta.json            # exactly what was measured
head -1 runs/<stamp>/raw.jsonl | jq   # one request in full, errors included
```
