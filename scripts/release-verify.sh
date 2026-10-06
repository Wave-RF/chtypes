#!/usr/bin/env bash
# release-verify.sh — the post-publish check of every release workflow, in ONE
# place, so the dry run and the tag path cannot drift. On this branch it
# verifies a release of the channel scripts/release-channel.sh defines (until
# the v2 lock, the 2.0.0-dev.N pre-releases, #511) and refuses any other
# version; 1.x releases are verified by main's copy of this script.
#
#   scripts/release-verify.sh <rust|ts|python|go> registry <version>
#   scripts/release-verify.sh <rust|ts|python|go> local <version> <source>
#   scripts/release-verify.sh --selftest
#
# `registry` installs the PUBLISHED package from its public registry, anonymously,
# retrying for registry lag. `local` installs the package a dry run just built,
# and skips only that retry loop. <source> is, per binding: rust, the crate
# directory; ts, the packed tarball; python, the built wheel; go, the module
# directory (a `replace` points the clean module at it). <version> is the tag's
# version (2.0.0-dev.N).
#
# Both modes then run six checks, in a clean directory with a clean cache, and
# report every one of them: PASS, FAIL, or NOT RUN when an earlier failure left
# nothing honest to check (a NOT RUN is never a pass). The script fails unless
# all six pass. The bracketed names are the channel's, from
# scripts/release-channel.sh:
#   1. fingerprint: the binding's generation-[ABI] fingerprint constant equals
#      CHS_ABI_FINGERPRINT in [HEADER] (include/v2/chtypes.h), never the v1
#      header;
#   2. cache root: `chtypes where` names <CHTYPES_CACHE>/[CACHE_SUBROOT] (rule
#      r5 of spec/abi-v2/docs.md), so the CLI is a channel build, not a 1.x one;
#   3. listing: `chtypes list` names at least one line, every line it names
#      is a tag that [REGISTRY_BASE] serves, read here independently from that
#      repository's own tags/list, AND the listing provably came from that
#      registry (#523). Tag names alone cannot prove it: staging serves the
#      same line tags as production, so a 1.x CLI listing production matches
#      them. No CLI's `list` or `where` names its registry, or a digest, so the
#      proof is a probe: `chtypes fetch <newest line>` into a throwaway cache,
#      whose install directory is named by the manifest digest the CLI
#      verified, must be the platform manifest [REGISTRY_BASE] serves for that
#      line. The probe runs only after the names matched, and its cache is
#      discarded; check 4 still fetches into the real one;
#   4. fetch: `chtypes fetch <newest line>` installs into
#      <CHTYPES_CACHE>/[CACHE_SUBROOT]/unpacked/sha256/<hex>, and sha256:<hex>
#      is the platform manifest [REGISTRY_BASE] serves for that line on this
#      platform. An install directory is named by the digest of the manifest it
#      was verified from, so this proves the bytes the CLI installed are that
#      registry's, whatever the CLI was configured with;
#   5. record: that directory's verified.json is record schema
#      [RECORD_SCHEMA], signed by the channel's key [KEY_ID], and its predicate
#      names abi [ABI] and the header's fingerprint;
#   6. load: the public API loads that line, and the library's own build_info
#      reports abi [ABI] and the header's fingerprint.
#
# Checks 3 to 6 are the registry assertion. The dev bindings are hard-wired to
# the staging dev repository with no override (rule r6), so a verify that ran
# against the production v1 registry and "passed" is exactly the failure this
# exists to refuse. When check 3 fails nothing is fetched: a line the CLI was
# not shown to have listed from the channel's registry is never downloaded.
#
# Where each binding keeps its generation-[ABI] constant (`abi<N>` beside v1's
# `abi1`) is the dev bindings' to decide; the paths are in the fingerprint_*
# functions below, one place to change.
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
# The channel: VERSION_RE, ABI, HEADER, REGISTRY_BASE, KEY_ID, CACHE_SUBROOT,
# RECORD_SCHEMA, GO_MODULE, NPM_PACKAGE, and parse_json and http_get.
# shellcheck source=release-channel.sh
. "$HERE/release-channel.sh"

fail() { echo "::error::$*" >&2; exit 1; }

