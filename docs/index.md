# Documentation

`tps` measures one model across many providers and gateways.

## Start here

| Doc | Read it when |
|---|---|
| [Getting started](getting-started.md) | first time; install → first measurement → reading the table |
| [CLI reference](cli.md) | you need a flag, an exit code, or the JSON shape |
| [Configuration](configuration.md) | writing or editing a `targets.toml` |

## Concepts

| Doc | Read it when |
|---|---|
| [Metrics](metrics.md) | you want the exact definition behind TTFT, ITL, tok/s, cost |
| [Methodology](methodology.md) | you are about to publish a comparison and want it to survive review |
| [Gateways](gateways.md) | comparing a gateway against direct, or gateway against gateway |
| [Fingerprinting](fingerprinting.md) | a price looks too good, or two "identical" endpoints differ 3× in speed |

## Operations

| Doc | Read it when |
|---|---|
| [Automation](automation.md) | CI, cron, JSON pipelines, agent integration |
| [Troubleshooting](troubleshooting.md) | something failed |
| [FAQ](faq.md) | short answers |

## For agents

[AGENTS.md](https://github.com/teal-sea/tps-test/blob/main/AGENTS.md) is the machine-oriented contract: commands, exit codes,
JSON schema, invariants. [llms.txt](https://github.com/teal-sea/tps-test/blob/main/llms.txt) is the index for crawlers.

## The one-paragraph summary

Pick a model, enumerate its providers, pin one upstream per target, send identical
prompts at temperature 0 with streaming enabled, and measure TTFT, inter-token
latency, decode rate, aggregate throughput, failures and cost — interleaved so
time-of-day doesn't leak into one row. Then test whether the "same model" is
actually the same weights. Everything else in these docs is detail about not
fooling yourself.

## Wiki

The same material is mirrored to the GitHub wiki for browsing. Source of truth is
[`wiki/`](https://github.com/teal-sea/tps-test/tree/main/wiki) in this repo; publish with:

```bash
scripts/publish-wiki.sh            # needs Settings -> Features -> Wikis enabled
```
