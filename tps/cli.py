"""tps — command line interface.

Exit codes are part of the contract (scripts and agents depend on them):

    0  success
    1  usage or configuration error
    2  missing credentials
    3  upstream/provider failure
    4  run completed but every request failed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, core
from .ui import UI, bar

SCHEMA_VERSION = 1
CONFIG_CANDIDATES = ("targets.toml", "targets.gateway.toml")

STARTER_CONFIG = """\
# tps config — one [[targets]] block per upstream you want to measure.
# Generate the whole list for a model instead of typing it:
#   tps discover <author>/<slug> --out targets.toml

[run]
reps = 7
concurrency = [1, 4]
warmup = 2
temperature = 0.0
prompts = ["prose-256"]
# baseline = "my-direct-endpoint"   # adds Δ columns to the report

[[targets]]
name = "my-direct-endpoint"
base_url = "https://api.example.com/v1"
model = "some-model-id"
api_key_env = "EXAMPLE_API_KEY"
price = { in_per_1m = 0.30, out_per_1m = 1.20 }

[[targets]]
name = "same-model-via-gateway"
base_url = "https://openrouter.ai/api/v1"
model = "vendor/model-name"
api_key_env = "OPENROUTER_API_KEY"
extra_body.provider = { only = ["example-provider"], allow_fallbacks = false }
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def resolve_config(path: Path | None) -> Path:
    if path:
        if not path.exists():
            raise core.UsageError(f"config not found: {path}")
        return path
    for candidate in CONFIG_CANDIDATES:
        if Path(candidate).exists():
            return Path(candidate)
    raise core.UsageError(
        "no config found — run `tps init` to scaffold one, or pass -c <file>"
    )


def require_keys(cfg: core.RunConfig) -> None:
    missing = [
        t.name
        for t in cfg.targets
        if not t.key()
        and "localhost" not in t.base_url
        and "127.0.0.1" not in t.base_url
    ]
    if missing:
        envs = sorted({t.api_key_env for t in cfg.targets if t.name in missing and t.api_key_env})
        raise core.MissingKeyError(
            f"missing credentials for: {', '.join(missing)}"
            + (f" — set {', '.join(envs)}" if envs else "")
            + " (or run with --mock)"
        )


def apply_overrides(cfg: core.RunConfig, args: argparse.Namespace) -> None:
    if getattr(args, "reps", None) is not None:
        cfg.reps = args.reps
    if getattr(args, "concurrency", None):
        cfg.concurrency = [int(c) for c in args.concurrency.split(",") if c]
    if getattr(args, "prompts", None):
        wanted = [p.strip() for p in args.prompts.split(",") if p.strip()]
        cfg.prompts = {
            p: (cfg.prompts[p] if p in cfg.prompts else dict(core.DEFAULT_PROMPTS[p]))
            for p in wanted
        }
    if getattr(args, "no_warmup", False):
        cfg.warmup = 0
    if getattr(args, "mock", False):
        for target in cfg.targets:
            if target.mock is None:
                target.mock = {}


def summarise(samples: list[core.Sample], meta: dict[str, Any]) -> dict[str, Any]:
    """Structured result used by both --json and the terminal table."""
    stats = core.summarize([s.as_dict() for s in samples])
    per_stream = stats["per_stream"]
    aggregate = stats["aggregate"]
    prices = {t["name"]: t for t in meta.get("targets", [])}
    rows: list[dict[str, Any]] = []
    for (prompt, target), slot in sorted(per_stream.items()):
        info = prices.get(target, {})
        agg = {}
        for (p, t, conc), bucket in aggregate.items():
            if p != prompt or t != target or not bucket["batches"]:
                continue
            agg[str(conc)] = round(statistics.median(bucket["batches"]), 2)
        rows.append(
            {
                "prompt": prompt,
                "target": target,
                "model": info.get("model"),
                "base_url": info.get("base_url"),
                "pin": info.get("pin"),
                "ttft_p50_s": core.percentile(slot["ttft"], 0.5),
                "ttft_p90_s": core.percentile(slot["ttft"], 0.9),
                "out_tps_p50": core.percentile(slot["tps"], 0.5),
                "out_tps_p90": core.percentile(slot["tps"], 0.9),
                "itl_p50_ms": core.percentile(slot["itl"], 0.5),
                "e2e_p50_s": core.percentile(slot["e2e"], 0.5),
                "out_tokens_median": (
                    statistics.median(slot["out"]) if slot["out"] else None
                ),
                "ok": slot["ok"],
                "n": slot["n"],
                "errors": slot["errors"],
                "aggregate_tps": agg,
                "price_in_per_1m": info.get("price_in"),
                "price_out_per_1m": info.get("price_out"),
            }
        )
    return {"rows": rows, "baseline": meta.get("baseline")}


