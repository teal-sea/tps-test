"""Terminal output: colour, tables, progress. No third-party dependencies.

Everything degrades gracefully — if stdout is not a TTY, or NO_COLOR is set,
or --no-color is passed, the exact same text is emitted without ANSI codes.
That matters because the output is read by pipes and agents, not just eyes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Sequence

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
GREY = "\033[90m"


def _detect_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


class UI:
    """Small façade so command code never touches ANSI codes directly."""

    def __init__(
        self,
        json_mode: bool = False,
        color: bool | None = None,
        quiet: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.color = _detect_color() if color is None else color

    # -- styling ---------------------------------------------------------
    def _wrap(self, code: str, text: str) -> str:
        if not self.color or self.json_mode:
            return text
        return f"{code}{text}{RESET}"

    def bold(self, text: str) -> str:
        return self._wrap(BOLD, text)

    def dim(self, text: str) -> str:
        return self._wrap(DIM, text)

    def green(self, text: str) -> str:
        return self._wrap(GREEN, text)

    def red(self, text: str) -> str:
        return self._wrap(RED, text)

    def yellow(self, text: str) -> str:
        return self._wrap(YELLOW, text)

    def cyan(self, text: str) -> str:
        return self._wrap(CYAN, text)

    def grey(self, text: str) -> str:
        return self._wrap(GREY, text)

    # -- structure -------------------------------------------------------
    def say(self, text: str = "", stream=None) -> None:
        if self.json_mode or self.quiet:
            return
        print(text, file=stream or sys.stdout)

    def heading(self, text: str) -> None:
        if self.json_mode or self.quiet:
            return
        self.say("")
        self.say(self.bold(text))

    def hint(self, text: str) -> None:
        if self.json_mode or self.quiet:
            return
        self.say(self.grey(text))

    def ok(self, text: str) -> None:
        self.say(f"  {self.green('✓')} {text}")

    def fail(self, text: str) -> None:
        self.say(f"  {self.red('✗')} {text}")

    def warn(self, text: str) -> None:
        self.say(f"  {self.yellow('!')} {text}")

    def error(self, text: str) -> None:
        print(f"{self.red('error')}: {text}", file=sys.stderr)

    # -- tables ----------------------------------------------------------
    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        aligns: Sequence[str] | None = None,
        highlight: int | None = None,
    ) -> None:
        """Aligned columns; `highlight` is the index of the row to accent."""
        if self.json_mode or self.quiet:
            return
        cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
        widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
        aligns = aligns or ["l"] + ["r"] * (len(headers) - 1)

        def line(row: Sequence[str]) -> str:
            out = []
            for i, value in enumerate(row):
                out.append(value.ljust(widths[i]) if aligns[i] == "l" else value.rjust(widths[i]))
            return "  ".join(out).rstrip()

        print(self.dim(line(cells[0])))
        print(self.dim("  ".join("-" * w for w in widths)))
        for index, row in enumerate(cells[1:]):
            text = line(row)
            print(self.bold(text) if index == highlight else text)

    # -- progress --------------------------------------------------------
    def progress(self, text: str, end: bool = False) -> None:
        """One self-overwriting status line on stderr; silent when piped."""
        if self.json_mode or not sys.stderr.isatty():
            return
        if end:
            sys.stderr.write("\r" + " " * 100 + "\r")
        else:
            sys.stderr.write("\r" + text[:100].ljust(100))
        sys.stderr.flush()

    # -- machine output --------------------------------------------------
    def emit(self, payload: dict[str, Any]) -> None:
        """The only thing on stdout in --json mode."""
        print(json.dumps(payload, indent=2, default=str))


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{seconds // 60:.0f}m{seconds % 60:02.0f}s"


def bar(value: float | None, peak: float | None, width: int = 24) -> str:
    if not value or not peak:
        return ""
    filled = max(1, round(width * value / peak))
    return "█" * filled
