#!/usr/bin/env bash
# release-verify.sh — the post-publish check of every release workflow, in ONE
# place, so the dry run and the tag path cannot drift. On this branch it
# verifies a release of either mode scripts/release-channel.sh defines (THE
# MODES there: the version alone selects it), and refuses any other version;
# 1.x releases are verified by main's copy of this script.
#
#   scripts/release-verify.sh <rust|ts|python|go> registry <version>
#   scripts/release-verify.sh <rust|ts|python|go> local <version> <source>
#   scripts/release-verify.sh <rust|ts|python|go> standin <version> <source>
#   scripts/release-verify.sh --selftest
#
# `registry` installs the PUBLISHED package from its public registry, anonymously,
# retrying for registry lag. `local` installs the package a dry run just built,
# and skips only that retry loop. <source> is, per binding: rust, the crate
# directory; ts, the packed tarball; python, the built wheel; go, the module
# directory (a `replace` points the clean module at it). <version> is the tag's
# version (2.0.0-dev.N, or 2.N.N).
#
# `standin` is `local` for a STABLE dry run, against the stand-in (#597):
# production chtypes/v2 holds nothing until the lock, so the production
# channel's base and trust point at the staging dev repository and the staging
# key, through that channel's own overrides (CHTYPES_ARTIFACTS_URL and
# CHTYPES_TRUSTED_KEYS; scripts/release-channel.sh, channel_standin). It is
# refused unless RELEASE_DRY_RUN=true and the run is not a tag push: a real
# release is verified against production and nothing else. Everything else
# stays the stable mode's (the v2 cache subroot, record schema 2, abi 2, no
# alias step), so the stand-in proves the same six checks against another
# registry and key. Before the lock (spec/abi-v2/abi.json not `locked`) no
# non-test binary of this repository speaks the production channel: it is
# reached only through each binding's test-only selector (UseProdV2ForTests,
# use_prod_v2_for_tests, useProdV2ForTests, use_prod_v2_for_tests). So the
# stand-in then builds the binding's own TEST binary around its CLI and its
# public API (the `selector_*` functions below: a Go test binary, pytest,
# vitest, a cargo test binary), selects the production channel through that
# selector, and runs every check through it. Once the spec is locked the
# shipped CLI speaks the production channel by default, and the stand-in runs
# it as is.
#
# Every mode then runs six checks, in a clean directory with a clean cache, and
# reports every one of them: PASS, FAIL, or NOT RUN when an earlier failure left
# nothing honest to check (a NOT RUN is never a pass). The script fails unless
# all six pass. The bracketed names are the selected mode's, from
# scripts/release-channel.sh:
#   1. fingerprint: the binding's generation-[ABI] fingerprint constant equals
#      CHS_ABI_FINGERPRINT in [HEADER] (include/v2/chtypes.h), never the v1
#      header;
#   2. cache root: `chtypes where` names <CHTYPES_CACHE>/[CACHE_SUBROOT] (v2-dev
#      in the dev mode, rule r5 of spec/abi-v2/docs.md; v2 in the stable mode),
#      so the CLI speaks the mode's channel, not a 1.x one or the other mode's;
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
#      discarded; check 4 still fetches into the real one. The stable mode's
#      production repository caches its tags and tags/list for 300 s, so there
#      this check retries for [READBACK_WINDOW] seconds (360) before it fails;
#      the dev mode reads once, as it always did;
#   4. fetch: `chtypes fetch <newest line>` installs into
#      <CHTYPES_CACHE>/[CACHE_SUBROOT]/unpacked/sha256/<hex>, and sha256:<hex>
#      is the platform manifest [REGISTRY_BASE] serves THIS binding for that
#      line on this platform. On a channel with [FP_ALIAS] (the dev channel,
#      docs/guides/fetch-v1.md §3) that is the manifest of the alias
#      <line>--fp-<the header's fingerprint> when the registry serves it, and
#      the line's own tag's when it answers the alias 404; the CLI resolves
#      exactly so, and any other answer is no manifest at all, as the CLI
#      does not fall back then either. The production channel has no alias
#      step, so there it is the line's own tag's. The probe of check 3
#      compares the same way. An install directory is named by the digest of
#      the manifest it was verified from, so this proves the bytes the CLI
#      installed are that registry's, whatever the CLI was configured with;
#   5. record: that directory's verified.json is record schema
#      [RECORD_SCHEMA], signed by the mode's key [KEY_ID] (the staging key in
#      the dev mode and the stand-in, the release key in the stable mode), and
#      its predicate names abi [ABI] and the header's fingerprint;
#   6. load: the public API loads that line, and the library's own build_info
#      reports abi [ABI] and the header's fingerprint.
#
# Checks 3 to 6 are the registry assertion. The dev bindings are hard-wired to
# the staging dev repository with no override (rule r6), so a verify that ran
# against the production v1 registry and "passed" is exactly the failure this
# exists to refuse. When check 3 fails nothing is fetched: a line the CLI was
# not shown to have listed from the mode's registry is never downloaded.
#
# Where each binding keeps its generation-[ABI] constant (`abi<N>` beside v1's
# `abi1`) is the bindings' to decide; the paths are in the fingerprint_*
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
# The modes (release_mode, channel_select, channel_standin, spec_stability)
# and what each defines: VERSION_RE, ABI, HEADER, REGISTRY_BASE, KEY, KEY_ID,
# CACHE_SUBROOT, RECORD_SCHEMA, FP_ALIAS, READBACK_WINDOW, GO_MODULE,
# NPM_PACKAGE, and parse_json and http_get.
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

