#!/usr/bin/env python3
"""Regenerate docs/assets/ images and examples/ reports from the real CLI.

    pip install -e ".[assets]"
    python scripts/render_assets.py            # all assets
    python scripts/render_assets.py --check    # exit 1 if anything would change

Screenshots are *rendered from actual `tps` output* (mock mode, so offline and
deterministic): change the CLI, re-run this, and the README can't drift from
what users see. Text is converted to glyph outlines and box-drawing/block
characters are drawn as geometry, so every image renders identically on GitHub,
in dark and light mode, with no font downloads.

Fonts (OFL): JetBrains Mono and Inter. Point TPS_FONT_DIR at a folder holding
JetBrainsMono-{Regular,Bold}.ttf and Inter-{SemiBold,Medium}.otf if they are not
in the usual system locations.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets"

FONT_DIRS = [
    os.environ.get("TPS_FONT_DIR", ""),
    "/usr/share/fonts/truetype/jetbrains-mono",
    "/usr/share/fonts/opentype/inter",
    "/usr/share/fonts/truetype/dejavu",
    str(Path.home() / "Library/Fonts"),
    "/Library/Fonts",
]


def font_file(name: str) -> Path:
    for folder in FONT_DIRS:
        if folder and (Path(folder) / name).exists():
            return Path(folder) / name
    sys.exit(f"font not found: {name} (set TPS_FONT_DIR)")


MONO = "JetBrainsMono-Regular.ttf"
MONO_BOLD = "JetBrainsMono-Bold.ttf"
MONO_XBOLD = "JetBrainsMono-ExtraBold.ttf"
SANS = "Inter-SemiBold.otf"
SANS_MED = "Inter-Medium.otf"
FALLBACK = "DejaVuSansMono.ttf"


# --------------------------------------------------------------------------
# text -> outlines
# --------------------------------------------------------------------------


@cache
def _font(name: str) -> tuple[TTFont, hb.Font]:
    path = font_file(name)
    return TTFont(path), hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def shape(name: str, text: str, size: float, x: float, y: float, tracking: float = 0.0):
    """Kerned outline path for `text` with its baseline at (x, y); returns (d, width)."""
    tt, hbfont = _font(name)
    upm = tt["head"].unitsPerEm
    scale = size / upm
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {"kern": True, "liga": True})
    glyphs = tt.getGlyphSet()
    order = tt.getGlyphOrder()
    pen = SVGPathPen(glyphs, ntos=_num)
    cursor = 0.0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
        origin = (x + (cursor + pos.x_offset) * scale, y - pos.y_offset * scale)
        glyphs[order[info.codepoint]].draw(TransformPen(pen, (scale, 0, 0, -scale, *origin)))
        cursor += pos.x_advance + tracking / scale
    return pen.getCommands(), cursor * scale


def text(name, s, size, x, y, fill, anchor="start", tracking=0.0, extra="") -> str:
    _, width = shape(name, s, size, 0, 0, tracking)
    if anchor == "middle":
        x -= width / 2
    elif anchor == "end":
        x -= width
    d, _ = shape(name, s, size, x, y, tracking)
    return f'<path d="{d}" fill="{fill}"{extra}/>'


# --------------------------------------------------------------------------
# ANSI -> cell grid
# --------------------------------------------------------------------------

# A dark theme tuned for legibility on GitHub's light and dark backgrounds.
THEME = {
    "bg": "#0d1117",
    "chrome": "#161b22",
    "fg": "#c9d1d9",
    "ansi": [
        "#484f58",
        "#ff7b72",
        "#3fb950",
        "#d29922",
        "#58a6ff",
        "#bc8cff",
        "#39c5cf",
        "#b1bac4",
        "#6e7681",
        "#ffa198",
        "#56d364",
        "#e3b341",
        "#79c0ff",
        "#d2a8ff",
        "#56d4dd",
        "#f0f6fc",
    ],
}


def xterm256(n: int) -> str:
    if n < 16:
        return THEME["ansi"][n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        r, g, b = steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    level = 8 + (n - 232) * 10
    return f"#{level:02x}{level:02x}{level:02x}"


@dataclass
class Cell:
    ch: str
    fg: str | None
    bg: str | None
    bold: bool
    dim: bool


SGR = re.compile(r"\x1b\[([0-9;]*)m")


def parse_ansi(source: str) -> list[list[Cell]]:
    rows: list[list[Cell]] = []
    fg = bg = None
    bold = dim = reverse = False
    for raw_line in source.split("\n"):
        row: list[Cell] = []
        pos = 0
        for match in [*SGR.finditer(raw_line), None]:
            chunk = raw_line[pos : match.start() if match else len(raw_line)]
            for ch in chunk:
                f, b = (bg or THEME["bg"], fg or THEME["fg"]) if reverse else (fg, bg)
                row.append(Cell(ch, f, b, bold, dim))
            if match is None:
                break
            pos = match.end()
            codes = [int(c) if c else 0 for c in match.group(1).split(";")]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg = bg = None
                    bold = dim = reverse = False
                elif c == 1:
                    bold = True
                elif c == 2:
                    dim = True
                elif c == 22:
                    bold = dim = False
                elif c == 7:
                    reverse = True
                elif c == 27:
                    reverse = False
                elif 30 <= c <= 37:
                    fg = THEME["ansi"][c - 30]
                elif 90 <= c <= 97:
                    fg = THEME["ansi"][c - 90 + 8]
                elif c == 39:
                    fg = None
                elif 40 <= c <= 47:
                    bg = THEME["ansi"][c - 40]
                elif 100 <= c <= 107:
                    bg = THEME["ansi"][c - 100 + 8]
                elif c == 49:
                    bg = None
                elif c in (38, 48) and i + 1 < len(codes):
                    if codes[i + 1] == 5 and i + 2 < len(codes):
                        colour = xterm256(codes[i + 2])
                        i += 2
                    elif codes[i + 1] == 2 and i + 4 < len(codes):
                        colour = "#{:02x}{:02x}{:02x}".format(*codes[i + 2 : i + 5])
                        i += 4
                    else:
                        colour = None
                    if c == 38:
                        fg = colour
                    else:
                        bg = colour
                i += 1
        rows.append(row)
    while rows and not any(c.ch.strip() for c in rows[-1]):
        rows.pop()
    return rows


# --------------------------------------------------------------------------
# cell grid -> SVG
# --------------------------------------------------------------------------

SIZE = 14.0  # px
CELL_W = SIZE * 0.6  # JetBrains Mono advance is 600/1000 em
LINE_H = 21.0
PAD_X, PAD_TOP, PAD_BOTTOM, CHROME_H, MARGIN = 22.0, 18.0, 20.0, 34.0, 26.0

# light box drawing: char -> segments from the cell centre (r, l, d, u)
BOX = {
    "─": "rl",
    "│": "du",
    "┌": "rd",
    "┐": "ld",
    "└": "ru",
    "┘": "lu",
    "├": "rdu",
    "┤": "ldu",
    "┬": "rld",
    "┴": "rlu",
    "┼": "rldu",
    "╴": "l",
    "╶": "r",
    "╵": "u",
    "╷": "d",
}
ROUND = {"╭": "rd", "╮": "ld", "╰": "ru", "╯": "lu"}
HEAVY = {"━": "rl", "┃": "du", "╸": "l", "╺": "r"}
EIGHTHS = " ▏▎▍▌▋▊▉█"
LOWER = " ▁▂▃▄▅▆▇█"
SHADE = {"░": 0.22, "▒": 0.45, "▓": 0.7}


def _glyph_font(ch: str, bold: bool) -> str:
    for name in ((MONO_BOLD if bold else MONO), FALLBACK):
        if ord(ch) in _font(name)[0].getBestCmap():
            return name
    return FALLBACK


class Defs:
    """Each distinct glyph/shape is defined once and instanced with <use>."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def glyph(self, ch: str, bold: bool) -> str:
        name = _glyph_font(ch, bold)
        key = f"g{'b' if name == MONO_BOLD else 'f' if name == FALLBACK else 'r'}{ord(ch):x}"
        if key not in self.items:
            d, width = shape(name, ch, SIZE, 0, 0)
            if name == FALLBACK and width > CELL_W * 1.05:  # squeeze wide fallback glyphs
                d, _ = shape(name, ch, SIZE * CELL_W / width, 0, 0)
            self.items[key] = f'<path id="{key}" d="{d}"/>'
        return key

    def shape(self, key: str, svg: str) -> str:
        self.items.setdefault(key, svg.replace("ID", key))
        return key