def print_run_table(ui: UI, summary: dict[str, Any], prompt: str) -> None:
    rows = [
        r for r in summary["rows"] if r["prompt"] == prompt and r["n"]
    ]
    if not rows:
        return
    baseline = summary["baseline"]
    base = next((r for r in rows if r["target"] == baseline), None)
    peak = max((r["out_tps_p50"] or 0) for r in rows) or 1

    headers = ["provider", "ttft", "tok/s", "spread", "itl", "ok", "$/1M out"]
    cells = []
    for row in rows:
        cells.append(
            [
                row["target"],
                core.fmt(row["ttft_p50_s"], 2) + "s",
                core.fmt(row["out_tps_p50"], 1),
                bar(row["out_tps_p50"], peak),
                core.fmt(row["itl_p50_ms"], 0) + "ms",
                f"{row['ok']}/{row['n']}",
                core.fmt(row["price_out_per_1m"], 2),
            ]
        )
    ui.say("")
    ui.say(
        ui.bold(f"{prompt} — single stream")
        + ui.grey("   ttft = first token · tok/s = per-stream decode · itl = gap between chunks")
    )
    best = max(range(len(rows)), key=lambda i: rows[i]["out_tps_p50"] or 0)
    ui.table(headers, cells, aligns=["l", "r", "r", "l", "r", "r", "r"], highlight=best)

    if baseline and base:
        ui.say("")
        ui.say(ui.grey(f"Δ vs {baseline}:"))
        for row in rows:
            if row["target"] == baseline:
                continue
            dt = (row["ttft_p50_s"] or 0) - (base["ttft_p50_s"] or 0)
            ds = (
                ((row["out_tps_p50"] or 0) - (base["out_tps_p50"] or 0))
                / (base["out_tps_p50"] or 1)
                * 100
            )
            colour = ui.green if ds >= 0 else ui.red
            ui.say(
                f"  {row['target']:<24} {dt:+.2f}s ttft   "
                + colour(f"{ds:+.0f}% tok/s")
            )


def print_rankings(ui: UI, summary: dict[str, Any], prompt: str) -> None:
    rows = [r for r in summary["rows"] if r["prompt"] == prompt and r["ok"]]
    if not rows:
        return
    fastest = max(rows, key=lambda r: r["out_tps_p50"] or 0)
    snappiest = min(rows, key=lambda r: r["ttft_p50_s"] or 9e9)
    priced = [r for r in rows if r["price_out_per_1m"]]
    cheapest = min(priced, key=lambda r: r["price_out_per_1m"]) if priced else None
    ui.say("")
    ui.say(
        f"  fastest  {ui.bold(fastest['target'])} "
        f"({core.fmt(fastest['out_tps_p50'], 1)} tok/s)"
    )
    ui.say(
        f"  snappiest {ui.bold(snappiest['target'])} "
        f"({core.fmt(snappiest['ttft_p50_s'], 2)}s to first token)"
    )
    if cheapest:
        ui.say(
            f"  cheapest {ui.bold(cheapest['target'])} "
            f"(${core.fmt(cheapest['price_out_per_1m'], 2)}/1M out)"
        )


