#!/usr/bin/env bash
# release-channel.sh — what the release workflows on this branch may release,
# defined ONCE: the two release modes a tag selects, the assertions each mode
# makes about what a plain install resolves, and the gate a stable release
# passes before anything is published (public issues #511 and #597).
#
#   scripts/release-channel.sh mode <rust|ts|python|go> <tag>
#       print the mode the tag selects, `dev` or `stable` (THE MODES, below),
#       or refuse a tag this branch does not release
#   scripts/release-channel.sh tag <rust|ts|python|go> <tag>
#       print the tag's version, with the same refusals
#   scripts/release-channel.sh get <rust|ts|python|go> <tag> <NAME>
#       print one constant of the mode the tag selects
#   scripts/release-channel.sh get <NAME>
#       the same, of the dev mode: the channel every non-test binary of this
#       branch speaks until the lock (scripts/fetch-v1/cache-interop.py reads it)
#   scripts/release-channel.sh go-module <go.mod>
#       refuse unless that go.mod's module path is exactly the one this branch
#       releases
#   scripts/release-channel.sh gate <rust|ts|python|go> <tag> <true|false>
#       THE GATE (below): the last argument is whether the run is a dry run. A
#       stable release that is not a dry run refuses unless the repository
#       variable CHTYPES_V2_LOCKED_FP (read from the environment) equals the
#       fingerprint the binding is built against and the spec is locked
#   scripts/release-channel.sh default <rust|ts|python|go> <tag>
#       print the version a plain install resolves NOW: the record a release
#       workflow takes before it publishes. dev: refused unless it is a stable
#       DEFAULT_MAJOR.x (the pre-publish half of "never by default"). stable:
#       refused if it already is the version
#   scripts/release-channel.sh default-check <binding> <tag> <version> <recorded> <after|twin>
#       what a plain install resolves, asserted against the registry itself.
#       dev mode, `never-default` (unchanged since #511):
#         after: poll until the registry serves <version>, asserting on EVERY
#                read that the default is still <recorded> and never <version>
#         twin:  the dry run's copy of the same assertions, against the
#                registry as it is now: the default is <recorded>, <version>
#                is not there yet, and the same presence check finds
#                <recorded> (the positive control without which "not there"
#                would prove nothing)
#       stable mode, the opposite (#597):
#         after: poll until the registry serves <version> AND a plain install
#                resolves it, asserting on every read that the default is
#                <recorded> or <version> and nothing else
#         twin:  the default is <recorded> and not <version> yet, <version> is
#                not there yet, and the presence check finds <recorded>
#   scripts/release-channel.sh --selftest
#       every refusal above fires on planted input, good input passes, and the
#       registry assertions of both modes run against recorded registry
#       answers (no network)
#
# <version> is the tag's version, except for python, where it is the PEP 440
# spelling scripts/release-pep440.py mapped the tag to (2.0.0.devN, 2.0.0).
#
# Every registry read here is anonymous and sends the User-Agent
# `chtypes-release-check`. A read that fails is an error, never an "absent": a
# registry that cannot be reached must not look like a version that is not
# there.
#
# scripts/release-verify.sh sources this file for the modes and the version
# rule; sourced, it defines and runs nothing else.
set -euo pipefail

RC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ============================================================================
# THE MODES — the one place this branch's releases are defined.
#
# A tag selects its mode, and nothing else does: no workflow input, variable
# or file sets it (#597). release_mode, below, is the derivation, and the only
# one:
#
#   <binding>/v2.0.0-dev.N  dev     a pre-release, which no package manager
#                                   installs by default
#   <binding>/v2.N.N        stable  2.0.0 and later: the default install once
#                                   published
#   anything else           refused: a 1.x is tagged from main, and every
#                           other shape is not a release
#
# dev is #511's pre-release channel: npm's `dev` dist-tag, PyPI 2.0.0.devN,
# crates.io 2.0.0-dev.N, and Go go/v2.0.0-dev.N under the module path .../go/v2.
# Its SDKs fetch only from the staging dev repository and trust only the
# staging key (rule r6 of spec/abi-v2/docs.md), and cache under the v2-dev
# subroot (rule r5). Its values are written below and pinned against rule r6
# by the selftest.
#
# stable is the production generation-2 channel the four bindings already
# carry (docs/guides/fetch-v1.md, "Generation 2 after the lock"): its name,
# registry, cache subroot, record schema and abi are `prod_v2` of
# spec/fetch-v1/constants.json, and its key is that file's release key, the
# definition scripts/fetch-v1/gen-constants.py generates into every binding.
# They are read from that file here, never copied. It publishes under npm's
# `latest`, and has no alias step and no own-fingerprint filter. The
# production repository caches its tags and tags/list for 300 s (the dev
# repository, 60 s), so a read-back of it retries for READBACK_WINDOW seconds,
# more than 300 (scripts/release-verify.sh).
#
# A stable release publishes only past THE GATE, below.
# ============================================================================
DEV_VERSION_RE='^2\.0\.0-dev\.(0|[1-9][0-9]*)$'
STABLE_VERSION_RE='^2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
# The dev channel's registry and key: rule r6, and the stable mode's dry-run
# stand-in (channel_standin).
DEV_REGISTRY_BASE=https://registry-staging.wavehouse.dev/chtypes/v2-dev
DEV_KEY=5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c
DEV_KEY_ID=824345f9bcf8e5bf
# The same in both modes.
ABI=2
HEADER=include/v2/chtypes.h
RECORD_SCHEMA=2
GO_MODULE=github.com/wave-rf/chtypes/go/v2
# The packages themselves, and the module path a plain `go get` resolves.
NPM_PACKAGE=@wavehouse/chtypes
PYPI_PROJECT=chtypes
CRATE=chtypes
GO_DEFAULT_MODULE=github.com/wave-rf/chtypes/go
RC_UA=chtypes-release-check
# The repository variable THE GATE reads, set only by the maintainer.
LOCKED_FP_VARIABLE=CHTYPES_V2_LOCKED_FP

MODE=""   # set by channel_select; nothing is defined until a mode is selected

rc_fail() { echo "::error::release-channel: $*" >&2; exit 1; }

# release_mode <version>: the mode a version selects, or nothing (status 1).
release_mode() {
  if [[ "$1" =~ $DEV_VERSION_RE ]]; then
    echo dev
  elif [[ "$1" =~ $STABLE_VERSION_RE ]]; then
    echo stable
  else
    return 1
  fi
}

