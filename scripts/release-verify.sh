#!/usr/bin/env bash
# release-verify.sh — the post-publish check of every release workflow, in ONE
# place, so the dry run and the tag path cannot drift.
#
#   scripts/release-verify.sh <rust|ts|python|go> registry <version>
#   scripts/release-verify.sh <rust|ts|python|go> local <version> <source>
#   scripts/release-verify.sh --selftest
#
# `registry` installs the PUBLISHED package from its public registry, anonymously,
# retrying for registry lag. `local` installs the package a dry run just built,
# and skips only that retry loop. <source> is, per binding: rust, the crate
# directory; ts, the packed tarball; python, the built wheel; go, the module
# directory (a `replace` points the clean module at it).
#
# Both modes then do the same, in a clean directory with a clean cache:
#   1. the binding's ABI fingerprint constant equals CHS_ABI_FINGERPRINT in
#      include/chtypes.h;
#   2. its CLI runs `list` against the PRODUCTION registry, and the newest
#      two-part line that listing names is parsed out (parse_line, below);
#   3. its CLI fetches that line;
#   4. its public API loads it, and the loaded library's own build_info reports
#      the same fingerprint.
#
# WHY THE PARSER IS HERE. The CLI prints flat lines (`installed <version>
# <platform> <dir>`, `published <spelling> support unknown`), and a parser that
# waited for a header or expected bare `26.9` lines found nothing on 1.0.0's
# first publish (measured, release-rust run 37170693890). The dry run never ran
# this, so the class was invisible before an irreversible publish. Only the
# `published` rows and only their second column are read.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# parse_line: stdin is `chtypes list`; stdout is the newest published two-part
# line, or nothing.
parse_line() {
  awk '$1=="published" && $2 ~ /^[0-9]+\.[0-9]+$/ {print $2}' | sort -V | tail -n 1
}

fail() { echo "::error::$*" >&2; exit 1; }

if [ "${1:-}" = "--selftest" ]; then
  expect() { # name, want, got
    if [ "$2" != "$3" ]; then echo "selftest FAIL: $1: wanted '$2', got '$3'" >&2; exit 1; fi
    echo "selftest ok: $1 -> '$3'"
  }
  flat=$'installed 26.3.1 linux-amd64 /c/26.3.1\npublished 26.3 support unknown\npublished 26.10 support unknown\npublished 26.9 support unknown\npublished 26.8.1 support unknown\npublished latest support unknown'
  expect "newest two-part line, numeric not lexical" "26.10" "$(printf '%s\n' "$flat" | parse_line)"
  expect "installed rows are not published lines" "" "$(printf 'installed 26.3 linux-amd64 /c/26.3\n' | parse_line)"
  expect "three-part spellings are not lines" "" "$(printf 'published 26.8.1 support unknown\n' | parse_line)"
  expect "the old bare form is not what the CLI prints" "" "$(printf '26.9\n' | parse_line)"
  expect "empty listing" "" "$(printf '' | parse_line)"
  exit 0
fi

lang="${1:-}"; mode="${2:-}"; version="${3:-}"; source_arg="${4:-}"
case "$lang" in rust | ts | python | go) ;; *) fail "usage: release-verify.sh <rust|ts|python|go> <registry|local> <version> [source]" ;; esac
case "$mode" in registry) ;; local) [ -n "$source_arg" ] || fail "local mode needs a <source>" ;; *) fail "mode is registry or local, not '$mode'" ;; esac
[ -n "$version" ] || fail "no version"
[ -z "$source_arg" ] || source_arg="$(cd "$(dirname "$source_arg")" && pwd)/$(basename "$source_arg")"

want="$(sed -n 's/^#define CHS_ABI_FINGERPRINT "\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$ROOT/include/chtypes.h")"
[ -n "$want" ] || fail "could not read CHS_ABI_FINGERPRINT from include/chtypes.h"