def fingerprint_json(
    prints: list[core.Fingerprint], targets: list[str], prompts: list[str]
) -> dict[str, Any]:
    self_agree = core.self_agreement_map(prints, targets, prompts)
    clusters = core.cluster_targets(prints, targets, prompts[0], self_agree)
    by_key = {(f.target, f.prompt, f.rep): f for f in prints}
    pairs = []
    for i, a_name in enumerate(targets):
        for b_name in targets[i + 1:]:
            a = by_key.get((a_name, prompts[0], 0))
            b = by_key.get((b_name, prompts[0], 0))
            if not a or not b or not a.ok or not b.ok:
                continue
            frac, first = core.compare_prints(a, b)
            pairs.append(
                {
                    "a": a_name,
                    "b": b_name,
                    "agreement": round(frac, 4),
                    "first_divergence": first,
                }
            )
    prompt_tokens = {
        target: (
            by_key[(target, prompts[0], 0)].prompt_tokens
            if (target, prompts[0], 0) in by_key
            else None
        )
        for target in targets
    }
    return {
        "self_agreement": {k: round(v, 4) for k, v in self_agree.items()},
        "clusters": clusters,
        "pairs": pairs,
        "prompt_tokens": prompt_tokens,
        "prompt_used": prompts[0],
    }


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0] + " (need 3.11+)")
    try:
        import httpx

        add("httpx", True, httpx.__version__)
    except ImportError as exc:  # pragma: no cover
        add("httpx", False, str(exc))
        httpx = None  # type: ignore

    add(
        "http2",
        core.HTTP2,
        "h2 installed" if core.HTTP2 else "optional — pip install \"httpx[http2]\"",
        critical=False,
    )

    config_path = None
    cfg = None
    try:
        config_path = resolve_config(args.config)
        cfg = core.load_config(config_path)
        add("config", True, f"{config_path} · {len(cfg.targets)} targets")
    except core.TpsError as exc:
        add("config", False, str(exc))

    if cfg:
        missing = [t.name for t in cfg.targets if not t.key() and "localhost" not in t.base_url]
        add(
            "credentials",
            not missing,
            "all set" if not missing else f"missing for {len(missing)} target(s): {', '.join(missing[:4])}",
        )

        if args.network and httpx is not None:
            async def probe() -> list[tuple[str, str]]:
                timeout = httpx.Timeout(5.0)
                out = []
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async def one(target: core.Target) -> tuple[str, str]:
                        try:
                            response = await client.get(target.base_url)
                            return target.name, f"HTTP {response.status_code}"
                        except Exception as exc:  # noqa: BLE001 - report, never crash
                            return target.name, type(exc).__name__

                    out = await asyncio.gather(*(one(t) for t in cfg.targets))
                return out

            for name, detail in asyncio.run(probe()):
                add(f"reach {name}", not detail.startswith(("Connect", "Timeout")), detail, critical=False)

    ui.heading("Environment")
    for item in checks:
        line = f"{item['check']}: {item['detail']}"
        if item["ok"]:
            ui.ok(line)
        elif item["critical"]:
            ui.fail(line)
        else:
            ui.warn(line)

    ok = all(c["ok"] for c in checks if c.get("critical"))
    if ok:
        ui.say("")
        ui.say(ui.green("Ready.") + ui.grey(" next: tps run -c targets.toml"))
    else:
        ui.say("")
        ui.say(ui.grey("Fix the ✗ rows above, then re-run `tps doctor`."))
    return {"command": "doctor", "ok": ok, "checks": checks, "exit_code": 0 if ok else 2}


