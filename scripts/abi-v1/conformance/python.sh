#!/usr/bin/env bash
# scripts/abi-v1/conformance/python.sh: the Python binding's v1-abi-conformance
# leg. .github/workflows/v1-abi.yml dispatches here with these exported:
#
#   CHTYPES_ABI<N>_STUBS      the stub directory build-stubs.sh produced (stubs.json, *.so)
#   CHTYPES_ABI<N>_REPORT     where to write the report (spec/abi-v<N>/schema/report.schema.json)
#   CHTYPES_ABI<N>_TOOLCHAIN  this leg's Python version ("3.11", "3.13", "3.14")
#
# N is the major spec/binding-majors.json gives python at this commit
# (scripts/abi-v1/majors.py get python): 2 on the `v2` branch, whose Python
# binding reads CHTYPES_ABI2_* and nothing named for ABI v1, and runs
# python/tests/abi2. The workflow sets the variables of the same derived major.
#
# and astral-sh/setup-uv already on PATH, pinned to that exact interpreter
# (its own "set up uv, at this leg's Python version" step). This script is a
# thin, uniform entry point: python/tests/abi<N> (plain pytest tests) is what
# actually runs every case and decides pass/fail, and
# python/tests/abi<N>/conftest.py's pytest_sessionfinish hook is what writes
# CHTYPES_ABI<N>_REPORT, as a BY-PRODUCT of the real run rather than a second,
# independently maintained notion of the result.
#
# Locally, with no CHTYPES_ABI<N>_STUBS, python/tests/abi<N> skips every case
# loudly by name and this exits 0 (the same "no registry, no run" convention
# every other test in this repository follows) -- those tests are also part
# of the ordinary `uv run pytest -q` suite, so that is true of it too.
#
# With CHTYPES_ABI<N>_STUBS set the run is read back through the stub census
# (scripts/abi-v1/stub_census.py, public issue #463): pytest's own junit result,
# not its exit code, must show tests passed, no skip naming CHTYPES_ABI<N>_STUBS,
# and no test file in which nothing passed. At ABI v2 the reader rules (r2, r3)
# and the dev fingerprint refusal (r6) are REQUIRED to have run and passed. Every
# test that needs the stubs lives under python/tests/abi<N> (measured: no other
# python test file reads the variable).
#
#     scripts/abi-v1/conformance/python.sh --selftest   prove the census on synthetic results
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="$(cd "$HERE/../../../python" && pwd)"
CENSUS="$HERE/../stub_census.py"

if [ "${1:-}" = "--selftest" ]; then
    command -v python3 >/dev/null 2>&1 || { echo "python.sh: python3 is not on PATH; the selftest cannot run" >&2; exit 1; }
    python3 "$CENSUS" --selftest
    exit 0
fi

MAJOR="$(python3 "$HERE/../majors.py" get python)"
STUBS_VAR="CHTYPES_ABI${MAJOR}_STUBS"
TOOLCHAIN_VAR="CHTYPES_ABI${MAJOR}_TOOLCHAIN"
STUBS="${!STUBS_VAR:-}"
TOOLCHAIN="${!TOOLCHAIN_VAR:-}"
SUITE="tests/abi$MAJOR"

cd "$PYTHON_DIR"
if [ -z "$STUBS" ]; then
    echo "python.sh: $STUBS_VAR is unset; $SUITE skips loudly by name (python speaks ABI v$MAJOR, spec/binding-majors.json)"
    if [ -n "$TOOLCHAIN" ]; then
        uv run --python "$TOOLCHAIN" pytest "$SUITE" -q
    else
        uv run pytest "$SUITE" -q
    fi
    exit 0
fi

REQUIRE=()
if [ "$MAJOR" -ge 2 ]; then
    REQUIRE=(
        --require "tests\.abi$MAJOR\.test_reader_rules::test_r2_.*"
        --require "tests\.abi$MAJOR\.test_reader_rules::test_r3_.*"
        --require "tests\.abi$MAJOR\.test_dev_fingerprint::test_dev_fingerprint_.*"
    )
fi

XML="$(mktemp "${TMPDIR:-/tmp}/py-stub-census.XXXXXX")"
trap 'rm -f "$XML"' EXIT
echo "python.sh: ABI v$MAJOR: pytest $SUITE ($STUBS_VAR=$STUBS)"
rc=0
if [ -n "$TOOLCHAIN" ]; then
    uv run --python "$TOOLCHAIN" pytest "$SUITE" -q -rs --junitxml="$XML" || rc=$?
else
    uv run pytest "$SUITE" -q -rs --junitxml="$XML" || rc=$?
fi
echo "python.sh: stub census"
census_rc=0
python3 "$CENSUS" junit "$XML" "$rc" ${REQUIRE[@]+"${REQUIRE[@]}"} || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "python.sh: the stub census refused this run (pytest rc=$rc)" >&2
    exit 1
fi
exit "$rc"
