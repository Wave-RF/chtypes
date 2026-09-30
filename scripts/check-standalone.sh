#!/usr/bin/env bash
# check-standalone.sh — prove the Go SDK builds, vets and passes its registry
# tests from a bare copy of go/: no header beside it, no core repository, no
# environment but a registry directory. That copy is the tree a consumer's
# `go get` produces.
#
#   scripts/check-standalone.sh [--registry <dir>] [--no-artifacts | --require-artifacts]
#   scripts/check-standalone.sh --selftest
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
# are then the core repository's server-truth suites' to prove. --no-artifacts
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

# --selftest drives the census logic (scripts/lib/standalone_census.py)
# directly against synthetic go-test -json logs, needing no registry and no
# `go` on PATH: it proves the --require-artifacts verdict itself, including
# the zero-checked guard (chtypes#225) — "a parent test whose subtests ALL
# skipped" must be refused, not read as "TestGoldens ran". Same discipline as
# check-suite.sh's own --selftest: a checker nobody has seen fail is not a
# checker.
if [ "${1:-}" = "--selftest" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; the selftest cannot run"
  python3 "$SCRIPTS/lib/standalone_census.py" --selftest || exit 1
  exit 0
fi

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
  # The per-user cache is keyed by the ABI revision the SDK speaks — this
  # tree's own header, the number every binding's default follows.
  _abi="$(sed -n 's/^#define CHS_ABI_REVISION \([0-9][0-9]*\)$/\1/p' "$ROOT/include/chtypes.h")"
  case "$_abi" in ''|*[!0-9]*) die "cannot read one CHS_ABI_REVISION from $ROOT/include/chtypes.h" ;; esac
  REG="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/artifacts/abi$_abi/$_os-$_arch"
fi

# A registry PATH is not a fingerprint: the same path holds different builds
# on different days, and a machine that also builds artifacts can hold
# several producer commits worth at once (chtypes#190). Before anything
# runs, say what is actually in the registry this run will use — one line
# per artifact line's manifest fields, the goldens file's identity, and a
# loud (never fatal) WARNING when a line's ABI revision does not match this
# binding's own header.
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; provenance cannot be printed"
if [ "$NO_ARTIFACTS" -eq 1 ]; then
  python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/include/chtypes.h"
else
  python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/include/chtypes.h" --registry "$REG"
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
# Two levels count as "have a registry" (chtypes#284, "Layout rule"): a
# line's flat <minor>/manifest.json, or any OTHER exact patch nested at
# patches/<minor>/<clickhouse_version>/manifest.json — a registry holding
# only the latter must not read as empty.
HAVE_REG=0
if [ -d "$REG" ] && { compgen -G "$REG/*/manifest.json" >/dev/null || compgen -G "$REG/patches/*/*/manifest.json" >/dev/null; }; then
  HAVE_REG=1
fi
if [ "$HAVE_REG" -eq 1 ]; then
  say "go test ./... (untagged, CHTYPES_REGISTRY=$REG, -json into $LOG)"
  [ "$REQUIRE" -eq 0 ] || note "--require-artifacts: TestGoldens must run, and nothing may skip for want of a registry"
elif [ "$REQUIRE" -eq 1 ]; then
  die "--require-artifacts, but no artifact registry at $REG (no <line>/manifest.json, and no patches/<line>/<version>/manifest.json) — run scripts/fetch.sh <line> --dest $REG first"
else
  say "go test ./... (untagged, NO artifact registry at $REG — the registry tests will SKIP by name; -json into $LOG)"
  note "scripts/fetch.sh <line> installs one; --registry or CHTYPES_REGISTRY point at one"
fi
# A (line, platform) pairing the release will NEVER build past this SDK's ABI
# revision (chtypes#150 — darwin-arm64/24.8 today) must not read as this
# checkout's failure: its only served artifact correctly refuses to load, and
# no fetch or retry fixes that. The registry tests name it and skip it,
# rather than let it redden the whole run, but ONLY when it is on the
# release's own SERVED exclusion list (index.json's top-level `unbuildable`
# array) — never a list typed here, and never inferred from a line this
# registry simply does not have. That needs the registry to exist at all, so
# this is skipped in --no-artifacts mode, where every registry test skips for
# want of a registry before it would ever matter.
CHTYPES_UNBUILDABLE=""
if [ "$HAVE_REG" -eq 1 ]; then
  UNBUILDABLE_URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
  UNBUILDABLE_URL="${UNBUILDABLE_URL%/}/artifacts/index.json"
  UNBUILDABLE_JSON="$(curl -fsSL --retry 3 --max-time 30 "$UNBUILDABLE_URL")" \
    || die "could not fetch $UNBUILDABLE_URL to read the served 'unbuildable' exclusion list (chtypes#150) — without it a by-design gap cannot be told apart from a real failure, so this refuses rather than guess"
  CHTYPES_UNBUILDABLE="$(printf '%s' "$UNBUILDABLE_JSON" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print(",".join("%s-%s/%s" % (e["os"], e["arch"], e["clickhouse_minor"]) for e in doc.get("unbuildable", [])))
')" || die "could not read 'unbuildable' out of $UNBUILDABLE_URL"
  if [ -n "$CHTYPES_UNBUILDABLE" ]; then
    note "excluded by design, per $UNBUILDABLE_URL: $CHTYPES_UNBUILDABLE"
  fi
fi
rc=0
# The golden set is SERVED, and a fetch installs it at <registry>/sdk-goldens.json
# — so the bare copy needs no path for it, only the registry it already gets.
# The fetch fixtures are different: they live in this repository
# (tests/fixtures/fetch, docs/guides/fetch.md §9), so the fetch suite is told where they
# are through CHTYPES_FETCH_FIXTURES and skips loudly without them.
( cd "$DEST" && CHTYPES_REGISTRY="$REG" CHTYPES_FETCH_FIXTURES="$ROOT/tests/fixtures/fetch" CHTYPES_UNBUILDABLE="$CHTYPES_UNBUILDABLE" go test -json -count=1 ./... ) > "$LOG" 2>&1 || rc=$?
[ -s "$LOG" ] || die "go test produced no -json output (rc=$rc)"
# The verdict logic lives in scripts/lib/standalone_census.py, not here, so
# `--selftest` above can drive it directly against a synthetic log — the
# zero-checked guard (chtypes#225) in particular is exercised there, offline.
set +e
python3 "$SCRIPTS/lib/standalone_census.py" "$LOG" "$rc" "$HAVE_REG" "$REQUIRE"
census_rc=$?
set -e
[ "$census_rc" -eq 0 ] || die "the standalone suite did not pass its census (go test rc=$rc); see $LOG"
[ "$rc" -eq 0 ] || die "go test exited $rc; see $LOG"