# channel_select <dev|stable>: define the mode's constants.
# shellcheck disable=SC2034 # read through `get` and by scripts/release-verify.sh, which sources this file
channel_select() {
  local vals
  case "$1" in
    dev)
      CHANNEL=v2-dev
      VERSION_RE="$DEV_VERSION_RE"
      VERSION_FORM='2.0.0-dev.N'
      PRERELEASE=1   # every registry reads the version as a pre-release
      NPM_DIST_TAG=dev
      DEFAULT_MAJOR=1   # what every registry must still install by default
      GO_LATEST_MODULE="$GO_DEFAULT_MODULE"   # whose @latest is the default install asserted on
      REGISTRY_BASE="$DEV_REGISTRY_BASE"
      KEY="$DEV_KEY"
      KEY_ID="$DEV_KEY_ID"
      CACHE_SUBROOT=v2-dev
      RECORD_SCHEMA=2
      ABI=2
      FP_ALIAS=1   # a dev SDK resolves <tag>--fp-<its fingerprint> before <tag> (docs/guides/fetch-v1.md §3)
      READBACK_WINDOW=0   # the listing is read once, as it always was
      ;;
    stable)
      vals="$(python3 - "$RC_ROOT/spec/fetch-v1/constants.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
p, keys = d["prod_v2"], d["trust"]["release_keys"]
if len(p["default_bases"]) != 1 or len(keys) != 1:
    sys.exit(f"prod_v2 names {len(p['default_bases'])} bases and the release trust list {len(keys)} keys; this script verifies against exactly one of each")
print(p["name"], p["default_bases"][0], p["cache_leaf"], p["record_schema"], p["abi_generation"], keys[0]["keyid"], keys[0]["ed25519_hex"])
PY
      )" || rc_fail "could not read the production generation-2 channel from spec/fetch-v1/constants.json"
      read -r CHANNEL REGISTRY_BASE CACHE_SUBROOT RECORD_SCHEMA ABI KEY_ID KEY <<<"$vals"
      VERSION_RE="$STABLE_VERSION_RE"
      VERSION_FORM='2.N.N'
      PRERELEASE=0
      NPM_DIST_TAG=latest
      DEFAULT_MAJOR=2   # what every registry installs by default once this is published
      GO_LATEST_MODULE="$GO_MODULE"   # `go get .../go/v2` resolves @latest of the /v2 path
      FP_ALIAS=0   # a production channel never resolves an alias
      READBACK_WINDOW=360   # more than the production repository's 300 s tag cache
      ;;
    *) rc_fail "mode is dev or stable, not '$1'" ;;
  esac
  MODE="$1"
}

# channel_standin <dry_run>: the stable mode's dry-run stand-in. Production
# chtypes/v2 holds nothing until the lock (a fetch answers
# CHTYPES_ARTIFACT_UNPUBLISHED), so a dry run of a stable release points the
# production channel's base and trust at the staging dev repository and the
# staging key, through the channel's OWN overrides (CHTYPES_ARTIFACTS_URL and
# CHTYPES_TRUSTED_KEYS, which the production channel honors and the dev
# channel ignores). Everything else stays the stable mode's: the v2 cache
# subroot, record schema 2, abi 2, no alias step. Refused unless the run is a
# dry run and not a tag push, and unless the stable mode is selected: a real
# release is verified against production and nothing else.
# shellcheck disable=SC2034 # read by scripts/release-verify.sh
channel_standin() {
  [ "$MODE" = stable ] || rc_fail "the stand-in is the stable mode's, and this is the '${MODE:-<none>}' mode (a dev SDK honors no override: rule r6)"
  [ "$1" = true ] || rc_fail "the stand-in is for a dry run only, and this run's dry_run is '$1': a real release is verified against $REGISTRY_BASE and nothing else (#597)"
  [ "${GITHUB_EVENT_NAME:-}" != push ] || rc_fail "the stand-in is for a dry run only, and this run is a tag push (#597)"
  STANDIN_OF="$REGISTRY_BASE"
  REGISTRY_BASE="$DEV_REGISTRY_BASE"
  KEY="$DEV_KEY"
  KEY_ID="$DEV_KEY_ID"
  READBACK_WINDOW=0   # the dev repository, read once
  STANDIN=1
}

# channel_version_ok <version>: whether the selected mode releases this version.
channel_version_ok() { [ -n "$MODE" ] && [[ "$1" =~ $VERSION_RE ]]; }

# channel_tag <binding> <tag>: the tag's version, or a refusal naming the rule.
# It selects the tag's mode as it goes (channel_select).
channel_tag() {
  local b="$1" tag="$2" version mode
  case "$b" in rust | ts | python | go) ;; *) rc_fail "unknown binding '$b' (rust, ts, python or go)" ;; esac
  case "$tag" in
    "$b"/v*) version="${tag#"$b"/v}" ;;
    *) rc_fail "'$tag' is not a $b release tag ($b/v2.0.0-dev.N or $b/v2.N.N)" ;;
  esac
  if ! mode="$(release_mode "$version")"; then
    if [[ "$version" =~ ^1\. ]]; then
      rc_fail "'$tag' is a 1.x tag: 1.x releases are tagged from main, never from this branch (#511)"
    fi
    rc_fail "'$tag' is not releasable from this branch. The releasable tags are $b/v2.0.0-dev.N (the dev mode, a pre-release) and $b/v2.N.N (the stable mode), and the tag alone selects which (scripts/release-channel.sh, THE MODES; #511, #597)"
  fi
  channel_select "$mode"
  printf '%s\n' "$version"
}

# channel_go_module <go.mod>: refuse unless the module path is the one released.
channel_go_module() {
  local file="$1" mod
  [ -f "$file" ] || rc_fail "no go.mod at $file"
  mod="$(sed -n 's/^module[[:space:]][[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*$/\1/p' "$file" | head -n 1)"
  [ "$mod" = "$GO_MODULE" ] || rc_fail "$file declares module '${mod:-<none>}', and this branch releases $GO_MODULE: a go/v2.x tag is a release only of a module path ending in /v2 (Go's major-version rule; #511)."
  printf '%s\n' "$mod"
}

# ============================================================================
# THE GATE — a stable release publishes only with the maintainer's fingerprint.
#
# A stable release that is not a dry run (a tag push, or a dispatch with
# dry_run=false) refuses before anything is built unless the repository
# variable CHTYPES_V2_LOCKED_FP equals the generation-2 fingerprint the
# binding is built against, read from the binding's own generated constant
# (binding_fingerprint, never typed in a workflow), and spec/abi-v2/abi.json
# says `"stability": "locked"`. Only the maintainer sets that variable, to the
# locked fingerprint, when he approves the lock; nothing in this repository
# sets it. Without it, or when it names another fingerprint, the stable mode
# can only dry-run, so no session can tag or publish 2.0.0 by accident. A dry
# run always passes the gate (it publishes nothing), and the dev mode has none
# (a 2.0.0-dev.N is never the default). Go publishes by the tag existing, so
# for Go the gate cannot stop the publish: it turns that run red, loudly, and
# that is one more reason Go goes last.
# ============================================================================

