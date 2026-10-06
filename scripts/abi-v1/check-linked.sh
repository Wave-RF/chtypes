#!/usr/bin/env bash
# check-linked.sh — the v1-abi-linked job body: type-check Go's STATICALLY
# LINKED path (go/internal/abi<N>/linked_gen.go, `-tags chtypes_linked`)
# against the header of the ABI major Go speaks, with no artifact.
#
# THE MAJOR. spec/binding-majors.json (scripts/abi-v1/majors.py) says which
# major go speaks at this commit: ABI v1 is go/internal/abi1 against
# include/chtypes.h, ABI v2 go/internal/abi2 against include/v2/chtypes.h. The
# job keeps its v1-named context until the lock and prints the major it
# tested; the generated ABI v2 linked_gen.go itself refuses, at compile time,
# any header whose CHS_ABI_VERSION is not 2, and the selftest proves that
# refusal by handing it ABI v1's header.
#
#   scripts/abi-v1/check-linked.sh            type-check the linked path
#   scripts/abi-v1/check-linked.sh --selftest plant a mistyped call site and
#                                              a drifted header constant,
#                                              and prove each turns this RED
#
# WHY THIS IS SAFE WITHOUT AN ARTIFACT (measured 2026-09-21 for v0's own
# linked-build check, true here for the identical reason): `go
# build` and `go vet` of a NON-MAIN cgo package run the C compiler over the
# translation unit and type-check every call site, and only PROBE the link;
# a missing `-lchtypes` is recorded and deferred to whoever actually LINKS a
# binary. So the type check needs a C compiler and this repository's own
# in-tree header, and nothing else — no artifact, no library, no network.
#
# This is the only linked-path check: v0's header and its check were
# deleted at the cutover.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TAG=chtypes_linked
TAGGED_FILE=linked_gen.go
MAJOR="$(python3 "$HERE/majors.py" get go)"
PKG="./internal/abi$MAJOR"
if [ "$MAJOR" = "1" ]; then HEADER="include/chtypes.h"; else HEADER="include/v$MAJOR/chtypes.h"; fi

# check <tree> — type-check the v1 linked path in <tree> (a directory holding
# this repository's `go/` and `include/`).
check() {
  local tree="$1"
  local listed stamp
  local base_cflags="${CGO_CFLAGS:--O2 -g}"

  # The same "a header reached through -I is outside the Go build cache's
  # keys" trap v0's linked-build check documented: mix a digest
  # of include/chtypes.h into CGO_CFLAGS so editing it invalidates the
  # cgo package's build cache entry instead of replaying a stale verdict.
  stamp="$(cksum < "$tree/$HEADER" | awk '{print $1}')"
  # -Werror: unlike v0's linked-build check (whose plants are GO-level
  # type mismatches at a cgo call site, caught by the Go compiler on every
  # toolchain alike), this file's own type checking happens entirely INSIDE
  # the cgo preamble -- chtypes_abi1_linked_fill assigns real C function
  # addresses into typed struct fields via an explicit cast, so a mismatch
  # is a C diagnostic, not a Go one. Measured: Apple clang already treats
  # -Wincompatible-function-pointer-types as an error by default, but
  # ubuntu-latest's gcc (this job's actual runner) only WARNS by default --
  # a plant there passed silently until this was added. -Werror makes the
  # check behave the same on every C compiler instead of borrowing whichever
  # one happens to be strict today.
  local -x CGO_CFLAGS="$base_cflags -Werror -DCHTYPES_ABI${MAJOR}_HEADER_STAMP=${stamp}u"

  if ! listed="$(cd "$tree/go" && go list -tags "$TAG" -f '{{range .CgoFiles}}{{.}}{{"\n"}}{{end}}' "$PKG")"; then
    echo "check-linked: go list failed under -tags $TAG — nothing was checked" >&2
    return 1
  fi
  if ! printf '%s\n' "$listed" | grep -qxF "$TAGGED_FILE"; then
    echo "check-linked: -tags $TAG did not select go/internal/abi$MAJOR/$TAGGED_FILE, so this run would" >&2
    echo "  have compiled none of the linked path. Fix the tag or its //go:build line; do not trust" >&2
    echo "  the exit code of a run that checked nothing." >&2
    return 1
  fi

  (cd "$tree/go" && go vet -tags "$TAG" "$PKG") || return 1
  (cd "$tree/go" && go build -tags "$TAG" "$PKG") || return 1

  echo "check-linked: ok — ABI v$MAJOR: go/internal/abi$MAJOR/$TAGGED_FILE compiled and vetted under -tags $TAG"
  echo "  against $HEADER; every generated chtypes_abi${MAJOR}_linked_fill call site type-checked"
  echo "  by the compiler."
  return 0
}

