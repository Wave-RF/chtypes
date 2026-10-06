#!/usr/bin/env bash
# go.sh — the Go leg of the v1-abi-conformance job: run every ABI case in
# tests/fixtures/abi-v<N>/cases.json through go/internal/abi<N>'s generated
# invoke-by-name dispatcher (invoke_gen_test.go) and loader (loader.go)
# against the stub libraries scripts/abi-v1/build-stubs.sh built for ABI v<N>,
# and write a spec/abi-v<N>/schema/report.schema.json-shaped report.
#
#     CHTYPES_ABI<N>_STUBS=<dir> CHTYPES_ABI<N>_REPORT=<file> \
#       [CHTYPES_ABI<N>_TOOLCHAIN=<label>] scripts/abi-v1/conformance/go.sh
#
# N is the major spec/binding-majors.json gives go at this commit
# (scripts/abi-v1/majors.py get go): 2 on the `v2` branch, whose Go binding
# reads CHTYPES_ABI2_* and nothing named for ABI v1. The workflow sets the
# variables of the same derived major.
#
# With CHTYPES_ABI<N>_STUBS set this runs the WHOLE Go suite (go test ./...), so every
# stub-backed test runs, not three named ones, and reads the verdict off the
# go test -json census (scripts/abi-v1/stub_census.py): a stub-skip while the stubs
# are set is a failure, a zero-run is a failure, and TestConformance,
# TestGoldensV1StubSelfCheck, TestSetupCases, TestStub* and TestRegistry* must each
# have passed, by name, in the printed census. (Public issue #463: the three named
# runs left TestStub* and TestRegistry* running in no CI job.)
#
#     scripts/abi-v1/conformance/go.sh --selftest   prove the census on synthetic logs
#
# CHTYPES_ABI<N>_STUBS and CHTYPES_ABI<N>_REPORT are required; without
# CHTYPES_ABI<N>_STUBS, the test itself skips LOUDLY by name (TestConformance)
# rather than this script refusing — exactly as every other no-registry test
# in this repository behaves, so `go test ./...` with no stub directory set
# (scripts/check-standalone.sh's own run) still passes, having run nothing
# for this suite and said so.
#
# This script is invoked from the repository root (the v1-abi-conformance
# job's working directory, per .github/workflows/v1-abi.yml); it does not
# change directory itself beyond `cd go`, so a caller's CHTYPES_ABI<N>_STUBS/
# CHTYPES_ABI<N>_REPORT must be absolute paths (the workflow's own
# ${{ runner.temp }}-rooted paths already are).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
CENSUS="$ROOT/scripts/abi-v1/stub_census.py"

MAJOR="$(python3 "$ROOT/scripts/abi-v1/majors.py" get go)"
STUBS_VAR="CHTYPES_ABI${MAJOR}_STUBS"
REPORT_VAR="CHTYPES_ABI${MAJOR}_REPORT"
TOOLCHAIN_VAR="CHTYPES_ABI${MAJOR}_TOOLCHAIN"

if [ "${1:-}" = "--selftest" ]; then
    command -v python3 >/dev/null 2>&1 || { echo "go.sh: python3 is not on PATH; the selftest cannot run" >&2; exit 1; }
    python3 "$ROOT/scripts/abi-v1/stub_census.py" --selftest
    exit 0
fi

if [ -z "${!REPORT_VAR:-}" ]; then
    echo "go.sh: $REPORT_VAR must be set (where the report is written); go speaks ABI v$MAJOR (spec/binding-majors.json)" >&2
    exit 1
fi
export "$TOOLCHAIN_VAR=${!TOOLCHAIN_VAR:-go.mod}"

cd "$ROOT/go"

if [ -z "${!STUBS_VAR:-}" ]; then
    # No stubs: nothing stub-backed can run. Keep the old loud, exit-0 shape.
    echo "go.sh: $STUBS_VAR is unset; go test ./internal/abi$MAJOR/... -run TestConformance skips loudly by name"
    go test "./internal/abi$MAJOR/..." -run TestConformance -v
    exit 0
fi

# The whole suite with the stubs set. TestConformance writes the report;
# TestGoldensV1StubSelfCheck runs the hand-written stub goldens through the Go
# goldens runner and scripts/goldens-v1/compare.py (it must pass the runner's
# report and refuse the same report with one planted wrong byte); TestSetupCases
# runs the shared process-setup cases over a fresh copy of a stub; TestStub* and
# TestRegistry* are the public API and the registry adapter over the stubs. -race: the
# concurrent stub tests (pooled buffers filled from C memory) run under the race detector.
LOG="$(mktemp "${TMPDIR:-/tmp}/go-stub-census.XXXXXX")"
trap 'rm -f "$LOG"' EXIT
echo "go.sh: ABI v$MAJOR: go test -race ./... -count=1 -json ($STUBS_VAR=${!STUBS_VAR})"
rc=0
go test -race ./... -count=1 -json >"$LOG" 2>&1 || rc=$?

echo "go.sh: stub census"
census_rc=0
python3 "$CENSUS" go "$LOG" "$rc" \
    --require 'TestConformance' \
    --require 'TestGoldensV1StubSelfCheck' \
    --require 'TestSetupCases' \
    --require 'TestStub.*' \
    --require 'TestRegistry.*' \
    --require 'TestABIIdentity' \
    --require 'TestR2.*' \
    --require 'TestR3.*' \
    --require 'TestDevFingerprint.*' || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "go.sh: the stub census refused this run (go test rc=$rc)" >&2
    exit 1
fi
[ "$rc" -eq 0 ] || { echo "go.sh: go test exited $rc" >&2; exit "$rc"; }
