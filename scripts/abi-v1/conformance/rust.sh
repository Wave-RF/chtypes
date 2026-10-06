#!/usr/bin/env bash
# conformance/rust.sh: the Rust leg of v1-abi-conformance
# (.github/workflows/v1-abi.yml). Dispatched once per (toolchain, os) matrix
# entry, with the toolchain already selected on PATH by that job's own setup
# steps (`msrv`, resolved from rust/Cargo.toml's rust-version, or `stable`).
#
#     CHTYPES_ABI<N>_STUBS=<dir> CHTYPES_ABI<N>_REPORT=<file> \
#       CHTYPES_ABI<N>_TOOLCHAIN=<label> scripts/abi-v1/conformance/rust.sh
#
# N is the major spec/binding-majors.json gives rust at this commit
# (scripts/abi-v1/majors.py get rust): 2 on the `v2` branch, whose Rust
# binding reads CHTYPES_ABI2_* and nothing named for ABI v1. The workflow sets
# the variables of the same derived major.
#
# Runs rust/tests/abi<N>_conformance.rs, which reads the three env vars above
# itself, loads the stub libraries CHTYPES_ABI<N>_STUBS names, runs every case
# in tests/fixtures/abi-v<N>/cases.json, and writes a
# spec/abi-v<N>/schema/report.schema.json-shaped report to CHTYPES_ABI<N>_REPORT
# before its final assertion — so a partial report is on disk even when some
# cases fail.
#
# The same cargo invocation also runs the public-API suites (api_v1,
# api_v1_setup, api_v1_setup_cases, reader_rules) and the crate's own unit
# tests (--lib: the registry over a real signed OCI layout lives there, because
# only a unit test reaches the fetch layer's test-only seam), which read the
# same CHTYPES_ABI<N>_STUBS and prove the public layer's plumbing over the
# stubs: error mapping per status, handle lifetimes and zero live handles, the
# process setup and the shared setup cases (tests/fixtures/abi-v<N>/setup-cases.json),
# the registry adapter, and ABI v2's reader rules (r2, r3) and dev
# fingerprint refusal (r6). They write no report; this script's own exit code is
# the worst of the test binaries.
#
# goldens_v1_runner (docs/guides/goldens-v1.md) joins them: with only
# CHTYPES_ABI<N>_STUBS set it runs the hand-written stub goldens through the
# goldens runner, then scripts/goldens-v1/compare.py, which must pass the
# runner's report and must refuse the same report with one planted wrong byte.
#
# Every stub-backed test binary is named below (measured, public issue #463: these
# are all the rust test files that read CHTYPES_ABI<N>_STUBS). The run is read back through
# the stub census (scripts/abi-v1/stub_census.py): libtest's own result lines must show tests
# passed and no loud SKIP line may name CHTYPES_ABI<N>_STUBS (these suites skip by
# early return, so the line is the evidence).
#
#     scripts/abi-v1/conformance/rust.sh --selftest   prove the census on synthetic output
set -euo pipefail

if [ "${1:-}" = "--selftest" ]; then
    command -v python3 >/dev/null 2>&1 || { echo "rust.sh: python3 is not on PATH; the selftest cannot run" >&2; exit 1; }
    python3 "$(dirname "${BASH_SOURCE[0]}")/../stub_census.py" --selftest
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

MAJOR="$(python3 "$ROOT/scripts/abi-v1/majors.py" get rust)"
for var in STUBS REPORT TOOLCHAIN; do
    name="CHTYPES_ABI${MAJOR}_${var}"
    if [ -z "${!name:-}" ]; then
        echo "rust.sh: $name must be set (the stub directory, the report path and the toolchain label); rust speaks ABI v$MAJOR (spec/binding-majors.json)" >&2
        exit 1
    fi
done
STUBS_VAR="CHTYPES_ABI${MAJOR}_STUBS"

cd "$ROOT/rust"
OUT="$(mktemp "${TMPDIR:-/tmp}/rust-stub-census.XXXXXX")"
trap 'rm -f "$OUT"' EXIT
rc=0
echo "rust.sh: ABI v$MAJOR: cargo test ($STUBS_VAR=${!STUBS_VAR})"
cargo test --locked --features abi-v1 --lib --test "abi${MAJOR}_conformance" --test api_v1 --test api_v1_setup --test api_v1_setup_cases --test reader_rules --test goldens_v1_runner -- --nocapture >"$OUT" 2>&1 || rc=$?
cat "$OUT"
echo "rust.sh: stub census"
census_rc=0
python3 "$HERE/../stub_census.py" text "$OUT" "$rc" || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "rust.sh: the stub census refused this run (cargo rc=$rc)" >&2
    exit 1
fi
exit "$rc"