# alias_tag <line> <fingerprint>: the dev channel's own-fingerprint alias of
# <line>, <line>--fp-<64 hex> (docs/guides/fetch-v1.md §3).
alias_tag() { printf '%s--fp-%s\n' "$1" "${2#sha256:}"; }
# served_ref <the alias's HTTP status>: which tag the CLI resolves, by the rule
# it follows (§3): `alias` when the registry serves the alias, `line` when it
# answers the alias 404, and nothing otherwise (the CLI does not fall back).
served_ref() { case "$1" in 200) echo alias ;; 404) echo line ;; *) echo "" ;; esac; }
# fetch_index <ref>: GET the channel registry's index for <ref> into
# $work/index.json; stdout is the HTTP status, empty when the request failed.
fetch_index() {
  local s
  s="$(curl -sS -A "$RC_UA" --retry 3 --retry-all-errors -o "$work/index.json" -w '%{http_code}' \
    -H 'Accept: application/vnd.oci.image.index.v1+json' "$api/manifests/$1")" || s=""
  printf '%s' "$s"
}
# staging_manifest <line>: the platform manifest digest the channel's registry
# serves THIS binding for <line> (check 4): with FP_ALIAS, the alias for the
# header's fingerprint ($want) when it is served, else, on a 404, the line's
# own tag. Leaves the ref it compared and that ref's HTTP status in
# $work/manifest.ref and $work/manifest.status (a $(...) caller cannot see a
# variable). Empty when the registry serves none.
staging_manifest() {
  local ref="$1" s
  if [ "${FP_ALIAS:-0}" = 1 ]; then
    ref="$(alias_tag "$1" "$want")"
    s="$(fetch_index "$ref")"
    if [ "$(served_ref "$s")" = line ]; then
      ref="$1"
      s="$(fetch_index "$ref")"
    fi
  else
    s="$(fetch_index "$ref")"
  fi
  printf '%s' "$ref" > "$work/manifest.ref"
  printf '%s' "$s" > "$work/manifest.status"
  [ "$s" != 200 ] || manifest_for "$os" "$arch" < "$work/index.json"
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

# readback <seconds> <attempt function> <args...>: run one attempt of a check
# until it passes, retrying every 30 s for <seconds> (READBACK_WINDOW: 0 in the
# dev mode, which reads once as it always did; more than the production
# repository's 300 s tag cache in the stable mode). The attempt runs in this
# shell, so what it sets survives, and it leaves what it saw in
# readback_detail, which is printed on every retry: a retry never hides what
# the check found.
readback() {
  local limit="$1" deadline; shift
  deadline=$((SECONDS + limit))
  while :; do
    if "$@"; then return 0; fi
    [ "$SECONDS" -lt "$deadline" ] || return 1
    echo "  not yet: ${readback_detail:-}; the registry caches its tags for up to 300 s, so retrying in 30s" >&2
    sleep 30
  done
}

# ---- the stand-in's selector path: a stable dry run before the lock ----
#
# The production channel is reachable only through each binding's test-only
# selector until the lock, so the stand-in builds the binding's own TEST
# binary around its CLI and its public API (selector_build_<lang>), and every
# `chtypes ...` this script runs, and the load of check 6, goes through it
# (selector_run). Each harness selects the production channel through the
# selector, runs one command line (`cli`) or the load (`load`), and leaves the
# command's stdout, stderr and exit status in CHTYPES_RELEASE_STANDIN_OUT; Rust
# prints them between markers instead (rust_markers), because a test cannot
# redirect println!. None of it is committed: each harness is written here,
# built, and deleted.

# selector_report <dir>: replay a harness's stdout, stderr and exit status as
# the command's own; a harness that left no status did not run the command.
selector_report() {
  local out="$1" code
  if [ ! -f "$out/code" ]; then
    echo "the stand-in's test binary did not report a status for this command; its log:" >&2
    tail -n 40 "$out/runner.log" >&2 || true
    return 1
  fi
  [ ! -f "$out/stdout" ] || cat "$out/stdout"
  [ ! -f "$out/stderr" ] || cat "$out/stderr" >&2
  code="$(cat "$out/code")"
  [[ "$code" =~ ^[0-9]+$ ]] || { echo "the stand-in's test binary reported the status '$code'" >&2; return 1; }
  return "$code"
}

# rust_markers <dir>: libtest's output ($dir/raw) holds the command's stdout
# between `<<<release-standin begin>>>` and
# `<<<release-standin end N>>>`; write it to $dir/stdout, and N to
# $dir/code. Nothing is written without both markers.
rust_markers() {
  awk -v out="$1" '
    /<<<release-standin end [0-9]+>>>/ { if (on) { match($0, /end [0-9]+/); code = substr($0, RSTART + 4, RLENGTH - 4) } on = 0; next }
    on { body = body $0 "\n" }
    /<<<release-standin begin>>>/ { on = 1; body = "" }
    END { if (code != "") { printf "%s", body > (out "/stdout"); print code > (out "/code") } }
  ' "$1/raw"
}

# selector_run <lang> <cli|load> <args...>: one command line (cli), or the
# load of the line <args> names (load), through the binding's test binary
# under the production channel; its stdout, stderr and exit status are the
# command's.
selector_run() {
  local lang="$1" what="$2" out exe; shift 2
  out="$(mktemp -d "$work/selector-run.XXXXXX")"
  (
    export CHTYPES_RELEASE_STANDIN="$what" CHTYPES_RELEASE_STANDIN_ARGS="$*" CHTYPES_RELEASE_STANDIN_OUT="$out"
    [ "$what" != load ] || export CHTYPES_RELEASE_STANDIN_LINE="$1"
    case "$lang" in
      go) "$work/bin/standin-go.test" "$@" ;;
      python) cd "$work/standin" && "$work/venv/bin/python" -m pytest -q -p no:cacheprovider test_release_standin.py ;;
      ts) cd "$work/standin" && CHTYPES_RELEASE_STANDIN_PKG="$work/node_modules/$NPM_PACKAGE" "$ROOT/ts/node_modules/.bin/vitest" run --root "$work/standin" ;;
      rust)
        exe="$(cat "$work/bin/standin-rust.$what")"
        "$exe" --exact "zz_release_standin::release_standin_$what" --nocapture --test-threads=1 > "$out/raw" 2> "$out/stderr" || true
        rust_markers "$out"
        [ -f "$out/code" ] || cat "$out/raw" "$out/stderr"
        ;;
    esac
  ) > "$out/runner.log" 2>&1 || true
  selector_report "$out"
}

