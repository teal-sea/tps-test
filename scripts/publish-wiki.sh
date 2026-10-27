#!/usr/bin/env bash
# Publish ./wiki/*.md to the repository's GitHub wiki.
#
# Prerequisites: the wiki feature must be enabled for the repo
#   Settings -> Features -> Wikis  (or: gh api -X PATCH repos/OWNER/REPO -F has_wiki=true)
# and you need push access to it.
#
# Usage:
#   scripts/publish-wiki.sh                 # defaults to teal-sea/tps-test
#   scripts/publish-wiki.sh owner/repo
set -euo pipefail

REPO="${1:-teal-sea/tps-test}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/wiki"

[ -d "$SRC" ] || { echo "no wiki/ directory in $ROOT" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! git clone --depth 1 "https://github.com/${REPO}.wiki.git" "$TMP" 2>/dev/null; then
  cat >&2 <<EOF
Could not clone the wiki for ${REPO}.

The usual cause is that wikis are disabled for the repository:

  https://github.com/${REPO}/settings   ->  Features  ->  Wikis  [x]

or the wiki has never been created (GitHub only materialises the wiki git
repository after the first page exists — click "Create the first page").

Then re-run: scripts/publish-wiki.sh ${REPO}
EOF
  exit 1
fi

find "$TMP" -maxdepth 1 -name '*.md' -delete
cp "$SRC"/*.md "$TMP"/

cd "$TMP"
git add -A
if git diff --cached --quiet; then
  echo "wiki already up to date"
  exit 0
fi
git -c user.name="tps-test" -c user.email="noreply@github.com" \
  commit -q -m "docs: sync wiki from repo"
git push -q origin master 2>/dev/null || git push -q origin main
echo "published $(ls -1 *.md | wc -l | tr -d ' ') pages to https://github.com/${REPO}/wiki"