# binding_fingerprint <binding> <root>: the generation-ABI fingerprint the
# binding under <root> is built against, from its own generated constant
# (scripts/abi-v1/gen.py writes each one). Empty unless exactly one is found.
binding_fingerprint() {
  local f got
  case "$1" in
    go) f="$2/go/internal/abi$ABI/abi_gen.go"; [ -f "$f" ] && got="$(sed -n 's/^const ChsAbiFingerprint = "\(sha256:[0-9a-f]\{64\}\)"$/\1/p' "$f")" ;;
    python) f="$2/python/src/chtypes/_abi$ABI/_decls.py"; [ -f "$f" ] && got="$(sed -n 's/^CHS_ABI_FINGERPRINT = "\(sha256:[0-9a-f]\{64\}\)"$/\1/p' "$f")" ;;
    ts) f="$2/ts/src/abi$ABI/decls.gen.ts"; [ -f "$f" ] && got="$(sed -n 's/^export const ABI_FINGERPRINT = "\(sha256:[0-9a-f]\{64\}\)";$/\1/p' "$f")" ;;
    # rustfmt breaks the constant after its `=`, so the value is on that line or the next.
    rust) f="$2/rust/src/abi$ABI/decls.rs"; [ -f "$f" ] && got="$(awk '/^pub\(crate\) const CHS_ABI_FINGERPRINT: &str =/ { if ($0 !~ /"sha256:/) getline; print; exit }' "$f" | sed -n 's/.*"\(sha256:[0-9a-f]\{64\}\)";$/\1/p')" ;;
    *) rc_fail "unknown binding '$1'" ;;
  esac
  [ -n "${got:-}" ] && [ "$(printf '%s\n' "$got" | wc -l | tr -d ' ')" = 1 ] && printf '%s\n' "$got"
  return 0
}

# spec_stability <root>: spec/abi-v2/abi.json's `stability`.
spec_stability() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("stability", ""))' "$1/spec/abi-v$ABI/abi.json"
}

# stable_gate <dry_run> <locked_fp> <binding_fp> <stability>: the gate for the
# selected mode, on its inputs alone (selftested row by row).
stable_gate() {
  local dry="$1" locked="$2" fp="$3" stability="$4"
  case "$dry" in true | false) ;; *) rc_fail "dry_run is true or false, not '$dry'" ;; esac
  if [ "$MODE" = dev ]; then
    echo "gate: the dev mode has no gate (a 2.0.0-dev.N is never the default install)"
    return 0
  fi
  [ "$MODE" = stable ] || rc_fail "no mode selected"
  if [ "$dry" = true ]; then
    echo "gate: stable mode, dry run: allowed to run to the dry run's end, which publishes nothing (a real run needs $LOCKED_FP_VARIABLE to equal ${fp:-the fingerprint of the binding})"
    return 0
  fi
  [ -n "$locked" ] || rc_fail "a stable release is refused: the repository variable $LOCKED_FP_VARIABLE is not set. Only the maintainer sets it, to the locked v2 fingerprint, when he approves the lock; until then the stable mode can only dry-run (scripts/release-channel.sh, THE GATE; #597)."
  [[ "$fp" =~ ^sha256:[0-9a-f]{64}$ ]] || rc_fail "a stable release is refused: the binding's own generated fingerprint constant could not be read ('${fp:-<nothing>}')"
  [ "$locked" = "$fp" ] || rc_fail "a stable release is refused: $LOCKED_FP_VARIABLE is '$locked', and this binding is built against $fp. The maintainer's variable names the ONE locked fingerprint; a tree that speaks another is not the locked release (THE GATE; #597)."
  [ "$stability" = locked ] || rc_fail "a stable release is refused: spec/abi-v$ABI/abi.json says stability '${stability:-<none>}', not 'locked', although $LOCKED_FP_VARIABLE matches this binding (THE GATE; #597)"
  echo "gate: stable mode, a real release: $LOCKED_FP_VARIABLE equals this binding's fingerprint $fp, and the spec is locked"
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

# check_default <binding> <observed> <recorded> <version> [what]: the dev
# mode's assertion. The default a plain install resolves (<what> read it) must
# be the recorded stable DEFAULT_MAJOR.x, and never <version>. The first
# refusal is the one that matters: it means the pre-release is what everyone
# now installs.
check_default() {
  local b="$1" observed="$2" recorded="$3" version="$4" what="${5:-the registry default}"
  [ -n "$observed" ] || rc_fail "$b: the registry named no default version (an empty read is never a pass)"
  [ "${observed#v}" != "${version#v}" ] || rc_fail "$b: the registry's DEFAULT is now the pre-release $version — a plain, unpinned install picks it. Remedy: $(remedy "$b" "$version")."
  is_stable_major "$observed" "$DEFAULT_MAJOR" || rc_fail "$b: the registry's default is '$observed', not a stable $DEFAULT_MAJOR.x — a plain install no longer gets the 1.x."
  [ "${observed#v}" = "${recorded#v}" ] || rc_fail "$b: the registry's default moved from $recorded (recorded before the publish) to $observed. If a 1.x release landed during this run that is not this publish's doing, but it must be read before anything else is tagged."
  echo "OK $b: $what is still $observed, not $version"
}

# check_is_default <binding> <observed> <recorded> <version> <before|now> [what]:
# the stable mode's assertion. before: the default is still <recorded>, the
# value read before the publish, and not <version> yet. now: the default IS
# <version>, which a plain, unpinned install resolves.
check_is_default() {
  local b="$1" observed="$2" recorded="$3" version="$4" when="$5" what="${6:-the registry default}"
  [ -n "$observed" ] || rc_fail "$b: the registry named no default version (an empty read is never a pass)"
  case "$when" in
    before)
      [ "${observed#v}" != "${version#v}" ] || rc_fail "$b: $what is already $version, before any publish: a version is published once"
      [ "${observed#v}" = "${recorded#v}" ] || rc_fail "$b: $what moved from $recorded (recorded before this run's publish) to $observed"
      echo "OK $b: $what is $observed now; after the publish it must be $version"
      ;;
    now)
      [ "${observed#v}" = "${version#v}" ] || rc_fail "$b: $what is '$observed', not $version: a plain, unpinned install does not get the stable release"
      echo "OK $b: $what is $version"
      ;;
    *) rc_fail "check_is_default: before or now, not '$when'" ;;
  esac
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

# ---- registry reads (network; the selftest replaces exactly these five) ----