def _box_path(segments: str, w: float, h: float, ov: float = 0.6) -> str:
    cx, cy = w / 2, h / 2
    ends = {"r": (w + ov, cy), "l": (-ov, cy), "d": (cx, h + ov), "u": (cx, -ov)}
    return " ".join(
        f"M{_num(cx)} {_num(cy)}L{_num(ends[s][0])} {_num(ends[s][1])}" for s in segments
    )


def _round_path(kind: str, w: float, h: float, ov: float = 0.6) -> str:
    cx, cy, r = w / 2, h / 2, w / 2
    horiz = w + ov if "r" in kind else -ov
    vert = h + ov if "d" in kind else -ov
    hx = cx + (r if "r" in kind else -r)
    vy = cy + (r if "d" in kind else -r)
    sweep = {"rd": 0, "ld": 1, "ru": 1, "lu": 0}[kind]
    return (
        f"M{_num(horiz)} {_num(cy)}L{_num(hx)} {_num(cy)}"
        f"A{_num(r)} {_num(r)} 0 0 {sweep} {_num(cx)} {_num(vy)}L{_num(cx)} {_num(vert)}"
    )


def render_cells(defs: Defs, cell: Cell, x: float, y: float) -> tuple[str, str]:
    """Return (background svg, foreground svg) for one cell at top-left (x, y)."""
    w, h = CELL_W, LINE_H
    colour = cell.fg or THEME["fg"]
    opacity = ' opacity="0.55"' if cell.dim else ""
    bg = ""
    if cell.bg:
        bg = f'<rect x="{_num(x - 0.3)}" y="{_num(y)}" width="{_num(w + 0.6)}" height="{_num(h)}" fill="{cell.bg}"/>'
    ch = cell.ch
    if ch == " ":
        return bg, ""
    pos = f'x="{_num(x)}" y="{_num(y)}"'
    if ch in BOX or ch in HEAVY:
        heavy = ch in HEAVY
        key = defs.shape(
            f"box{ord(ch):x}",
            f'<path id="ID" d="{_box_path((HEAVY if heavy else BOX)[ch], w, h)}" '
            f'fill="none" stroke-width="{2.4 if heavy else 1.25}"/>',
        )
        return bg, f'<use href="#{key}" {pos} stroke="{colour}"{opacity}/>'
    if ch in ROUND:
        key = defs.shape(
            f"box{ord(ch):x}",
            f'<path id="ID" d="{_round_path(ROUND[ch], w, h)}" fill="none" stroke-width="1.25"/>',
        )
        return bg, f'<use href="#{key}" {pos} stroke="{colour}"{opacity}/>'
    if ch in EIGHTHS[1:]:
        frac = EIGHTHS.index(ch) / 8
        bar_h = SIZE * 0.8
        top = (h - bar_h) / 2
        key = defs.shape(
            f"blk{ord(ch):x}",
            f'<rect id="ID" x="-0.6" y="{_num(top)}" width="{_num(w * frac + 1.2)}" '
            f'height="{_num(bar_h)}"/>',
        )
        return bg, f'<use href="#{key}" {pos} fill="{colour}"{opacity}/>'
    if ch in LOWER[1:7]:
        frac = LOWER.index(ch) / 8
        key = defs.shape(
            f"low{ord(ch):x}",
            f'<rect id="ID" x="0.5" y="{_num(h * (1 - frac) - 2)}" width="{_num(w - 1)}" height="{_num(h * frac)}"/>',
        )
        return bg, f'<use href="#{key}" {pos} fill="{colour}"{opacity}/>'
    if ch in SHADE:
        key = defs.shape(
            f"shd{ord(ch):x}",
            f'<rect id="ID" x="0" y="{_num((h - SIZE) / 2)}" width="{_num(w + 0.4)}" height="{_num(SIZE)}" '
            f'opacity="{SHADE[ch]}"/>',
        )
        return bg, f'<use href="#{key}" {pos} fill="{colour}"{opacity}/>'
    key = defs.glyph(ch, cell.bold)
    baseline = y + (h - SIZE * 1.32) / 2 + SIZE * 1.02
    return bg, f'<use href="#{key}" x="{_num(x)}" y="{_num(baseline)}" fill="{colour}"{opacity}/>'


