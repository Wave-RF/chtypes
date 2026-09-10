#!/usr/bin/env bash
# check-standalone.sh — prove the Go SDK builds, vets and passes its registry
# tests from a bare copy of go/: no header beside it, no core repository, no
# environment but a registry directory. That copy is the tree a consumer's
# `go get` produces.
#
#   scripts/check-standalone.sh [--registry <dir>] [--no-artifacts | --require-artifacts]
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
#
# Without a registry — a hosted CI runner, a fresh clone — the suite still
# runs: the fetch, fixture and CLI tests need no artifact, and every test that
# does SKIPS, by name, in the census; the golden set and the registry suite
# are then the core repository's certify workflow's to prove. --no-artifacts
# reproduces that runner here: an empty XDG_CACHE_HOME and no
# $CHTYPES_REGISTRY, so this machine's own cache is invisible.
# --require-artifacts is the opposite rule, for CI's artifact-backed run:
# TestGoldens must have RUN, and no test may have skipped for want of one.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-standalone: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

REG="${CHTYPES_REGISTRY:-}"
NO_ARTIFACTS=0; REQUIRE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) [ $# -ge 2 ] || die "--registry needs a value"; REG="$2"; shift ;;
    --no-artifacts) NO_ARTIFACTS=1 ;;
    --require-artifacts) REQUIRE=1 ;;
    -h|--help)  sed -n '2,29p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done
[ "$NO_ARTIFACTS" -eq 0 ] || [ "$REQUIRE" -eq 0 ] || die "--no-artifacts and --require-artifacts contradict each other"
if [ "$NO_ARTIFACTS" -eq 1 ]; then
  [ -z "$REG" ] || [ -z "${CHTYPES_REGISTRY:-}" ] || die "--no-artifacts and a registry (--registry / CHTYPES_REGISTRY) contradict each other"
  REG=""
  SCRATCH_CACHE="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-no-artifacts.XXXXXX")"
  export XDG_CACHE_HOME="$SCRATCH_CACHE"
  unset CHTYPES_REGISTRY CHTYPES_AUTOFETCH
  say "--no-artifacts: XDG_CACHE_HOME=$SCRATCH_CACHE (empty), CHTYPES_REGISTRY unset"
  for sysroot in /usr/local/share/chtypes/artifacts /opt/chtypes/artifacts; do
    [ ! -d "$sysroot" ] || note "WARNING: $sysroot exists and is on the search path; this run is not artifact-free"
  done
fi
command -v go >/dev/null 2>&1 || die "go is not on PATH; the check cannot run and must not be reported as passing"
SRC="$ROOT/go"
[ -d "$SRC/chtypes" ] || die "no Go SDK at $SRC"

if [ -z "$REG" ]; then
  _os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in x86_64|amd64) _arch=amd64 ;; arm64|aarch64) _arch=arm64 ;; *) _arch="$(uname -m)" ;; esac
  REG="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/artifacts/$_os-$_arch"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-standalone.XXXXXX")"
trap 'rm -rf "$TMP" "${SCRATCH_CACHE:-}"' EXIT
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
# Two modes, decided by one fact and said out loud. With a registry the whole
# untagged suite runs. Without one the tests that need an artifact SKIP, by
# name, in the census below, and the rest still runs. What is never allowed
# is the third state: a suite that ran nothing and passed.
HAVE_REG=0
if [ -d "$REG" ] && compgen -G "$REG/*/manifest.json" >/dev/null; then HAVE_REG=1; fi
if [ "$HAVE_REG" -eq 1 ]; then
  say "go test ./... (untagged, CHTYPES_REGISTRY=$REG, -json into $LOG)"
  [ "$REQUIRE" -eq 0 ] || note "--require-artifacts: TestGoldens must run, and nothing may skip for want of a registry"
elif [ "$REQUIRE" -eq 1 ]; then
  die "--require-artifacts, but no artifact registry at $REG (no <line>/manifest.json) — run scripts/fetch.sh <line> --dest $REG first"
else
  say "go test ./... (untagged, NO artifact registry at $REG — the registry tests will SKIP by name; -json into $LOG)"
  note "scripts/fetch.sh <line> installs one; --registry or CHTYPES_REGISTRY point at one"
fi
rc=0
# The golden set is SERVED, and a fetch installs it at <registry>/sdk-goldens.json
# — so the bare copy needs no path for it, only the registry it already gets.
# The fetch fixtures are different: they live in this repository
# (spec/fixtures/fetch, docs/fetch.md §9), so the fetch suite is told where they
# are through CHTYPES_FETCH_FIXTURES and skips loudly without them.
( cd "$DEST" && CHTYPES_REGISTRY="$REG" CHTYPES_FETCH_FIXTURES="$ROOT/spec/fixtures/fetch" go test -json -count=1 ./... ) > "$LOG" 2>&1 || rc=$?
[ -s "$LOG" ] || die "go test produced no -json output (rc=$rc)"
set +e
python3 - "$LOG" "$rc" "$HAVE_REG" "$REQUIRE" <<'PY'
import json, sys
log, rc, have_reg, require = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1", sys.argv[4] == "1"
ran = skip = fail = 0; skips = []; noise = []; failed = []; output = {}; passed = set()
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
    if a == "pass": ran += 1; passed.add(t)
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
if require:
    # The artifact-backed run's rule, read off the same census: the golden
    # set ran, and no test skipped for the one reason artifacts remove.
    if "TestGoldens" not in passed: problems.append("TestGoldens did not pass with artifacts required")
    starved = [t for t in skips if any("no chtypes artifacts under" in l for l in output.get(t, []))]
    if starved: problems.append("%d test(s) skipped for want of a registry with artifacts required: %s" % (len(starved), ", ".join(starved)))
if problems:
    print("  VERDICT: NOT a pass —"); [print("    * " + p) for p in problems]; sys.exit(1)
mode = "" if have_reg else " (no artifact registry: %d test(s) skipped by name above)" % skip
print("  VERDICT: pass — the dlopen-only SDK built, vetted and ran %d assertions from a bare copy%s" % (ran, mode))
PY
census_rc=$?
set -e
[ "$census_rc" -eq 0 ] || die "the standalone suite did not pass its census (go test rc=$rc); see $LOG"
[ "$rc" -eq 0 ] || die "go test exited $rc; see $LOG"