http_get() { # <url> <out>: prints the HTTP status
  curl -sS -A "$RC_UA" -H 'Accept: application/json' --retry 3 --retry-all-errors -o "$2" -w '%{http_code}' "$1"
}
rc_tmp() { mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/release-channel.XXXXXX"; }

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

npm_packument() { # <out>
  local status
  status="$(http_get "https://registry.npmjs.org/${NPM_PACKAGE/\//%2f}" "$1")" || rc_fail "npm: the packument read failed"
  [ "$status" = 200 ] || rc_fail "npm: the packument read answered HTTP $status"
}
registry_dist_tag() { # <tag>: npm's dist-tag value now
  local dir; dir="$(rc_tmp)"
  npm_packument "$dir/p.json"
  npm_dist_tag_of "$1" < "$dir/p.json"
}

# default_what <binding>: what registry_default reads, for the messages.
default_what() {
  case "$1" in
    ts) echo "npm's dist-tags.latest" ;;
    python) echo "a plain, unpinned 'uv pip compile $PYPI_PROJECT' (no --pre)" ;;
    rust) echo "crates.io's max_stable_version" ;;
    go) echo "'go list -m $GO_LATEST_MODULE@latest'" ;;
  esac
}

# registry_default <binding>: the version a plain, unpinned install resolves
# now. For Go that is @latest of the selected mode's module path: the 1.x path
# in the dev mode (which a /v2 tag never moves), the /v2 path in the stable
# mode (which `go get github.com/wave-rf/chtypes/go/v2` resolves).
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
      go_query -m -json "$GO_LATEST_MODULE@latest" > "$dir/l.json" 2> "$dir/err" || { cat "$dir/err" >&2; rc_fail "go: $GO_LATEST_MODULE@latest did not resolve"; }
      go_list_version < "$dir/l.json"
      ;;
  esac
}

# registry_presence <binding> <version> <after|twin>: present or absent;
# exits on an error. Go's twin reads the version LIST instead of the version
# itself, so a dry run does not ask the proxy for a tag that does not exist
# yet (that the proxy then remembers the miss for a while is `unverified`).
# Go serves a 2.x only under the /v2 path, and a 1.x only under the 1.x path.
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
      v="${v#v}"
      case "$v" in 2.*) mod="$GO_MODULE" ;; *) mod="$GO_DEFAULT_MODULE" ;; esac
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

# record_default <binding> <version>: the `default` subcommand.
record_default() {
  local b="$1" v="$2" observed
  observed="$(registry_default "$b")"
  case "$MODE" in
    dev)
      is_stable_major "$observed" "$DEFAULT_MAJOR" || rc_fail "$b: the registry's default is '$observed', not a stable $DEFAULT_MAJOR.x — refusing to publish a pre-release over a default that is already wrong"
      ;;
    stable)
      [ -n "$observed" ] || rc_fail "$b: the registry named no default version (an empty read is never a pass)"
      [ "${observed#v}" != "${v#v}" ] || rc_fail "$b: $(default_what "$b") is already $v: a version is published once"
      ;;
  esac
  printf '%s\n' "${observed#v}"
}

# never_default <binding> <version> <recorded> <after|twin>: the dev mode.
never_default() {
  local b="$1" v="$2" recorded="$3" phase="$4" observed presence dev plain wait deadline
  [ "${PRERELEASE:-0}" = 1 ] || rc_fail "never-default asserts a pre-release; the '$MODE' mode is not one"
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

# is_default <binding> <version> <recorded> <after|twin>: the stable mode,
# never-default's opposite. The dev twin's positive control stays: the
# presence check must find <recorded> before its "absent" for <version> means
# anything.
is_default() {
  local b="$1" v="$2" recorded="$3" phase="$4" observed presence plain wait deadline
  [ "$MODE" = stable ] || rc_fail "is-default asserts a stable release; the '$MODE' mode is not one"
  [ -n "$recorded" ] || rc_fail "$b: no recorded default, so there is nothing to compare against (the record step's output did not reach this job?)"
  [ "${recorded#v}" != "${v#v}" ] || rc_fail "$b: the recorded default is already $v: a version is published once"
  case "$phase" in
    twin)
      observed="$(registry_default "$b")"
      check_is_default "$b" "$observed" "$recorded" "$v" before "$(default_what "$b")"
      if [ "$b" = rust ]; then
        plain="$(cargo_plain_resolution)"
        check_is_default rust "$plain" "$recorded" "$v" before "a plain 'cargo add $CRATE'"
      fi
      presence="$(registry_presence "$b" "$recorded" twin)"
      [ "$presence" = present ] || rc_fail "$b: the presence check does not find the recorded default $recorded itself, so its 'absent' below would prove nothing (positive control)"
      echo "OK $b: the presence check finds $recorded (positive control)"
      presence="$(registry_presence "$b" "$v" twin)"
      [ "$presence" = absent ] || rc_fail "$b: $v is already on the registry; a version is published once"
      echo "OK $b: $v is not on the registry yet"
      echo "twin OK: $b installs $recorded by default now, the registry does not have $v yet, and after the publish a plain install must resolve $v"
      ;;
    after)
      wait="${RELEASE_CHANNEL_WAIT:-$(case "$b" in ts) echo 3600 ;; go) echo 1200 ;; *) echo 900 ;; esac)}"
      deadline=$((SECONDS + wait))
      while :; do
        observed="$(registry_default "$b")"
        [ -n "$observed" ] || rc_fail "$b: the registry named no default version (an empty read is never a pass)"
        [ "${observed#v}" = "${recorded#v}" ] || [ "${observed#v}" = "${v#v}" ] || rc_fail "$b: $(default_what "$b") is '$observed', neither $recorded (recorded before the publish) nor $v: something else moved the default during this run"
        presence="$(registry_presence "$b" "$v" after)"
        if [ "$presence" = present ] && [ "${observed#v}" = "${v#v}" ]; then break; fi
        [ "$SECONDS" -lt "$deadline" ] || rc_fail "$b: $v did not become the default within ${wait}s (present: $presence; $(default_what "$b"): $observed)"
        echo "$b: not yet (present: $presence; $(default_what "$b"): $observed); retrying in 30s"
        sleep 30
      done
      check_is_default "$b" "$observed" "$recorded" "$v" now "$(default_what "$b")"
      if [ "$b" = rust ]; then
        plain="$(cargo_plain_resolution)"
        check_is_default rust "$plain" "$recorded" "$v" now "a plain 'cargo add $CRATE'"
      fi
      echo "after OK: $v is served, and a plain install of $b resolves it"
      ;;
    *) rc_fail "phase is after or twin, not '$phase'" ;;
  esac
}

# default_check <binding> <version> <recorded> <after|twin>: the selected mode's.
default_check() {
  case "$MODE" in
    dev) never_default "$@" ;;
    stable) is_default "$@" ;;
    *) rc_fail "no mode selected" ;;
  esac
}