def terminal_svg(ansi: str, title: str, cols: int | None = None) -> str:
    rows = parse_ansi(ansi)
    cols = cols or max((len(r) for r in rows), default=40)
    inner_w = cols * CELL_W
    width = inner_w + 2 * PAD_X
    height = CHROME_H + PAD_TOP + len(rows) * LINE_H + PAD_BOTTOM
    defs = Defs()
    back, front = [], []
    for r, row in enumerate(rows):
        y = CHROME_H + PAD_TOP + r * LINE_H
        for c, cell in enumerate(row):
            b, f = render_cells(defs, cell, PAD_X + c * CELL_W, y)
            if b:
                back.append(b)
            if f:
                front.append(f)
    total_w, total_h = width + 2 * MARGIN, height + 2 * MARGIN
    dots = "".join(
        f'<circle cx="{_num(18 + i * 20)}" cy="{_num(CHROME_H / 2)}" r="6" fill="{c}"/>'
        for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840"))
    )
    caption = text(SANS_MED, title, 12.5, width / 2, CHROME_H / 2 + 4.5, "#8b949e", anchor="middle")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{_num(total_w)}" height="{_num(total_h)}" viewBox="0 0 {_num(total_w)} {_num(total_h)}" role="img" aria-label="{html.escape(title)}">