# parse_line: stdin is `chtypes list`; stdout is the newest published two-part
# line, or nothing.
parse_line() {
  awk '$1=="published" && $2 ~ /^[0-9]+\.[0-9]+$/ {print $2}' | sort -V | tail -n 1
}
# published_lines: stdin is `chtypes list`; stdout is every spelling it lists as published.
published_lines() { awk '$1=="published" {print $2}'; }

# header_fingerprint <header>: its CHS_ABI_FINGERPRINT.
header_fingerprint() { sed -n 's/^#define CHS_ABI_FINGERPRINT "\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$1"; }

# registry_api <base>: the OCI API root of a repository-qualified base
# (`https://host/a/b` -> `https://host/v2/a/b`).
registry_api() {
  local rest="${1#https://}"
  printf 'https://%s/v2/%s\n' "${rest%%/*}" "${rest#*/}"
}
# this_platform: the OCI os/arch of this machine.
this_platform() {
  local os arch
  case "$(uname -s)" in Linux) os=linux ;; Darwin) os=darwin ;; *) os="$(uname -s)" ;; esac
  case "$(uname -m)" in x86_64 | amd64) arch=amd64 ;; aarch64 | arm64) arch=arm64 ;; *) arch="$(uname -m)" ;; esac
  printf '%s %s\n' "$os" "$arch"
}
tags_of() { parse_json '"\n".join(d.get("tags") or [])'; }
manifest_for() { # <os> <arch>: stdin is an OCI index; stdout is that platform's manifest digest
  parse_json 'next((m["digest"] for m in d.get("manifests") or [] if (m.get("platform") or {}).get("os") == a[0] and (m.get("platform") or {}).get("architecture") == a[1]), None)' "$1" "$2"
}

# ---- the checks: each prints what it found and returns its verdict ----