# Sourced (by scripts/release-verify.sh): the definitions above, nothing run.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then return 0; fi

if [ "${1:-}" = "--selftest" ]; then
  ROOT="$RC_ROOT"
  n=0
  ok() { # name, want, command...
    local name="$1" want="$2" got; shift 2
    got="$("$@" 2>/dev/null)" || { echo "selftest FAIL: $name: refused, wanted '$want'" >&2; "$@" >&2 || true; exit 1; }
    [ "$got" = "$want" ] || { echo "selftest FAIL: $name: wanted '$want', got '$got'" >&2; exit 1; }
    echo "selftest ok: $name -> '$got'"; n=$((n + 1))
  }
  passes() { # name, phrase the output must carry, command...
    local name="$1" phrase="$2" got; shift 2
    got="$("$@" 2>&1)" || { echo "selftest FAIL: $name: refused: $got" >&2; exit 1; }
    grep -qF -- "$phrase" <<<"$got" || { echo "selftest FAIL: $name: passed, but without '$phrase': $got" >&2; exit 1; }
    echo "selftest ok: $name passes ('$phrase')"; n=$((n + 1))
  }
  refused() { # name, phrase the refusal must carry, command...
    local name="$1" phrase="$2" err; shift 2
    if err="$("$@" 2>&1 >/dev/null)"; then echo "selftest FAIL: $name: was not refused" >&2; exit 1; fi
    grep -qF -- "$phrase" <<<"$err" || { echo "selftest FAIL: $name: refused, but without '$phrase': $err" >&2; exit 1; }
    echo "selftest ok: $name refused ('$phrase')"; n=$((n + 1))
  }
  me="${BASH_SOURCE[0]}"

  # ---- The mode a tag selects: every row of the derivation (#597) ----
  ok "a dev tag selects the dev mode" "dev" bash "$me" mode ts ts/v2.0.0-dev.0
  ok "a later dev tag" "dev" bash "$me" mode go go/v2.0.0-dev.17
  ok "2.0.0 selects the stable mode" "stable" bash "$me" mode rust rust/v2.0.0
  ok "a later stable 2.x" "stable" bash "$me" mode python python/v2.1.3
  ok "a dev tag's version" "2.0.0-dev.0" bash "$me" tag ts ts/v2.0.0-dev.0
  ok "a stable tag's version" "2.0.0" bash "$me" tag go go/v2.0.0
  refused "a 1.x tag" "1.x releases are tagged from main" bash "$me" mode ts ts/v1.1.0
  refused "a 1.x tag's version" "1.x releases are tagged from main" bash "$me" tag rust rust/v1.0.4
  refused "another pre-release kind" "is not releasable from this branch" bash "$me" mode python python/v2.0.0-rc.1
  refused "a dev tag with no number" "is not releasable from this branch" bash "$me" mode ts ts/v2.0.0-dev
  refused "a leading zero (PEP 440 would read it as another number)" "is not releasable from this branch" bash "$me" mode python python/v2.0.0-dev.01
  refused "a leading zero in a stable version" "is not releasable from this branch" bash "$me" mode go go/v2.01.0
  refused "build metadata on a dev tag" "is not releasable from this branch" bash "$me" mode rust rust/v2.0.0-dev.0+x
  refused "build metadata on a stable tag" "is not releasable from this branch" bash "$me" mode rust rust/v2.0.0+x
  refused "a 2.0.1 dev" "is not releasable from this branch" bash "$me" mode ts ts/v2.0.1-dev.0
  refused "the PEP 440 spelling as a tag" "is not releasable from this branch" bash "$me" mode python python/v2.0.0.dev0
  refused "a two-part version" "is not releasable from this branch" bash "$me" mode ts ts/v2.0
  refused "a 3.x" "is not releasable from this branch" bash "$me" mode go go/v3.0.0
  refused "another binding's tag" "is not a ts release tag" bash "$me" mode ts rust/v2.0.0
  refused "no v" "is not a go release tag" bash "$me" mode go go/2.0.0
  refused "an unknown binding" "unknown binding" bash "$me" mode java java/v2.0.0

  # ---- The dev mode: rule r6's values, unchanged ----
  channel_select dev
  ok "the dev mode's channel" "v2-dev" printf '%s' "$CHANNEL"
  [ "$NPM_DIST_TAG" != latest ] || { echo "selftest FAIL: the dev mode publishes to npm's latest" >&2; exit 1; }
  ok "the dev key id is sha256-first16hex of the dev key" "$KEY_ID" python3 -c 'import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.argv[1])).hexdigest()[:16])' "$KEY"
  for fact in "$KEY" "$KEY_ID" "$REGISTRY_BASE" "$GO_MODULE"; do
    grep -qF -- "$fact" "$ROOT/spec/abi-v2/docs.md" || { echo "selftest FAIL: rule r6 in spec/abi-v2/docs.md does not name $fact" >&2; exit 1; }
  done
  echo "selftest ok: the dev key, key id, registry and module path are rule r6's"; n=$((n + 1))
  grep -q "^#define CHS_ABI_VERSION $ABI\$" "$ROOT/$HEADER" || { echo "selftest FAIL: $HEADER is not generation $ABI" >&2; exit 1; }
  echo "selftest ok: $HEADER is generation $ABI"; n=$((n + 1))
  ok "get, of the dev mode, by its tag" "dev" bash "$me" get ts ts/v2.0.0-dev.3 NPM_DIST_TAG
  ok "get <NAME> alone is the dev mode (cache-interop.py)" "v2-dev" bash "$me" get CACHE_SUBROOT
  ok "the dev mode is a pre-release" "1" bash "$me" get python python/v2.0.0-dev.3 PRERELEASE

  # ---- The stable mode: the production generation-2 channel, from constants.json ----
  ok "the stable mode's registry" "https://registry.wavehouse.dev/chtypes/v2" bash "$me" get go go/v2.0.0 REGISTRY_BASE
  ok "the stable mode's release key id" "deb275922dbff76e" bash "$me" get ts ts/v2.0.0 KEY_ID
  ok "the stable mode's cache subroot" "v2" bash "$me" get rust rust/v2.0.0 CACHE_SUBROOT
  ok "the stable mode's channel name" "v2" bash "$me" get rust rust/v2.0.0 CHANNEL
  ok "the stable mode's record schema" "2" bash "$me" get rust rust/v2.0.0 RECORD_SCHEMA
  ok "the stable mode's abi" "2" bash "$me" get rust rust/v2.0.0 ABI
  ok "the stable mode publishes under npm's latest" "latest" bash "$me" get ts ts/v2.0.0 NPM_DIST_TAG
  ok "the stable mode is not a pre-release" "0" bash "$me" get python python/v2.0.0 PRERELEASE
  ok "the stable mode resolves no alias" "0" bash "$me" get go go/v2.0.0 FP_ALIAS
  ok "the stable read-back window covers production's 300 s tag cache" "360" bash "$me" get go go/v2.0.0 READBACK_WINDOW
  ok "the stable mode's Go default install is @latest of /v2" "$GO_MODULE" bash "$me" get go go/v2.0.0 GO_LATEST_MODULE
  ok "the dev mode's Go default install is @latest of the 1.x path" "$GO_DEFAULT_MODULE" bash "$me" get go go/v2.0.0-dev.3 GO_LATEST_MODULE
  channel_select stable
  ok "the release key id is sha256-first16hex of the release key" "$KEY_ID" python3 -c 'import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.argv[1])).hexdigest()[:16])' "$KEY"
  [ "$KEY" != "$DEV_KEY" ] && [ "$REGISTRY_BASE" != "$DEV_REGISTRY_BASE" ] || { echo "selftest FAIL: the stable mode carries the dev key or registry" >&2; exit 1; }
  echo "selftest ok: the stable mode carries neither the staging key nor the staging registry"; n=$((n + 1))
  # A constants.json the script cannot verify against one registry and one key is refused.
  t="$(mktemp -d)"; mkdir -p "$t/spec/fetch-v1"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["prod_v2"]["default_bases"].append("https://mirror.example/chtypes/v2"); json.dump(d, open(sys.argv[2], "w"))' "$ROOT/spec/fetch-v1/constants.json" "$t/spec/fetch-v1/constants.json"
  refused "a production channel with two bases" "could not read the production generation-2 channel" bash -c '. "$1"; RC_ROOT="$2"; channel_select stable' _ "$me" "$t"
  rm -rf "$t"

  # ---- The stand-in: stable mode, dry runs only ----
  channel_select stable
  ok "the stand-in points the production channel at the staging dev repository" "$DEV_REGISTRY_BASE $DEV_KEY_ID v2" env GITHUB_EVENT_NAME=workflow_dispatch bash -c '. "$1"; channel_select stable; channel_standin true; echo "$REGISTRY_BASE $KEY_ID $CACHE_SUBROOT"' _ "$me"
  refused "the stand-in on a real run" "the stand-in is for a dry run only" bash -c '. "$1"; channel_select stable; channel_standin false' _ "$me"
  refused "the stand-in on a tag push" "this run is a tag push" bash -c '. "$1"; channel_select stable; GITHUB_EVENT_NAME=push channel_standin true' _ "$me"
  refused "the stand-in in the dev mode" "the stand-in is the stable mode's" bash -c '. "$1"; channel_select dev; channel_standin true' _ "$me"

  # ---- THE GATE, row by row (#597) ----
  fp="$(sed -n 's/^#define CHS_ABI_FINGERPRINT "\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$ROOT/$HEADER")"
  [[ "$fp" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "selftest FAIL: $HEADER has no fingerprint" >&2; exit 1; }
  for b in go python ts rust; do
    ok "the gate reads $b's own generated fingerprint, the header's" "$fp" binding_fingerprint "$b" "$ROOT"
  done
  ok "a binding constant that is missing reads as nothing" "" binding_fingerprint go "$(mktemp -d)"
  other="sha256:$(printf 'e%.0s' {1..64})"
  channel_select dev
  passes "dev mode: no gate, not a dry run, no variable" "the dev mode has no gate" stable_gate false "" "$fp" unstable
  channel_select stable
  passes "stable, dry run, the variable unset: allowed to the dry run's end" "allowed to run to the dry run's end" stable_gate true "" "$fp" unstable
  passes "stable, dry run, the variable on another fingerprint: allowed to the dry run's end" "allowed to run to the dry run's end" stable_gate true "$other" "$fp" unstable
  refused "stable, a real run, the variable unset" "$LOCKED_FP_VARIABLE is not set" stable_gate false "" "$fp" locked
  refused "stable, a real run, the variable on another fingerprint" "this binding is built against $fp" stable_gate false "$other" "$fp" locked
  passes "stable, a real run, the variable on this fingerprint, the spec locked: allowed" "equals this binding's fingerprint" stable_gate false "$fp" "$fp" locked
  refused "stable, a real run, the variable matches but the spec is not locked" "not 'locked'" stable_gate false "$fp" "$fp" unstable
  refused "stable, a real run, the binding's fingerprint unreadable" "could not be read" stable_gate false "$fp" "" locked
  refused "a dry_run that is neither true nor false" "dry_run is true or false" stable_gate yes "" "$fp" locked
  # The subcommand reads the variable from the environment and the fingerprint from this tree.
  refused "gate subcommand: stable tag, real run, no variable" "$LOCKED_FP_VARIABLE is not set" env -u "$LOCKED_FP_VARIABLE" bash "$me" gate ts ts/v2.0.0 false
  # This tree's own spec decides the next row: refused until the lock, allowed after it.
  if [ "$(spec_stability "$ROOT")" = locked ]; then
    passes "gate subcommand: stable tag, real run, the variable on this tree's fingerprint, the spec locked" "equals this binding's fingerprint" env "$LOCKED_FP_VARIABLE=$fp" bash "$me" gate go go/v2.0.0 false
  else
    refused "gate subcommand: stable tag, real run, the variable on this tree's fingerprint, the spec $(spec_stability "$ROOT")" "not 'locked'" env "$LOCKED_FP_VARIABLE=$fp" bash "$me" gate go go/v2.0.0 false
  fi
  passes "gate subcommand: stable tag, dry run" "allowed to run to the dry run's end" env -u "$LOCKED_FP_VARIABLE" bash "$me" gate rust rust/v2.0.0 true
  passes "gate subcommand: dev tag, real run" "the dev mode has no gate" env -u "$LOCKED_FP_VARIABLE" bash "$me" gate python python/v2.0.0-dev.4 false

  # ---- The Go module path ----
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
  rm -rf "$t"

  # ---- The dev mode's assertion on the default, unchanged ----
  channel_select dev
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
  # ---- The stable mode's assertion, the opposite ----
  channel_select stable
  ok "stable, after: the default is the release" "OK ts: npm's latest is 2.0.0" check_is_default ts 2.0.0 1.1.0 2.0.0 now "npm's latest"
  ok "stable, after: Go spells it with a v" "OK go: the registry default is 2.0.0" check_is_default go v2.0.0 v2.0.0-dev.6 2.0.0 now
  refused "stable, after: the default stayed on the 1.x" "is '1.1.0', not 2.0.0" check_is_default ts 1.1.0 1.1.0 2.0.0 now
  refused "stable, after: an empty read" "named no default" check_is_default rust "" 1.1.0 2.0.0 now
  ok "stable, twin: the default is the recorded one" "OK python: the registry default is 1.1.0 now; after the publish it must be 2.0.0" check_is_default python 1.1.0 1.1.0 2.0.0 before
  refused "stable, twin: the release is already the default" "is already 2.0.0" check_is_default rust 2.0.0 1.1.0 2.0.0 before
  refused "stable, twin: the default moved since the record" "moved from 1.1.0" check_is_default rust 1.1.1 1.1.0 2.0.0 before
  refused "stable: no recorded default" "nothing to compare against" is_default ts 2.0.0 "" twin
  refused "stable: the recorded default is the release" "is already 2.0.0" is_default ts 2.0.0 2.0.0 twin
  refused "is-default in the dev mode" "is-default asserts a stable release" bash -c '. "$1"; channel_select dev; is_default ts 2.0.0-dev.1 1.1.0 twin' _ "$me"

  # ---- Both modes end to end, against RECORDED registry answers ----
  # The five network reads are replaced by these stubs, each answering from
  # one record: a registry's answer as it would be after a publish (or before,
  # for a twin). Every assertion above then runs unchanged.
  rec_npm="" rec_crate="" rec_crate_versions="" rec_pypi_default="" rec_pypi_versions=""
  rec_go_v1_latest="" rec_go_v2_latest="" rec_go_v1_versions="" rec_go_v2_versions="" rec_cargo_plain=""
  http_get() {
    case "$1" in
      https://registry.npmjs.org/*) printf '%s' "$rec_npm" > "$2"; echo 200 ;;
      "https://crates.io/api/v1/crates/$CRATE") printf '%s' "$rec_crate" > "$2"; echo 200 ;;
      "https://crates.io/api/v1/crates/$CRATE/"*)
        if grep -qxF -- "${1##*/}" <<<"$rec_crate_versions"; then echo '{"version":{}}' > "$2"; echo 200; else echo '{"errors":[{"detail":"Not Found"}]}' > "$2"; echo 404; fi ;;
      *) echo "selftest: no recorded answer for $1" >&2; echo 599 ;;
    esac
  }
  uv_resolve() {
    case "$1" in
      "$PYPI_PROJECT") printf '%s==%s\n' "$PYPI_PROJECT" "$rec_pypi_default" > "$2" ;;
      "$PYPI_PROJECT=="*)
        if grep -qxF -- "${1#*==}" <<<"$rec_pypi_versions"; then printf '%s\n' "$1" > "$2"; else
          printf '  x No solution found when resolving dependencies:\n  `-> Because there is no version of %s and you require %s, we can conclude\n' "$1" "$1" > "$3"; return 1
        fi ;;
    esac
  }
  go_query() {
    local last="${*: -1}" mod versions
    case "$last" in "$GO_MODULE"*) versions="$rec_go_v2_versions" ;; *) versions="$rec_go_v1_versions" ;; esac
    case "$*" in
      "-m -json $GO_MODULE@latest") printf '{"Path":"%s","Version":"%s"}\n' "$GO_MODULE" "$rec_go_v2_latest" ;;
      "-m -json $GO_DEFAULT_MODULE@latest") printf '{"Path":"%s","Version":"%s"}\n' "$GO_DEFAULT_MODULE" "$rec_go_v1_latest" ;;
      "-m -versions -json "*) python3 -c 'import json,sys; print(json.dumps({"Path": sys.argv[1], "Versions": sys.argv[2].split()}))' "$last" "$versions" ;;
      "-m -json "*@v*)
        mod="${last%@*}"
        if grep -qxF -- "${last##*@}" <<<"$(tr ' ' '\n' <<<"$versions")"; then printf '{"Path":"%s","Version":"%s"}\n' "$mod" "${last##*@}"; else
          printf 'go: %s: reading https://proxy.golang.org/%s/@v/%s.info: 404 Not Found\n\tserver response: not found: %s: invalid version: unknown revision\n' "$last" "$mod" "${last##*@}" "$last" >&2; return 1
        fi ;;
      *) echo "selftest: no recorded answer for go list $*" >&2; return 1 ;;
    esac
  }
  cargo_plain_resolution() { printf '%s\n' "$rec_cargo_plain"; }
  export RELEASE_CHANNEL_WAIT=0   # a record that is not yet right fails at once, never polls

  # Before 2.0.0: the registries as they are on the eve of the stable release.
  eve() {
    rec_npm='{"dist-tags":{"latest":"1.1.0","dev":"2.0.0-dev.6"},"versions":{"1.1.0":{},"2.0.0-dev.6":{}}}'
    rec_crate='{"crate":{"max_version":"2.0.0-dev.6","max_stable_version":"1.1.0"}}'; rec_crate_versions=$'1.1.0\n2.0.0-dev.6'
    rec_pypi_default=1.1.0; rec_pypi_versions=$'1.1.0\n2.0.0.dev6'
    rec_go_v1_latest=v1.1.0; rec_go_v1_versions="v1.0.4 v1.1.0"
    rec_go_v2_latest=v2.0.0-dev.6; rec_go_v2_versions="v2.0.0-dev.5 v2.0.0-dev.6"
    rec_cargo_plain=1.1.0
  }
  # After 2.0.0, as a stable release must leave them: 2.0.0 IS the default.
  published_default() {
    eve
    rec_npm='{"dist-tags":{"latest":"2.0.0","dev":"2.0.0-dev.6"},"versions":{"1.1.0":{},"2.0.0-dev.6":{},"2.0.0":{}}}'
    rec_crate='{"crate":{"max_version":"2.0.0","max_stable_version":"2.0.0"}}'; rec_crate_versions=$'1.1.0\n2.0.0-dev.6\n2.0.0'
    rec_pypi_default=2.0.0; rec_pypi_versions=$'1.1.0\n2.0.0.dev6\n2.0.0'
    rec_go_v2_latest=v2.0.0; rec_go_v2_versions="v2.0.0-dev.5 v2.0.0-dev.6 v2.0.0"
    rec_cargo_plain=2.0.0
  }
  # After 2.0.0, published but NOT the default: what a stable release must refuse.
  published_not_default() {
    published_default
    rec_npm='{"dist-tags":{"latest":"1.1.0","next":"2.0.0"},"versions":{"1.1.0":{},"2.0.0":{}}}'
    rec_crate='{"crate":{"max_version":"2.0.0","max_stable_version":"1.1.0"}}'
    rec_pypi_default=1.1.0
    rec_go_v2_latest=v2.0.0-dev.6
    rec_cargo_plain=1.1.0
  }
  channel_select stable
  for b in ts python rust go; do
    case "$b" in go) rec=2.0.0-dev.6 ;; *) rec=1.1.0 ;; esac
    eve
    passes "stable twin, $b: the eve of 2.0.0" "twin OK: $b installs $rec by default now" is_default "$b" 2.0.0 "$rec" twin
    published_default
    passes "stable after, $b: 2.0.0 published and the default (a passing record)" "after OK: 2.0.0 is served, and a plain install of $b resolves it" is_default "$b" 2.0.0 "$rec" after
    refused "stable twin, $b: 2.0.0 already published" "is already" is_default "$b" 2.0.0 "$rec" twin
    published_not_default
    refused "stable after, $b: 2.0.0 published but not the default (a failing record)" "did not become the default" is_default "$b" 2.0.0 "$rec" after
  done
  published_default; rec_npm='{"dist-tags":{"latest":"1.2.0"},"versions":{"1.1.0":{},"1.2.0":{},"2.0.0":{}}}'
  refused "stable after, ts: the default moved to a third version" "neither 1.1.0" is_default ts 2.0.0 1.1.0 after
  published_default; rec_cargo_plain=1.1.0
  refused "stable after, rust: crates.io says 2.0.0 but a plain cargo add still locks 1.1.0" "a plain 'cargo add chtypes' is '1.1.0', not 2.0.0" is_default rust 2.0.0 1.1.0 after
  # The dev mode on the same records: 2.0.0-dev.7 published, never the default.
  channel_select dev
  dev_published() {
    eve
    rec_npm='{"dist-tags":{"latest":"1.1.0","dev":"2.0.0-dev.7"},"versions":{"1.1.0":{},"2.0.0-dev.6":{},"2.0.0-dev.7":{}}}'
    rec_crate_versions=$'1.1.0\n2.0.0-dev.6\n2.0.0-dev.7'; rec_pypi_versions=$'1.1.0\n2.0.0.dev6\n2.0.0.dev7'
    rec_go_v2_versions="v2.0.0-dev.6 v2.0.0-dev.7"
  }
  for b in ts python rust go; do
    case "$b" in python) v=2.0.0.dev7 ;; *) v=2.0.0-dev.7 ;; esac
    eve
    passes "dev twin, $b: unchanged" "twin OK: $b installs 1.1.0 by default" never_default "$b" "$v" 1.1.0 twin
    dev_published
    passes "dev after, $b: published, never the default (unchanged)" "after OK: $v is served, and $b still installs 1.1.0 by default" never_default "$b" "$v" 1.1.0 after
  done
  dev_published; rec_npm='{"dist-tags":{"latest":"2.0.0-dev.7","dev":"2.0.0-dev.7"},"versions":{"1.1.0":{},"2.0.0-dev.7":{}}}'
  refused "dev after, ts: the pre-release became latest" "DEFAULT is now the pre-release" never_default ts 2.0.0-dev.7 1.1.0 after
  unset RELEASE_CHANNEL_WAIT

  # ---- The parsers, on what the registries and tools print ----
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
  # ---- The subcommands parse their arguments ----
  refused "get an unknown name" "no constant named" bash "$me" get NOPE
  refused "get with a tag but no name" "usage: get" bash "$me" get ts ts/v2.0.0
  refused "the tag subcommand refuses 1.x" "1.x releases are tagged from main" bash "$me" tag ts ts/v1.1.0
  refused "default-check refuses a 1.x tag before any read" "1.x releases are tagged from main" bash "$me" default-check ts ts/v1.1.0 1.1.0 1.0.4 twin
  refused "gate refuses a malformed tag" "is not releasable from this branch" bash "$me" gate ts ts/v2.0.0-rc.1 true
  echo "selftest: $n cases, every refusal fired and every good input passed"
  exit 0
