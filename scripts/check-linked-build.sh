#!/usr/bin/env bash
# check-linked-build.sh — compile the STATICALLY LINKED Go path, so that the
# header type-checks it and linked_abi_check.go's pins actually fire.
#
# WHY THIS EXISTS (issue #118, split out of #93). go/chtypes/linked.go is cgo
# over this repository's own include/chtypes.h, so every `C.chs_*(...)` call
# site is checked against the real prototype BY THE COMPILER — a stronger check
# than scripts/check-abi-decls.py's equivalence classes can be, and the reason
# that script exempts this one file from its type rules. The problem was never
# that the check was weak. It was that nothing ran it: no CI job passed
# `-tags chtypes_linked`, so the compiler that would catch a wrong type there
# was never invoked, and go/chtypes/linked_abi_check.go — whose whole job is to
# pin the frozen ABI numbers to the header at compile time — asserted nothing
# either. The honest state was: the linked path's arity was checked on every
# pull request, and its types were checked by nobody.
#
# ⚠️ THE RECEIVED REASON FOR THAT WAS WRONG, AND IT IS WORTH KNOWING WHY.
# The belief was that a tagged build needs a native build tree from the other
# half of the project, which public CI has no access to. Measured 2026-09-21,
# on ubuntu-latest's toolchain and on this one: it does not. `go build` and
# `go vet` of a NON-MAIN cgo package run the C compiler over the translation
# unit and type-check every call site, and only PROBE the link; when that probe
# fails for want of `-lchtypes` the toolchain records the failure and defers it
# to whoever actually links a binary. So the type check needs a C compiler and
# the in-repo header, and nothing else — no artifact, no library, no network.
# Only linking a MAIN package (./cmd/chtypes, or the examples' `go run`) needs
# the library, and that is a symbol-existence question, not a type question:
# the dlopen path already resolves every one of those symbols by name in the
# suites.
#
# That is why this runs in the no-artifact leg that gates every pull request,
# and not in the artifact-backed job. The artifact-backed job is red BY DESIGN
# on a revision branch (revision-5 bindings, revision-4 published artifacts),
# and #105 is the record of what an expected-red job does to a check placed
# inside it: it becomes a hiding place. #93 stated the same rule from the other
# direction — a check that only runs where artifacts exist is not sufficient.
#
#   scripts/check-linked-build.sh            type-check the linked path
#   scripts/check-linked-build.sh --selftest plant a mistyped call site, a
#                                            swapped pointer pair and a drifted
#                                            header constant, and prove each
#                                            one turns this check RED
#
# A check that has only ever passed is not known to work, so --selftest runs
# first and in the same CI job — the precedent scripts/lint-public.sh and
# scripts/check-abi-decls.py set. It refuses to plant anything into a tree that
# is already failing, because a plant that fires there proves nothing.
set -euo pipefail

TAG=chtypes_linked
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The files the tag MUST select. A misspelled tag, a moved file or a dropped
# `//go:build` line would leave `go vet` exiting 0 having compiled none of the
# linked path — a green run that checked nothing, which is the one result this
# repository refuses to accept anywhere.
TAGGED_FILES=(linked.go linked_abi_check.go)

# check <tree> — type-check the linked path in <tree> (a directory holding this
# repository's `go/` and `include/`). Prints what it did; returns non-zero on
# any disagreement between a call site and the header.
check() {
  local tree="$1"
  local listed f stamp
  local base_cflags="${CGO_CFLAGS:--O2 -g}"

  # ⚠️ MEASURED, AND NOT OBVIOUS: Go's build cache does not hash a C header
  # reached through `-I`. include/chtypes.h sits outside go/chtypes/, so it is
  # not one of the package's own files, and editing it changes nothing the
  # cache keys on — `go vet` then REPLAYS the previous result and reports
  # success for a header it never recompiled against. That is exactly the
  # silent pass this script exists to abolish, one layer down, and it is why
  # the selftest's header plant went uncaught until this stamp existed.
  #
  # Mixing a digest of the header into CGO_CFLAGS makes its CONTENT part of
  # the action id, so a changed header forces the cgo package to be rebuilt
  # and nothing else. The macro is never referenced; being defined is the
  # whole job. `${CGO_CFLAGS:--O2 -g}` keeps cgo's own default, which setting
  # the variable would otherwise replace, and `local -x` keeps the stamp from
  # accumulating across the selftest's repeated calls.
  stamp="$(cksum < "$tree/include/chtypes.h" | awk '{print $1}')"
  local -x CGO_CFLAGS="$base_cflags -DCHTYPES_HEADER_STAMP=${stamp}u"

  if ! listed="$(cd "$tree/go" && go list -tags "$TAG" -f '{{range .CgoFiles}}{{.}}{{"\n"}}{{end}}' ./chtypes)"; then
    echo "check-linked-build: go list failed under -tags $TAG — nothing was checked" >&2
    return 1
  fi
  for f in "${TAGGED_FILES[@]}"; do
    if ! printf '%s\n' "$listed" | grep -qxF "$f"; then
      echo "check-linked-build: -tags $TAG did not select go/chtypes/$f, so this run would have" >&2
      echo "  compiled none of the linked path. Fix the tag or that file's build constraint;" >&2
      echo "  do not trust the exit code of a run that checked nothing." >&2
      return 1
    fi
  done

  # `go vet` type-checks every package under the tag, ./cmd/chtypes included,
  # without linking anything. `go build` of the non-main package additionally
  # compiles the cgo preamble's own C, so a broken `#include` or a bad CFLAGS
  # is caught here rather than by the next person to use the tag.
  (cd "$tree/go" && go vet -tags "$TAG" ./...) || return 1
  (cd "$tree/go" && go build -tags "$TAG" ./chtypes) || return 1

  echo "check-linked-build: ok — ${#TAGGED_FILES[@]} tagged file(s) (${TAGGED_FILES[*]}) compiled and vetted"
  echo "  under -tags $TAG against include/chtypes.h; every C.chs_* call site type-checked by the"
  echo "  compiler, and linked_abi_check.go's constant pins evaluated."
  return 0
}

