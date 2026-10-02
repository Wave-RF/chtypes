#!/usr/bin/env bash
# ts.sh — the TS leg of the v1-abi-conformance job
# (.github/workflows/v1-abi.yml): run every case in
# tests/fixtures/abi-v1/cases.json through ts/src/abi1's loader and generic
# invoke-by-name dispatcher (ts/src/abi1/raw.ts's rawCall) against the stub
# libraries scripts/abi-v1/build-stubs.sh built, and write a
# spec/abi-v1/schema/report.schema.json-shaped report.
#
# Contract with the caller (the job's own toolchain-setup steps, never this
# script): Node and pnpm are already on PATH, at the leg's own versions, and
# these three are exported:
#
#   CHTYPES_ABI1_STUBS      the stub directory (stubs.json + one *.so per variant)
#   CHTYPES_ABI1_REPORT     where this script's report.json goes
#   CHTYPES_ABI1_TOOLCHAIN  this leg's toolchain label, copied into the report
#
# Without CHTYPES_ABI1_STUBS, ts/test/abi1/conformance.test.ts skips every
# case LOUDLY by name (vitest reports it SKIPPED) rather than passing or
# failing silently — the same rule every other no-registry test in this
# repository follows — and this script still exits 0 (nothing to report).
#
#     scripts/abi-v1/conformance/ts.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TS_DIR="$ROOT/ts"

if [ -z "${CHTYPES_ABI1_STUBS:-}" ]; then
    echo "ts.sh: CHTYPES_ABI1_STUBS is unset — the conformance test will skip every case loudly by name"
fi
if [ -z "${CHTYPES_ABI1_REPORT:-}" ]; then
    echo "ts.sh: CHTYPES_ABI1_REPORT is unset — no report will be written" >&2
fi

cd "$TS_DIR"

# Defensive, not assumed: nothing upstream of this script's own contract
# promises node_modules already exists (only the toolchain itself —
# Node/pnpm on PATH — is promised). pnpm's content-addressable store makes a
# second `install` here a near no-op when it already ran.
echo "ts.sh: pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "ts.sh: vitest run test/abi1 (toolchain ${CHTYPES_ABI1_TOOLCHAIN:-unknown})"
pnpm exec vitest run test/abi1 --reporter=verbose
