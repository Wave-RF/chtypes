#!/usr/bin/env bash
# check-truth-namespace.sh — go/chtypes never declares a top-level identifier
# in the name range reserved for the artifact producer's overlay tests
# (issue #127). The rule and why it exists are documented at the top of
# scripts/check-truth-namespace.go, which does the actual parsing; this is
# a thin wrapper, the same shape as this repository's other scripts/*.sh
# entry points.
#
#   scripts/check-truth-namespace.sh              check go/chtypes/*.go
#   scripts/check-truth-namespace.sh --selftest   prove it fires — plants
#                                                 reserved names into a
#                                                 temporary copy, never the
#                                                 real tree, and requires
#                                                 every plant to be caught
#
# STATIC ONLY, on purpose: this parses source text with Go's own syntax
# parser (go/parser, go/ast) — it does not `go build` or `go test` the
# go/chtypes package, needs no artifact, no CGO and no network. See the
# "WHY GO'S OWN PARSER" section of check-truth-namespace.go for why a
# hand-rolled regex/text scan was rejected in favor of this over a grouped
# `var ( … )` / `const ( … )` block.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if ! command -v go >/dev/null 2>&1; then
  echo "go not found on PATH — this check parses go/chtypes/*.go with Go's own parser (go/parser, go/ast); install the Go toolchain (see go/go.mod for the version) to run it." >&2
  exit 1
fi

exec go run "$HERE/scripts/check-truth-namespace.go" "$@"
