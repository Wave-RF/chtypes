#!/usr/bin/env bash
# check-suite.sh — run one of the non-Go SDK suites here (python, ts, rust)
# and read its verdict off the runner's own summary line, color stripped,
# never off an exit code alone.
#
#   scripts/check-suite.sh [--no-artifacts | --require-artifacts] python|ts|rust
#
# WHY. This repository's CI runs on GitHub's hosted runners, twice over. The
# first run holds no artifact, so every test that needs one skips — loudly,
# by name — and the fetch, CLI and pure-language tests prove themselves; the
# second run fetches two published lines, and the same suites must then RUN
# the golden set and the registry tests. Both leave the same quiet failure
# class open: a runner that collected nothing, or skipped everything, and
# exited 0. So each suite must report at least one test PASSED on its own
# summary line and no failure anywhere in it; every skip it announced stays
# in the output above the verdict, so the log says what did not run.
#
# --no-artifacts reproduces the artifact-free runner on a developer machine:
# an empty XDG_CACHE_HOME and no $CHTYPES_REGISTRY, so this machine's own
# cache is invisible (the two system roots of docs/guides/fetch.md §1 cannot be
# hidden by environment; they are named if present).
# --require-artifacts is the second run's rule: the golden set must have RUN,
# and no test may have skipped for want of a registry.
#
# $CHTYPES_ABI_FIXTURES, when set, names a wrong-revision fixture set
# (scripts/abi-fixtures.sh builds one), and then each suite's two ABI-revision
# cases must have RUN and passed: the refusal naming both numbers, and the
# matching-revision control. A skipped or absent case reads exactly like a
# working handshake, which is the failure the fixture exists to close (#36).
#
# The artifact-backed proof beyond these — the server-truth suites, the
# oracle, the rigs — is the core repository's server-truth suites.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-suite: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

# abi_case_ran <test-name> <file> — true iff libtest reported `ok` for
# <test-name> in <file> AND the fixture's own uncaptured "ran" line for it is
# present. Deliberately STRICT and unchanged since before #66: `ok` is
# anchored to end-of-line. #66 was found to have THREE distinct measured
# interleave shapes (three separate PRs, none touching Rust), and every shape
# that loosens this anchor enough to accept one of them also accepts at
# least one that must be refused:
#
#   1. `test wrong_revision_is_refused ... okABI fixture: ran matching_revision_loads`
#      (verdict present, junk appended after it)
#   2. `test wrong_revision_is_refused ... ABI fixture: ran matching_revision_loads`
#      (verdict displaced — no "ok" on this line at all)
#   3. `ABI fixture: ran matching_revision_loadstest wrong_revision_is_refused ... `
#      (line PREFIXED — breaks the `^` start anchor too, and carries no
#      verdict either)
#
# Shape 3 is decisive: a matcher loose enough to accept it is really just
# "the test name appears somewhere on some line" — and the test's own name
# also appears on ITS OWN `ABI fixture: ran wrong_revision_is_refused` line,
# which prints at the START of the test body, before it can possibly have
# passed. A matcher that accepts that is worse than #66's original bug: today
# this census cries wolf (a false red on a case that ran and passed); a
# loosened matcher would go quiet on a real failure — reporting RUN-and-passed
# for a case that merely started and then panicked. This gate exists
# specifically to prove the ABI-revision refusal ran, so a false green here is
# the one outcome that must not happen. So the anchor stays exactly as it
# was; see --selftest, which pins that with all three shapes plus a clean
# line. The actual fix is making the input well-formed, not widening what
# counts as a pass — see the rust arm below: all three shapes only ever
# occurred because that arm's stdout and stderr were merged with `2>&1`
# before capture, and libtest's OWN "test NAME ... ok" print (stdout) was
# measured byte-clean on its own, ten times over at default full
# parallelism, once stderr was captured separately instead.
abi_case_ran() {
  local t="$1" file="$2"
  grep -qE "^test $t \.\.\. ok$" "$file" && grep -qF "ABI fixture: ran $t" "$file"
}