def cmd_init(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    out = args.out
    if out.exists() and not args.force:
        raise core.UsageError(f"{out} exists — pass --force to overwrite")

    if args.model:
        canonical, endpoints, _payload = core.fetch_endpoints(
            args.model, os.environ.get("OPENROUTER_API_KEY")
        )
        text, count, collapsed = core.targets_toml(canonical, endpoints)
        out.write_text(text, encoding="utf-8")
        ui.ok(f"wrote {out} — {count} pinned targets for {canonical}")
        note = f" ({collapsed} endpoint(s) collapsed)" if collapsed else ""
        ui.hint(f"next: export OPENROUTER_API_KEY=... && tps run -c {out}{note}")
        return {
            "command": "init",
            "ok": True,
            "config": str(out),
            "model": canonical,
            "targets": count,
        }

    out.write_text(STARTER_CONFIG, encoding="utf-8")
    ui.ok(f"wrote {out} (starter template)")
    ui.hint("edit the targets, then: tps doctor && tps run -c " + str(out))
    ui.hint("or generate for a real model: tps init --model deepseek/deepseek-v4.1-flash")
    return {"command": "init", "ok": True, "config": str(out), "model": None, "targets": 0}


def cmd_discover(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    canonical, endpoints, payload = core.fetch_endpoints(
        args.model, args.api_key or os.environ.get("OPENROUTER_API_KEY"), args.from_json
    )
    rows = core.normalize_endpoints(endpoints)
    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    ui.heading(f"{canonical} — {len(endpoints)} endpoints")
    ui.table(
        ["provider", "slug", "quant", "$/1M in", "$/1M out", "ctx"],
        [
            [
                row["provider"] or "—",
                row["slug"],
                row["quantization"] or "—",
                core.fmt(row["price_in_per_1m"], 3),
                core.fmt(row["price_out_per_1m"], 3),
                f"{row['context_length'] // 1000}k" if row["context_length"] else "—",
            ]
            for row in rows
        ],
        aligns=["l", "l", "l", "r", "r", "r"],
    )

    written = None
    count = 0
    if args.out:
        text, count, collapsed = core.targets_toml(canonical, endpoints)
        args.out.write_text(text, encoding="utf-8")
        written = str(args.out)
        ui.say("")
        ui.ok(f"wrote {written} — {count} pinned targets")
        if collapsed:
            ui.hint(f"{collapsed} duplicate endpoint(s) collapsed into the same pin")
        ui.hint(f"next: tps run -c {written}")
    return {
        "command": "discover",
        "ok": True,
        "model": canonical,
        "endpoints": rows,
        "config": written,
        "targets": count,
    }


def cmd_run(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    path = resolve_config(args.config)
    cfg = core.load_config(path)
    apply_overrides(cfg, args)
    if not args.mock:
        require_keys(cfg)

    outdir = args.out or Path("runs") / stamp()
    ui.heading(
        f"{len(cfg.targets)} targets · {len(cfg.prompts)} prompt(s) · "
        f"{cfg.reps} reps · concurrency {','.join(map(str, cfg.concurrency))}"
    )
    if args.mock:
        ui.warn("mock mode — synthetic numbers, no network")

    def on_batch(**kw: Any) -> None:
        ui.progress(
            f"  {kw['target'][:26]:<26} c={kw['concurrency']:<3} rep={kw['rep']:<3} "
            f"{kw['tps']:6.1f} tok/s  ok={kw['ok']}/{kw['total']}"
        )

    samples = asyncio.run(core.execute(cfg, outdir, args.mock, on_batch=on_batch))
    ui.progress("", end=True)
    meta = json.loads((outdir / "meta.json").read_text(encoding="utf-8"))
    core.write_report(outdir, [s.as_dict() for s in samples], meta)
    summary = summarise(samples, meta)

    for prompt in cfg.prompts:
        print_run_table(ui, summary, prompt)
        print_rankings(ui, summary, prompt)

    failed = sum(1 for s in samples if not s.ok)
    if failed:
        ui.say("")
        ui.warn(f"{failed}/{len(samples)} requests failed — see the Failures section in the report")

    ui.say("")
    ui.ok(f"wrote {outdir}/report.md")
    ui.hint(f"next: tps report {outdir}   ·   tps fingerprint -c {path}")

    return {
        "command": "run",
        "ok": failed < len(samples),
        "run_dir": str(outdir),
        "config": str(path),
        "model": next((t["model"] for t in meta["targets"]), None),
        "reps": cfg.reps,
        "concurrency": cfg.concurrency,
        "results": summary["rows"],
        "failed": failed,
        "total": len(samples),
        # partial failure is data, not a usage error: it shows up in `failed`
        "exit_code": 4 if samples and failed == len(samples) else 0,
        "artifacts": {
            "report": str(outdir / "report.md"),
            "csv": str(outdir / "summary.csv"),
            "raw": str(outdir / "raw.jsonl"),
        },
    }


def cmd_report(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    samples, meta = core.load_samples(args.run_dir)
    text = core.write_report(args.run_dir, samples, meta)
    if not args.json:
        print(text)
    return {"command": "report", "ok": True, "run_dir": str(args.run_dir), "report": text}


def cmd_fingerprint(args: argparse.Namespace, ui: UI) -> dict[str, Any]:
    path = resolve_config(args.config)
    cfg = core.load_config(path)
    if not args.mock:
        require_keys(cfg)
    outdir = args.out or Path("runs") / f"{stamp()}-fingerprint"
    ui.heading(
        f"fingerprinting {len(cfg.targets)} targets · {args.prompt_count} prompt(s)"
    )

    def on_progress(**kw: Any) -> None:
        ui.progress(f"  {kw['target'][:30]:<30} {kw['prompt'][:14]:<14} rep{kw['rep']}")

    report = core.run_fingerprint(
        cfg,
        outdir,
        args.max_tokens,
        args.reps,
        args.prompt_count,
        args.mock,
        on_progress=on_progress,
    )
    ui.progress("", end=True)

    prints = [core.Fingerprint(**row) for row in _read_prints(outdir)]
    targets = [t.name for t in cfg.targets]
    prompt_names = list(cfg.prompts)[: args.prompt_count]
    data = fingerprint_json(prints, targets, prompt_names)

    for index, group in enumerate(data["clusters"], start=1):
        ui.say("")
        ui.say(ui.bold(f"group {index}") + f"  ({len(group)})  " + ", ".join(group))
    ui.say("")
    if len(data["clusters"]) == 1:
        ui.ok("indistinguishable at this resolution — consistent with the same weights")
    else:
        ui.warn("multiple groups — these are not the same artifact")
    ui.hint(f"details: {outdir}/fingerprint.md")

    return {
        "command": "fingerprint",
        "ok": True,
        "run_dir": str(outdir),
        "report": report,
        **data,
    }


def _read_prints(outdir: Path) -> list[dict[str, Any]]:
    path = outdir / "fingerprints.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="tps",
        description="Measure one model across many providers and gateways.",
        epilog=(
            "examples:\n"
            "  tps init --model deepseek/deepseek-v4.1-flash\n"
            "  tps doctor\n"
            "  tps run -c targets.toml --reps 7 --concurrency 1,4\n"
            "  tps fingerprint -c targets.toml\n"
            "\ndocs: https://github.com/teal-sea/tps-test/tree/main/docs"
        ),
        formatter_class=formatter,
    )
    parser.add_argument("--version", action="version", version=f"tps {__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("-q", "--quiet", action="store_true", help="only errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="tracebacks on failure")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def common(p: argparse.ArgumentParser) -> None:
        # mirrored on subcommands so `tps run --json` works as well as `tps --json run`
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    p_doctor = sub.add_parser("doctor", help="check environment, config and credentials")
    p_doctor.add_argument("-c", "--config", type=Path, default=None)
    p_doctor.add_argument("--network", action="store_true", help="also probe base URLs")
    common(p_doctor)

    p_init = sub.add_parser("init", help="scaffold a config")
    p_init.add_argument("--model", default=None, help="generate pinned targets for a model")
    p_init.add_argument("--out", type=Path, default=Path("targets.toml"))
    p_init.add_argument("--force", action="store_true")
    common(p_init)

    p_disc = sub.add_parser("discover", help="list providers serving a model, with prices")
    p_disc.add_argument("model", help="e.g. deepseek/deepseek-v4.1-flash")
    p_disc.add_argument("--out", type=Path, default=None, help="write a config")
    p_disc.add_argument("--api-key", default=None)
    p_disc.add_argument("--from-json", type=Path, default=None, help="offline: recorded response")
    p_disc.add_argument("--json-out", type=Path, default=None, help="save the raw API payload")
    common(p_disc)

    p_run = sub.add_parser("run", help="measure throughput and latency")
    p_run.add_argument("-c", "--config", type=Path, default=None)
    p_run.add_argument("-o", "--out", type=Path, default=None)
    p_run.add_argument("--reps", type=int, default=None)
    p_run.add_argument("--concurrency", default=None, help="e.g. 1,4,16")
    p_run.add_argument("--prompts", default=None, help="comma separated prompt names")
    p_run.add_argument("--mock", action="store_true", help="synthetic numbers, no network")
    p_run.add_argument("--no-warmup", action="store_true")
    common(p_run)

    p_rep = sub.add_parser("report", help="re-render a previous run")
    p_rep.add_argument("run_dir", type=Path)
    common(p_rep)

    p_fp = sub.add_parser("fingerprint", help="test whether targets serve the same weights")
    p_fp.add_argument("-c", "--config", type=Path, default=None)
    p_fp.add_argument("-o", "--out", type=Path, default=None)
    p_fp.add_argument("--max-tokens", type=int, default=160)
    p_fp.add_argument("--reps", type=int, default=2)
    p_fp.add_argument("--prompt-count", type=int, default=2, help="how many prompts to probe")
    p_fp.add_argument("--mock", action="store_true")
    common(p_fp)

    return parser


COMMANDS = {
    "doctor": cmd_doctor,
    "init": cmd_init,
    "discover": cmd_discover,
    "run": cmd_run,
    "report": cmd_report,
    "fingerprint": cmd_fingerprint,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    ui = UI(
        json_mode=args.json,
        color=False if args.no_color else None,
        quiet=args.quiet,
    )
    handler = COMMANDS[args.command]

    try:
        payload = handler(args, ui)
        if args.json:
            ui.emit({"schema_version": SCHEMA_VERSION, **payload})
        return int(payload.get("exit_code", 0))
    except core.TpsError as exc:
        code = exc.exit_code
        if args.json:
            ui.emit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "command": args.command,
                    "ok": False,
                    "error": {"code": code, "message": str(exc)},
                }
            )
        else:
            ui.error(str(exc))
        return code
    except KeyboardInterrupt:
        if not args.json:
            print("", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, keep it readable
        if args.json:
            ui.emit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "command": args.command,
                    "ok": False,
                    "error": {"code": 1, "message": f"{type(exc).__name__}: {exc}"},
                }
            )
        elif args.verbose:
            raise
        else:
            ui.error(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
