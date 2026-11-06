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
# api_v1_setup, api_v1_setup_cases, reader_rules, null_input, no_unload) and the crate's own unit
# tests (--lib: the registry over a real signed OCI layout lives there, because
# only a unit test reaches the fetch layer's test-only seam), which read the
# same CHTYPES_ABI<N>_STUBS and prove the public layer's plumbing over the
# stubs: error mapping per status, handle lifetimes and zero live handles, the
# process setup and the shared setup cases (tests/fixtures/abi-v<N>/setup-cases.json),
# the registry adapter, and ABI v2's reader rules (r2, r3) and dev
# fingerprint refusal (r6). They write no report; this script's own exit code is
# the worst of the test targets'.
#
# goldens_v1_runner (docs/guides/goldens-v1.md) joins them: with only
# CHTYPES_ABI<N>_STUBS set it runs the hand-written stub goldens through the
# goldens runner, then scripts/goldens-v1/compare.py, which must pass the
# runner's report and must refuse the same report with one planted wrong byte.
#
# Every stub-backed test binary is named below (measured, public issue #463: these
# are all the rust test files that read CHTYPES_ABI<N>_STUBS). Each target runs as
# its own `cargo test`, its output and exit code kept apart, and the run is read
# back through the stub census (scripts/abi-v1/stub_census.py cargo): libtest's own
# result lines must add up to its own summary of each target, no loud SKIP line may
# name CHTYPES_ABI<N>_STUBS (these suites skip by early return, so the line is the
# evidence), and the tests that passed must be exactly the ones
# tests/fixtures/abi-v<N>/stub-census/rust.txt pins (public issue #727).
#
# --show-output, never --nocapture: libtest captures each test's own output and
# prints it, under the test's name, after the result lines (a failing test's in
# its `failures:` section, a passing test's in `successes:`, where a loud SKIP
# line is read). Under --nocapture a test's own lines, printed from another test
# thread, split libtest's `test <name> ... ok` lines, and the count drifted
# between identical runs (measured: 177 to 182 over five runs, against
# libtest's own 182 each time; public issue #727).
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
OUT="$(mktemp -d "${TMPDIR:-/tmp}/rust-stub-census.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
# `lib` is --lib; every other name is a --test target. goldens_v1_runner has no
# libtest harness (harness = false): it ignores the trailing arguments, and the
# census counts it as one test, passed when it exits 0.
TARGETS=(lib "abi${MAJOR}_conformance" api_v1 api_v1_setup api_v1_setup_cases reader_rules null_input no_unload goldens_v1_runner)
rc=0
echo "rust.sh: ABI v$MAJOR: cargo test, one target at a time ($STUBS_VAR=${!STUBS_VAR})"
for target in "${TARGETS[@]}"; do
    if [ "$target" = lib ]; then
        sel=(--lib)
    else
        sel=(--test "$target")
    fi
    target_rc=0
    cargo test --locked --features abi-v1 "${sel[@]}" -- --show-output >"$OUT/$target.log" 2>&1 || target_rc=$?
    echo "$target_rc" >"$OUT/$target.rc"
    cat "$OUT/$target.log"
    echo "rust.sh: $target exited $target_rc"
    if [ "$target_rc" -ne 0 ]; then
        rc=$target_rc
    fi
done
echo "rust.sh: stub census"
census_rc=0
python3 "$HERE/../stub_census.py" cargo "$OUT" "$rc" --binding rust || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "rust.sh: the stub census refused this run (cargo rc=$rc)" >&2
    exit 1
fi
exit "$rc"