work="${RUNNER_TEMP:-$(mktemp -d)}/release-verify-$lang"
rm -rf "$work"; mkdir -p "$work"
export CHTYPES_CACHE="$work/cache"
# No credentials, no override of the registry or the trust: exactly a stranger.
unset CHTYPES_ARTIFACTS_URL CHTYPES_TRUSTED_KEYS CHTYPES_ALLOW_UNSIGNED CHTYPES_DOWNLOAD_TOKEN
echo "verifying $lang $version from $mode${source_arg:+ ($source_arg)}, expecting $want"

# retry <seconds> <what> <command...>: the registry-lag loop. Registry mode only.
retry() {
  local limit="$1" what="$2"; shift 2
  local deadline=$((SECONDS + limit))
  until "$@" > "$work/install.log" 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      tail -n 20 "$work/install.log"
      fail "$what did not become installable within ${limit}s"
    fi
    echo "$what is not installable yet; retrying in 30s"
    sleep 30
  done
}

check_fingerprint() { # got
  echo "binding fingerprint: $1"
  echo "header fingerprint:  $want"
  [ "$1" = "$want" ] || fail "the binding speaks '$1', include/chtypes.h says '$want'"
}

# list_and_fetch <chtypes-binary>: steps 2 and 3. Sets $line.
list_and_fetch() {
  local bin="$1" listing
  listing="$("$bin" list)"
  echo "--- chtypes list (production) ---"
  printf '%s\n' "$listing"
  echo "---------------------------------"
  line="$(printf '%s\n' "$listing" | parse_line)"
  [ -n "$line" ] || fail "the production registry lists no line (no 'published <major.minor> ...' row)"
  echo "parser picked: $line"
  "$bin" fetch "$line"
}

loaded_ok() { # loaded fingerprint
  echo "loaded fingerprint: $1"
  [ "$1" = "$want" ] || fail "the loaded library reports '$1', expected '$want'"
  echo "OK: $lang $version ($mode) speaks $want, fetched $line and loaded it"
}

case "$lang" in
  rust)
    if [ "$mode" = registry ]; then
      retry 900 "chtypes $version" cargo install "chtypes@=$version" --locked --root "$work/bin"
    else
      cargo install --path "$source_arg" --locked --root "$work/bin"
    fi
    cd "$work"
    cargo init --name chtypes-verify --vcs none >/dev/null 2>&1
    if [ "$mode" = registry ]; then cargo add "chtypes@=$version" >/dev/null 2>&1; else cargo add chtypes --path "$source_arg" >/dev/null 2>&1; fi
    manifest="$(cargo metadata --format-version 1 | python3 -c 'import json,sys; print(next(p["manifest_path"] for p in json.load(sys.stdin)["packages"] if p["name"] == "chtypes"))')"
    got="$(sed -n 's/^ *"\(sha256:[0-9a-f]*\)";$/\1/p' "$(dirname "$manifest")/src/abi1/decls.rs" | head -n 1)"
    check_fingerprint "$got"
    list_and_fetch "$work/bin/bin/chtypes"
    cat > src/main.rs <<RS
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let registry = chtypes::Registry::new(chtypes::RegistryOptions::default())?;
    let lib = registry.for_version("$line")?;
    println!("{}", lib.build_info().abi_fingerprint);
    Ok(())
}
RS
    loaded_ok "$(cargo run --quiet)"
    ;;
  ts)
    cd "$work"
    : > .npmrc   # no token
    echo '{"name":"verify","private":true}' > package.json
    if [ "$mode" = registry ]; then
      retry 3600 "@wavehouse/chtypes@$version" npm install --no-audit --no-fund --userconfig ./.npmrc "@wavehouse/chtypes@$version"
    else
      npm install --no-audit --no-fund --userconfig ./.npmrc "$source_arg"
    fi
    # The constant lives in the generated layer, which the entry point does not re-export: import it by file URL.
    got="$(node --input-type=module -e "
      import { pathToFileURL } from 'node:url';
      import path from 'node:path';
      const m = await import(pathToFileURL(path.resolve('node_modules/@wavehouse/chtypes/dist/abi1/decls.gen.js')).href);
      process.stdout.write(m.ABI_FINGERPRINT);
    ")"
    check_fingerprint "$got"
    list_and_fetch "$work/node_modules/.bin/chtypes"
    "$work/node_modules/.bin/chtypes" verify
    loaded="$(LINE="$line" node --input-type=module -e "
      import { Registry } from '@wavehouse/chtypes';
      const registry = await Registry.open();
      const lib = await registry.for(process.env.LINE);
      const type = lib.validateType('UInt8').toString();
      if (type !== 'UInt8') throw new Error('validateType(UInt8) answered ' + JSON.stringify(type));
      process.stdout.write(lib.buildInfo.abiFingerprint);
    ")"
    loaded_ok "$loaded"
    ;;
  python)
    cd "$work"
    export UV_NO_CONFIG=1
    unset PIP_INDEX_URL UV_INDEX_URL UV_EXTRA_INDEX_URL
    uv venv --quiet --python 3.13 "$work/venv"
    if [ "$mode" = registry ]; then
      retry 900 "chtypes==$version" uv pip install --quiet --python "$work/venv/bin/python" --refresh --no-cache "chtypes==$version"
    else
      uv pip install --quiet --python "$work/venv/bin/python" "$source_arg"
    fi
    py="$work/venv/bin/python"
    check_fingerprint "$("$py" -c 'from chtypes._abi1 import _decls; print(_decls.CHS_ABI_FINGERPRINT)')"
    list_and_fetch "$work/venv/bin/chtypes"
    loaded="$(LINE="$line" "$py" -c '
