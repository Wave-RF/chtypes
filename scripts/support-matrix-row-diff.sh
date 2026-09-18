#!/usr/bin/env bash
# support-matrix-row-diff.sh — turns two docs/support.md snapshots (before
# and after scripts/support-matrix.sh regenerates the file) into a per-row
# summary of what moved in the "## ClickHouse lines" table.
#
#   scripts/support-matrix-row-diff.sh <before> <after>
#
# A patch bump on an existing line reads very differently from a line
# appearing or disappearing — the first is routine, the second is a real
# event that deserves a human's attention — and only a per-row view makes
# that distinction visible at a glance (issue #100). This is the summary the
# support-matrix-refresh workflow writes to $GITHUB_STEP_SUMMARY when the
# generated table changed; it never touches git or GitHub itself, so it is
# just as usable from a terminal while reproducing a staleness report.
#
# Prints a markdown table to stdout and exits 0 when at least one row in the
# ClickHouse-lines table changed. Exits 1 (message on stderr, nothing on
# stdout) when the two files' tables are identical — a caller should already
# know not to reach for this when nothing changed (a plain `git diff` or
# `support-matrix.sh --check` decides that), so this is a safety net, not
# the primary signal.
set -euo pipefail
die() { echo "support-matrix-row-diff: $*" >&2; exit 1; }

[ $# -eq 2 ] || die "usage: support-matrix-row-diff.sh <before> <after>"
BEFORE="$1"; AFTER="$2"
[ -f "$BEFORE" ] || die "$BEFORE does not exist"
[ -f "$AFTER" ] || die "$AFTER does not exist"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"

python3 - "$BEFORE" "$AFTER" <<'PY'
import re, sys

before_path, after_path = sys.argv[1], sys.argv[2]

# Matches a generated ClickHouse-lines row: | `<line>` | `<exact version>` | <platforms> |
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|")
TABLE = re.compile(r"## ClickHouse lines\n.*?\n\|---\|---\|---\|\n(.*?)\n\n", re.S)

def lines_table(path):
    text = open(path, encoding="utf-8").read()
    m = TABLE.search(text)
    rows = {}
    if not m:
        return rows
    for line in m.group(1).splitlines():
        rm = ROW.match(line)
        if rm:
            rows[rm.group(1)] = rm.group(2)
    return rows

before = lines_table(before_path)
after = lines_table(after_path)

changed = set(before) ^ set(after)
for minor in set(before) & set(after):
    if before[minor] != after[minor]:
        changed.add(minor)

if not changed:
    print("support-matrix-row-diff: no ClickHouse-lines row changed between "
          "%s and %s" % (before_path, after_path), file=sys.stderr)
    sys.exit(1)

def order(minor):
    return tuple(int(p) for p in re.findall(r"\d+", minor))

print("| Line | Change |")
print("|---|---|")
for minor in sorted(changed, key=order):
    if minor not in before:
        print("| `%s` | added: `%s` |" % (minor, after[minor]))
    elif minor not in after:
        print("| `%s` | removed (was `%s`) |" % (minor, before[minor]))
    else:
        print("| `%s` | `%s` → `%s` |" % (minor, before[minor], after[minor]))
PY
