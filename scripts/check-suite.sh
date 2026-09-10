#!/usr/bin/env bash
# check-suite.sh — run one of the non-Go SDK suites here (python, ts, rust)
# and read its verdict off the runner's own summary line, colour stripped,
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
# cache is invisible (the two system roots of docs/fetch.md §1 cannot be
# hidden by environment; they are named if present).
# --require-artifacts is the second run's rule: the golden set must have RUN,
# and no test may have skipped for want of a registry.
#
# The artifact-backed proof beyond these — the server-truth suites, the
# oracle, the rigs — is the core repository's certify workflow.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-suite: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

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

LOG_DIR="$ROOT/scratch/check-suite"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$WHICH.log"; PLAIN="$LOG_DIR/$WHICH.plain.log"
RC=0
# run <dir> <cmd...> — the runner's output reaches the terminal AND the log;
# its exit code is kept, and read last.
run() {
  local dir="$1"; shift
  RC=0
  ( cd "$dir" && "$@" ) 2>&1 | tee "$LOG" || RC=$?
  # vitest colours its summary under CI even without a TTY, and an anchored
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
    if [ "$REQUIRE" -eq 1 ]; then
      ! grep -qE '^SKIPPED .*tests/test_golden\.py' "$PLAIN" || PROBLEMS+=("the golden set was SKIPPED with artifacts required")
      ! grep -qE '^SKIPPED .*no chtypes artifacts on the search path' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
    fi
    ;;
  ts)
    command -v pnpm >/dev/null 2>&1 || die "pnpm is not on PATH; the ts suite cannot run and must not be reported as passing"
    [ -d "$ROOT/ts/node_modules" ] || die "ts/node_modules is absent — run 'pnpm install --frozen-lockfile && pnpm build' in ts/ first"
    say "vitest run --reporter=verbose (ts/) — every skipped test is listed by name"
    run "$ROOT/ts" env NO_COLOR=1 pnpm -s test --reporter=verbose
    FILES_LINE="$(grep -E '^\s*Test Files ' "$PLAIN" | tail -1 || true)"
    TESTS_LINE="$(grep -E '^\s*Tests ' "$PLAIN" | tail -1 || true)"
    SUMMARY="$(printf '%s / %s' "${FILES_LINE#"${FILES_LINE%%[![:space:]]*}"}" "${TESTS_LINE#"${TESTS_LINE%%[![:space:]]*}"}")"
    [ -n "$TESTS_LINE" ] || SUMMARY=""
    PASSED="$(printf '%s\n' "$TESTS_LINE" | first_number passed || true)"
    SKIPPED="$(printf '%s\n' "$TESTS_LINE" | first_number skipped || true)"
    ! printf '%s\n%s\n' "$FILES_LINE" "$TESTS_LINE" | grep -qE '[0-9]+ failed' || PROBLEMS+=("the summary reports failures")
    if [ "$REQUIRE" -eq 1 ]; then
      grep -qF '✓ test/golden.test.ts >' "$PLAIN" || PROBLEMS+=("no golden case RAN with artifacts required")
      ! grep -qF '↓ test/golden.test.ts' "$PLAIN" || PROBLEMS+=("the golden set was SKIPPED with artifacts required")
      ! grep -qE 'SKIPPED: (no artifact|none on the search path)' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
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
    if [ "$REQUIRE" -eq 1 ]; then
      grep -qE '^test goldens_hold_on_every_artifact \.\.\. ok' "$PLAIN" || PROBLEMS+=("the golden test did not pass with artifacts required")
      ! grep -qE '^SKIP goldens_hold_on_every_artifact|Every test in this file is skipped|^SKIP integration::' "$PLAIN" || PROBLEMS+=("tests skipped for want of a registry with artifacts required")
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