check_fingerprint() { # <binding's constant> <header's>
  [ -n "$1" ] || { echo "the binding carries no generation-$ABI fingerprint constant"; return 1; }
  [ "$1" = "$2" ] || { echo "the binding speaks '$1', $HEADER says '$2'"; return 1; }
  echo "the binding speaks $1, as $HEADER does"
}
check_cache_root() { # <`chtypes where`> <want>
  [ "$1" = "$2" ] || { echo "'chtypes where' names '${1:-<nothing>}', and a channel build caches under '$2' (rule r5): this CLI is not a $CHANNEL build"; return 1; }
  echo "'chtypes where' names $1"
}
check_listing() { # <the CLI's published lines> <the registry's tags>: newline-separated
  local cli="$1" tags="$2" l missing=""
  [ -n "$cli" ] || { echo "the CLI listed no published line"; return 1; }
  while IFS= read -r l; do
    grep -qxF -- "$l" <<<"$tags" || missing="$missing $l"
  done <<<"$cli"
  [ -z "$missing" ] || { echo "the CLI listed$missing, which $REGISTRY_BASE does not serve (it serves: $(paste -sd' ' - <<<"${tags:-<no tags>}")): the CLI did not list the channel's registry"; return 1; }
  echo "every line the CLI listed ($(paste -sd' ' - <<<"$cli")) is a tag $REGISTRY_BASE serves"
}
check_origin() { # <dir the probe fetch printed> <the registry's manifest digest for that line>
  local dir="$1" want="$2" hex="${1##*/}"
  [ "$(basename "$(dirname "$dir")")" = sha256 ] && [[ "$hex" =~ ^[0-9a-f]{64}$ ]] || { echo "the probe fetch installed into '${dir:-<nothing>}', which is not named by a manifest digest, so the listing's origin is unproven"; return 1; }
  [ -n "$want" ] || { echo "$REGISTRY_BASE serves no manifest for this platform, so the listing's origin cannot be proven"; return 1; }
  [ "sha256:$hex" = "$want" ] || { echo "the names match, but the CLI's manifest for that line is sha256:$hex and $REGISTRY_BASE serves $want: the listing came from another registry"; return 1; }
  echo "the CLI's manifest for that line is $want, the one $REGISTRY_BASE serves"
}
check_fetch_dir() { # <dir the CLI printed> <cache root> <the registry's manifest digest>
  local dir="$1" root="$2" want="$3" hex
  case "$dir" in
    "$root"/unpacked/sha256/*) hex="${dir#"$root"/unpacked/sha256/}" ;;
    *) echo "the CLI installed into '${dir:-<nothing>}', not under $root/unpacked/sha256/"; return 1 ;;
  esac
  [[ "$hex" =~ ^[0-9a-f]{64}$ ]] || { echo "'$dir' is not named by a manifest digest"; return 1; }
  [ -n "$want" ] || { echo "$REGISTRY_BASE serves no manifest for this platform, so nothing can match sha256:$hex"; return 1; }
  [ "sha256:$hex" = "$want" ] || { echo "the CLI installed manifest sha256:$hex, and $REGISTRY_BASE serves $want for that line: the bytes came from another registry"; return 1; }
  echo "the CLI installed manifest $want, the one $REGISTRY_BASE serves"
}
check_record() { # <verified.json> <header fingerprint>
  [ -f "$1" ] || { echo "no record at $1"; return 1; }
  python3 -c '
import json, sys
path, schema, key_id, abi, fp = sys.argv[1:]
try:
    r = json.load(open(path))
    p = r.get("predicate") or {}
    got = (r.get("schema"), r.get("signed_by"), p.get("abi"), p.get("abi_fingerprint"))
except (OSError, ValueError, AttributeError) as e:
    print(f"the record does not parse: {e}"); sys.exit(1)
want = (int(schema), key_id, int(abi), fp)
names = ("schema", "signed_by", "predicate.abi", "predicate.abi_fingerprint")
bad = [f"{n} is {g!r}, wanted {w!r}" for n, g, w in zip(names, got, want) if g != w]
if bad:
    print("the record says " + "; ".join(bad)); sys.exit(1)
print(f"record schema {got[0]}, signed by {got[1]}, abi {got[2]}, {got[3]}")
' "$1" "$RECORD_SCHEMA" "$KEY_ID" "$ABI" "$2"
}
check_loaded() { # <"abi fingerprint" as the loaded library reports them> <header fingerprint>
  local abi="${1%% *}" fp="${1#* }"
  [ -n "$1" ] && [ "$abi" != "$1" ] || { echo "the load printed '${1:-<nothing>}', not '<abi> <fingerprint>'"; return 1; }
  [ "$abi" = "$ABI" ] && [ "$fp" = "$2" ] || { echo "the loaded library reports abi $abi and $fp; the channel is abi $ABI and $2"; return 1; }
  echo "the loaded library reports abi $abi and $fp"
}

if [ "${1:-}" = "--selftest" ]; then
  n=0
  expect() { # name, want, got
    if [ "$2" != "$3" ]; then echo "selftest FAIL: $1: wanted '$2', got '$3'" >&2; exit 1; fi
    echo "selftest ok: $1 -> '$3'"; n=$((n + 1))
  }
  passes() { # name, check...
    local name="$1" out; shift
    out="$("$@")" || { echo "selftest FAIL: $name: refused: $out" >&2; exit 1; }
    echo "selftest ok: $name passes"; n=$((n + 1))
  }
  refuses() { # name, phrase, check...
    local name="$1" phrase="$2" out; shift 2
    if out="$("$@")"; then echo "selftest FAIL: $name: passed: $out" >&2; exit 1; fi
    grep -qF -- "$phrase" <<<"$out" || { echo "selftest FAIL: $name: refused without '$phrase': $out" >&2; exit 1; }
    echo "selftest ok: $name refused ('$phrase')"; n=$((n + 1))
  }
  flat=$'installed 26.3.1 linux-amd64 /c/26.3.1\npublished 26.3 support unknown\npublished 26.10 support unknown\npublished 26.9 support unknown\npublished 26.8.1 support unknown\npublished latest support unknown'
  expect "newest two-part line, numeric not lexical" "26.10" "$(printf '%s\n' "$flat" | parse_line)"
  expect "installed rows are not published lines" "" "$(printf 'installed 26.3 linux-amd64 /c/26.3\n' | parse_line)"
  expect "three-part spellings are not lines" "" "$(printf 'published 26.8.1 support unknown\n' | parse_line)"
  expect "the old bare form is not what the CLI prints" "" "$(printf '26.9\n' | parse_line)"
  expect "empty listing" "" "$(printf '' | parse_line)"
  expect "every published spelling" $'26.3\n26.10\n26.9\n26.8.1\nlatest' "$(printf '%s\n' "$flat" | published_lines)"

  # The channel's identity.
  dev="$(header_fingerprint "$ROOT/$HEADER")"; v1="$(header_fingerprint "$ROOT/include/chtypes.h")"
  [[ "$dev" =~ ^sha256:[0-9a-f]{64}$ ]] && [ "$dev" != "$v1" ] || { echo "selftest FAIL: $HEADER's fingerprint '$dev' is missing or is v1's" >&2; exit 1; }
  echo "selftest ok: the channel's header is $HEADER ($dev), not include/chtypes.h ($v1)"; n=$((n + 1))
  channel_version_ok 2.0.0-dev.3 && ! channel_version_ok 1.0.4 || { echo "selftest FAIL: the channel's version rule" >&2; exit 1; }
  echo "selftest ok: the channel takes 2.0.0-dev.3 and refuses 1.0.4"; n=$((n + 1))
  expect "the registry's API root" "https://registry-staging.wavehouse.dev/v2/chtypes/v2-dev" "$(registry_api "$REGISTRY_BASE")"

  # 1. fingerprint
  passes "the dev fingerprint" check_fingerprint "$dev" "$dev"
  refuses "a binding that speaks v1" "the binding speaks '$v1'" check_fingerprint "$v1" "$dev"
  refuses "a binding with no generation-2 layer" "no generation-$ABI fingerprint" check_fingerprint "" "$dev"
  # 2. cache root
  passes "the dev cache subroot" check_cache_root /c/v2-dev /c/v2-dev
  refuses "a 1.x CLI's cache root (the whole CHTYPES_CACHE)" "is not a $CHANNEL build" check_cache_root /c /c/v2-dev
  # 3. listing
  passes "lines the channel's registry serves" check_listing $'26.8\n26.9' $'26.8\n26.8.15\n26.9\n26.9.8'
  refuses "production's lines against an empty dev repository" "did not list the channel's registry" check_listing $'26.3\n26.9' ""
  refuses "one line the dev repository does not serve" "the CLI listed 26.7" check_listing $'26.9\n26.7' $'26.9\n26.9.8'
  refuses "no line listed" "listed no published line" check_listing "" $'26.9'
  # 3b. the listing's origin (#523): names that match are not enough
  prod_names=$'26.3\n26.9'
  passes "the production-shaped listing's names DO match staging's (why names are not proof)" check_listing "$prod_names" $'26.3\n26.9\n26.9.8'
  stg="sha256:$(printf 'b%.0s' {1..64})"; prod="sha256:$(printf 'c%.0s' {1..64})"
  refuses "a production-shaped listing with matching names (the probe lands on production's manifest)" "the listing came from another registry" check_origin "/p/unpacked/sha256/${prod#sha256:}" "$stg"
  passes "a true staging listing (positive control)" check_origin "/p/v2-dev/unpacked/sha256/${stg#sha256:}" "$stg"
  refuses "a probe that installed outside a digest directory" "origin is unproven" check_origin /p/unpacked/26.9 "$stg"
  refuses "a probe with nothing printed" "origin is unproven" check_origin "" "$stg"
  refuses "no staging manifest for this platform" "listing's origin cannot be proven" check_origin "/p/unpacked/sha256/${stg#sha256:}" ""
  # 4. fetch
  index='{"manifests":[{"digest":"sha256:aaaa","platform":{"os":"darwin","architecture":"arm64"}},{"digest":"sha256:'"$(printf 'b%.0s' {1..64})"'","platform":{"os":"linux","architecture":"amd64"}}]}'
  bdigest="sha256:$(printf 'b%.0s' {1..64})"; cdigest="sha256:$(printf 'c%.0s' {1..64})"
  expect "the platform's manifest" "$bdigest" "$(manifest_for linux amd64 <<<"$index")"
  expect "a platform the index lacks" "" "$(manifest_for linux arm64 <<<"$index")"
  passes "the registry's manifest, in the subroot" check_fetch_dir "/c/v2-dev/unpacked/sha256/${bdigest#sha256:}" /c/v2-dev "$bdigest"
  refuses "another registry's manifest" "the bytes came from another registry" check_fetch_dir "/c/v2-dev/unpacked/sha256/${cdigest#sha256:}" /c/v2-dev "$bdigest"
  refuses "an install outside the subroot" "not under /c/v2-dev/unpacked/sha256/" check_fetch_dir "/c/unpacked/sha256/${bdigest#sha256:}" /c/v2-dev "$bdigest"
  refuses "a directory not named by a digest" "is not named by a manifest digest" check_fetch_dir /c/v2-dev/unpacked/sha256/26.9 /c/v2-dev "$bdigest"
  refuses "no manifest for this platform" "serves no manifest for this platform" check_fetch_dir "/c/v2-dev/unpacked/sha256/${bdigest#sha256:}" /c/v2-dev ""
  # 5. record
  t="$(mktemp -d)"
  record() { printf '{"schema":%s,"signed_by":"%s","predicate":{"abi":%s,"abi_fingerprint":"%s"}}' "$1" "$2" "$3" "$4" > "$t/verified.json"; }
  record "$RECORD_SCHEMA" "$KEY_ID" "$ABI" "$dev"
  passes "a channel record" check_record "$t/verified.json" "$dev"
  record 1 "$KEY_ID" "$ABI" "$dev"
  refuses "a 1.x record (schema 1)" "schema is 1, wanted $RECORD_SCHEMA" check_record "$t/verified.json" "$dev"
  record "$RECORD_SCHEMA" deb275922dbff76e "$ABI" "$dev"
  refuses "signed by the release key, not the staging key" "signed_by is 'deb275922dbff76e', wanted '$KEY_ID'" check_record "$t/verified.json" "$dev"
  record "$RECORD_SCHEMA" "$KEY_ID" 1 "$v1"
  refuses "a generation-1 library" "predicate.abi is 1, wanted $ABI" check_record "$t/verified.json" "$dev"
  printf 'not json' > "$t/verified.json"
  refuses "an unparsable record" "does not parse" check_record "$t/verified.json" "$dev"
  refuses "no record" "no record at" check_record "$t/none.json" "$dev"
  # 6. load
  passes "a dev library" check_loaded "$ABI $dev" "$dev"
  refuses "a v1 library" "reports abi 1 and $v1" check_loaded "1 $v1" "$dev"
  refuses "another dev fingerprint" "reports abi $ABI and $v1" check_loaded "$ABI $v1" "$dev"
  refuses "a load that printed nothing" "printed '<nothing>'" check_loaded "" "$dev"
  refuses "a load that printed only a fingerprint (1.x's program)" "not '<abi> <fingerprint>'" check_loaded "$dev" "$dev"
  # The script refuses a version outside the channel before it installs anything.
  if out="$(bash "${BASH_SOURCE[0]}" ts local 1.0.4 /nonexistent.tgz 2>&1)"; then echo "selftest FAIL: a 1.x version was verified" >&2; exit 1; fi
  grep -qF "1.x releases are verified from main" <<<"$out" || { echo "selftest FAIL: the 1.x refusal: $out" >&2; exit 1; }
  echo "selftest ok: a 1.x version is refused before anything is installed"; n=$((n + 1))
  echo "selftest: $n cases, every refusal fired and every good input passed"
  exit 0
fi

lang="${1:-}"; mode="${2:-}"; version="${3:-}"; source_arg="${4:-}"
case "$lang" in rust | ts | python | go) ;; *) fail "usage: release-verify.sh <rust|ts|python|go> <registry|local> <version> [source]" ;; esac
case "$mode" in registry) ;; local) [ -n "$source_arg" ] || fail "local mode needs a <source>" ;; *) fail "mode is registry or local, not '$mode'" ;; esac
[ -n "$version" ] || fail "no version"
channel_version_ok "$version" || fail "$version is not a $CHANNEL release ($VERSION_FORM, scripts/release-channel.sh; #511); 1.x releases are verified from main"
[ -z "$source_arg" ] || source_arg="$(cd "$(dirname "$source_arg")" && pwd)/$(basename "$source_arg")"

want="$(header_fingerprint "$ROOT/$HEADER")"
[ -n "$want" ] || fail "could not read CHS_ABI_FINGERPRINT from $HEADER"

work="${RUNNER_TEMP:-$(mktemp -d)}/release-verify-$lang"
rm -rf "$work"; mkdir -p "$work"
export CHTYPES_CACHE="$work/cache"
cache_root="$CHTYPES_CACHE/$CACHE_SUBROOT"
# No credentials, no override of the registry or the trust: exactly a stranger.
unset CHTYPES_ARTIFACTS_URL CHTYPES_TRUSTED_KEYS CHTYPES_ALLOW_UNSIGNED CHTYPES_DOWNLOAD_TOKEN
echo "verifying $lang $version from $mode${source_arg:+ ($source_arg)} on the $CHANNEL channel: abi $ABI, $want, from $REGISTRY_BASE, key $KEY_ID"

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

# ---- per binding: install (sets CLI, and what the accessors read) ----

install_rust() {
  if [ "$mode" = registry ]; then
    retry 900 "chtypes $version" cargo install "chtypes@=$version" --locked --root "$work/bin"
  else
    cargo install --path "$source_arg" --locked --root "$work/bin"
  fi
  CLI="$work/bin/bin/chtypes"
  mkdir -p "$work/proj" && cd "$work/proj"
  cargo init --name chtypes-verify --vcs none >/dev/null 2>&1
  if [ "$mode" = registry ]; then cargo add "chtypes@=$version" >/dev/null 2>&1; else cargo add chtypes --path "$source_arg" >/dev/null 2>&1; fi
  PKG="$(dirname "$(cargo metadata --format-version 1 | python3 -c 'import json,sys; print(next(p["manifest_path"] for p in json.load(sys.stdin)["packages"] if p["name"] == "chtypes"))')")"
}
fingerprint_rust() {
  local f="$PKG/src/abi$ABI/decls.rs"
  [ -f "$f" ] || { echo "no $f" >&2; return 0; }
  sed -n 's/^ *"\(sha256:[0-9a-f]*\)";$/\1/p' "$f" | head -n 1
}
load_rust() {
  cat > src/main.rs <<RS
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let registry = chtypes::Registry::new(chtypes::RegistryOptions::default())?;
    let lib = registry.for_version("$1")?;
    let info = lib.build_info();
    println!("{} {}", info.abi, info.abi_fingerprint);
    Ok(())
}
RS
  cargo run --quiet || return 1
}

install_ts() {
  cd "$work"
  : > .npmrc   # no token
  echo '{"name":"verify","private":true}' > package.json
  if [ "$mode" = registry ]; then
    retry 3600 "$NPM_PACKAGE@$version" npm install --no-audit --no-fund --userconfig ./.npmrc "$NPM_PACKAGE@$version"
  else
    npm install --no-audit --no-fund --userconfig ./.npmrc "$source_arg"
  fi
  CLI="$work/node_modules/.bin/chtypes"
  PKG="$work/node_modules/$NPM_PACKAGE"
}
fingerprint_ts() {
  # The constant lives in the generated layer, which the entry point does not re-export: import it by file URL.
  local f="$PKG/dist/abi$ABI/decls.gen.js"
  [ -f "$f" ] || { echo "no $f" >&2; return 0; }
  node --input-type=module -e "
    import { pathToFileURL } from 'node:url';
    const m = await import(pathToFileURL(process.argv[1]).href);
    process.stdout.write(String(m.ABI_FINGERPRINT ?? ''));
  " "$f"
}
# load_* run inside `if`, where errexit does not apply: every step returns its own failure.
load_ts() {
  "$CLI" verify >&2 || return 1
  LINE="$1" node --input-type=module -e "
    import { Registry } from '$NPM_PACKAGE';
    const registry = await Registry.open();
    const lib = await registry.for(process.env.LINE);
    const type = lib.validateType('UInt8').toString();
    if (type !== 'UInt8') throw new Error('validateType(UInt8) answered ' + JSON.stringify(type));
    process.stdout.write(lib.buildInfo.abi + ' ' + lib.buildInfo.abiFingerprint);
  " || return 1
}

install_python() {
  cd "$work"
  export UV_NO_CONFIG=1
  unset PIP_INDEX_URL UV_INDEX_URL UV_EXTRA_INDEX_URL
  uv venv --quiet --python 3.13 "$work/venv"
  if [ "$mode" = registry ]; then
    retry 900 "chtypes==$version" uv pip install --quiet --python "$work/venv/bin/python" --refresh --no-cache "chtypes==$version"
  else
    uv pip install --quiet --python "$work/venv/bin/python" "$source_arg"
  fi
  CLI="$work/venv/bin/chtypes"
  PY="$work/venv/bin/python"
}
fingerprint_python() {
  "$PY" -c "
import importlib, sys
try:
    m = importlib.import_module('chtypes._abi$ABI._decls')
except ImportError as e:
    print(f'no chtypes._abi$ABI._decls: {e}', file=sys.stderr)
    sys.exit(0)
print(m.CHS_ABI_FINGERPRINT)
"
}
load_python() {
  LINE="$1" "$PY" -c '
import os
import chtypes
library = chtypes.Registry().for_version(os.environ["LINE"])
assert library.validate_type("UInt8") == b"UInt8"
print(library.build_info.abi, library.build_info.abi_fingerprint)
' || return 1
}

install_go() {
  mkdir -p "$work/mod" && cd "$work/mod"
  export GOPROXY="${GOPROXY:-https://proxy.golang.org}"
  go mod init published.example >/dev/null 2>&1
  if [ "$mode" = registry ]; then
    local ok=0 attempt
    for attempt in $(seq 1 20); do
      if go get "$GO_MODULE@v$version" > "$work/install.log" 2>&1; then ok=1; break; fi
      echo "attempt $attempt: $version is not served yet; waiting for the proxy"
      sleep 30
    done
    [ "$ok" = 1 ] || { tail -n 20 "$work/install.log"; fail "the proxy never served $GO_MODULE@v$version"; }
    PKG="$(go list -m -f '{{.Dir}}' "$GO_MODULE")"
    GOBIN="$work/bin" go install "$GO_MODULE/cmd/chtypes@v$version"
  else
    go mod edit -replace "$GO_MODULE=$source_arg"
    PKG="$source_arg"
    (cd "$source_arg" && go build -o "$work/bin/chtypes" ./cmd/chtypes)
  fi
  CLI="$work/bin/chtypes"
}
fingerprint_go() {
  local f="$PKG/internal/abi$ABI/abi_gen.go"
  [ -f "$f" ] || { echo "no $f" >&2; return 0; }
  sed -n 's/^const ChsAbiFingerprint = "\(sha256:[0-9a-f]*\)".*/\1/p' "$f"
}
load_go() {
  cat > main.go <<'GO'
package main

import (
	"fmt"
	"os"

	"MODULE/chtypes"
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
	fmt.Println(lib.BuildInfo().ABI, lib.BuildInfo().ABIFingerprint)
}
GO
  sed -i.bak "s#\"MODULE/chtypes\"#\"$GO_MODULE/chtypes\"#" main.go && rm main.go.bak || return 1
  # A consumer's own `go mod tidy`, with no -e and no masking: it fails loudly
  # if the module ships any file, under any build tag except `ignore`, that
  # imports a package the module does not contain (go/v1.0.0 did: #454).
  go mod tidy >&2 || return 1
  go run . "$1" || return 1
}