if [ "${1:-}" = "--selftest" ]; then
  # Pin the matcher's strictness: all three interleave shapes measured on
  # #66 (three separate PRs — #61, #65, #71 — none touching Rust) must read
  # as NOT RUN, because none of them carries a trustworthy verdict; only a
  # clean, un-interleaved line may read as RUN. A future change that loosens
  # the anchor to make one of these pass must break this test.
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

  printf 'test wrong_revision_is_refused ... ok\n' > "$tmp/clean.log"
  printf 'ABI fixture: ran wrong_revision_is_refused\n' >> "$tmp/clean.log"
  abi_case_ran wrong_revision_is_refused "$tmp/clean.log" \
    || { echo "SELFTEST FAILED: a clean, un-interleaved pass was read as absent" >&2; exit 1; }

  # Shape 1 (PR #61): verdict present, junk appended after "ok" on the line.
  printf 'ABI fixture: ran wrong_revision_is_refused\ntest wrong_revision_is_refused ... okABI fixture: ran matching_revision_loads\n' \
    > "$tmp/shape1.log"
  abi_case_ran wrong_revision_is_refused "$tmp/shape1.log" \
    && { echo "SELFTEST FAILED: shape 1 (#61, ok present but line not ended) was read as RUN — the anchor must stay strict" >&2; exit 1; }

  # Shape 2 (PR #65): verdict displaced — no "ok" on this line at all.
  printf 'test wrong_revision_is_refused ... ABI fixture: ran matching_revision_loads\n' \
    > "$tmp/shape2.log"
  abi_case_ran wrong_revision_is_refused "$tmp/shape2.log" \
    && { echo "SELFTEST FAILED: shape 2 (#65, no ok on the line) was read as RUN" >&2; exit 1; }

  # Shape 3 (PR #71): line PREFIXED — breaks the ^ start anchor too, and the
  # test's own name is present only because it names ITSELF at test START,
  # not because it passed. The decisive case: a matcher that accepts this
  # would go quiet on a real failure, which is worse than #66's original bug.
  printf 'ABI fixture: ran matching_revision_loadstest wrong_revision_is_refused ... \n' \
    > "$tmp/shape3.log"
  abi_case_ran wrong_revision_is_refused "$tmp/shape3.log" \
    && { echo "SELFTEST FAILED: shape 3 (#71, line prefixed, no verdict) was read as RUN — this would go quiet on a real failure" >&2; exit 1; }

  # Negative control: the case genuinely did not run (the fixture directory
  # was unset, so the test announced its own skip on stderr and returned).
  # The census must still fail this, loudly and by name, or the fix has
  # turned the gate into a no-op.
  printf 'SKIPPED: no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset\n' > "$tmp/skipped.log"
  abi_case_ran wrong_revision_is_refused "$tmp/skipped.log" \
    && { echo "SELFTEST FAILED: a case that did not run was reported as RUN" >&2; exit 1; }

  echo "check-suite: selftest ok — the ABI-fixture matcher stays strict on all three #66 interleave shapes and a genuine non-run, and still reads a clean line as RUN"
  exit 0
fi

WHICH=""; NO_ARTIFACTS=0; REQUIRE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-artifacts)      NO_ARTIFACTS=1 ;;
    --require-artifacts) REQUIRE=1 ;;
    python|ts|rust)      [ -z "$WHICH" ] || die "one suite per run ($WHICH, $1)"; WHICH="$1" ;;
    -h|--help)           sed -n '2,27p' "$0"; exit 0 ;;
    *)                   die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$WHICH" ] || die "which suite? python|ts|rust"
[ "$NO_ARTIFACTS" -eq 0 ] || [ "$REQUIRE" -eq 0 ] || die "--no-artifacts and --require-artifacts contradict each other"

if [ "$NO_ARTIFACTS" -eq 1 ]; then
  SCRATCH_CACHE="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-no-artifacts.XXXXXX")"
  trap 'rm -rf "$SCRATCH_CACHE"' EXIT
  export XDG_CACHE_HOME="$SCRATCH_CACHE"
  unset CHTYPES_REGISTRY CHTYPES_AUTOFETCH
  say "--no-artifacts: XDG_CACHE_HOME=$SCRATCH_CACHE (empty), CHTYPES_REGISTRY unset"
  for sysroot in /usr/local/share/chtypes/artifacts /opt/chtypes/artifacts; do
    [ ! -d "$sysroot" ] || note "WARNING: $sysroot exists and is on the search path; this run is not artifact-free"
  done
fi
[ "$REQUIRE" -eq 0 ] || say "--require-artifacts: CHTYPES_REGISTRY=${CHTYPES_REGISTRY:-<unset — the search path>}; the golden set must run"

