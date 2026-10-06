#!/usr/bin/env bash
# go.sh — the Go leg of the v1-abi-conformance job: run every ABI case in
# tests/fixtures/abi-v1/cases.json through go/internal/abi1's generated
# invoke-by-name dispatcher (invoke_gen_test.go) and loader (loader.go)
# against the stub libraries scripts/abi-v1/build-stubs.sh built, and write a
# spec/abi-v1/schema/report.schema.json-shaped report.
#
#     CHTYPES_ABI1_STUBS=<dir> CHTYPES_ABI1_REPORT=<file> \
#       [CHTYPES_ABI1_TOOLCHAIN=<label>] scripts/abi-v1/conformance/go.sh
#
# CHTYPES_ABI1_STUBS and CHTYPES_ABI1_REPORT are required; without
# CHTYPES_ABI1_STUBS, the test itself skips LOUDLY by name (TestConformance)
# rather than this script refusing — exactly as every other no-registry test
# in this repository behaves, so `go test ./...` with no stub directory set
# (scripts/check-standalone.sh's own run) still passes, having run nothing
# for this suite and said so.
#
# This script is invoked from the repository root (the v1-abi-conformance
# job's working directory, per .github/workflows/v1-abi.yml); it does not
# change directory itself beyond `cd go`, so a caller's CHTYPES_ABI1_STUBS/
# CHTYPES_ABI1_REPORT must be absolute paths (the workflow's own
# ${{ runner.temp }}-rooted paths already are).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

: "${CHTYPES_ABI1_REPORT:?CHTYPES_ABI1_REPORT must be set (where the report is written)}"
export CHTYPES_ABI1_TOOLCHAIN="${CHTYPES_ABI1_TOOLCHAIN:-go.mod}"

cd "$ROOT/go"
echo "go.sh: go test ./internal/abi1/... -run TestConformance (CHTYPES_ABI1_STUBS=${CHTYPES_ABI1_STUBS:-<unset>})"
go test ./internal/abi1/... -run TestConformance -v

# The goldens runner's stub self-check (docs/guides/goldens-v1.md): with the same
# CHTYPES_ABI1_STUBS it runs the hand-written stub goldens through the Go goldens
# runner, then scripts/goldens-v1/compare.py, which must pass the runner's report
# and refuse the same report with one planted wrong byte. -v prints the PASS line.
echo "go.sh: go test ./chtypes -run TestGoldensV1StubSelfCheck"
go test ./chtypes -run TestGoldensV1StubSelfCheck -count=1 -v

# The process setup's shared public-API cases (tests/fixtures/abi-v1/setup-cases.json,
# which all four bindings run): each case through the public API over a fresh
# copy of a stub. -v prints each case's PASS line.
echo "go.sh: go test ./chtypes -run TestSetupCases"
go test ./chtypes -run TestSetupCases -count=1 -v
