#!/usr/bin/env bash
# ts.sh — the TS leg of the v1-abi-conformance job
# (.github/workflows/v1-abi.yml): run every case in
# tests/fixtures/abi-v<N>/cases.json through ts/src/abi<N>'s loader and generic
# invoke-by-name dispatcher (ts/src/abi<N>/raw.ts's rawCall) against the stub
# libraries scripts/abi-v1/build-stubs.sh built for ABI v<N>, and write a
# spec/abi-v<N>/schema/report.schema.json-shaped report.
#
# N is the major spec/binding-majors.json gives ts at this commit
# (scripts/abi-v1/majors.py get ts): 2 on the `v2` branch, whose TS binding
# reads CHTYPES_ABI2_* and nothing named for ABI v1, and runs test/abi2. The
# workflow sets the variables of the same derived major.
#
# Contract with the caller (the job's own toolchain-setup steps, never this
# script): Node and pnpm are already on PATH, at the leg's own versions, and
# these three are exported:
#
#   CHTYPES_ABI<N>_STUBS      the stub directory (stubs.json + one *.so per variant)
#   CHTYPES_ABI<N>_REPORT     where this script's report.json goes
#   CHTYPES_ABI<N>_TOOLCHAIN  this leg's toolchain label, copied into the report
#
# Without CHTYPES_ABI<N>_STUBS, ts/test/abi<N>/conformance.test.ts skips every
# case LOUDLY by name (vitest reports it SKIPPED) rather than passing or
# failing silently — the same rule every other no-registry test in this
# repository follows — and this script still exits 0 (nothing to report).
#
#     scripts/abi-v1/conformance/ts.sh
#
# With CHTYPES_ABI<N>_STUBS set the run is read back through the stub census
# (scripts/abi-v1/stub_census.py, public issue #463): vitest's own JSON result,
# not its exit code, must show tests passed, and no test file in which nothing
# passed (every file under ts/test/abi<N> gates on the stubs; measured: no other
# ts test file reads the variable).
#
#     scripts/abi-v1/conformance/ts.sh --selftest   prove the census on synthetic results
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TS_DIR="$ROOT/ts"
CENSUS="$HERE/../stub_census.py"

if [ "${1:-}" = "--selftest" ]; then
    command -v python3 >/dev/null 2>&1 || { echo "ts.sh: python3 is not on PATH; the selftest cannot run" >&2; exit 1; }
    python3 "$CENSUS" --selftest
    exit 0
fi

MAJOR="$(python3 "$ROOT/scripts/abi-v1/majors.py" get ts)"
STUBS_VAR="CHTYPES_ABI${MAJOR}_STUBS"
REPORT_VAR="CHTYPES_ABI${MAJOR}_REPORT"
TOOLCHAIN_VAR="CHTYPES_ABI${MAJOR}_TOOLCHAIN"
TESTS="test/abi${MAJOR}"

if [ -z "${!STUBS_VAR:-}" ]; then
    echo "ts.sh: $STUBS_VAR is unset — the conformance test will skip every case loudly by name (ts speaks ABI v$MAJOR, spec/binding-majors.json)"
fi
if [ -z "${!REPORT_VAR:-}" ]; then
    echo "ts.sh: $REPORT_VAR is unset — no report will be written" >&2
fi

cd "$TS_DIR"

# Defensive, not assumed: nothing upstream of this script's own contract
# promises node_modules already exists (only the toolchain itself —
# Node/pnpm on PATH — is promised). pnpm's content-addressable store makes a
# second `install` here a near no-op when it already ran.
echo "ts.sh: pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "ts.sh: ABI v$MAJOR: vitest run $TESTS (toolchain ${!TOOLCHAIN_VAR:-unknown})"
if [ -z "${!STUBS_VAR:-}" ]; then
    pnpm exec vitest run "$TESTS" --reporter=verbose
    exit 0
fi
JSON="$(mktemp "${TMPDIR:-/tmp}/ts-stub-census.XXXXXX")"
trap 'rm -f "$JSON"' EXIT
rc=0
pnpm exec vitest run "$TESTS" --reporter=verbose --reporter=json --outputFile.json="$JSON" || rc=$?
echo "ts.sh: stub census"
census_rc=0
python3 "$CENSUS" vitest "$JSON" "$rc" || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "ts.sh: the stub census refused this run (vitest rc=$rc)" >&2
    exit 1
fi
exit "$rc"
