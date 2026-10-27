# Security policy

## Reporting a vulnerability

Please report privately via **Security → Report a vulnerability** on the GitHub
repository, not in a public issue. You'll get an acknowledgement within a few
days, and a fix or a plan before anything is disclosed.

## What `tps` does with your credentials

- API keys are read from the environment variable named in each target's
  `api_key_env` (or, discouraged, a literal `api_key` in the config).
- A key is sent only as the `Authorization` header to that target's own
  `base_url`. It is never logged, never printed, and never written to
  `runs/` (`meta.json` records names, models, URLs, pins and prices — no keys).
- Nothing is sent anywhere except the endpoints in your config, plus
  `openrouter.ai` for `discover` / `init --model`. There is no telemetry.
- Requests carry a `User-Agent: tps/<version>` header so providers can identify
  benchmark traffic.

`runs/` contains the prompts you configured and the responses you received. It
is gitignored; keep it that way if your prompts are sensitive.

## Supported versions

Only the latest release on `main` receives fixes.
