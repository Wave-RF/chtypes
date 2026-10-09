#!/usr/bin/env bash
# release-channel.sh — what the release workflows on this branch may release,
# defined ONCE, and the registry assertions that keep a pre-release from ever
# being what a plain install picks (public issue #511).
#
#   scripts/release-channel.sh get <NAME>
#       print one constant of the channel (the names are in THE CHANNEL, below)
#   scripts/release-channel.sh tag <rust|ts|python|go> <tag>
#       print the tag's version, or refuse a tag the channel does not release
#   scripts/release-channel.sh go-module <go.mod>
#       refuse unless that go.mod's module path is exactly the channel's
#   scripts/release-channel.sh default <rust|ts|python|go>
#       print the version the registry installs by default NOW, refusing unless
#       it is a stable DEFAULT_MAJOR.x: the record a release workflow takes
#       before it publishes, and the pre-publish half of "never by default"
#   scripts/release-channel.sh never-default <binding> <version> <recorded> <after|twin>
#       after: poll until the registry serves <version>, asserting on EVERY read
#              that the default is still <recorded> and never <version>
#       twin:  the dry run's copy of the same assertions, against the registry
#              as it is now: the default is <recorded>, <version> is not there
#              yet, and the same presence check finds <recorded> (the positive
#              control without which "not there" would prove nothing)
#   scripts/release-channel.sh --selftest
#       every refusal above fires on planted input, and good input passes (no
#       network)
#
# <version> is the tag's version, except for python, where it is the PEP 440
# spelling scripts/release-pep440.py mapped the tag to (2.0.0.devN).
#
# Every registry read here is anonymous and sends the User-Agent
# `chtypes-release-check`. A read that fails is an error, never an "absent": a
# registry that cannot be reached must not look like a version that is not
# there.
#
# scripts/release-verify.sh sources this file for the constants and the
# version rule; sourced, it defines and runs nothing else.
set -euo pipefail

# ============================================================================
# THE CHANNEL — the one place this branch's releases are defined.
#
# Until ABI v2 locks (#511 names the conditions: the downstream consumer's
# acceptance suite passes against v2-dev, no interface change for a week, and
# the maintainer's OK), this branch releases ONLY the 2.0.0-dev.N
# pre-releases, which no package manager installs by default: npm's `dev`
# dist-tag, PyPI 2.0.0.devN, crates.io 2.0.0-dev.N, and Go go/v2.0.0-dev.N
# under the module path .../go/v2. They fetch only from the staging dev
# repository and trust only the staging key (rule r6 of
# spec/abi-v2/docs.md), and cache under the v2-dev subroot (rule r5). Every
# release workflow on this branch and scripts/release-verify.sh read the names
# below and nothing else; no other file spells a channel's version rule,
# registry, key, cache root or dist-tag.
#
# THE LOCK flips this block, and only this block: CHANNEL="v2-dev" becomes
# CHANNEL="v2", and the `v2` arm is written in the same pull request, with
# versions ^2\.N\.N$ (not pre-releases), the production repository
# https://registry.wavehouse.dev/chtypes/v2, the release key (key id
# deb275922dbff76e), the cache subroot v2, npm's `latest` dist-tag, and
# never-default replaced by its opposite (the default then IS the new 2.x),
# and no FP_ALIAS: a production channel never resolves an alias.
# The bindings already carry that channel, built and tested but not the default
# (docs/guides/fetch-v1.md, "Generation 2 after the lock"), so the lock is a
# switch. The production repository caches its tags and tags/list for 300 s (the
# dev repository, 60 s): release-verify.sh reads the listing once, and a retry
# window added around that read must cover the channel's value, not 60 s.
# Until that pull request exists, any other CHANNEL refuses everything below.
# ============================================================================
CHANNEL="v2-dev"
# shellcheck disable=SC2034 # read through `get` and by scripts/release-verify.sh, which sources this file
case "$CHANNEL" in
  v2-dev)
    VERSION_RE='^2\.0\.0-dev\.(0|[1-9][0-9]*)$'
    VERSION_FORM='2.0.0-dev.N'
    PRERELEASE=1   # every registry reads the version as a pre-release
    NPM_DIST_TAG=dev
    DEFAULT_MAJOR=1   # what every registry must still install by default
    ABI=2
    HEADER=include/v2/chtypes.h
    REGISTRY_BASE=https://registry-staging.wavehouse.dev/chtypes/v2-dev
    KEY=5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c
    KEY_ID=824345f9bcf8e5bf
    CACHE_SUBROOT=v2-dev
    RECORD_SCHEMA=2
    GO_MODULE=github.com/wave-rf/chtypes/go/v2
    FP_ALIAS=1   # a dev SDK resolves <tag>--fp-<its fingerprint> before <tag> (docs/guides/fetch-v1.md §3)
    ;;
  *)
    echo "::error::release-channel: CHANNEL '$CHANNEL' has no definition. Only the lock writes another arm (scripts/release-channel.sh, THE LOCK; #511)." >&2
    exit 1
    ;;
