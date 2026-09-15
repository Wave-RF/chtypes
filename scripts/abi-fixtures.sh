#!/usr/bin/env bash
# abi-fixtures.sh — build the wrong-revision ABI fixture set against THIS
# repository's include/chtypes.h, for the four suites' refusal tests (#36).
#
#   scripts/abi-fixtures.sh --out DIR [--generator PATH]
#
# WHY. docs/reference/artifact.md step 5 says an artifact answering an
# unexpected chs_abi_revision() MUST be refused, naming both numbers. A text
# match between the header and each binding's constant (the `abi` CI job)
# proves the constant, not the refusal, and every published artifact is at the
# current revision, so no test ever meets a mismatching one. This builds one:
# a stub library generated from the header, laid out as two one-artifact
# registries — `wrong-revision/` answering CHS_ABI_REVISION + 1, and
# `at-revision/` answering CHS_ABI_REVISION, the control that proves a refusal
# came from the revision check and not from an unloadable fixture.
#
# WHERE THE GENERATOR COMES FROM. It is served, not tracked: the signed release
# carries it inside sdk-fetch-fixtures.tar.gz under abi-revision/, and
# scripts/fetch.sh --release-file installs that tarball through the same
# signature and sha256 chain as an artifact. --generator names a gen.py you
# already have instead (it must be that same generator).
#
# WHAT IT HANDS BACK. DIR, for $CHTYPES_ABI_FIXTURES: fixture.json,
# wrong-revision/ and at-revision/. The generator loads both libraries and
# checks what they answer before it writes fixture.json; this script then
# refuses a result whose numbers disagree with the header it was given, so a
# fixture that would prove nothing never reaches a suite. The last line of
# output is `CHTYPES_ABI_FIXTURES=DIR`.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
HEADER="$ROOT/include/chtypes.h"
die() { printf 'abi-fixtures: %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

OUT=""; GEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out)       [ $# -ge 2 ] || die "--out needs a directory"; OUT="$2"; shift ;;
    --generator) [ $# -ge 2 ] || die "--generator needs a path"; GEN="$2"; shift ;;
    -h|--help)   sed -n '2,29p' "$0"; exit 0 ;;
    *)           die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$OUT" ] || die "--out DIR is required"
[ -f "$HEADER" ] || die "no header at $HEADER"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; the generator cannot run"
command -v "${CC:-cc}" >/dev/null 2>&1 || die "no C compiler (${CC:-cc}) on PATH; the stub library cannot be built"

# --out is replaced wholesale, so it must be empty, absent, or a previous
# fixture set — never a directory that holds anything else.
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ] && [ ! -f "$OUT/fixture.json" ]; then
  die "$OUT exists and is not a fixture set (no fixture.json); refusing to replace it"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-abi-fixtures-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

if [ -z "$GEN" ]; then
  say "the generator, from the signed release (sdk-fetch-fixtures.tar.gz)"
  "$SCRIPTS/fetch.sh" --release-file sdk-fetch-fixtures.tar.gz --dest "$WORK"
  tar -xzf "$WORK/sdk-fetch-fixtures.tar.gz" -C "$WORK" abi-revision/gen.py 2>/dev/null \
    || die "the served sdk-fetch-fixtures.tar.gz carries no abi-revision/gen.py — the release predates the fixture"
  GEN="$WORK/abi-revision/gen.py"
fi
[ -f "$GEN" ] || die "no generator at $GEN"

rm -rf "$OUT"
say "building against $HEADER"
python3 "$GEN" build --out "$OUT" --header "$HEADER" ${CC:+--cc "$CC"}

python3 - "$OUT" "$HEADER" <<'PY'
import json, re, sys
from pathlib import Path
out, header = Path(sys.argv[1]), Path(sys.argv[2])
def refuse(why): sys.exit("abi-fixtures: %s — not handing this fixture to a suite" % why)
m = re.search(r"^\s*#\s*define\s+CHS_ABI_REVISION\s+(\d+)\b", header.read_text(), re.M)
if not m: refuse("%s defines no CHS_ABI_REVISION" % header)
want = int(m.group(1))
try: doc = json.loads((out / "fixture.json").read_text())
except Exception as exc: refuse("fixture.json is missing or unreadable: %s" % exc)
if doc.get("abi_revision") != want: refuse("fixture.json says abi_revision %r but the header says %d" % (doc.get("abi_revision"), want))
if doc.get("mismatch_revision") != want + 1: refuse("fixture.json says mismatch_revision %r, not %d" % (doc.get("mismatch_revision"), want + 1))
for d in ("wrong-revision", "at-revision"):
    if not (out / d / doc.get("clickhouse_minor", "0.0") / "manifest.json").is_file():
        refuse("%s/ holds no artifact manifest" % d)
print("abi-fixtures: header revision %d, wrong-revision answers %d, at-revision answers %d" % (want, want + 1, want), file=sys.stderr)
PY

echo "CHTYPES_ABI_FIXTURES=$OUT"