# ---- the six checks ----

failed=0
verdict() { # PASS|FAIL|NOT-RUN <check> <detail>
  case "$1" in
    PASS) echo "PASS     $2: $3" ;;
    FAIL) echo "::error::FAIL $2: $3"; failed=$((failed + 1)) ;;
    NOT-RUN) echo "NOT RUN  $2: $3"; failed=$((failed + 1)) ;;
  esac
  summary="${summary:-}$(printf '\n  %-8s %s' "$1" "$2")"
}
run_check() { # <check> <check function> <args...>
  local name="$1" out; shift
  if out="$("$@")"; then verdict PASS "$name" "$out"; else verdict FAIL "$name" "$out"; fi
}

"install_$lang"

# 1
run_check "1 fingerprint" check_fingerprint "$("fingerprint_$lang")" "$want"
# 2
where_out="$("$CLI" where 2>"$work/where.err")" || where_out=""
[ -s "$work/where.err" ] && sed 's/^/  chtypes where: /' "$work/where.err"
run_check "2 cache root" check_cache_root "$where_out" "$cache_root"
# 3
line=""
api="$(registry_api "$REGISTRY_BASE")"
read -r os arch <<<"$(this_platform)"
# staging_manifest <line>: the platform manifest digest the channel's registry serves for <line>;
# leaves the HTTP status in $work/manifest.status (a $(...) caller cannot see a variable). Empty when it serves none.
staging_manifest() {
  local manifest_status
  manifest_status="$(curl -sS -A "$RC_UA" --retry 3 --retry-all-errors -o "$work/index.json" -w '%{http_code}' \
    -H 'Accept: application/vnd.oci.image.index.v1+json' "$api/manifests/$1")" || manifest_status=""
  printf '%s' "$manifest_status" > "$work/manifest.status"
  [ "$manifest_status" != 200 ] || manifest_for "$os" "$arch" < "$work/index.json"
}
if ! listing="$("$CLI" list 2>"$work/list.err")"; then
  verdict FAIL "3 listing" "'chtypes list' failed: $(paste -sd' ' - < "$work/list.err")"