esac
# The packages themselves, and the module path a plain `go get` resolves.
NPM_PACKAGE=@wavehouse/chtypes
PYPI_PROJECT=chtypes
CRATE=chtypes
GO_DEFAULT_MODULE=github.com/wave-rf/chtypes/go
RC_UA=chtypes-release-check

rc_fail() { echo "::error::release-channel: $*" >&2; exit 1; }

# channel_version_ok <version>: whether the channel releases this version.
channel_version_ok() { [[ "$1" =~ $VERSION_RE ]]; }

# channel_tag <binding> <tag>: the tag's version, or a refusal naming the rule.
channel_tag() {
  local b="$1" tag="$2" version
  case "$b" in rust | ts | python | go) ;; *) rc_fail "unknown binding '$b' (rust, ts, python or go)" ;; esac
  case "$tag" in
    "$b"/v*) version="${tag#"$b"/v}" ;;
    *) rc_fail "'$tag' is not a $b release tag ($b/v$VERSION_FORM)" ;;
  esac
  channel_version_ok "$version" || rc_fail "'$tag' is not releasable from this branch. Until the v2 lock the ONLY releasable versions are $VERSION_FORM, tagged $b/v$VERSION_FORM, which no package manager installs by default (scripts/release-channel.sh, THE CHANNEL; #511). 1.x releases are tagged from main."
  printf '%s\n' "$version"
}

# channel_go_module <go.mod>: refuse unless the module path is the channel's.
channel_go_module() {
  local file="$1" mod
  [ -f "$file" ] || rc_fail "no go.mod at $file"
  mod="$(sed -n 's/^module[[:space:]][[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*$/\1/p' "$file" | head -n 1)"
  [ "$mod" = "$GO_MODULE" ] || rc_fail "$file declares module '${mod:-<none>}', and the channel releases $GO_MODULE: a go/v2.x tag is a release only of a module path ending in /v2 (Go's major-version rule; #511). The rename is the Go dev binding's change."
  printf '%s\n' "$mod"
}

# is_stable_major <version> <major>: a plain MAJOR.MINOR.PATCH (an optional
# leading v, as Go spells it) whose major is <major>.
is_stable_major() {
  [[ "$1" =~ ^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] && [ "${BASH_REMATCH[1]}" = "$2" ]
}

# remedy <binding> <version>: what a maintainer does when <version> became the default.
remedy() {
  case "$1" in
    ts) echo "point latest back, by hand, from a maintainer's login: npm dist-tag add $NPM_PACKAGE@<the 1.x> latest" ;;
    python) echo "yank $2 on PyPI (project settings), which a plain resolution then skips" ;;
    rust) echo "yank it: cargo yank --version $2 $CRATE" ;;
    go) echo "retract it in a new go.mod (Go cannot delete a version)" ;;
  esac
}

# check_default <binding> <observed> <recorded> <version> [what]: the default
# a plain install resolves (<what> read it) must be the recorded stable
# DEFAULT_MAJOR.x, and never <version>. The first refusal is the one that matters: it means the
# pre-release is what everyone now installs.
check_default() {
  local b="$1" observed="$2" recorded="$3" version="$4" what="${5:-the registry default}"
  [ -n "$observed" ] || rc_fail "$b: the registry named no default version (an empty read is never a pass)"
  [ "${observed#v}" != "${version#v}" ] || rc_fail "$b: the registry's DEFAULT is now the pre-release $version — a plain, unpinned install picks it. Remedy: $(remedy "$b" "$version")."
  is_stable_major "$observed" "$DEFAULT_MAJOR" || rc_fail "$b: the registry's default is '$observed', not a stable $DEFAULT_MAJOR.x — a plain install no longer gets the 1.x."
  [ "${observed#v}" = "${recorded#v}" ] || rc_fail "$b: the registry's default moved from $recorded (recorded before the publish) to $observed. If a 1.x release landed during this run that is not this publish's doing, but it must be read before anything else is tagged."
  echo "OK $b: $what is still $observed, not $version"
}

