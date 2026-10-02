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
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="$(cd "$HERE/../../../python" && pwd)"

cd "$PYTHON_DIR"
if [ -n "${CHTYPES_ABI1_TOOLCHAIN:-}" ]; then
    uv run --python "$CHTYPES_ABI1_TOOLCHAIN" pytest tests/abi1 -q
else
    uv run pytest tests/abi1 -q
fi