elif ! status="$(http_get "$api/tags/list" "$work/tags.json")" || [ "$status" != 200 ]; then
  verdict FAIL "3 listing" "could not read $api/tags/list (HTTP ${status:-none}), so nothing the CLI listed can be checked"
else
  echo "--- chtypes list ---"; printf '%s\n' "$listing"; echo "--------------------"
  if out="$(check_listing "$(published_lines <<<"$listing")" "$(tags_of < "$work/tags.json")")"; then
    line="$(parse_line <<<"$listing")"
    if [ -z "$line" ]; then
      verdict FAIL "3 listing" "$out, but none is a two-part line"
    else
      # The origin probe (#523): the names matched, which production's listing would also do.
      want_manifest="$(staging_manifest "$line")"
      manifest_status="$(cat "$work/manifest.status")"
      [ "$manifest_status" = 200 ] || echo "  $api/manifests/$line answered HTTP ${manifest_status:-none}"
      probe_dir="$(CHTYPES_CACHE="$work/probe-cache" "$CLI" fetch "$line" 2>"$work/probe.err" | tail -n 1)" || probe_dir=""
      [ ! -s "$work/probe.err" ] || sed 's/^/  probe fetch: /' "$work/probe.err"
      if origin="$(check_origin "$probe_dir" "$want_manifest")"; then
        verdict PASS "3 listing" "$out; newest line $line; $origin"
      else
        verdict FAIL "3 listing" "$origin"; line=""
      fi
      rm -rf "$work/probe-cache"
    fi
  else
    verdict FAIL "3 listing" "$out"
  fi