# The binding parity contract (tests/parity/manifest.json, docs/reference/bindings.md) is
# checked by each language's OWN suite. It needs no artifact and no registry, so
# there is no run in which it may be absent — and a parity check that was deleted,
# renamed or skipped would otherwise leave the census looking exactly as healthy
# as one that ran. Each arm below asserts it off the runner's own output.
PARITY_MIN=6

LOG_DIR="$ROOT/scratch/check-suite"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$WHICH.log"; PLAIN="$LOG_DIR/$WHICH.plain.log"
RC=0
# run <dir> <cmd...> — the runner's output reaches the terminal AND the log;
# its exit code is kept, and read last.
run() {
  local dir="$1"; shift
  RC=0
  ( cd "$dir" && "$@" ) 2>&1 | tee "$LOG" || RC=$?
  # vitest colors its summary under CI even without a TTY, and an anchored
  # grep on the raw bytes then reads "passed=0" (measured in the core
  # repository, 2026-09-09); every verdict below reads the stripped copy.
  sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' "$LOG" > "$PLAIN"
}
first_number() { grep -oE "[0-9]+ $1" | grep -oE '[0-9]+' | tail -1; }

PROBLEMS=(); SUMMARY=""; PASSED=0; SKIPPED=""
case "$WHICH" in
  python)
    command -v uv >/dev/null 2>&1 || die "uv is not on PATH; the python suite cannot run and must not be reported as passing"
    say "pytest -q -rs (python/)"
    run "$ROOT/python" uv run --quiet pytest -q -rs tests
    SUMMARY="$(grep -E '^[0-9]+ (passed|failed|skipped|error)' "$PLAIN" | tail -1 || true)"
    PASSED="$(printf '%s\n' "$SUMMARY" | first_number passed || true)"
    SKIPPED="$(printf '%s\n' "$SUMMARY" | first_number skipped || true)"
    ! printf '%s\n' "$SUMMARY" | grep -qE '[0-9]+ (failed|error)' || PROBLEMS+=("the summary reports failures")
    # pytest -q names no passing test, so the parity contract's presence is read
    # off the collector instead: these tests need no artifact and cannot skip, so
    # collected + no failures + a non-zero pass count is them having passed.
    PARITY_N="$( (cd "$ROOT/python" && uv run --quiet pytest -q --collect-only tests/test_parity.py 2>/dev/null) \
                 | grep -cE '^tests/test_parity\.py::' || true)"
    [ "${PARITY_N:-0}" -ge "$PARITY_MIN" ] || PROBLEMS+=("the binding parity contract was not proven \
here: python collected ${PARITY_N:-0} of at least $PARITY_MIN parity tests (tests/parity/manifest.json)")
    if [ "$REQUIRE" -eq 1 ]; then
      ! grep -qE '^SKIPPED .*tests/test_golden\.py' "$PLAIN" || PROBLEMS+=("the golden set was SKIPPED with artifacts required")
      ! grep -qE '^SKIPPED .*no chtypes artifacts on the search path' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
    fi
    if [ -n "${CHTYPES_ABI_FIXTURES:-}" ]; then
      # pytest -q names no passing test: collected by name + not skipped + no
      # failure in the summary is the two cases having passed.
      ! grep -qF 'no ABI revision fixture' "$PLAIN" || PROBLEMS+=("the ABI-revision cases SKIPPED although \$CHTYPES_ABI_FIXTURES is set")
      ABI_N="$( (cd "$ROOT/python" && uv run --quiet pytest -q --collect-only tests/test_abi_revision.py 2>/dev/null) \
               | grep -cE '^tests/test_abi_revision\.py::test_(wrong_revision_is_refused|matching_revision_loads)$' || true)"
      [ "${ABI_N:-0}" -eq 2 ] || PROBLEMS+=("the ABI-revision handshake was not proven here: python collected ${ABI_N:-0} of its 2 cases")
    fi
    ;;
  ts)
    command -v pnpm >/dev/null 2>&1 || die "pnpm is not on PATH; the ts suite cannot run and must not be reported as passing"
    [ -d "$ROOT/ts/node_modules" ] || die "ts/node_modules is absent — run 'pnpm install --frozen-lockfile && pnpm build' in ts/ first"
    say "vitest run --reporter=verbose (ts/) — every skipped test is listed by name"
    # vitest is invoked DIRECTLY, not through pnpm. The census below counts
    # per-test lines out of the verbose reporter, and pnpm changes the child's
    # environment in a way that makes vitest stop emitting them: measured on
    # pnpm 12.4.1, `pnpm -s test`, `pnpm test` and `pnpm exec vitest` all yield
    # ZERO census lines while ./node_modules/.bin/vitest yields 8, same vitest
    # 5.0.0, same flag, same pipe. CI pins pnpm 11, where it happened to work —
    # so this census was one Dependabot bump away from silently counting zero
    # and failing a guard that had nothing wrong with it.
    run "$ROOT/ts" env NO_COLOR=1 ./node_modules/.bin/vitest run --reporter=verbose
    FILES_LINE="$(grep -E '^\s*Test Files ' "$PLAIN" | tail -1 || true)"
    TESTS_LINE="$(grep -E '^\s*Tests ' "$PLAIN" | tail -1 || true)"
    SUMMARY="$(printf '%s / %s' "${FILES_LINE#"${FILES_LINE%%[![:space:]]*}"}" "${TESTS_LINE#"${TESTS_LINE%%[![:space:]]*}"}")"
    [ -n "$TESTS_LINE" ] || SUMMARY=""
    PASSED="$(printf '%s\n' "$TESTS_LINE" | first_number passed || true)"
    SKIPPED="$(printf '%s\n' "$TESTS_LINE" | first_number skipped || true)"
    ! printf '%s\n%s\n' "$FILES_LINE" "$TESTS_LINE" | grep -qE '[0-9]+ failed' || PROBLEMS+=("the summary reports failures")
    PARITY_N="$(grep -cE '^\s*.{0,3} test/parity\.test\.ts > ' "$PLAIN" || true)"
    [ "${PARITY_N:-0}" -ge "$PARITY_MIN" ] || PROBLEMS+=("the binding parity contract was not proven \
here: ts ran ${PARITY_N:-0} of at least $PARITY_MIN parity tests (tests/parity/manifest.json)")
    if [ "$REQUIRE" -eq 1 ]; then
      grep -qF '✓ test/golden.test.ts >' "$PLAIN" || PROBLEMS+=("no golden case RAN with artifacts required")
      # A per-line skip is EXPECTED and not a problem: a case is only a golden
      # for the exact ClickHouse build it was generated on, so a registry holding
      # an older patch skips that line by name (the core repository's golden-set documentation). What must not
      # happen is the set being skipped wholesale — that is the sentinel test,
      # and the rule above already requires at least one case to have run.
      ! grep -qF '↓ goldens > has at least one artifact' "$PLAIN" || PROBLEMS+=("the golden set was skipped wholesale with artifacts required")
      ! grep -qE 'SKIPPED: (no artifact|none on the search path)' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
    fi
    if [ -n "${CHTYPES_ABI_FIXTURES:-}" ]; then
      for c in 'wrong revision is refused naming both numbers' 'matching revision loads'; do
        grep -qF "✓ test/abi-revision.test.ts > abi revision fixture > $c" "$PLAIN" \
          || PROBLEMS+=("the ABI-revision case '$c' did not pass although \$CHTYPES_ABI_FIXTURES is set")
      done
    fi
    ;;
  rust)
    command -v cargo >/dev/null 2>&1 || die "cargo is not on PATH; the rust suite cannot run and must not be reported as passing"
    say "cargo test --locked (rust/) — a skipped test says SKIP on stderr, by name"
    run "$ROOT/rust" cargo test --locked
    # Anchored on the summary's exact shape: a bare `^test result:` also
    # matches this crate's own `result` module ("test result::tests::… ok").
    RESULTS="$(grep -E '^test result: (ok|FAILED)\. ' "$PLAIN" || true)"
    PASSED="$(printf '%s\n' "$RESULTS" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')"
    FAILED_N="$(printf '%s\n' "$RESULTS" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')"
    [ -z "$RESULTS" ] || SUMMARY="$(printf '%s\n' "$RESULTS" | grep -c .) test binaries: $PASSED passed, $FAILED_N failed (each binary's own line above)"
    SKIPPED="$(grep -cE '^SKIP ' "$PLAIN" || true)"
    ! grep -qE '^test result: FAILED|^error(\[E[0-9]+\])?:|panicked at' "$PLAIN" || PROBLEMS+=("a test binary reported FAILED, or did not build")
    PARITY_N="$(grep -cE '^test (parity_manifest|rust_(exposes|answers)|the_(rust|go_copy))[a-z_]* \.\.\. ok$' "$PLAIN" || true)"
    [ "${PARITY_N:-0}" -ge "$PARITY_MIN" ] || PROBLEMS+=("the binding parity contract was not proven \
here: rust passed ${PARITY_N:-0} of at least $PARITY_MIN parity tests (tests/parity/manifest.json)")
    if [ "$REQUIRE" -eq 1 ]; then
      grep -qE '^test goldens_hold_on_every_artifact \.\.\. ok' "$PLAIN" || PROBLEMS+=("the golden test did not pass with artifacts required")
      ! grep -qE '^SKIP goldens_hold_on_every_artifact|Every test in this file is skipped|^SKIP integration::' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
    fi
    if [ -n "${CHTYPES_ABI_FIXTURES:-}" ]; then
      # abi_case_ran (above) reads the verdict off a plain log and STAYS
      # STRICT — see its own comment for why loosening it is unsafe, not
      # merely unnecessary.
      #
      # THE FIX (#66): libtest's own progress line ("test NAME ... ok") is
      # written to stdout, and rust/tests/abi_revision.rs's `announce` writes
      # its own "ABI fixture: ran NAME" proof directly, unbuffered, to the
      # REAL stderr (deliberately — so it survives on a PASSING test even
      # without --nocapture). The census above and the ordinary run() helper
      # both merge those two streams with `2>&1` before capturing, so two
      # independent, unsynchronized write()s land on one fd, and the pipe can
      # interleave them at an arbitrary byte boundary — measured in three
      # separate shapes across three PRs (#61, #65, #71), none touching
      # Rust. MEASURED (ten repeated runs, default full parallelism,
      # `cargo test --test abi_revision`, stdout and stderr captured to
      # SEPARATE files): stdout alone was byte-clean every time — libtest's
      # own printing needs no help from thread count. `--test-threads=1`
      # neither fixes this (merged output still splits: in single-threaded
      # mode libtest prints "test NAME ... " BEFORE running the test body,
      # so this test's OWN mid-body stderr write still lands inside its own
      # line once merged) nor is it needed (stdout stays clean under full
      # parallelism). So the fix is simply never merging the two streams for
      # this proof: this binary is run again here, alone, stdout and stderr
      # captured SEPARATELY, and only concatenated together after both have
      # finished — which cannot itself introduce an interleave. Every
      # verdict below is read off that concatenation, never off the
      # suite-wide, 2>&1-merged $PLAIN above (which still runs this binary
      # too, as part of the ordinary suite, and is not relied on here).
      ABI_STDOUT="$LOG_DIR/rust-abi-fixture.stdout.log"
      ABI_STDERR="$LOG_DIR/rust-abi-fixture.stderr.log"
      ABI_PLAIN="$LOG_DIR/rust-abi-fixture.plain.log"
      ( cd "$ROOT/rust" && cargo test --locked --test abi_revision ) \
        > "$ABI_STDOUT" 2> "$ABI_STDERR" || true
      cat "$ABI_STDOUT" "$ABI_STDERR"
      sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' "$ABI_STDOUT" "$ABI_STDERR" > "$ABI_PLAIN"
      for t in wrong_revision_is_refused matching_revision_loads; do
        abi_case_ran "$t" "$ABI_PLAIN" \
          || PROBLEMS+=("the ABI-revision case $t did not RUN and pass although \$CHTYPES_ABI_FIXTURES is set")
      done
    fi
    ;;
esac

[ -n "$SUMMARY" ] || PROBLEMS+=("no summary line — the runner did not finish; see $LOG")
[ "$RC" -eq 0 ] || PROBLEMS+=("the runner exited $RC")
[ "${PASSED:-0}" -gt 0 ] || PROBLEMS+=("zero tests passed — a suite that ran nothing is not a pass")

echo "  $WHICH census — ${SUMMARY:-<no summary line>}"
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  echo "  VERDICT $WHICH: NOT a pass —"; for p in "${PROBLEMS[@]}"; do echo "    * $p"; done
  exit 1
fi
echo "  VERDICT $WHICH: pass — ${PASSED} test(s) passed${SKIPPED:+, $SKIPPED skipped (each named above)}"
