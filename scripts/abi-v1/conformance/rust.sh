#!/usr/bin/env bash
# conformance/rust.sh: the Rust leg of v1-abi-conformance
# (.github/workflows/v1-abi.yml). Dispatched once per (toolchain, os) matrix
# entry, with the toolchain already selected on PATH by that job's own setup
# steps (`msrv`, resolved from rust/Cargo.toml's rust-version, or `stable`).
#
#     CHTYPES_ABI1_STUBS=<dir> CHTYPES_ABI1_REPORT=<file> \
#       CHTYPES_ABI1_TOOLCHAIN=<label> scripts/abi-v1/conformance/rust.sh
#
# Runs rust/tests/abi1_conformance.rs, which reads the three env vars above
# itself, loads the stub libraries CHTYPES_ABI1_STUBS names, runs every case
# in tests/fixtures/abi-v1/cases.json, and writes a
# spec/abi-v1/schema/report.schema.json-shaped report to CHTYPES_ABI1_REPORT
# before its final assertion — so a partial report is on disk even when some
# cases fail.
#
# The same cargo invocation also runs the public-API suites (api_v1,
# api_v1_setup, api_v1_registry), which read the same CHTYPES_ABI1_STUBS and
# prove the public layer's plumbing over the stubs: error mapping per status,
# handle lifetimes and zero live handles, the process setup, and the registry
# adapter over a real signed OCI layout. They write no report; this script's
# own exit code is the worst of the test binaries.
#
# goldens_v1_runner (docs/guides/goldens-v1.md) joins them: with only
# CHTYPES_ABI1_STUBS set it runs the hand-written stub goldens through the
# goldens runner, then scripts/goldens-v1/compare.py, which must pass the
# runner's report and must refuse the same report with one planted wrong byte.
set -euo pipefail

: "${CHTYPES_ABI1_STUBS:?CHTYPES_ABI1_STUBS must be set, to the v1-abi-stubs artifact directory}"
: "${CHTYPES_ABI1_REPORT:?CHTYPES_ABI1_REPORT must be set, to the path this leg writes its report to}"
: "${CHTYPES_ABI1_TOOLCHAIN:?CHTYPES_ABI1_TOOLCHAIN must be set, to this leg toolchain label}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

cd "$ROOT/rust"
exec cargo test --locked --features abi-v1 --test abi1_conformance --test api_v1 --test api_v1_setup --test api_v1_registry --test goldens_v1_runner -- --nocapture