fi
# 4, 5, 6
if [ -z "$line" ]; then
  for c in "4 fetch" "5 record" "6 load"; do verdict NOT-RUN "$c" "no line was shown to come from $REGISTRY_BASE, so nothing is fetched"; done
else
  dir=""
  if ! dir="$("$CLI" fetch "$line" | tail -n 1)"; then
    verdict FAIL "4 fetch" "'chtypes fetch $line' failed"
  else
    manifest="$(staging_manifest "$line")"
    manifest_status="$(cat "$work/manifest.status")"
    [ "$manifest_status" = 200 ] || echo "  $api/manifests/$line answered HTTP ${manifest_status:-none}"
    run_check "4 fetch" check_fetch_dir "$dir" "$cache_root" "$manifest"
  fi
  if [ -n "$dir" ] && [ -d "$dir" ]; then
    run_check "5 record" check_record "$dir/verified.json" "$want"
    if loaded="$("load_$lang" "$line")"; then
      run_check "6 load" check_loaded "$loaded" "$want"
    else
      verdict FAIL "6 load" "the public API did not load $line"
    fi
  else
    verdict NOT-RUN "5 record" "nothing was installed"
    verdict NOT-RUN "6 load" "nothing was installed"
  fi
fi

echo "$lang $version ($mode) on the $CHANNEL channel:$summary"
[ "$failed" = 0 ] || fail "$failed of 6 checks did not pass; read each line above (NOT RUN is never a pass)"
echo "OK: $lang $version ($mode) speaks $want, listed, fetched and loaded $line from $REGISTRY_BASE"