# ---- parsers: stdin is what a registry or a tool printed (selftested) ----

parse_json() { # <python expression over `d`, the JSON, and `a`, the arguments>: print it, or nothing when absent
  python3 -c '
import json, sys
d = json.load(sys.stdin)
a = sys.argv[2:]
try:
    v = eval(sys.argv[1], {"d": d, "a": a})
except (KeyError, TypeError, IndexError):
    v = None
print("" if v is None else v)
' "$@"
}
npm_latest() { parse_json 'd["dist-tags"]["latest"]'; }
npm_dist_tag_of() { parse_json 'd["dist-tags"].get(a[0])' "$1"; }
npm_has_version() { parse_json '"present" if a[0] in d["versions"] else "absent"' "$1"; }
crate_max_stable() { parse_json 'd["crate"]["max_stable_version"]'; }
go_list_version() { parse_json 'd["Version"]'; }
go_versions_has() { parse_json '"present" if a[0] in (d.get("Versions") or []) else "absent"' "$1"; }

# uv_pinned <project>: the one `<project>==X` line `uv pip compile` printed, as X.
uv_pinned() {
  local lines
  lines="$(sed -n "s/^$1==\([^[:space:];]*\).*/\1/p")"
  [ -n "$lines" ] && [ "$(printf '%s\n' "$lines" | wc -l | tr -d ' ')" = 1 ] || return 1
  printf '%s\n' "$lines"
}
# cargo_lock_version <crate>: the version a Cargo.lock resolved <crate> to.
cargo_lock_version() {
  python3 -c '
import sys, tomllib
lock = tomllib.loads(sys.stdin.read())
got = [p["version"] for p in lock.get("package", []) if p.get("name") == sys.argv[1]]
print(got[0] if len(got) == 1 else "")
' "$1"
}
# classify_uv_failure / classify_go_failure: stdin is a failed resolution's
# output. "absent" only for the registry's own not-found; anything else is an
# error, so an outage never reads as "not published".
classify_uv_failure() {
  tr -s '[:space:]' ' ' | grep -q "there is no version of $PYPI_PROJECT==" && echo absent || echo error
}
classify_go_failure() {
  local text
  text="$(cat)"
  if grep -Eq '(404 Not Found|410 Gone)' <<<"$text" && grep -Eq 'not found: .*(unknown revision|no matching versions|invalid version)' <<<"$text"; then
    echo absent
  else
    echo error
  fi
}
classify_http() { # <status>: present, absent or error
  case "$1" in 200) echo present ;; 404) echo absent ;; *) echo error ;; esac
}

# ---- registry reads (network) ----

http_get() { # <url> <out>: prints the HTTP status
  curl -sS -A "$RC_UA" -H 'Accept: application/json' --retry 3 --retry-all-errors -o "$2" -w '%{http_code}' "$1"
}
rc_tmp() { mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/release-channel.XXXXXX"; }

npm_packument() { # <out>
  local status
  status="$(http_get "https://registry.npmjs.org/${NPM_PACKAGE/\//%2f}" "$1")" || rc_fail "npm: the packument read failed"
  [ "$status" = 200 ] || rc_fail "npm: the packument read answered HTTP $status"
}
uv_resolve() { # <requirement> <stdout-file> <stderr-file>: exit status of the resolution
  local dir; dir="$(rc_tmp)"
  printf '%s\n' "$1" > "$dir/req.in"
  (
    unset PIP_INDEX_URL UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX UV_INDEX UV_PRERELEASE
    export UV_NO_CONFIG=1 NO_COLOR=1
    uv pip compile --quiet --no-deps --no-header --no-annotate --no-cache --python-version 3.13 \
      --index-url https://pypi.org/simple "$dir/req.in" > "$2" 2> "$3"
  )
}
go_query() { # <go list args...>: run in a clean module-less directory against the public proxy
  local dir; dir="$(rc_tmp)"
  (cd "$dir" && GOPROXY=https://proxy.golang.org GOFLAGS=-mod=mod GOWORK=off go list "$@")
}

# default_what <binding>: what registry_default reads, for the messages.
default_what() {
  case "$1" in
    ts) echo "npm's dist-tags.latest" ;;
    python) echo "a plain, unpinned 'uv pip compile $PYPI_PROJECT' (no --pre)" ;;
    rust) echo "crates.io's max_stable_version" ;;
    go) echo "'go list -m $GO_DEFAULT_MODULE@latest'" ;;
  esac
}