# ------------------------------------------------------------------- selftest
#
# Each plant is MISTYPED, never merely short: arity in this file is already
# covered by scripts/check-abi-decls.py, and the whole point of building the
# tagged path is the class of defect that script cannot see. One plant per
# distinct thing this check is supposed to be able to catch.
PLANT_LABEL=(
  "scalar type at a call site"
  "two pointer kinds swapped"
  "a header constant drifts from its Go twin"
)
PLANT_FILE=(
  "go/chtypes/linked.go"
  "go/chtypes/linked.go"
  "include/chtypes.h"
)
PLANT_FIND=(
  'C.chs_row(cs.handle, C.int(format), praw, C.size_t(len(raw)), csj, pcols)'
  'C.chs_validate_type(cexpr, &cCanon, &code, &cErr)'
  '#define CHS_DOC_TRANSFORMS 0x2u'
)
PLANT_REPL=(
  'C.chs_row(cs.handle, C.size_t(format), praw, C.size_t(len(raw)), csj, pcols)'
  'C.chs_validate_type(cexpr, &code, &cCanon, &cErr)'
  '#define CHS_DOC_TRANSFORMS 0x8u'
)
# Extended regexes, matched against the failing run's combined output.
PLANT_WANT=(
  'cannot use C\.size_t\(format\)'
  'cannot use &cCanon'
  'linked_abi_check\.go:[0-9]+:[0-9]+: constant -[0-9]+ overflows uint'
)

# The third plant moves a constant the `abi` job's revision grep does NOT look
# at — that job compares CHS_ABI_REVISION alone, while linked_abi_check.go pins
# CHS_DOC_*, CHS_CODE_UNSUPPORTED, CHS_EXPORT_NONE, CHS_COMPILE_DECLARED and
# every chs_format member. Planting the revision instead would have proved only
# something already covered elsewhere.

# apply <path> <find> <replace> — a literal replacement that refuses unless the
# anchor is present EXACTLY once, so a plant cannot half-apply after the source
# moves and then be reported as "not caught".
apply() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys

path, find, repl = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
n = text.count(find)
if n != 1:
    sys.exit(f"the plant's anchor is in {path} {n} time(s), expected exactly 1 — the source moved; update PLANT_FIND")
open(path, "w", encoding="utf-8").write(text.replace(find, repl))
PY
}

selftest() {
  local tmp out status i path
  echo "==> the tree itself must pass before anything is planted" >&2
  if ! check "$ROOT" >/dev/null 2>&1; then
    echo "SELFTEST FAILED: the tree does not pass, so a planted failure would prove nothing." >&2
    echo "  The failing run follows:" >&2
    check "$ROOT" || true
    return 1
  fi

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/check-linked-build-selftest.XXXXXX")"
  # shellcheck disable=SC2064  # $tmp is expanded now on purpose: the trap must
  # name this directory even if the variable is reassigned later.
  trap "rm -rf '$tmp'" EXIT
  cp -R "$ROOT/go" "$ROOT/include" "$tmp/"

  # The copy must pass too. A plant firing against a tree that was already
  # failing — because the copy dropped something, say — proves nothing.
  if ! check "$tmp" >/dev/null 2>&1; then
    echo "SELFTEST FAILED: the copied tree does not pass; the copy, not the plant, is wrong." >&2
    check "$tmp" || true
    return 1
  fi

  for i in "${!PLANT_LABEL[@]}"; do
    path="$tmp/${PLANT_FILE[$i]}"
    cp "$path" "$path.orig"
    apply "$path" "${PLANT_FIND[$i]}" "${PLANT_REPL[$i]}"
    set +e
    out="$(check "$tmp" 2>&1)"
    status=$?
    set -e
    mv "$path.orig" "$path"
    if [ "$status" -eq 0 ]; then
      echo "SELFTEST FAILED: the '${PLANT_LABEL[$i]}' plant was NOT caught." >&2
      echo "  ${PLANT_FILE[$i]}: ${PLANT_FIND[$i]}" >&2
      echo "         became: ${PLANT_REPL[$i]}" >&2
      return 1
    fi
    if ! printf '%s\n' "$out" | grep -qE "${PLANT_WANT[$i]}"; then
      echo "SELFTEST FAILED: the '${PLANT_LABEL[$i]}' plant turned the check red, but not for the" >&2
      echo "  expected reason. Wanted a line matching: ${PLANT_WANT[$i]}" >&2
      echo "  Got:" >&2
      printf '%s\n' "$out" >&2
      return 1
    fi
    printf '  plant %-42s caught\n' "${PLANT_LABEL[$i]}"
  done

  # Control: everything restored, the tree passes again. Without it a plant
  # loop that corrupted the copy would still have "caught" every later plant.
  if ! check "$tmp" >/dev/null 2>&1; then
    echo "SELFTEST FAILED: the copied tree does not pass after the plants were reverted." >&2
    check "$tmp" || true
    return 1
  fi

  echo "check-linked-build: selftest ok — ${#PLANT_LABEL[@]} mistyped plants, each one red, and the tree green before and after"
  return 0
}

if [ "${1:-}" = "--selftest" ]; then
  selftest
  exit $?
fi
if [ $# -gt 0 ]; then
  echo "usage: scripts/check-linked-build.sh [--selftest]" >&2
  exit 2
fi
check "$ROOT"
