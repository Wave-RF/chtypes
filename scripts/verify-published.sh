#!/usr/bin/env bash
# verify-published.sh — install a just-published binding FROM ITS PUBLIC REGISTRY,
# in a clean room, and make it speak. The last step of every release workflow.
#
#   scripts/verify-published.sh go|python|ts|rust <version> [--timeout <seconds>]
#
# WHY THIS EXISTS. A publish step exiting 0 does not mean anyone can install the
# package. Measured on ts/v0.1.1 (issue #10): pnpm printed "Published" and the
# job went green at ~12:17Z, npm's packument did not list the version until
# 12:21Z, and the TARBALL did not serve until ~12:24Z. For about two minutes in
# between, `dist-tags.latest` pointed at a version whose tarball 404'd, so a
# clean `npm install` failed outright — while CI showed a green release. A
# publish that never completes looks exactly the same.
#
# WHY INSTALL, RATHER THAN PROBE A URL. An HTTP 200 proves presence, not
# usability. Installing the way a consumer does and then importing proves the
# artifact resolves, downloads, satisfies its own dependency metadata, and
# speaks the ABI this repository builds against — which is the claim a release
# actually makes. The ABI revision is read from include/chtypes.h rather than
# hardcoded, so this cannot drift from the header the bindings are pinned to.
#
# WHY ANONYMOUS. A registry can show a maintainer a version the public cannot
# see. On a developer laptop ~/.npmrc may carry a token, and `npm view` will
# then report a version nobody else can install — which is what made #10 take
# three wrong turns to diagnose. Every fetch here is made with no credentials.
#
# WHY IT RETRIES. Propagation is real and uneven: ~7 minutes end to end on the
# one npm sample we have, and not atomic. The retry bound is generous on
# purpose; a spurious red release is worse than a slow green one.
set -euo pipefail
die() { echo "verify-published: $*" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMEOUT=900
ECO=""; VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)  [ $# -ge 2 ] || die "--timeout needs a value"; TIMEOUT="$2"; shift ;;
    -h|--help)  sed -n '2,36p' "$0"; exit 0 ;;
    -*)         die "unknown flag: $1" ;;
    *)          if [ -z "$ECO" ]; then ECO="$1"; elif [ -z "$VERSION" ]; then VERSION="$1"; else die "unexpected argument: $1"; fi ;;
  esac
  shift
done
[ -n "$ECO" ] && [ -n "$VERSION" ] || die "usage: verify-published.sh go|python|ts|rust <version> [--timeout <seconds>]"
case "$ECO" in go|python|ts|rust) ;; *) die "unknown ecosystem: $ECO" ;; esac

# The ABI revision the published package must report, from the header that owns it.
WANT_ABI="$(sed -n 's/^#define CHS_ABI_REVISION \([0-9][0-9]*\)$/\1/p' "$HERE/include/chtypes.h")"
[ -n "$WANT_ABI" ] || die "could not read CHS_ABI_REVISION from include/chtypes.h"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-verify.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
echo "verify-published: $ECO $VERSION — expecting ABI revision $WANT_ABI, up to ${TIMEOUT}s"

# One attempt at "install it and make it talk". Prints the ABI revision the
# installed package reports, or fails. Each runs in a directory of its own with
# no credentials in scope.
attempt() {
  local d="$WORK/try"; rm -rf "$d"; mkdir -p "$d"; cd "$d"
  case "$ECO" in
    go)
      # The proxy is lazy: this fetch is also what warms it. The checksum
      # database still verifies the module, so this is a real resolution test.
      go mod init chtypes.verify >/dev/null
      GOFLAGS=-mod=mod go get "github.com/wave-rf/chtypes/go@v$VERSION" >/dev/null
      cat > main.go <<'GO'
package main

import (
	"fmt"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func main() { fmt.Println(chtypes.ABIRevision) }
GO
      go run . ;;
    python)
      uv venv .venv >/dev/null 2>&1
      VIRTUAL_ENV=.venv uv pip install --no-cache "chtypes==$VERSION" >/dev/null 2>&1
      VIRTUAL_ENV=.venv uv run --no-project python -c 'import chtypes;print(chtypes.ABI_REVISION)' ;;
    ts)
      printf '{ "name": "v", "version": "1.0.0", "type": "module", "private": true }\n' > package.json
      : > .npmrc   # no token: exactly what a stranger gets
      npm install --no-audit --no-fund --userconfig ./.npmrc "@wavehouse/chtypes@$VERSION" >/dev/null 2>&1
      node -e "import('@wavehouse/chtypes').then(m=>console.log(m.ABI_REVISION))" ;;
    rust)
      cargo init --name chtypes-verify >/dev/null 2>&1
      cargo add "chtypes@=$VERSION" >/dev/null 2>&1
      cat > src/main.rs <<'RS'
fn main() { println!("{}", chtypes::ABI_REVISION); }
RS
      cargo run --quiet ;;
  esac
}

deadline=$(( $(date +%s) + TIMEOUT ))
n=0
while :; do
  n=$((n + 1))
  got="$(attempt 2>"$WORK/err" || true)"
  got="$(printf '%s' "$got" | tr -d '[:space:]')"
  if [ -n "$got" ]; then
    [ "$got" = "$WANT_ABI" ] \
      || die "$ECO $VERSION installed from its registry but reports ABI revision '$got', not $WANT_ABI — the published artifact disagrees with include/chtypes.h"
    echo "verify-published: OK — $ECO $VERSION installs from its public registry and reports ABI revision $got (attempt $n)"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "verify-published: last error was:" >&2
    tail -15 "$WORK/err" >&2 || true
    die "$ECO $VERSION did not become installable from its public registry within ${TIMEOUT}s.
  The publish step reported success, so the registry accepted it and has not served it.
  npm:      the version may be staged — 'npm stage list' then 'npm stage approve <id>' (npm >= 11, 2FA).
            If the dist-tag already moved, installs are BROKEN: 'npm dist-tag add <pkg>@<last-good> latest'.
  PyPI:     check the project's Files tab; a partially-uploaded release blocks reuse of the version.
  crates.io the index lags the API; if it never appears the publish did not land.
  Go:       the proxy fetches lazily, so this failing means the tag itself does not resolve."
  fi
  echo "  attempt $n: not installable yet ($(( deadline - $(date +%s) ))s left)"
  sleep 15
done
