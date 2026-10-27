# Contributing

Thanks for helping. `tps` is small on purpose: one runtime dependency, no
retries, numbers you can defend. Changes that keep it that way are very welcome.

## Setup

```bash
git clone https://github.com/teal-sea/tps-test && cd tps-test
python -m venv .venv && . .venv/bin/activate
pip install -e ".[http2,dev]"
```

## Before you open a PR

```bash
python tests/selftest.py          # offline: local SSE server, recorded fixture, CLI contract
ruff check . && ruff format --check .
```

Both run in CI on Python 3.11–3.13, plus a strict docs build.

- **Tests never touch the network or need keys.** New measurement logic gets a
  check in `tests/selftest.py` — usually a new misbehaving model in its local
  server.
- **Docs live in `docs/` only.** The mkdocs site and the GitHub wiki are both
  generated from it; never edit the wiki directly.
- **Changed what the terminal looks like?** Regenerate the README images from
  the real CLI: `pip install -e ".[assets]" && python scripts/render_assets.py`
  (needs the JetBrains Mono and Inter fonts, and Playwright for the PNG).

## Invariants

These are contracts other people's scripts depend on. A PR that changes one
needs a very good reason and a docs update:

1. No automatic retries for 429/5xx — failures are data.
2. Target order rotates every rep; never batch by provider.
3. `--json` prints exactly one JSON object on stdout; exit codes match
   [docs/cli.md](docs/cli.md#exit-codes).
4. Token counts come from `usage`; estimates are always flagged.
5. Mock data is always flagged and never mixes into a real run.
6. A failed request is never evidence that a provider serves different weights.
7. Demo data and screenshots use fictional providers only.

[AGENTS.md](AGENTS.md) has the same rules in a denser form.

## Reporting results about real providers

Please don't open issues or PRs that accuse a named provider of serving the
wrong model based on one fingerprint run. The tool can fail to disprove
identical weights; it cannot prove a difference is intentional. Share the
`fingerprint.md`, the config and the date, and say what you'd like to change in
the tool.