<title>{html.escape(title)}</title>
<defs>
<filter id="shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#000" flood-opacity="0.35"/></filter>
<clipPath id="win"><rect width="{_num(width)}" height="{_num(height)}" rx="10"/></clipPath>
{"".join(defs.items.values())}
</defs>
<g transform="translate({_num(MARGIN)} {_num(MARGIN - 6)})">
<rect width="{_num(width)}" height="{_num(height)}" rx="10" fill="{THEME["bg"]}" filter="url(#shadow)"/>
<g clip-path="url(#win)">
<rect width="{_num(width)}" height="{_num(CHROME_H)}" fill="{THEME["chrome"]}"/>
<rect y="{_num(CHROME_H - 1)}" width="{_num(width)}" height="1" fill="#30363d"/>
{dots}{caption}
{"".join(back)}
{"".join(front)}
</g>
<rect x="0.5" y="0.5" width="{_num(width - 1)}" height="{_num(height - 1)}" rx="9.5" fill="none" stroke="#30363d"/>
</g>
</svg>
"""


# --------------------------------------------------------------------------
# capture real CLI output
# --------------------------------------------------------------------------


def capture(args: list[str], cwd: Path, cols: int) -> str:
    env = dict(os.environ, FORCE_COLOR="1", COLUMNS=str(cols))
    env.pop("NO_COLOR", None)
    result = subprocess.run(
        [sys.executable, "-m", "tps", *args],
        cwd=cwd,
        env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 2):
        sys.exit(f"tps {' '.join(args)} failed ({result.returncode}):\n{result.stderr}")
    return result.stdout


def prompt_line(command: str) -> str:
    return f"\x1b[32m$\x1b[0m \x1b[1m{command}\x1b[0m\n"


# --------------------------------------------------------------------------
# banner / logo
# --------------------------------------------------------------------------


def lanes(x0: float, x1: float, y0: float, gap: float, lengths, palette) -> str:
    out = []
    for i, (frac, (c1, c2)) in enumerate(zip(lengths, palette, strict=True)):
        y = y0 + i * gap
        end = x0 + (x1 - x0) * frac
        out.append(
            f'<rect x="{x0}" y="{y - 1}" width="{x1 - x0}" height="2" rx="1" fill="#ffffff" opacity="0.06"/>'
        )
        out.append(
            f'<linearGradient id="lane{i}" x1="0" x2="1"><stop offset="0" stop-color="{c1}" stop-opacity="0.15"/>'
            f'<stop offset="1" stop-color="{c2}"/></linearGradient>'
        )
        out.append(
            f'<rect x="{x0}" y="{y - 7}" width="{_num(end - x0)}" height="14" rx="7" fill="url(#lane{i})"/>'
        )
        out.append(f'<circle cx="{_num(end - 7)}" cy="{y}" r="11" fill="{c2}" opacity="0.18"/>')
        out.append(f'<circle cx="{_num(end - 7)}" cy="{y}" r="4.5" fill="#ffffff"/>')
    return "".join(out)


PALETTE = [
    ("#22d3ee", "#34d399"),
    ("#38bdf8", "#22d3ee"),
    ("#818cf8", "#60a5fa"),
    ("#a78bfa", "#818cf8"),
    ("#c084fc", "#a78bfa"),
]


def banner(width: int = 1280, height: int = 400, social: bool = False) -> str:
    left = 88
    word_y = height * 0.5 + 8
    word_size = 168 if not social else 200
    word = text(MONO_XBOLD, "tps", word_size, left, word_y, "#f8fafc", tracking=-4)
    _, word_w = shape(MONO_XBOLD, "tps", word_size, 0, 0, tracking=-4)
    cursor_x = left + word_w + word_size * 0.08
    tag_y = word_y + (64 if not social else 78)
    tag_size = 30 if not social else 36
    tagline = text(
        SANS,
        "One model. Every provider. Measured.",
        tag_size,
        left,
        tag_y,
        "#e2e8f0",
        tracking=-0.3,
    )
    sub = text(
        MONO,
        "ttft · itl · tok/s · load · cost · same-weights check",
        17 if not social else 21,
        left,
        tag_y + (38 if not social else 46),
        "#8b96b5",
    )
    kicker = text(
        MONO_BOLD,
        "LLM PROVIDER BENCHMARK",
        14 if not social else 17,
        left + 2,
        word_y - word_size * 0.78,
        "#22d3ee",
        tracking=3,
    )
    lane_x0, lane_x1 = width * 0.60, width - 90
    lane_top = height * 0.5 - 92 + (0 if not social else -10)
    race = lanes(lane_x0, lane_x1, lane_top, 46, [0.94, 0.78, 0.61, 0.47, 0.33], PALETTE)
    ticks = "".join(
        f'<rect x="{_num(lane_x0 + (lane_x1 - lane_x0) * t / 10)}" y="{_num(lane_top + 4 * 46 + 30)}" width="1" height="{8 if t % 5 == 0 else 4}" fill="#8b96b5" opacity="0.5"/>'
        for t in range(11)
    )
    axis = text(MONO, "tok/s →", 13, lane_x1, lane_top + 4 * 46 + 58, "#8b96b5", anchor="end")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="tps — one model, every provider, measured">
<title>tps — one model, every provider, measured</title>
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1022"/><stop offset="1" stop-color="#111a3a"/></linearGradient>
<radialGradient id="glowA" cx="0.85" cy="0.1" r="0.6"><stop offset="0" stop-color="#7c3aed" stop-opacity="0.35"/><stop offset="1" stop-color="#7c3aed" stop-opacity="0"/></radialGradient>
<radialGradient id="glowB" cx="0.05" cy="1" r="0.55"><stop offset="0" stop-color="#06b6d4" stop-opacity="0.22"/><stop offset="1" stop-color="#06b6d4" stop-opacity="0"/></radialGradient>
<linearGradient id="cursor2" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#a78bfa"/></linearGradient>
<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.1" fill="#ffffff" opacity="0.05"/></pattern>
<clipPath id="frame"><rect width="{width}" height="{height}" rx="{0 if social else 22}"/></clipPath>
</defs>
<g clip-path="url(#frame)">
<rect width="{width}" height="{height}" fill="url(#bg)"/>
<rect width="{width}" height="{height}" fill="url(#glowA)"/>
<rect width="{width}" height="{height}" fill="url(#glowB)"/>
<rect width="{width}" height="{height}" fill="url(#dots)"/>
{kicker}
{word}
<rect x="{_num(cursor_x)}" y="{_num(word_y - word_size * 0.09)}" width="{_num(word_size * 0.5)}" height="{_num(word_size * 0.1)}" rx="3" fill="url(#cursor2)"/>
{tagline}
{sub}
{race}
{ticks}
{axis}
</g>
</svg>
"""