# registry_default <binding>: the version a plain, unpinned install resolves now.
registry_default() {
  local b="$1" dir out
  dir="$(rc_tmp)"
  case "$b" in
    ts)
      npm_packument "$dir/p.json"
      npm_latest < "$dir/p.json"
      ;;
    python)
      uv_resolve "$PYPI_PROJECT" "$dir/out" "$dir/err" || { cat "$dir/err" >&2; rc_fail "python: a plain resolution of $PYPI_PROJECT failed"; }
      uv_pinned "$PYPI_PROJECT" < "$dir/out" || rc_fail "python: the resolution printed no single $PYPI_PROJECT== line: $(cat "$dir/out")"
      ;;
    rust)
      out="$(http_get "https://crates.io/api/v1/crates/$CRATE" "$dir/c.json")" || rc_fail "rust: the crates.io read failed"
      [ "$out" = 200 ] || rc_fail "rust: crates.io answered HTTP $out"
      crate_max_stable < "$dir/c.json"
      ;;
    go)
      go_query -m -json "$GO_DEFAULT_MODULE@latest" > "$dir/l.json" 2> "$dir/err" || { cat "$dir/err" >&2; rc_fail "go: $GO_DEFAULT_MODULE@latest did not resolve"; }
      go_list_version < "$dir/l.json"
      ;;
  esac
}

# registry_presence <binding> <version> <after|twin>: present or absent;
# exits on an error. Go's twin reads the version LIST instead of the version
# itself, so a dry run does not ask the proxy for a tag that does not exist
# yet (that the proxy then remembers the miss for a while is `unverified`).
registry_presence() {
  local b="$1" v="$2" how="$3" dir status mod
  dir="$(rc_tmp)"
  case "$b" in
    ts)
      npm_packument "$dir/p.json"
      npm_has_version "$v" < "$dir/p.json"
      ;;
    python)
      if uv_resolve "$PYPI_PROJECT==$v" "$dir/out" "$dir/err"; then
        [ "$(uv_pinned "$PYPI_PROJECT" < "$dir/out")" = "$v" ] || rc_fail "python: ==$v resolved to $(cat "$dir/out")"
        echo present
      else
        status="$(classify_uv_failure < "$dir/err")"
        [ "$status" = absent ] || { cat "$dir/err" >&2; rc_fail "python: resolving ==$v failed for a reason other than 'no such version'"; }
        echo absent
      fi
      ;;
    rust)
      status="$(http_get "https://crates.io/api/v1/crates/$CRATE/$v" "$dir/v.json")" || rc_fail "rust: the crates.io read failed"
      status="$(classify_http "$status")"
      [ "$status" != error ] || rc_fail "rust: crates.io answered an error for $CRATE $v"
      echo "$status"
      ;;
    go)
      if channel_version_ok "$v"; then mod="$GO_MODULE"; else mod="$GO_DEFAULT_MODULE"; fi
      if [ "$how" = twin ]; then
        if go_query -m -versions -json "$mod" > "$dir/l.json" 2> "$dir/err"; then
          go_versions_has "v$v" < "$dir/l.json"
        else
          status="$(classify_go_failure < "$dir/err")"
          [ "$status" = absent ] || { cat "$dir/err" >&2; rc_fail "go: listing $mod failed for a reason other than 'not found'"; }
          echo absent
        fi
      else
        if go_query -m -json "$mod@v$v" > "$dir/l.json" 2> "$dir/err"; then
          [ "$(go_list_version < "$dir/l.json")" = "v$v" ] || rc_fail "go: $mod@v$v resolved to $(go_list_version < "$dir/l.json")"
          echo present
        else
          status="$(classify_go_failure < "$dir/err")"
          [ "$status" = absent ] || { cat "$dir/err" >&2; rc_fail "go: $mod@v$v failed for a reason other than 'not found'"; }
          echo absent
        fi
      fi
      ;;
  esac
}