fi

cmd="${1:-}"
[ "$#" -gt 0 ] && shift
need_binding() { case "$1" in rust | ts | python | go) ;; *) rc_fail "unknown binding '$1'" ;; esac; }
case "$cmd" in
  mode)
    [ "$#" = 2 ] || rc_fail "usage: mode <rust|ts|python|go> <tag>"
    channel_tag "$1" "$2" > /dev/null
    printf '%s\n' "$MODE"
    ;;
  tag) [ "$#" = 2 ] || rc_fail "usage: tag <rust|ts|python|go> <tag>"; channel_tag "$1" "$2" ;;
  get)
    case "$#" in
      1) channel_select dev; name="$1" ;;
      3) channel_tag "$1" "$2" > /dev/null; name="$3" ;;
      *) rc_fail "usage: get <rust|ts|python|go> <tag> <NAME>, or get <NAME> for the dev mode" ;;
    esac
    case "$name" in
      MODE | CHANNEL | VERSION_RE | VERSION_FORM | PRERELEASE | NPM_DIST_TAG | DEFAULT_MAJOR | ABI | HEADER | REGISTRY_BASE | KEY | KEY_ID | CACHE_SUBROOT | RECORD_SCHEMA | FP_ALIAS | READBACK_WINDOW | GO_MODULE | GO_LATEST_MODULE | GO_DEFAULT_MODULE | NPM_PACKAGE | PYPI_PROJECT | CRATE | LOCKED_FP_VARIABLE)
        printf '%s\n' "${!name}" ;;
      *) rc_fail "no constant named '$name'" ;;
    esac
    ;;
  go-module) [ "$#" = 1 ] || rc_fail "usage: go-module <go.mod>"; channel_go_module "$1" ;;
  gate)
    [ "$#" = 3 ] || rc_fail "usage: gate <rust|ts|python|go> <tag> <true|false: whether this run is a dry run>"
    channel_tag "$1" "$2" > /dev/null
    fp="$(binding_fingerprint "$1" "$RC_ROOT")"
    echo "gate: $2 selects the $MODE mode; $1 is built against ${fp:-<unreadable>}"
    stable_gate "$3" "${!LOCKED_FP_VARIABLE:-}" "$fp" "$(spec_stability "$RC_ROOT")"
    ;;
  default)
    [ "$#" = 2 ] || rc_fail "usage: default <rust|ts|python|go> <tag>"
    need_binding "$1"
    channel_tag "$1" "$2" > /dev/null   # refuses, or selects the tag's mode
    # The stable mode compares the default with the version; PEP 440 spells a
    # stable 2.N.N as the tag does.
    record_default "$1" "${2#"$1"/v}"
    ;;
  default-check)
    [ "$#" = 5 ] || rc_fail "usage: default-check <rust|ts|python|go> <tag> <version> <recorded> <after|twin>"
    need_binding "$1"
    channel_tag "$1" "$2" > /dev/null
    default_check "$1" "$3" "$4" "$5"
    ;;
  *) rc_fail "usage: release-channel.sh mode|tag|get|go-module|gate|default|default-check|--selftest (see the header)" ;;
esac