# ------------------------------------------------------------------- selftest
PLANT_LABEL=(
  "a described function's address cast to the wrong field"
  "the header drops a symbol the fill still names"
)
PLANT_FILE=(
  "go/internal/abi$MAJOR/linked_gen.go"
  "$HEADER"
)
PLANT_FIND=(
  "t->chs_abi_version = (chtypes_abi${MAJOR}_fn_chs_abi_version) &chs_abi_version;"
  "CHS_API int32_t chs_abi_version(void);"
)
PLANT_REPL=(
  "t->chs_abi_version = (chtypes_abi${MAJOR}_fn_chs_buf_free) &chs_abi_version;"
  "/* chs_abi_version dropped by the plant */"
)
PLANT_WANT=(
  # clang: "incompatible function pointer types" (plural). gcc: "incompatible
  # pointer type" (singular, no "function"). Measured on CI (ubuntu-latest's
  # gcc): a regex anchored to clang's exact plural wording missed gcc's real,
  # correctly-fired error entirely, failing the selftest over a wording
  # mismatch rather than a missing diagnostic.
  "incompatible.*pointer type|cannot use"
  # clang: "use of undeclared identifier 'X'". gcc: "'X' undeclared (first
  # use in this function)" -- no "use of ... identifier" substring at all.
  # Measured the same way as the plant above: match the one word both
  # compilers actually share.
  "undeclared|implicit declaration|undefined reference"
)

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
    check "$ROOT" || true
    return 1
  fi

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/check-linked-selftest.XXXXXX")"
  # shellcheck disable=SC2064  # $tmp is expanded now on purpose.
  trap "rm -rf '$tmp'" EXIT
  mkdir -p "$(dirname "$tmp/$HEADER")"
  cp -R "$ROOT/go" "$tmp/"
  cp -R "$ROOT/$HEADER" "$tmp/$HEADER"

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
      return 1
    fi
    if ! printf '%s\n' "$out" | grep -qE "${PLANT_WANT[$i]}"; then
      echo "SELFTEST FAILED: the '${PLANT_LABEL[$i]}' plant turned the check red, but not for the" >&2
      echo "  expected reason. Wanted a line matching: ${PLANT_WANT[$i]}. Got:" >&2
      printf '%s\n' "$out" >&2
      return 1
    fi
    printf '  plant %-50s caught\n' "${PLANT_LABEL[$i]}"
  done

  if ! check "$tmp" >/dev/null 2>&1; then
    echo "SELFTEST FAILED: the copied tree does not pass after the plants were reverted." >&2
    check "$tmp" || true
    return 1
  fi

  # From ABI v2 on: ABI v1's header where the major's own should be (a job
  # that tests v1 after go converted) is refused at compile time by the
  # generated pin, never type-checked as if it were the right one.
  if [ "$MAJOR" != "1" ]; then
    cp "$tmp/$HEADER" "$tmp/$HEADER.orig"
    cp "$ROOT/include/chtypes.h" "$tmp/$HEADER"
    set +e
    out="$(check "$tmp" 2>&1)"
    status=$?
    set -e
    mv "$tmp/$HEADER.orig" "$tmp/$HEADER"
    if [ "$status" -eq 0 ] || ! printf '%s\n' "$out" | grep -qE "go/internal/abi$MAJOR is ABI v$MAJOR"; then
      echo "SELFTEST FAILED: ABI v1's header in place of $HEADER was not refused by the ABI v$MAJOR pin:" >&2
      printf '%s\n' "$out" >&2
      return 1
    fi
    printf '  plant %-50s caught\n' "ABI v1's header where ABI v$MAJOR's should be"
  fi

  echo "check-linked: selftest ok — ABI v$MAJOR: ${#PLANT_LABEL[@]} mistyped plants (and, from ABI v2 on, ABI v1's header), each one red, and the tree green before and after"
  return 0
}

if [ "${1:-}" = "--selftest" ]; then
  selftest
  exit $?
fi
if [ $# -gt 0 ]; then
  echo "usage: scripts/abi-v1/check-linked.sh [--selftest]" >&2
  exit 2
fi
check "$ROOT"