# cargo_plain_resolution: what `cargo add chtypes` (no version) and a fresh
# lock resolve to, in a clean project.
cargo_plain_resolution() {
  local dir; dir="$(rc_tmp)"
  (
    cd "$dir"
    cargo init --quiet --name default-check --vcs none >/dev/null 2>&1
    cargo add --quiet "$CRATE" >/dev/null 2>&1 || exit 1
    cargo generate-lockfile --quiet >/dev/null 2>&1 || exit 1
    cargo_lock_version "$CRATE" < Cargo.lock
  ) || rc_fail "rust: a plain 'cargo add $CRATE' and lock in a clean project failed"
}

# never_default <binding> <version> <recorded> <after|twin>
never_default() {
  local b="$1" v="$2" recorded="$3" phase="$4" observed presence dev plain wait deadline
  [ "${PRERELEASE:-0}" = 1 ] || rc_fail "never-default asserts a pre-release channel; '$CHANNEL' is not one (THE LOCK replaces this check)"
  is_stable_major "$recorded" "$DEFAULT_MAJOR" || rc_fail "$b: the recorded default '$recorded' is not a stable $DEFAULT_MAJOR.x, so there is nothing to compare against (the record step's output did not reach this job?)"
  case "$phase" in
    twin)
      observed="$(registry_default "$b")"
      check_default "$b" "$observed" "$recorded" "$v" "$(default_what "$b")"
      if [ "$b" = rust ]; then
        plain="$(cargo_plain_resolution)"
        check_default rust "$plain" "$recorded" "$v" "a plain 'cargo add $CRATE'"
      fi
      presence="$(registry_presence "$b" "$recorded" twin)"
      [ "$presence" = present ] || rc_fail "$b: the presence check does not find the recorded default $recorded itself, so its 'absent' below would prove nothing (positive control)"
      echo "OK $b: the presence check finds $recorded (positive control)"
      presence="$(registry_presence "$b" "$v" twin)"
      [ "$presence" = absent ] || rc_fail "$b: $v is already on the registry; a version is published once"
      echo "OK $b: $v is not on the registry yet"
      if [ "$b" = ts ]; then
        dev="$(registry_dist_tag "$NPM_DIST_TAG")"
        [ "$dev" != "$v" ] || rc_fail "ts: dist-tags.$NPM_DIST_TAG already names $v"
        echo "OK ts: dist-tags.$NPM_DIST_TAG is now '${dev:-<none>}'; after the publish it must be $v and latest still $recorded"
      fi
      echo "twin OK: $b installs $recorded by default, and the registry does not have $v yet"
      ;;
    after)
      wait="${RELEASE_CHANNEL_WAIT:-$(case "$b" in ts) echo 3600 ;; go) echo 1200 ;; *) echo 900 ;; esac)}"
      deadline=$((SECONDS + wait))
      while :; do
        observed="$(registry_default "$b")"
        check_default "$b" "$observed" "$recorded" "$v" "$(default_what "$b")"
        presence="$(registry_presence "$b" "$v" after)"
        dev=""
        [ "$b" != ts ] || dev="$(registry_dist_tag "$NPM_DIST_TAG")"
        if [ "$presence" = present ] && { [ "$b" != ts ] || [ "$dev" = "$v" ]; }; then break; fi
        [ "$SECONDS" -lt "$deadline" ] || rc_fail "$b: $v was not served as expected within ${wait}s (present: $presence${dev:+, dist-tags.$NPM_DIST_TAG: $dev}); the default stayed $observed throughout"
        echo "$b: $v not served yet (present: $presence${dev:+, dist-tags.$NPM_DIST_TAG: $dev}); retrying in 30s"
        sleep 30
      done
      if [ "$b" = rust ]; then
        plain="$(cargo_plain_resolution)"
        check_default rust "$plain" "$recorded" "$v" "a plain 'cargo add $CRATE'"
      fi
      [ "$b" != ts ] || echo "OK ts: dist-tags.$NPM_DIST_TAG is $v"
      echo "after OK: $v is served, and $b still installs $recorded by default"
      ;;
    *) rc_fail "phase is after or twin, not '$phase'" ;;
  esac
}
registry_dist_tag() { # <tag>: npm's dist-tag value now
  local dir; dir="$(rc_tmp)"
  npm_packument "$dir/p.json"
  npm_dist_tag_of "$1" < "$dir/p.json"
}