def logo(size: int = 64) -> str:
    bars = []
    for i, (frac, (c1, c2)) in enumerate(zip([0.86, 0.64, 0.42], PALETTE[:3], strict=True)):
        y = 17 + i * 15
        bars.append(
            f'<linearGradient id="l{i}" x1="0" x2="1"><stop offset="0" stop-color="{c1}" stop-opacity="0.35"/>'
            f'<stop offset="1" stop-color="{c2}"/></linearGradient>'
            f'<rect x="12" y="{y}" width="{_num(40 * frac)}" height="9" rx="4.5" fill="url(#l{i})"/>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 64 64" role="img" aria-label="tps">
<rect width="64" height="64" rx="14" fill="#0f1630"/>
{"".join(bars)}
</svg>
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def png_from_svg(svg: str, out: Path, width: int, height: int) -> bool:
    """Rasterise with headless Chromium (Python or Node Playwright, whichever exists)."""
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "page.html"
        page_file.write_text(
            f'<html><body style="margin:0;background:#0b1022">{svg}</body></html>', encoding="utf-8"
        )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            script = (
                "const {chromium}=require('playwright');(async()=>{"
                "const b=await chromium.launch();"
                f"const p=await b.newPage({{viewport:{{width:{width},height:{height}}}}});"
                f"await p.goto({json.dumps(page_file.as_uri())});"
                f"await p.screenshot({{path:{json.dumps(str(out))}}});"
                "await b.close();})().catch(e=>{console.error(e);process.exit(1)});"
            )
            try:
                subprocess.run(["node", "-e", script], check=True, capture_output=True)
                return True
            except (OSError, subprocess.CalledProcessError):
                return False
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(page_file.as_uri())
            page.screenshot(path=str(out))
            browser.close()
        return True


def build() -> dict[str, str]:
    work = Path(tempfile.mkdtemp(prefix="tps-assets-"))
    (work / "targets.mock.toml").write_text((ROOT / "targets.mock.toml").read_text())
    cols = 96
    files: dict[str, str] = {}

    run = capture(["run", "-c", "targets.mock.toml", "--mock", "-o", "runs/demo"], work, cols)
    files["screenshot-run.svg"] = terminal_svg(
        prompt_line("tps run -c targets.mock.toml --mock") + run,
        "tps run  ·  demo data (--mock)",
        cols,
    )
    # fictional providers only: a screenshot must never show a real provider
    # "serving different weights" on synthetic data
    (work / "fingerprint-demo.toml").write_text(
        (ROOT / "examples" / "fingerprint-demo.toml").read_text()
    )
    fp = capture(
        ["fingerprint", "-c", "fingerprint-demo.toml", "--mock", "-o", "runs/fp"], work, cols
    )
    files["screenshot-fingerprint.svg"] = terminal_svg(
        prompt_line("tps fingerprint -c examples/fingerprint-demo.toml --mock") + fp,
        "tps fingerprint  ·  demo data (--mock)",
        cols,
    )
    doctor = capture(["doctor", "-c", "targets.mock.toml"], work, cols)
    files["screenshot-doctor.svg"] = terminal_svg(
        prompt_line("tps doctor -c targets.mock.toml") + doctor, "tps doctor", cols
    )
    # example reports, from the same demo runs (timestamps normalised)
    report = (work / "runs" / "demo" / "report.md").read_text(encoding="utf-8")
    report = re.sub(
        r"- captured: .*",
        "- captured: (demo run — regenerate with scripts/render_assets.py)",
        report,
    )
    files["../../examples/demo-throughput-report.md"] = report
    files["../../examples/demo-fingerprint-report.md"] = (
        work / "runs" / "fp" / "fingerprint.md"
    ).read_text(encoding="utf-8")
    files["banner.svg"] = banner()
    files["social-preview.svg"] = banner(1280, 640, social=True)
    files["logo.svg"] = logo()
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if assets are stale")
    parser.add_argument("--no-png", action="store_true", help="skip the PNG social preview")
    args = parser.parse_args()

    files = build()
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, content in files.items():
        path = OUT / name
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        stale.append(name)
        if not args.check:
            path.resolve().parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if args.check:
        if stale:
            print("stale assets: " + ", ".join(stale) + " — run scripts/render_assets.py")
            return 1
        print("assets up to date")
        return 0
    if not args.no_png:
        if png_from_svg(files["social-preview.svg"], OUT / "social-preview.png", 1280, 640):
            print("wrote docs/assets/social-preview.png")
        else:
            print("skipped social-preview.png (needs Playwright + Chromium)", file=sys.stderr)
    for name in stale:
        print(f"wrote {(OUT / name).resolve().relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
