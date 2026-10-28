#!/usr/bin/env bash
# Publish docs/*.md to the repository's GitHub wiki.
#
# docs/ is the single source of truth: wiki pages are generated from it on
# every publish (renamed, links rewritten, sidebar built), so the wiki can
# never drift from the docs or the mkdocs site.
#
# Usage:
#   scripts/publish-wiki.sh                    # defaults to teal-sea/tpsbench
#   scripts/publish-wiki.sh owner/repo
#   scripts/publish-wiki.sh --dry-run [DIR]    # build pages locally, push nothing
#
# Needs the wiki feature enabled (Settings -> Features -> Wikis) and push
# access. GitHub only creates the wiki's git repository after the first page
# exists, so click "Create the first page" once if the clone fails.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS="$ROOT/docs"
REPO="teal-sea/tpsbench"
DRY_RUN=""

if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN="${2:-$ROOT/site-wiki}"
elif [ -n "${1:-}" ]; then
  REPO="$1"
fi

build() {  # build <out_dir>
  python3 - "$DOCS" "$1" "$REPO" <<'PY'
import re
import sys
from pathlib import Path

docs, out, repo = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
# docs file -> wiki page name, in sidebar order
PAGES = {
    "index.md": "Home",
    "getting-started.md": "Getting-Started",
    "cli.md": "CLI-Reference",
    "configuration.md": "Configuration",
    "metrics.md": "Metrics",
    "methodology.md": "Methodology",
    "gateways.md": "Gateways",
    "automation.md": "Automation",
    "troubleshooting.md": "Troubleshooting",
    "faq.md": "FAQ",
}
missing = sorted(p.name for p in docs.glob("*.md") if p.name not in PAGES)
if missing:
    sys.exit(f"docs not mapped to a wiki page: {missing} (add them to PAGES)")

link = re.compile(r"\]\(([a-z0-9-]+\.md)(#[^)]*)?\)")
blob = f"https://github.com/{repo}/blob/main"

def rewrite(match: re.Match) -> str:
    page = PAGES.get(match.group(1))
    return f"]({page}{match.group(2) or ''})" if page else match.group(0)

out.mkdir(parents=True, exist_ok=True)
for name, page in PAGES.items():
    text = link.sub(rewrite, (docs / name).read_text(encoding="utf-8"))
    # repo-relative paths don't resolve inside the wiki; point them at GitHub
    text = re.sub(r"\]\(assets/([^)]+)\)", rf"]({blob}/docs/assets/\1?raw=true)", text)
    text = re.sub(r"\]\(\.\./([^)]+)\)", rf"]({blob}/\1)", text)
    text = re.sub(r'src="\.\./([^"]+)"', rf'src="{blob}/\1?raw=true"', text)
    footer = f"\n\n---\n<sub>Generated from [`docs/{name}`]({blob}/docs/{name}) — edit it there.</sub>\n"
    (out / f"{page}.md").write_text(text.rstrip() + footer, encoding="utf-8")

sidebar = ["**tps**", ""] + [f"- [[{p.replace('-', ' ')}|{p}]]" for p in PAGES.values()]
sidebar += ["", "---", "", f"- [Source](https://github.com/{repo})",
            f"- [AGENTS.md]({blob}/AGENTS.md)"]
(out / "_Sidebar.md").write_text("\n".join(sidebar) + "\n", encoding="utf-8")
print(f"built {len(PAGES)} pages in {out}")
PY
}

if [ -n "$DRY_RUN" ]; then
  build "$DRY_RUN"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! git clone --depth 1 "https://github.com/${REPO}.wiki.git" "$TMP" 2>/dev/null; then
  cat >&2 <<EOF
Could not clone the wiki for ${REPO}.

Either wikis are disabled (https://github.com/${REPO}/settings -> Features ->
Wikis) or the wiki has never been created: open
https://github.com/${REPO}/wiki and click "Create the first page", then
re-run: scripts/publish-wiki.sh ${REPO}
EOF
  exit 1
fi

find "$TMP" -maxdepth 1 -name '*.md' -delete
build "$TMP" >/dev/null

cd "$TMP"
git add -A
if git diff --cached --quiet; then
  echo "wiki already up to date"
  exit 0
fi
git -c user.name="tps docs" -c user.email="noreply@github.com" \
  commit -q -m "docs: sync wiki from docs/"
git push -q origin HEAD
echo "published $(ls -1 *.md | grep -vc '^_') pages to https://github.com/${REPO}/wiki"
