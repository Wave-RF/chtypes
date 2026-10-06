#!/usr/bin/env bash
# scripts/abi-v1/conformance/python.sh: the Python binding's v1-abi-conformance
# leg. .github/workflows/v1-abi.yml dispatches here with these exported:
#
#   CHTYPES_ABI1_STUBS      the stub directory build-stubs.sh produced (stubs.json, *.so)
#   CHTYPES_ABI1_REPORT     where to write the report (spec/abi-v1/schema/report.schema.json)
#   CHTYPES_ABI1_TOOLCHAIN  this leg's Python version ("3.11", "3.13", "3.14")
#
# and astral-sh/setup-uv already on PATH, pinned to that exact interpreter
# (its own "set up uv, at this leg's Python version" step). This script is a
# thin, uniform entry point: python/tests/abi1 (plain pytest tests) is what
# actually runs every case and decides pass/fail, and
# python/tests/abi1/conftest.py's pytest_sessionfinish hook is what writes
# CHTYPES_ABI1_REPORT, as a BY-PRODUCT of the real run rather than a second,
# independently maintained notion of the result.
#
# Locally, with no CHTYPES_ABI1_STUBS, python/tests/abi1 skips every case
# loudly by name and this exits 0 (the same "no registry, no run" convention
# every other test in this repository follows) -- those tests are also part
# of the ordinary `uv run pytest -q` suite, so that is true of it too.
#
# With CHTYPES_ABI1_STUBS set the run is read back through the stub census
# (scripts/abi-v1/stub_census.py, public issue #463): pytest's own junit result,
# not its exit code, must show tests passed, no skip naming CHTYPES_ABI1_STUBS,
# and no test file in which nothing passed. Every test that needs the stubs lives
# under python/tests/abi1 (measured: no other python test file reads the variable).
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

cd "$PYTHON_DIR"
if [ -z "${CHTYPES_ABI1_STUBS:-}" ]; then
    if [ -n "${CHTYPES_ABI1_TOOLCHAIN:-}" ]; then
        uv run --python "$CHTYPES_ABI1_TOOLCHAIN" pytest tests/abi1 -q
    else
        uv run pytest tests/abi1 -q
    fi
    exit 0
fi

XML="$(mktemp "${TMPDIR:-/tmp}/py-stub-census.XXXXXX")"
trap 'rm -f "$XML"' EXIT
rc=0
if [ -n "${CHTYPES_ABI1_TOOLCHAIN:-}" ]; then
    uv run --python "$CHTYPES_ABI1_TOOLCHAIN" pytest tests/abi1 -q -rsP --junitxml="$XML" || rc=$?
else
    uv run pytest tests/abi1 -q -rsP --junitxml="$XML" || rc=$?
fi
echo "python.sh: stub census"
census_rc=0
python3 "$CENSUS" junit "$XML" "$rc" || census_rc=$?
if [ "$census_rc" -ne 0 ]; then
    echo "python.sh: the stub census refused this run (pytest rc=$rc)" >&2
    exit 1
fi
exit "$rc"