# Sourced (by scripts/release-verify.sh): the definitions above, nothing run.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then return 0; fi

if [ "${1:-}" = "--selftest" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ROOT="$(cd "$HERE/.." && pwd)"
  n=0
  ok() { # name, want, command...
    local name="$1" want="$2" got; shift 2
    got="$("$@" 2>/dev/null)" || { echo "selftest FAIL: $name: refused, wanted '$want'" >&2; "$@" >&2 || true; exit 1; }
    [ "$got" = "$want" ] || { echo "selftest FAIL: $name: wanted '$want', got '$got'" >&2; exit 1; }
    echo "selftest ok: $name -> '$got'"; n=$((n + 1))
  }
  refused() { # name, phrase the refusal must carry, command...
    local name="$1" phrase="$2" err; shift 2
    if err="$("$@" 2>&1 >/dev/null)"; then echo "selftest FAIL: $name: was not refused" >&2; exit 1; fi
    grep -qF -- "$phrase" <<<"$err" || { echo "selftest FAIL: $name: refused, but without '$phrase': $err" >&2; exit 1; }
    echo "selftest ok: $name refused ('$phrase')"; n=$((n + 1))
  }
  # The channel itself.
  ok "the channel is the dev channel" "v2-dev" printf '%s' "$CHANNEL"
  [ "$NPM_DIST_TAG" != latest ] || { echo "selftest FAIL: a pre-release channel publishes to npm's latest" >&2; exit 1; }
  ok "the key id is sha256-first16hex of the key" "$KEY_ID" python3 -c 'import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.argv[1])).hexdigest()[:16])' "$KEY"
  for fact in "$KEY" "$KEY_ID" "$REGISTRY_BASE" "$GO_MODULE"; do
    grep -qF -- "$fact" "$ROOT/spec/abi-v2/docs.md" || { echo "selftest FAIL: rule r6 in spec/abi-v2/docs.md does not name $fact" >&2; exit 1; }
  done
  echo "selftest ok: the key, key id, registry and module path are rule r6's"; n=$((n + 1))
  grep -q "^#define CHS_ABI_VERSION $ABI\$" "$ROOT/$HEADER" || { echo "selftest FAIL: $HEADER is not generation $ABI" >&2; exit 1; }
  echo "selftest ok: $HEADER is generation $ABI"; n=$((n + 1))
  # The tag rule.
  ok "a dev tag" "2.0.0-dev.0" channel_tag ts ts/v2.0.0-dev.0
  ok "a later dev tag" "2.0.0-dev.17" channel_tag go go/v2.0.0-dev.17
  refused "a 1.x tag" "ONLY releasable versions are 2.0.0-dev.N" channel_tag ts ts/v1.1.0
  refused "a 2.0.0 final before the lock" "#511" channel_tag rust rust/v2.0.0
  refused "another pre-release kind" "#511" channel_tag python python/v2.0.0-rc.1
  refused "a dev tag with no number" "#511" channel_tag ts ts/v2.0.0-dev
  refused "a leading zero (PEP 440 would read it as another number)" "#511" channel_tag python python/v2.0.0-dev.01
  refused "build metadata" "#511" channel_tag rust rust/v2.0.0-dev.0+x
  refused "a 2.0.1 dev" "#511" channel_tag ts ts/v2.0.1-dev.0
  refused "another binding's tag" "is not a ts release tag" channel_tag ts rust/v2.0.0-dev.0
  refused "no v" "is not a go release tag" channel_tag go go/2.0.0-dev.0
  refused "an unknown binding" "unknown binding" channel_tag java java/v2.0.0-dev.0
  # The Go module path.
  t="$(mktemp -d)"
  printf 'module github.com/wave-rf/chtypes/go/v2\n\ngo 1.27\n' > "$t/v2.mod"
  printf 'module "github.com/wave-rf/chtypes/go/v2" // quoted\n' > "$t/quoted.mod"
  printf 'module github.com/wave-rf/chtypes/go\n\ngo 1.27\n' > "$t/v1.mod"
  printf 'module github.com/wave-rf/chtypes/go/v3\n' > "$t/v3.mod"
  printf 'module github.com/someone/else/v2\n' > "$t/other.mod"
  ok "the /v2 module path" "$GO_MODULE" channel_go_module "$t/v2.mod"
  ok "a quoted module path" "$GO_MODULE" channel_go_module "$t/quoted.mod"
  refused "the v1 module path" "module path ending in /v2" channel_go_module "$t/v1.mod"
  refused "a /v3 module path" "declares module 'github.com/wave-rf/chtypes/go/v3'" channel_go_module "$t/v3.mod"
  refused "another module's /v2" "declares module 'github.com/someone/else/v2'" channel_go_module "$t/other.mod"
  refused "no go.mod" "no go.mod" channel_go_module "$t/missing.mod"
  # The default a plain install resolves.
  ok "the default unchanged" "OK ts: the registry default is still 1.0.4, not 2.0.0-dev.0" check_default ts 1.0.4 1.0.4 2.0.0-dev.0
  ok "Go spells it with a v" "OK go: the registry default is still v1.0.4, not 2.0.0-dev.0" check_default go v1.0.4 1.0.4 2.0.0-dev.0
  refused "the pre-release became the default" "DEFAULT is now the pre-release" check_default ts 2.0.0-dev.0 1.0.4 2.0.0-dev.0
  refused "Go's v2 became the default" "DEFAULT is now the pre-release" check_default go v2.0.0-dev.0 1.0.4 2.0.0-dev.0
  refused "a default that is another pre-release" "not a stable 1.x" check_default ts 1.1.0-rc.1 1.0.4 2.0.0-dev.0
  refused "a default on another major" "not a stable 1.x" check_default rust 2.0.0 1.0.4 2.0.0-dev.0
  refused "a default that moved" "moved from 1.0.4" check_default python 1.1.0 1.0.4 2.0.0.dev0
  refused "an empty read" "named no default" check_default rust "" 1.0.4 2.0.0-dev.0
  refused "a recorded value that is not a 1.x" "nothing to compare against" never_default ts 2.0.0-dev.0 "" twin
  refused "an unknown phase" "phase is after or twin" never_default ts 2.0.0-dev.0 1.0.4 later
  # The parsers, on what the registries and tools print.
  packument='{"name":"@wavehouse/chtypes","dist-tags":{"latest":"1.0.4","dev":"2.0.0-dev.0"},"versions":{"1.0.4":{},"2.0.0-dev.0":{}}}'
  ok "npm latest" "1.0.4" npm_latest <<<"$packument"
  ok "npm dev" "2.0.0-dev.0" npm_dist_tag_of dev <<<"$packument"
  ok "npm with no dev tag" "" npm_dist_tag_of dev <<<'{"dist-tags":{"latest":"1.0.4"},"versions":{}}'
  ok "npm has the version" "present" npm_has_version 2.0.0-dev.0 <<<"$packument"
  ok "npm lacks the version" "absent" npm_has_version 2.0.0-dev.1 <<<"$packument"
  ok "crates.io max_stable_version" "1.0.4" crate_max_stable <<<'{"crate":{"max_version":"2.0.0-dev.0","max_stable_version":"1.0.4"}}'
  ok "go list -m -json" "v1.0.4" go_list_version <<<'{"Path":"github.com/wave-rf/chtypes/go","Version":"v1.0.4","Query":"latest"}'
  ok "go -versions has it" "present" go_versions_has v1.0.4 <<<'{"Path":"x","Versions":["v1.0.3","v1.0.4"]}'
  ok "go -versions lacks it" "absent" go_versions_has v2.0.0-dev.0 <<<'{"Path":"x","Versions":["v1.0.4"]}'
  ok "go -versions with none" "absent" go_versions_has v2.0.0-dev.0 <<<'{"Path":"x"}'
  ok "uv pip compile" "1.0.4" uv_pinned chtypes <<<'chtypes==1.0.4'
  ok "uv pip compile, a dev release" "2.0.0.dev0" uv_pinned chtypes <<<'chtypes==2.0.0.dev0'
  refused "uv printed two pins" "" uv_pinned chtypes <<<$'chtypes==1.0.4\nchtypes==1.0.3'
  refused "uv printed none" "" uv_pinned chtypes <<<'backports-zstd==1.7.0'
  ok "Cargo.lock" "1.0.4" cargo_lock_version chtypes <<<$'version = 4\n\n[[package]]\nname = "cfg-if"\nversion = "1.0.4"\n\n[[package]]\nname = "chtypes"\nversion = "1.0.4"\n'
  ok "uv's 'no version' is absent (wrapped as uv wraps it)" "absent" classify_uv_failure <<<$'  x No solution found when resolving dependencies:\n  `-> Because there is no version of\n      chtypes==2.0.0.dev0 and you require chtypes==2.0.0.dev0, we can conclude'
  ok "uv's network failure is an error, not absent" "error" classify_uv_failure <<<'error: Failed to fetch: `https://pypi.org/simple/chtypes/` Caused by: Request failed after 3 retries'
  ok "the proxy's not-found is absent" "absent" classify_go_failure <<<$'go: github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.0: reading https://proxy.golang.org/github.com/wave-rf/chtypes/go/v2/@v/v2.0.0-dev.0.info: 404 Not Found\n\tserver response: not found: github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.0: invalid version: unknown revision go/v2.0.0-dev.0'
  ok "the proxy's unknown module is absent" "absent" classify_go_failure <<<$'go: module github.com/wave-rf/chtypes/go/v2: reading https://proxy.golang.org/github.com/wave-rf/chtypes/go/v2/@v/list: 404 Not Found\n\tserver response: not found: module github.com/wave-rf/chtypes/go/v2: no matching versions for query "latest"'
  ok "a proxy outage is an error, not absent" "error" classify_go_failure <<<'go: github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.0: reading https://proxy.golang.org/...: 502 Bad Gateway'
  ok "a sum database mismatch is an error, not absent" "error" classify_go_failure <<<'verifying github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.0: checksum mismatch'
  ok "HTTP 200 is present" "present" classify_http 200
  ok "HTTP 404 is absent" "absent" classify_http 404
  ok "HTTP 503 is an error, not absent" "error" classify_http 503
  # The subcommands parse their arguments.
  refused "get an unknown name" "no channel constant" bash "${BASH_SOURCE[0]}" get NOPE
  ok "get the dist-tag" "dev" bash "${BASH_SOURCE[0]}" get NPM_DIST_TAG
  refused "the tag subcommand refuses 1.x" "#511" bash "${BASH_SOURCE[0]}" tag ts ts/v1.1.0
  echo "selftest: $n cases, every refusal fired and every good input passed"
  exit 0
fi

cmd="${1:-}"
[ "$#" -gt 0 ] && shift
case "$cmd" in
  get)
    [ "$#" = 1 ] || rc_fail "usage: get <NAME>"
    case "$1" in
      CHANNEL | VERSION_RE | VERSION_FORM | PRERELEASE | NPM_DIST_TAG | DEFAULT_MAJOR | ABI | HEADER | REGISTRY_BASE | KEY | KEY_ID | CACHE_SUBROOT | RECORD_SCHEMA | GO_MODULE | GO_DEFAULT_MODULE | NPM_PACKAGE | PYPI_PROJECT | CRATE)
        printf '%s\n' "${!1}" ;;
      *) rc_fail "no channel constant named '$1'" ;;
    esac
    ;;
  tag) [ "$#" = 2 ] || rc_fail "usage: tag <rust|ts|python|go> <tag>"; channel_tag "$1" "$2" ;;
  go-module) [ "$#" = 1 ] || rc_fail "usage: go-module <go.mod>"; channel_go_module "$1" ;;
  default)
    [ "$#" = 1 ] || rc_fail "usage: default <rust|ts|python|go>"
    case "$1" in rust | ts | python | go) ;; *) rc_fail "unknown binding '$1'" ;; esac
    observed="$(registry_default "$1")"
    is_stable_major "$observed" "$DEFAULT_MAJOR" || rc_fail "$1: the registry's default is '$observed', not a stable $DEFAULT_MAJOR.x — refusing to publish a pre-release over a default that is already wrong"
    printf '%s\n' "${observed#v}"
    ;;
  never-default)
    [ "$#" = 4 ] || rc_fail "usage: never-default <binding> <version> <recorded> <after|twin>"
    case "$1" in rust | ts | python | go) ;; *) rc_fail "unknown binding '$1'" ;; esac
    never_default "$1" "$2" "$3" "$4"
    ;;
  *) rc_fail "usage: release-channel.sh get|tag|go-module|default|never-default|--selftest (see the header)" ;;
esac
