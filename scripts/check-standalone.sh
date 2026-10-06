#!/usr/bin/env bash
# check-standalone.sh — prove the Go SDK builds, vets and passes its tests from
# a bare copy of go/: no header beside it, no core repository, no environment
# but what the tests start themselves. That copy is the tree a consumer's
# `go get` produces.
#
#   scripts/check-standalone.sh [--no-artifacts]
#   scripts/check-standalone.sh --selftest
#
# WHY. The default build is dlopen-only and the linked path sits behind the
# `chtypes_linked` tag — but "the default build needs nothing" is a claim about
# a tree this checkout can never be, because here include/ always sits two
# levels up. So this copies go/ to a scratch directory with nothing around it,
# builds and vets there, proves the linked API is UNDEFINED without the tag,
# then runs the untagged suite under `-race` and reads its verdict off the
# `go test -json` census (scripts/lib/standalone_census.py): the build's exit
# is only the first gate, and a suite that ran zero assertions fails. Tests
# that need something this bare copy does not have (the fetch fixtures, a stub
# library) SKIP by name in the census; the repository's own jobs
# (v1-conformance, v1-abi-conformance) run them with those inputs.
#
# --no-artifacts reproduces a contributor's machine: an empty XDG_CACHE_HOME
# and no $CHTYPES_REGISTRY, so this machine's own cache is invisible.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-standalone: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

# --selftest drives the census logic directly against synthetic go-test -json
# logs, needing no `go` on PATH. A checker nobody has seen fail is not a
# checker.
if [ "${1:-}" = "--selftest" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; the selftest cannot run"
  python3 "$SCRIPTS/lib/standalone_census.py" --selftest || exit 1
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --no-artifacts)
      SCRATCH_CACHE="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-no-artifacts.XXXXXX")"
      export XDG_CACHE_HOME="$SCRATCH_CACHE"
      unset CHTYPES_REGISTRY CHTYPES_AUTOFETCH
      say "--no-artifacts: XDG_CACHE_HOME=$SCRATCH_CACHE (empty), CHTYPES_REGISTRY unset" ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done
command -v go >/dev/null 2>&1 || die "go is not on PATH; the check cannot run and must not be reported as passing"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; the census cannot run"
SRC="$ROOT/go"
[ -d "$SRC/chtypes" ] || die "no Go SDK at $SRC"

# Say which ABI this binding was built against (scripts/lib/provenance.py):
# the header of the major spec/binding-majors.json gives go
# (scripts/abi-v1/majors.py; include/v2/chtypes.h once go speaks ABI v2).
python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/$(python3 "$SCRIPTS/abi-v1/majors.py" header go)"

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

func TestProbeLinkedAbsent(t *testing.T) { _, _ = OpenLinked() }
GO
if ( cd "$DEST" && go vet ./... ) >/dev/null 2>&1; then
  rm -f "$probe"; die "OpenLinked compiled WITHOUT chtypes_linked — the linked path leaked into the default build"
fi
rm -f "$probe"
note "OpenLinked is undefined without the tag (as it must be)"

LOG_DIR="$ROOT/scratch"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/check-standalone.json"
say "go test -race ./... (untagged, -json into $LOG)"
rc=0
( cd "$DEST" && go test -race -json -count=1 ./... ) > "$LOG" 2>&1 || rc=$?
[ -s "$LOG" ] || die "go test produced no -json output (rc=$rc)"
# The verdict logic lives in scripts/lib/standalone_census.py, not here, so
# `--selftest` above can drive it directly against a synthetic log.
set +e
python3 "$SCRIPTS/lib/standalone_census.py" "$LOG" "$rc"
census_rc=$?
set -e
[ "$census_rc" -eq 0 ] || die "the standalone suite did not pass its census (go test rc=$rc); see $LOG"
[ "$rc" -eq 0 ] || die "go test exited $rc; see $LOG"