import os
import chtypes
library = chtypes.Registry().for_version(os.environ["LINE"])
assert library.validate_type("UInt8") == b"UInt8"
print(library.build_info.abi_fingerprint)
')"
    loaded_ok "$loaded"
    ;;
  go)
    mkdir -p "$work/mod" && cd "$work/mod"
    export GOPROXY="${GOPROXY:-https://proxy.golang.org}"
    go mod init published.example >/dev/null 2>&1
    if [ "$mode" = registry ]; then
      ok=0
      for attempt in $(seq 1 20); do
        if go get "github.com/wave-rf/chtypes/go@v$version" > "$work/install.log" 2>&1; then ok=1; break; fi
        echo "attempt $attempt: $version is not served yet; waiting for the proxy"
        sleep 30
      done
      [ "$ok" = 1 ] || { tail -n 20 "$work/install.log"; fail "the proxy never served $version"; }
      dir="$(go list -m -f '{{.Dir}}' github.com/wave-rf/chtypes/go)"
      GOBIN="$work/bin" go install "github.com/wave-rf/chtypes/go/cmd/chtypes@v$version"
    else
      go mod edit -replace "github.com/wave-rf/chtypes/go=$source_arg"
      dir="$source_arg"
      (cd "$source_arg" && go build -o "$work/bin/chtypes" ./cmd/chtypes)
    fi
    got="$(sed -n 's/^const ChsAbiFingerprint = "\(sha256:[0-9a-f]*\)".*/\1/p' "$dir/internal/abi1/abi_gen.go")"
    check_fingerprint "$got"
    list_and_fetch "$work/bin/chtypes"
    cat > main.go <<'GO'
package main

import (
	"fmt"
	"os"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func main() {
	reg, err := chtypes.NewRegistry()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	lib, err := reg.For(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer schema.Close()
	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":256}`))
	if err != nil || batch.Outcome != chtypes.Accepted || len(batch.Rows) != 1 || len(batch.Rows[0].Transformed) != 1 || batch.Rows[0].Transformed[0].Reason != chtypes.ReasonOverflowWrap {
		fmt.Fprintf(os.Stderr, "unexpected answer: %+v, %v\n", batch, err)
		os.Exit(1)
	}
	fmt.Println(lib.BuildInfo().ABIFingerprint)
}
GO
    go mod tidy >/dev/null
    loaded_ok "$(go run . "$line")"
    ;;
esac
