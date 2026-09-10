#!/usr/bin/env bash
# check-standalone.sh — prove the Go SDK builds, vets and passes its registry
# tests from a bare copy of go/: no header beside it, no core repository, no
# environment but a registry directory. That copy is the tree a consumer's
# `go get` produces.
#
#   scripts/check-standalone.sh [--registry <dir>]
#
# WHY. The package once linked -lchtypes out of a build tree at compile time
# even for a caller that only dlopens artifacts, so it was unbuildable outside
# the repository that built the library. The default build is now dlopen-only
# and the linked path sits behind the `chtypes_linked` tag — but "the default
# build needs nothing" is a claim about a tree this checkout can never be,
# because here include/ always sits two levels up. So this copies go/ to a
# scratch directory with nothing around it, builds and vets there, proves the
# linked API is UNDEFINED without the tag, then runs the untagged suite against
# a registry ($CHTYPES_REGISTRY, --registry, or the per-user cache). Every
# verdict is read off an output: the build's exit is only the first gate, the
# -json census is the second, and a suite that ran zero assertions fails.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-standalone: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

REG="${CHTYPES_REGISTRY:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) [ $# -ge 2 ] || die "--registry needs a value"; REG="$2"; shift ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done
command -v go >/dev/null 2>&1 || die "go is not on PATH; the check cannot run and must not be reported as passing"
SRC="$ROOT/go"
[ -d "$SRC/chtypes" ] || die "no Go SDK at $SRC"

if [ -z "$REG" ]; then
  _os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in x86_64|amd64) _arch=amd64 ;; arm64|aarch64) _arch=arm64 ;; *) _arch="$(uname -m)" ;; esac
  REG="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/artifacts/$_os-$_arch"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-standalone.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
DEST="$TMP/go"
mkdir -p "$DEST"
( cd "$SRC" && tar --exclude='./.git' -cf - . ) | ( cd "$DEST" && tar -xf - )
[ ! -e "$TMP/include" ] || die "scratch tree unexpectedly has an include/ next to go/"

say "go build ./... (dlopen-only, in $DEST — nothing beside it)"
( cd "$DEST" && go build ./... ) || die "the dlopen-only build FAILED outside the repository"
say "go vet ./..."
( cd "$DEST" && go vet ./... ) || die "go vet failed on the dlopen-only build"

say "the linked API is absent without the tag"
probe="$DEST/chtypes/zz_probe_test.go"
cat > "$probe" <<'GO'
package chtypes

import "testing"

func TestProbeLinkedAbsent(t *testing.T) { _, _ = CompileDDL("", "x UInt8") }
GO
if ( cd "$DEST" && go vet ./... ) >/dev/null 2>&1; then
  rm -f "$probe"; die "CompileDDL compiled WITHOUT chtypes_linked — the linked path leaked into the default build"
fi
rm -f "$probe"
note "CompileDDL is undefined without the tag (as it must be)"

LOG_DIR="$ROOT/scratch"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/check-standalone.json"
[ -d "$REG" ] || die "no artifact registry at $REG — run scripts/fetch.sh, or pass --registry / set CHTYPES_REGISTRY"
say "go test ./... (untagged, CHTYPES_REGISTRY=$REG, -json into $LOG)"
rc=0
# The golden set lives in this repository (goldens/), not in the Go module, so
# the bare copy is told where it is; without that the golden test skips by
# name, which the census would print — but here it must RUN. The same goes
# for the fetch fixtures (spec/fixtures/fetch, docs/fetch.md §9): the fetch
# suite reaches them through CHTYPES_FETCH_FIXTURES and skips loudly without.
( cd "$DEST" && CHTYPES_REGISTRY="$REG" CHTYPES_GOLDENS="$ROOT/goldens/cases.json" CHTYPES_FETCH_FIXTURES="$ROOT/spec/fixtures/fetch" go test -json -count=1 ./... ) > "$LOG" 2>&1 || rc=$?
[ -s "$LOG" ] || die "go test produced no -json output (rc=$rc)"
set +e
python3 - "$LOG" "$rc" <<'PY'
import json, sys
log, rc = sys.argv[1], int(sys.argv[2])
ran = skip = fail = 0; skips = []; noise = []; failed = []; output = {}
for line in open(log, encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line.startswith("{"):
        if line: noise.append(line)
        continue
    try: ev = json.loads(line)
    except ValueError: noise.append(line); continue
    t, a = ev.get("Test"), ev.get("Action")
    if not t: continue
    if a == "output": output.setdefault(t, []).append(ev.get("Output", "").rstrip("\n")); continue
    if a == "pass": ran += 1
    elif a == "fail": ran += 1; fail += 1; failed.append(t)
    elif a == "skip": skip += 1; skips.append(t)
print("  standalone census — %d ran, %d skipped, %d failed" % (ran, skip, fail))
for t in skips: print("    SKIPPED", t)
# A failure is named, and its own last words are quoted — a count alone sends
# whoever reads the log to fetch the -json file, which CI does not keep.
for t in failed:
    print("    FAILED", t)
    tail = [l for l in output.get(t, []) if l.strip() and not l.startswith("=== RUN") and not l.startswith("--- FAIL")][-12:]
    for l in tail: print("      | " + l[:200])
for n in noise[:20]: print("    " + n[:160])
problems = []
if fail: problems.append("%d test(s) failed" % fail)
if ran == 0: problems.append("zero tests ran — the untagged suite asserted nothing")
if rc != 0 and not problems: problems.append("go test exited %d with no failing record; read %s" % (rc, log))
if problems:
    print("  VERDICT: NOT a pass —"); [print("    * " + p) for p in problems]; sys.exit(1)
print("  VERDICT: pass — the dlopen-only SDK built, vetted and ran %d assertions from a bare copy" % ran)
PY
census_rc=$?
set -e
[ "$census_rc" -eq 0 ] || die "the standalone registry suite did not pass its census (go test rc=$rc); see $LOG"
[ "$rc" -eq 0 ] || die "go test exited $rc; see $LOG"