selector_build_go() {
  local f="$source_arg/cmd/chtypes/zz_release_standin_test.go"
  cat > "$f" <<GO
package main

// zz_release_standin_test.go: written by scripts/release-verify.sh for a
// stable dry run's stand-in before the lock, built into this package's test
// binary, and deleted; never committed. Only a test binary reaches the
// production generation-2 channel before the lock (ocifetch.UseProdV2ForTests
// panics anywhere else), so this init, which runs before TestMain, selects it,
// runs one command line or the load, and exits: no test ever runs.

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"$GO_MODULE/chtypes"
	"$GO_MODULE/internal/ocifetch"
)

func init() {
	what := os.Getenv("CHTYPES_RELEASE_STANDIN")
	if what == "" {
		return
	}
	out := os.Getenv("CHTYPES_RELEASE_STANDIN_OUT")
	stdout, err := os.Create(filepath.Join(out, "stdout"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	stderr, err := os.Create(filepath.Join(out, "stderr"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ocifetch.UseProdV2ForTests()
	var code int
	if what == "load" {
		code = releaseStandinLoad(os.Getenv("CHTYPES_RELEASE_STANDIN_LINE"), stdout, stderr)
	} else {
		code = run(context.Background(), os.Args[1:], stdout, stderr)
	}
	_ = stdout.Close()
	_ = stderr.Close()
	if err := os.WriteFile(filepath.Join(out, "code"), []byte(strconv.Itoa(code)), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

// releaseStandinLoad is load_go's program, run under the selected channel.
func releaseStandinLoad(line string, stdout, stderr *os.File) int {
	reg, err := chtypes.NewRegistry()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	lib, err := reg.For(line)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x")
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	defer schema.Close()
	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(\`{"x":256}\`))
	if err != nil || batch.Outcome != chtypes.Accepted || len(batch.Rows) != 1 || len(batch.Rows[0].Transformed) != 1 || batch.Rows[0].Transformed[0].Reason != chtypes.ReasonOverflowWrap {
		fmt.Fprintf(stderr, "unexpected answer: %+v, %v\n", batch, err)
		return 1
	}
	fmt.Fprintln(stdout, lib.BuildInfo().ABI, lib.BuildInfo().ABIFingerprint)
	return 0
}
GO
  if ! (cd "$source_arg" && go test -c -o "$work/bin/standin-go.test" ./cmd/chtypes); then
    rm -f "$f"
    fail "the stand-in's Go test binary did not build"
  fi
  rm -f "$f"
}

selector_build_python() {
  local pytest
  pytest="$(python3 -c '
import sys, tomllib
lock = tomllib.load(open(sys.argv[1], "rb"))
got = [p["version"] for p in lock.get("package", []) if p.get("name") == "pytest"]
print(got[0] if len(got) == 1 else "")
' "$ROOT/python/uv.lock")"
  [ -n "$pytest" ] || fail "python/uv.lock pins no single pytest, which the stand-in runs"
  uv pip install --quiet --python "$work/venv/bin/python" "pytest==$pytest"
  mkdir -p "$work/standin"
  printf '[pytest]\n' > "$work/standin/pytest.ini"
  cat > "$work/standin/test_release_standin.py" <<'PY'
# Written by scripts/release-verify.sh for a stable dry run's stand-in before
# the lock; never committed. Only a test run reaches the production
# generation-2 channel before the lock (use_prod_v2_for_tests raises anywhere
# but under pytest), so this one test selects it, runs one command line or the
# load, and leaves its stdout, stderr and exit status for the script.
import contextlib
import os

from chtypes._ocifetch import _channel


def test_release_standin():
    out = os.environ["CHTYPES_RELEASE_STANDIN_OUT"]
    restore = _channel.use_prod_v2_for_tests()
    try:
        with (
            open(os.path.join(out, "stdout"), "w") as so,
            open(os.path.join(out, "stderr"), "w") as se,
            contextlib.redirect_stdout(so),
            contextlib.redirect_stderr(se),
        ):
            if os.environ["CHTYPES_RELEASE_STANDIN"] == "load":
                import chtypes

                library = chtypes.Registry().for_version(os.environ["CHTYPES_RELEASE_STANDIN_LINE"])
                assert library.validate_type("UInt8") == b"UInt8"
                print(library.build_info.abi, library.build_info.abi_fingerprint)
                code = 0
            else:
                from chtypes.__main__ import main

                code = main(os.environ["CHTYPES_RELEASE_STANDIN_ARGS"].split())
    finally:
        restore()
    with open(os.path.join(out, "code"), "w") as f:
        f.write(str(code))
PY
}

selector_build_ts() {
  [ -x "$ROOT/ts/node_modules/.bin/vitest" ] || fail "the stand-in runs the CLI under vitest, from ts/'s own lockfile: run 'pnpm install --frozen-lockfile' in ts/ first (release-ts.yml's verify job does)"
  mkdir -p "$work/standin"
  # No import: the harness lives outside ts/, where 'vitest' does not resolve.
  printf 'export default { test: { globals: true, testTimeout: 900000, include: ["release-standin.test.mjs"] } };\n' > "$work/standin/vitest.config.mjs"
  cat > "$work/standin/release-standin.test.mjs" <<'JS'
// Written by scripts/release-verify.sh for a stable dry run's stand-in before
// the lock; never committed. Only a vitest worker reaches the production
// generation-2 channel before the lock (useProdV2ForTests throws anywhere
// else), so this one test selects it, runs one command line or the load, and
// leaves its stdout, stderr and exit status for the script. The package is
// imported from where the verify installed it, by file URL.
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const pkg = process.env.CHTYPES_RELEASE_STANDIN_PKG;
const out = process.env.CHTYPES_RELEASE_STANDIN_OUT;
const load = (p) => import(pathToFileURL(join(pkg, p)).href);

test('release stand-in', async () => {
  const channel = await load('dist/ocifetch/channel.js');
  const restore = channel.useProdV2ForTests();
  let stdout = '';
  let stderr = '';
  let code = 1;
  try {
    if (process.env.CHTYPES_RELEASE_STANDIN === 'load') {
      const { Registry } = await load('dist/index.js');
      const registry = await Registry.open();
      const lib = await registry.for(process.env.CHTYPES_RELEASE_STANDIN_LINE);
      const type = lib.validateType('UInt8').toString();
      if (type !== 'UInt8') throw new Error('validateType(UInt8) answered ' + JSON.stringify(type));
      stdout = `${lib.buildInfo.abi} ${lib.buildInfo.abiFingerprint}\n`;
      code = 0;
    } else {
      const { runCli } = await load('dist/cli.js');
      const args = (process.env.CHTYPES_RELEASE_STANDIN_ARGS ?? '').split(' ').filter((a) => a !== '');
      code = await runCli(args, {
        stdout: (t) => {
          stdout += t;
        },
        stderr: (t) => {
          stderr += t;
        },
      });
    }
  } catch (err) {
    stderr += `${err && err.stack ? err.stack : err}\n`;
    code = 1;
  } finally {
    restore();
  }
  writeFileSync(join(out, 'stdout'), stdout);
  writeFileSync(join(out, 'stderr'), stderr);
  writeFileSync(join(out, 'code'), String(code));
});
JS
}

selector_build_rust() {
  local main="$source_arg/src/main.rs" lib="$source_arg/src/lib.rs" built=1
  cp "$main" "$work/main.rs.orig"
  cp "$lib" "$work/lib.rs.orig"
  cat >> "$main" <<'RS'

// Written by scripts/release-verify.sh for a stable dry run's stand-in before
// the lock, and removed after the build; never committed. Only a test build
// reaches the production generation-2 channel before the lock
// (`use_prod_v2_for_tests` exists only under cfg(test)), so the stand-in runs
// one command line here, on this test's thread, under that channel.
#[cfg(test)]
mod zz_release_standin {
    #[test]
    fn release_standin_cli() {
        let Ok(args) = std::env::var("CHTYPES_RELEASE_STANDIN_ARGS") else {
            return;
        };
        let _prod = super::ocifetch::channel::use_prod_v2_for_tests();
        let argv: Vec<String> = args.split_whitespace().map(String::from).collect();
        println!("\n<<<release-standin begin>>>");
        let result = match super::parse(&argv) {
            Ok(a) => match a.command.as_str() {
                "fetch" => super::cmd_fetch(&a),
                "verify" => super::cmd_verify(&a),
                "list" => super::cmd_list(&a),
                "where" => super::cmd_where(&a),
                other => Err(super::Usage(format!("unknown command: {other}"))),
            },
            Err(u) => Err(u),
        };
        let code = match result {
            Ok(c) => c,
            Err(super::Usage(message)) => {
                eprintln!("chtypes: {message}");
                super::EXIT_USAGE
            }
        };
        println!("<<<release-standin end {code}>>>");
    }
}
RS
  cat >> "$lib" <<'RS'

// Written by scripts/release-verify.sh for a stable dry run's stand-in before
// the lock, and removed after the build; never committed: load_rust's program,
// on this test's thread, under the production generation-2 channel.
#[cfg(test)]
mod zz_release_standin {
    #[test]
    fn release_standin_load() {
        let Ok(line) = std::env::var("CHTYPES_RELEASE_STANDIN_LINE") else {
            return;
        };
        let _prod = crate::ocifetch::channel::use_prod_v2_for_tests();
        let registry =
            crate::Registry::new(crate::RegistryOptions::default()).expect("Registry::new");
        let lib = registry.for_version(&line).expect("for_version");
        let info = lib.build_info();
        println!(
            "\n<<<release-standin begin>>>\n{} {}\n<<<release-standin end 0>>>",
            info.abi, info.abi_fingerprint
        );
    }
}
RS
  (cd "$source_arg" && cargo test --locked --no-run --message-format=json-render-diagnostics --lib --bin chtypes) > "$work/standin-rust-build.json" || built=0
  cp "$work/main.rs.orig" "$main"
  cp "$work/lib.rs.orig" "$lib"
  [ "$built" = 1 ] || fail "the stand-in's Rust test binaries did not build"
  python3 - "$work/standin-rust-build.json" "$work/bin" <<'PY' || fail "the stand-in's Rust test binaries were not found in cargo's output"
import json, sys
exes = {}
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        m = json.loads(line)
    except ValueError:
        continue
    t = m.get("target") or {}
    if m.get("reason") != "compiler-artifact" or not m.get("executable") or not (m.get("profile") or {}).get("test") or t.get("name") != "chtypes":
        continue
    exes["cli" if "bin" in t.get("kind", []) else "load"] = m["executable"]
if set(exes) != {"cli", "load"}:
    sys.exit(f"cargo built {exes}; wanted the bin's and the lib's test executables")
for kind, exe in exes.items():
    open(f"{sys.argv[2]}/standin-rust.{kind}", "w", encoding="utf-8").write(exe)
PY
}

# selector_build <lang>: build the harness, and make $CLI the command line
# that runs through it.
selector_build() {
  mkdir -p "$work/bin"
  "selector_build_$1"
  cat > "$work/bin/standin-cli" <<SH
#!/usr/bin/env bash
# The stand-in's \`chtypes\`: one command line through $1's test binary, under
# the production channel's test-only selector (scripts/release-verify.sh).
exec bash "$ROOT/scripts/release-verify.sh" --selector-run "$1" "$work" "\$@"
SH
  chmod +x "$work/bin/standin-cli"
  CLI="$work/bin/standin-cli"
}

# The stand-in's `chtypes` (selector_build) calls back here: one command line.
if [ "${1:-}" = "--selector-run" ]; then
  [ "$#" -ge 3 ] || fail "usage: --selector-run <lang> <work> <args...>"
  lang="$2"; work="$3"; shift 3
  rc=0; selector_run "$lang" cli "$@" || rc=$?
  exit "$rc"
fi

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
  # Every row below up to the stable mode's is the dev mode's, as before #597.
  channel_select dev
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
  # 4b. which index check 4 (and the probe of check 3) compares against: the
  # alias for this binding's fingerprint, the CLI's own first choice (§3).
  expect "the channel resolves its alias" "1" "${FP_ALIAS:-0}"
  expect "the alias of a line" "26.9--fp-${dev#sha256:}" "$(alias_tag 26.9 "$dev")"
  expect "an alias served: the alias" alias "$(served_ref 200)"
  expect "an alias not found: the line" line "$(served_ref 404)"
  expect "an alias that fails: nothing (no fallback)" "" "$(served_ref 503)"
  expect "an alias request that did not complete: nothing" "" "$(served_ref "")"
  # The same, end to end, against a stand-in registry: `curl` below answers
  # the alias with $stub_alias (and $alias_index on a 200), and the line's own
  # tag with 200 and $line_index.
  work="$(mktemp -d)"; api="https://registry.example/v2/chtypes/v2-dev"; os=linux; arch=amd64; want="$dev"
  line_index='{"manifests":[{"digest":"'"$bdigest"'","platform":{"os":"linux","architecture":"amd64"}}]}'
  alias_index='{"manifests":[{"digest":"'"$cdigest"'","platform":{"os":"linux","architecture":"amd64"}}]}'
  stub_alias=200
  curl() {
    local out="" url=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out="$2"; shift 2 ;;
        -A | -w | -H | --retry) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
      esac
    done
    case "$url" in
      "$api/manifests/26.9--fp-${dev#sha256:}")
        [ "$stub_alias" != 200 ] || printf '%s' "$alias_index" > "$out"
        printf '%s' "$stub_alias" ;;
      "$api/manifests/26.9") printf '%s' "$line_index" > "$out"; printf 200 ;;
      *) printf 404 ;;
    esac
  }
  expect "an alias served: its manifest, not the line's" "$cdigest" "$(staging_manifest 26.9)"
  expect "  ... and the ref compared is the alias" "26.9--fp-${dev#sha256:}" "$(cat "$work/manifest.ref")"
  stub_alias=404
  expect "no alias: the line's own manifest" "$bdigest" "$(staging_manifest 26.9)"
  expect "  ... and the ref compared is the line" "26.9" "$(cat "$work/manifest.ref")"
  stub_alias=503
  expect "an alias that fails: no manifest, never the line's" "" "$(staging_manifest 26.9)"
  stub_alias=200; alias_index='{"manifests":[{"digest":"'"$cdigest"'","platform":{"os":"darwin","architecture":"arm64"}}]}'
  expect "an alias without this platform: no manifest, never the line's" "" "$(staging_manifest 26.9)"
  unset -f curl; rm -rf "$work"
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
  # ---- The stable mode (#597): the production generation-2 channel ----
  channel_select stable
  expect "the stable mode's registry API root" "https://registry.wavehouse.dev/v2/chtypes/v2" "$(registry_api "$REGISTRY_BASE")"
  expect "the stable mode resolves no alias" "0" "$FP_ALIAS"
  passes "the stable cache subroot" check_cache_root /c/v2 /c/v2
  refuses "a dev CLI's cache root, in the stable mode" "is not a v2 build" check_cache_root /c/v2-dev /c/v2
  t="$(mktemp -d)"
  record "$RECORD_SCHEMA" "$KEY_ID" "$ABI" "$dev"
  passes "a stable record, signed by the release key" check_record "$t/verified.json" "$dev"
  record "$RECORD_SCHEMA" "$DEV_KEY_ID" "$ABI" "$dev"
  refuses "a record signed by the staging key, in the stable mode" "signed_by is '$DEV_KEY_ID', wanted '$KEY_ID'" check_record "$t/verified.json" "$dev"
  # With no alias step, checks 3 and 4 compare the line's own manifest even
  # where the registry serves an alias, and never ask for the alias.
  work="$(mktemp -d)"; api="https://registry.example/v2/chtypes/v2"; os=linux; arch=amd64; want="$dev"
  line_index='{"manifests":[{"digest":"'"$bdigest"'","platform":{"os":"linux","architecture":"amd64"}}]}'
  alias_index='{"manifests":[{"digest":"'"$cdigest"'","platform":{"os":"linux","architecture":"amd64"}}]}'
  curl() {
    local out="" url=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out="$2"; shift 2 ;;
        -A | -w | -H | --retry) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
      esac
    done
    case "$url" in
      "$api/manifests/26.9--fp-"*) echo "$url" >> "$work/asked"; printf '%s' "$alias_index" > "$out"; printf 200 ;;
      "$api/manifests/26.9") printf '%s' "$line_index" > "$out"; printf 200 ;;
      *) printf 404 ;;
    esac
  }
  expect "the stable mode compares the line's own manifest, though an alias is served" "$bdigest" "$(staging_manifest 26.9)"
  expect "  ... the ref compared is the line" "26.9" "$(cat "$work/manifest.ref")"
  expect "  ... and the alias was never asked for" "" "$(cat "$work/asked" 2>/dev/null || true)"
  unset -f curl; rm -rf "$work"

  # ---- The stand-in: a stable dry run only ----
  channel_select stable
  GITHUB_EVENT_NAME=workflow_dispatch channel_standin true
  expect "the stand-in's registry is the staging dev repository" "$DEV_REGISTRY_BASE" "$REGISTRY_BASE"
  expect "the stand-in trusts the staging key" "$DEV_KEY_ID" "$KEY_ID"
  expect "the stand-in keeps the production cache subroot" "v2" "$CACHE_SUBROOT"
  expect "the stand-in keeps the production channel's lack of an alias step" "0" "$FP_ALIAS"
  record "$RECORD_SCHEMA" "$DEV_KEY_ID" "$ABI" "$dev"
  passes "a stand-in record, signed by the staging key" check_record "$t/verified.json" "$dev"
  record "$RECORD_SCHEMA" deb275922dbff76e "$ABI" "$dev"
  refuses "a release-key record against the stand-in" "signed_by is 'deb275922dbff76e', wanted '$DEV_KEY_ID'" check_record "$t/verified.json" "$dev"
  rm -rf "$t"
  # A real run refuses the stand-in before anything is installed.
  standin_refused() { # name, phrase, environment...
    local name="$1" phrase="$2" out; shift 2
    if out="$(env "$@" bash "${BASH_SOURCE[0]}" ts standin 2.0.0 /nonexistent.tgz 2>&1)"; then echo "selftest FAIL: $name: the stand-in ran" >&2; exit 1; fi
    grep -qF -- "$phrase" <<<"$out" || { echo "selftest FAIL: $name: refused without '$phrase': $out" >&2; exit 1; }
    echo "selftest ok: $name refused ('$phrase')"; n=$((n + 1))
  }
  standin_refused "the stand-in on a real run (dry_run=false)" "the stand-in is for a dry run only" RELEASE_DRY_RUN=false GITHUB_EVENT_NAME=workflow_dispatch
  standin_refused "the stand-in with no dry_run at all" "the stand-in is for a dry run only" -u RELEASE_DRY_RUN GITHUB_EVENT_NAME=workflow_dispatch
  standin_refused "the stand-in on a tag push, even claiming a dry run" "this run is a tag push" RELEASE_DRY_RUN=true GITHUB_EVENT_NAME=push
  if out="$(env RELEASE_DRY_RUN=true GITHUB_EVENT_NAME=workflow_dispatch bash "${BASH_SOURCE[0]}" ts standin 2.0.0-dev.3 /nonexistent.tgz 2>&1)"; then echo "selftest FAIL: the stand-in ran in the dev mode" >&2; exit 1; fi
  grep -qF "the stand-in is the stable mode's" <<<"$out" || { echo "selftest FAIL: the dev mode's stand-in refusal: $out" >&2; exit 1; }
  echo "selftest ok: the stand-in in the dev mode is refused"; n=$((n + 1))
  if out="$(bash "${BASH_SOURCE[0]}" ts registry 3.0.0 2>&1)"; then echo "selftest FAIL: a 3.0.0 was verified" >&2; exit 1; fi
  grep -qF "is not a release of this branch" <<<"$out" || { echo "selftest FAIL: the 3.0.0 refusal: $out" >&2; exit 1; }
  echo "selftest ok: a version neither mode releases is refused"; n=$((n + 1))

  # ---- The selector path's plumbing (the harnesses themselves run only in a dry run) ----
  t="$(mktemp -d)"
  printf '\nrunning 1 test\ntest zz_release_standin::release_standin_cli ... \n<<<release-standin begin>>>\n/c/v2\n<<<release-standin end 0>>>\nok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 9 filtered out; finished in 0.01s\n\n' > "$t/raw"
  rust_markers "$t"
  expect "libtest's output: the command's stdout, between the markers" "/c/v2" "$(cat "$t/stdout")"
  expect "libtest's output: the command's status" "0" "$(cat "$t/code")"
  rm -f "$t/stdout" "$t/code"
  printf 'running 1 test\n\n<<<release-standin begin>>>\ninstalled 26.9.8.3 linux-amd64 /c/x\npublished 26.9 support unknown\n<<<release-standin end 3>>>\n' > "$t/raw"
  rust_markers "$t"
  expect "libtest's output: several lines, and a failing status" $'installed 26.9.8.3 linux-amd64 /c/x\npublished 26.9 support unknown 3' "$(cat "$t/stdout") $(cat "$t/code")"
  rm -f "$t/stdout" "$t/code"
  printf 'running 1 test\n<<<release-standin begin>>>\n/c/v2\nthread panicked\n' > "$t/raw"
  rust_markers "$t"
  expect "libtest's output with no end marker: no status, so no command ran" "absent" "$([ -f "$t/code" ] && echo present || echo absent)"
  printf 'the output\n' > "$t/stdout"; printf 'a warning\n' > "$t/stderr"; printf '0' > "$t/code"
  expect "a harness's report is the command's stdout" "the output" "$(selector_report "$t" 2>/dev/null)"
  printf '7' > "$t/code"
  expect "  ... and its status" "7" "$(selector_report "$t" >/dev/null 2>&1 && echo 0 || echo "$?")"
  rm -f "$t/code"; printf 'the harness log\n' > "$t/runner.log"
  report_all() { selector_report "$1" 2>&1; }
  refuses "a harness that left no status" "did not report a status" report_all "$t"
  rm -rf "$t"
  # The read-back loop: an attempt that passes on its third try passes within a
  # window, and fails at once with none (sleep stubbed: no time passes).
  sleep() { :; }
  tries=0; attempt() { tries=$((tries + 1)); readback_detail="try $tries"; [ "$tries" -ge 3 ]; }
  passes "a read-back that passes on the third try, within the window" readback 360 attempt
  tries=0
  expect "  ... and it ran exactly three tries" "3" "$(readback 360 attempt 2>/dev/null; echo "$tries")"
  tries=0
  readback_once() { readback 0 attempt 2>/dev/null || { echo "$readback_detail"; return 1; }; }
  refuses "a read-back with no window: one try only" "try 1" readback_once
  unset -f sleep

  # The script refuses a version outside the channel before it installs anything.
  if out="$(bash "${BASH_SOURCE[0]}" ts local 1.0.4 /nonexistent.tgz 2>&1)"; then echo "selftest FAIL: a 1.x version was verified" >&2; exit 1; fi
  grep -qF "1.x releases are verified from main" <<<"$out" || { echo "selftest FAIL: the 1.x refusal: $out" >&2; exit 1; }
  echo "selftest ok: a 1.x version is refused before anything is installed"; n=$((n + 1))
  echo "selftest: $n cases, every refusal fired and every good input passed"
  exit 0
fi

lang="${1:-}"; mode="${2:-}"; version="${3:-}"; source_arg="${4:-}"
case "$lang" in rust | ts | python | go) ;; *) fail "usage: release-verify.sh <rust|ts|python|go> <registry|local|standin> <version> [source]" ;; esac
case "$mode" in registry) ;; local | standin) [ -n "$source_arg" ] || fail "$mode mode needs a <source>" ;; *) fail "mode is registry, local or standin, not '$mode'" ;; esac
[ -n "$version" ] || fail "no version"
# The version selects the mode (scripts/release-channel.sh, THE MODES), and nothing else does.
release_kind="$(release_mode "$version")" || fail "$version is not a release of this branch (2.0.0-dev.N, the dev mode, or 2.N.N, the stable mode: scripts/release-channel.sh, THE MODES; #511, #597); 1.x releases are verified from main"
channel_select "$release_kind"
# The stand-in (a stable dry run only; refused otherwise) and how it reaches
# the production channel: the shipped CLI once the spec is locked, the
# binding's test-only selector before.
standin_path=""
if [ "$mode" = standin ]; then
  channel_standin "${RELEASE_DRY_RUN:-false}"
  if [ "$(spec_stability "$ROOT")" = locked ]; then standin_path=default; else standin_path=selector; fi
fi
[ -z "$source_arg" ] || source_arg="$(cd "$(dirname "$source_arg")" && pwd)/$(basename "$source_arg")"

want="$(header_fingerprint "$ROOT/$HEADER")"
[ -n "$want" ] || fail "could not read CHS_ABI_FINGERPRINT from $HEADER"

work="${RUNNER_TEMP:-$(mktemp -d)}/release-verify-$lang"
rm -rf "$work"; mkdir -p "$work"
export CHTYPES_CACHE="$work/cache"
cache_root="$CHTYPES_CACHE/$CACHE_SUBROOT"
# No credentials, no override of the registry or the trust: exactly a stranger.
unset CHTYPES_ARTIFACTS_URL CHTYPES_TRUSTED_KEYS CHTYPES_ALLOW_UNSIGNED CHTYPES_DOWNLOAD_TOKEN
echo "verifying $lang $version from $mode${source_arg:+ ($source_arg)} in the $MODE mode, on the $CHANNEL channel: abi $ABI, $want, from $REGISTRY_BASE, key $KEY_ID"
if [ -n "$standin_path" ]; then
  # The stand-in, through the production channel's OWN overrides, which the
  # dev channel would ignore (scripts/release-channel.sh, channel_standin).
  export CHTYPES_ARTIFACTS_URL="$REGISTRY_BASE" CHTYPES_TRUSTED_KEYS="$KEY"
  echo "THE STAND-IN (a dry run only): production $STANDIN_OF holds nothing until the lock, so the production channel's base and trust point at $REGISTRY_BASE and the staging key $KEY_ID, through CHTYPES_ARTIFACTS_URL and CHTYPES_TRUSTED_KEYS; the cache subroot ($CACHE_SUBROOT), record schema $RECORD_SCHEMA, abi $ABI and the lack of an alias step stay the production channel's"
  if [ "$standin_path" = selector ]; then
    echo "  spec/abi-v$ABI/abi.json is '$(spec_stability "$ROOT")', not locked, so no non-test binary speaks the production channel yet: every check below runs through $lang's own test binary, which selects that channel through the binding's test-only selector"
  else
    echo "  spec/abi-v$ABI/abi.json is locked, so the shipped CLI speaks the production channel by default, and the checks run it as is"
  fi
fi

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
  # The stand-in before the lock: the same program, in the crate's lib test
  # binary, under the production channel (selector_build_rust).
  if [ "$standin_path" = selector ]; then selector_run rust load "$1" || return 1; return 0; fi
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
  # The stand-in before the lock: the same program, in a vitest worker, under
  # the production channel (selector_build_ts).
  if [ "$standin_path" = selector ]; then selector_run ts load "$1" || return 1; return 0; fi
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
  # The stand-in before the lock: the same program, under pytest, under the
  # production channel (selector_build_python).
  if [ "$standin_path" = selector ]; then selector_run python load "$1" || return 1; return 0; fi
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
  # The stand-in before the lock: the same program, in the module's test
  # binary, under the production channel (selector_build_go).
  if [ "$standin_path" = selector ]; then selector_run go load "$1" || return 1; return 0; fi
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
[ "$standin_path" != selector ] || selector_build "$lang"

# 1
run_check "1 fingerprint" check_fingerprint "$("fingerprint_$lang")" "$want"
# 2
where_out="$("$CLI" where 2>"$work/where.err")" || where_out=""
[ -s "$work/where.err" ] && sed 's/^/  chtypes where: /' "$work/where.err"
run_check "2 cache root" check_cache_root "$where_out" "$cache_root"
# 3
api="$(registry_api "$REGISTRY_BASE")"
read -r os arch <<<"$(this_platform)"
# listing_attempt: check 3, once. Sets `line` (the newest line, on a pass only)
# and readback_detail (what it found), and returns the verdict.
listing_attempt() {
  local listing status out newest want_manifest manifest_status manifest_ref probe_dir origin
  line=""
  if ! listing="$("$CLI" list 2>"$work/list.err")"; then
    readback_detail="'chtypes list' failed: $(paste -sd' ' - < "$work/list.err")"
    return 1
  fi
  if ! status="$(http_get "$api/tags/list" "$work/tags.json")" || [ "$status" != 200 ]; then
    readback_detail="could not read $api/tags/list (HTTP ${status:-none}), so nothing the CLI listed can be checked"
    return 1
  fi
  echo "--- chtypes list ---"; printf '%s\n' "$listing"; echo "--------------------"
  if ! out="$(check_listing "$(published_lines <<<"$listing")" "$(tags_of < "$work/tags.json")")"; then
    readback_detail="$out"
    return 1
  fi
  newest="$(parse_line <<<"$listing")"
  if [ -z "$newest" ]; then
    readback_detail="$out, but none is a two-part line"
    return 1
  fi
  # The origin probe (#523): the names matched, which production's listing would also do.
  want_manifest="$(staging_manifest "$newest")"
  manifest_status="$(cat "$work/manifest.status")"; manifest_ref="$(cat "$work/manifest.ref")"
  echo "  the probe compares against $api/manifests/$manifest_ref"
  [ "$manifest_status" = 200 ] || echo "  $api/manifests/$manifest_ref answered HTTP ${manifest_status:-none}"
  probe_dir="$(CHTYPES_CACHE="$work/probe-cache" "$CLI" fetch "$newest" 2>"$work/probe.err" | tail -n 1)" || probe_dir=""
  [ ! -s "$work/probe.err" ] || sed 's/^/  probe fetch: /' "$work/probe.err"
  rm -rf "$work/probe-cache"
  if origin="$(check_origin "$probe_dir" "$want_manifest")"; then
    line="$newest"
    readback_detail="$out; newest line $newest; $origin"
    return 0
  fi
  readback_detail="$origin"
  return 1
}
readback_detail=""
if readback "$READBACK_WINDOW" listing_attempt; then
  verdict PASS "3 listing" "$readback_detail"
else
  line=""
  verdict FAIL "3 listing" "$readback_detail"
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
    manifest_status="$(cat "$work/manifest.status")"; manifest_ref="$(cat "$work/manifest.ref")"
    echo "  check 4 compares against $api/manifests/$manifest_ref"
    [ "$manifest_status" = 200 ] || echo "  $api/manifests/$manifest_ref answered HTTP ${manifest_status:-none}"
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

echo "$lang $version ($mode) in the $MODE mode, on the $CHANNEL channel${standin_path:+ against the stand-in ($REGISTRY_BASE, the staging key $KEY_ID; reached through the $standin_path path)}:$summary"
[ "$failed" = 0 ] || fail "$failed of 6 checks did not pass; read each line above (NOT RUN is never a pass)"
echo "OK: $lang $version ($mode) speaks $want, listed, fetched and loaded $line from $REGISTRY_BASE${standin_path:+, the stand-in for production}"
