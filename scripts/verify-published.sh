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
# three wrong turns to diagnose. Every fetch here is made with no credentials,
# including the measurement fetches this script makes on a timeout (below).
#
# WHY IT RETRIES, AND WHY npm GETS LONGER THAN THE REST. Propagation is real
# and uneven, and npm in particular does not keep to its own early samples.
# ts/v0.1.1 took ~7 minutes end to end, ts/v0.2.2 ~5 minutes, ts/v0.3.1 ~5
# minutes — then ts/v0.4.0 (2026-09-30) took ~26 minutes, with no human
# action anywhere in that window (no approval queue, no settings change; the
# registry owner confirmed it). This script's old flat 900s (15 min) bound
# went red on that publish even though it was entirely good, and the
# now-stale failure text it printed sent a human into npm account settings
# looking for a stuck approval that did not exist. go, python and rust have
# all passed on attempt 1-2 every time this has been measured, so they keep
# the 900s default; ts defaults to 3600s (one hour) instead. `--timeout`
# overrides either default explicitly, for a one-off run. A spurious red
# release is worse than a slow green one.
set -euo pipefail
die() { echo "verify-published: $*" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMEOUT=""
ECO=""; VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)  [ $# -ge 2 ] || die "--timeout needs a value"; TIMEOUT="$2"; shift ;;
    -h|--help)  sed -n '2,40p' "$0"; exit 0 ;;
    -*)         die "unknown flag: $1" ;;
    *)          if [ -z "$ECO" ]; then ECO="$1"; elif [ -z "$VERSION" ]; then VERSION="$1"; else die "unexpected argument: $1"; fi ;;
  esac
  shift
done
[ -n "$ECO" ] && [ -n "$VERSION" ] || die "usage: verify-published.sh go|python|ts|rust <version> [--timeout <seconds>]"
case "$ECO" in go|python|ts|rust) ;; *) die "unknown ecosystem: $ECO" ;; esac

# Per-ecosystem default, only when the caller did not pass --timeout: ts
# (npm) gets an hour, the rest keep the bound that has held so far. See the
# "WHY IT RETRIES" paragraph above for the measurement behind this split.
if [ -z "$TIMEOUT" ]; then
  case "$ECO" in
    ts) TIMEOUT=3600 ;;
    *)  TIMEOUT=900 ;;
  esac
fi

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

# On a timeout, print what actually happened rather than lead with a guess.
# npm exposes exactly the two facts a human reaches for by hand — whether the
# registry's PACKUMENT lists the version at all, and whether its `latest`
# dist-tag already moved — in one anonymous request, plus a distinct TARBALL
# existence check in a second. Only npm gets this; the other three registries
# have no equivalent public "list of versions plus a distinct file existence
# check" this script can reach anonymously as cheaply, so their hints below
# name the one URL/command worth checking by hand instead.
ts_measured_facts() {
  local version="$1" pkg="@wavehouse/chtypes"
  local packument listed latest tarball_url tarball_status
  packument="$(curl -fsS --max-time 20 "https://registry.npmjs.org/$pkg" 2>/dev/null || true)"
  if [ -n "$packument" ]; then
    listed="$(printf '%s' "$packument" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print("yes" if sys.argv[1] in d.get("versions", {}) else "no")
except Exception:
    print("(packument did not parse)")
' "$version" 2>/dev/null || echo "(packument did not parse)")"
    latest="$(printf '%s' "$packument" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("dist-tags", {}).get("latest", "(no dist-tags.latest)"))
except Exception:
    print("(packument did not parse)")
' 2>/dev/null || echo "(packument did not parse)")"
  else
    listed="(packument fetch failed)"
    latest="(packument fetch failed)"
  fi
  tarball_url="https://registry.npmjs.org/$pkg/-/chtypes-$version.tgz"
  tarball_status="$(curl -s -o /dev/null --max-time 20 -w '%{http_code}' "$tarball_url" 2>/dev/null || echo "(fetch failed)")"
  cat <<EOF
  measured just now, anonymously, against the public registry:
    packument lists $version: $listed
    dist-tags.latest: $latest
    tarball ($tarball_url): HTTP $tarball_status
EOF
}

start=$(date +%s)
deadline=$(( start + TIMEOUT ))
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
    elapsed=$(( $(date +%s) - start ))
    header="$ECO $VERSION did not become installable from its public registry within ${TIMEOUT}s (${elapsed}s elapsed, $n attempts).
  The publish step reported success, so the registry accepted it and has not served it yet — or something is actually wrong. What follows is measured, not assumed, where this script can measure it."
    case "$ECO" in
      ts)
        die "$header
$(ts_measured_facts "$VERSION")
  Possible causes, in order of likelihood:
    - still propagating: the common case — ts/v0.4.0 took ~26 minutes with
      zero human action anywhere in that window. Re-run only the verify job
      later; the publish itself already succeeded and must not be repeated
      ('npm publish' over an existing version is refused).
    - if 'packument lists' above still says no after roughly an hour: the
      version may be staged for manual approval — check with
      'npm stage list', then 'npm stage approve <id>' (npm >= 11, 2FA).
    - if 'dist-tags.latest' above already reports $VERSION but the tarball
      line above is not 'HTTP 200': installs are broken right now for
      everyone, not just this check — point latest back at a working
      version with
      'npm dist-tag add @wavehouse/chtypes@<last-good-version> latest'
      while this is investigated."
        ;;
      python)
        die "$header
  Check https://pypi.org/project/chtypes/$VERSION/#files directly: if it
  lists this version's files, PyPI is still propagating — re-run only the
  verify job. If it 404s, the upload did not complete; PyPI refuses to
  reuse a version number, so the release needs a new one."
        ;;
      rust)
        die "$header
  Check https://crates.io/api/v1/crates/chtypes/$VERSION directly: if that
  returns the version, crates.io's index is lagging its own publish API —
  re-run only the verify job. If it still 404s well past this timeout,
  check crates.io's status page before concluding the publish failed."
        ;;
      go)
        die "$header
  Check whether the tag itself resolves:
  'git ls-remote https://github.com/wave-rf/chtypes go/v$VERSION'. If the
  tag is there, the module proxy fetches lazily and only this first fetch
  failed — retry
  'GOPROXY=https://proxy.golang.org go get github.com/wave-rf/chtypes/go@v$VERSION'
  by hand. If the tag is not there, the tag push itself did not land."
        ;;
    esac
  fi
  if [ $(( $(date +%s) - start )) -ge 600 ]; then
    echo "  attempt $n: not installable yet ($(( deadline - $(date +%s) ))s left, polling every 30s now)"
    sleep 30
  else
    echo "  attempt $n: not installable yet ($(( deadline - $(date +%s) ))s left)"
    sleep 15
  fi
done
