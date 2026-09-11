#!/usr/bin/env bash
# lint-actions.sh — actionlint over .github/workflows/*.yml (expression/type
# errors, action-input mismatches, SHA-pin syntax, and shellcheck over every
# embedded `run:` block), plus shellcheck directly over this repository's
# standalone shell scripts, which actionlint never sees.
#
#   scripts/lint-actions.sh
#
# The ShellCheck binary itself is not installed here: ubuntu-latest (this
# repository's only CI runner, per .github/workflows/ci.yml's own header)
# ships it preinstalled, and it is a common local dev-machine tool too —
# install it yourself (`brew install shellcheck` / your distro's package) if
# `command -v shellcheck` fails below. actionlint is pure Go, so it is
# `go install`'d on demand into .bin/, pinned, the same way
# scripts/lint-prose.sh handles misspell.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "shellcheck not found on PATH — install it (ubuntu-latest ships it; elsewhere: your package manager)." >&2
  exit 1
fi

ACTIONLINT_VERSION="v1.7.8"
LOCAL_BIN="$ROOT/.bin"
ACTIONLINT="$LOCAL_BIN/actionlint-$ACTIONLINT_VERSION"

if [ ! -x "$ACTIONLINT" ]; then
  echo "==> Installing actionlint $ACTIONLINT_VERSION" >&2
  mkdir -p "$LOCAL_BIN"
  GOBIN="$LOCAL_BIN" go install "github.com/rhysd/actionlint/cmd/actionlint@$ACTIONLINT_VERSION"
  mv "$LOCAL_BIN/actionlint" "$ACTIONLINT"
fi

echo "==> actionlint (workflows, with embedded shellcheck)" >&2
"$ACTIONLINT" -shellcheck "$(command -v shellcheck)"

echo "==> shellcheck (every tracked *.sh)" >&2
# Tracked-file discovery, not a hard-coded directory list: a shell script can
# land anywhere in the tree (today: scripts/, playground/) and this must
# keep finding it after a directory rename.
mapfile -t sh_files < <(git ls-files '*.sh')
if [ "${#sh_files[@]}" -eq 0 ]; then
  echo "no tracked *.sh files found" >&2
  exit 1
fi
shellcheck -x -P SCRIPTDIR --severity=warning "${sh_files[@]}"
