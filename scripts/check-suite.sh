#!/usr/bin/env bash
# check-suite.sh — run one of the non-Go SDK suites here (python, ts, rust)
# and read its verdict off the runner's own summary line, color stripped,
# never off an exit code alone.
#
#   scripts/check-suite.sh [--no-artifacts] python|ts|rust
#   scripts/check-suite.sh --selftest
#
# WHY. A runner that collected nothing, or skipped everything, still exits 0.
# So each suite must report at least one test PASSED on its own summary line
# and no failure anywhere in it; every skip it announced stays in the output
# above the verdict, so the log says what did not run. Under ABI v1 the
# suites need no registry and no published artifact: the loader is exercised
# against generated stubs and the fetch layer against a registry the tests
# start themselves, so there is one mode.
#
# --no-artifacts is kept because CI passes it: it points XDG_CACHE_HOME at an
# empty scratch directory and unsets CHTYPES_REGISTRY, so a developer's own
# cache is invisible and the run reproduces what a contributor with nothing
# installed gets.
#
# One fixed line goes to stdout once the verdict is decided —
# `chtypes-count suite=<lang>-no-artifacts ran=<n> skipped=<n>` — so
# scripts/policy-merge-check.py's `test-counts` condition can read this job's
# own executed-test count back off its log.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die()  { printf 'check-suite: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }

first_number() { grep -oE "[0-9]+ $1" | grep -oE '[0-9]+' | tail -1; }

# The three censuses. Each reads $PLAIN (the runner's own output, color
# stripped) and sets SUMMARY, PASSED, SKIPPED and appends to PROBLEMS, so
# --selftest drives the same code the real run does.
census_python() {
  SUMMARY="$(grep -E '^[0-9]+ (passed|failed|skipped|error)' "$PLAIN" | tail -1 || true)"
  PASSED="$(printf '%s\n' "$SUMMARY" | first_number passed || true)"
  SKIPPED="$(printf '%s\n' "$SUMMARY" | first_number skipped || true)"
  ! printf '%s\n' "$SUMMARY" | grep -qE '[0-9]+ (failed|error)' || PROBLEMS+=("the summary reports failures")
}
census_ts() {
  local files_line tests_line
  files_line="$(grep -E '^\s*Test Files ' "$PLAIN" | tail -1 || true)"
  tests_line="$(grep -E '^\s*Tests ' "$PLAIN" | tail -1 || true)"
  SUMMARY="$(printf '%s / %s' "${files_line#"${files_line%%[![:space:]]*}"}" "${tests_line#"${tests_line%%[![:space:]]*}"}")"
  [ -n "$tests_line" ] || SUMMARY=""
  PASSED="$(printf '%s\n' "$tests_line" | first_number passed || true)"
  SKIPPED="$(printf '%s\n' "$tests_line" | first_number skipped || true)"
  ! printf '%s\n%s\n' "$files_line" "$tests_line" | grep -qE '[0-9]+ failed' || PROBLEMS+=("the summary reports failures")
}
census_rust() {
  local results failed_n
  # Anchored on the summary's exact shape: a bare `^test result:` also
  # matches a crate's own `result` module ("test result::tests::… ok").
  results="$(grep -E '^test result: (ok|FAILED)\. ' "$PLAIN" || true)"
  PASSED="$(printf '%s\n' "$results" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}' || true)"
  failed_n="$(printf '%s\n' "$results" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}' || true)"
  SUMMARY=""
  [ -z "$results" ] || SUMMARY="$(printf '%s\n' "$results" | grep -c .) test binaries: $PASSED passed, $failed_n failed (each binary's own line above)"
  SKIPPED="$(grep -cE '^SKIP ' "$PLAIN" || true)"
  ! grep -qE '^test result: FAILED|^error(\[E[0-9]+\])?:|panicked at' "$PLAIN" || PROBLEMS+=("a test binary reported FAILED, or did not build")
}

if [ "${1:-}" = "--selftest" ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  PLAIN="$tmp/plain.log"
  expect() { # <label> <census fn> <want ran> <want problems 0|1> ; log on stdin
    cat > "$PLAIN"
    PROBLEMS=(); SUMMARY=""; PASSED=0; SKIPPED=""
    "$2"
    [ "${PASSED:-0}" = "$3" ] || { echo "SELFTEST FAILED: $1 counted ${PASSED:-} passed, not $3" >&2; exit 1; }
    [ "${#PROBLEMS[@]}" -eq "$4" ] || { echo "SELFTEST FAILED: $1 raised ${#PROBLEMS[@]} problem(s), wanted $4" >&2; exit 1; }
  }
  printf '122 passed, 20 skipped in 2.52s\n' | expect "a clean pytest run" census_python 122 0
  printf '1 failed, 120 passed, 2 skipped in 2.52s\n' | expect "a pytest run with a failure" census_python 120 1
  printf '0 passed in 0.1s\n' | expect "a pytest run that ran nothing" census_python 0 0
  printf ' Test Files  3 passed (3)\n      Tests  17 passed | 2 skipped (19)\n' | expect "a clean vitest run" census_ts 17 0
  printf ' Test Files  1 failed | 2 passed (3)\n      Tests  1 failed | 16 passed (19)\n' | expect "a vitest run with a failure" census_ts 16 1
  printf 'test result: ok. 5 passed; 0 failed; 1 ignored\ntest result: ok. 7 passed; 0 failed; 0 ignored\n' | expect "a clean cargo run" census_rust 12 0
  printf 'test result: FAILED. 4 passed; 1 failed; 0 ignored\n' | expect "a cargo run with a failure" census_rust 4 1
  printf 'test result::tests::x ... ok\n' | expect "a test named result::" census_rust 0 0
  printf 'error[E0432]: unresolved import\n' | expect "a cargo build error" census_rust 0 1
  # scripts/lib/provenance.py (chtypes#190) — the ABI-identity printer this
  # script and check-standalone.sh call before running a suite. Its own
  # --selftest rewrites a TEMP COPY of the header's identity macros and
  # re-asserts that the printed line moves with them.
  python3 "$SCRIPTS/lib/provenance.py" --selftest \
    || { echo "SELFTEST FAILED: scripts/lib/provenance.py --selftest" >&2; exit 1; }
  echo "check-suite: selftest ok — each language's census counts a clean run, refuses a failure and a zero-run, and ignores a test merely named result::, and scripts/lib/provenance.py's own selftest passed (#190)"
  exit 0
fi

WHICH=""; NO_ARTIFACTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-artifacts)      NO_ARTIFACTS=1 ;;
    python|ts|rust)      [ -z "$WHICH" ] || die "one suite per run ($WHICH, $1)"; WHICH="$1" ;;
    -h|--help)           sed -n '2,25p' "$0"; exit 0 ;;
    *)                   die "unknown argument: $1" ;;
  esac
  shift
done
[ -n "$WHICH" ] || die "which suite? python|ts|rust"

if [ "$NO_ARTIFACTS" -eq 1 ]; then
  SCRATCH_CACHE="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-no-artifacts.XXXXXX")"
  trap 'rm -rf "$SCRATCH_CACHE"' EXIT
  export XDG_CACHE_HOME="$SCRATCH_CACHE"
  unset CHTYPES_REGISTRY CHTYPES_AUTOFETCH
  say "--no-artifacts: XDG_CACHE_HOME=$SCRATCH_CACHE (empty), CHTYPES_REGISTRY unset"
fi

# Before the suite runs, say which ABI this binding was built against: the
# header's version and fingerprint (scripts/lib/provenance.py). Under ABI v1 a
# library is identified when it is loaded, by its own build_info.
if ! command -v python3 >/dev/null 2>&1; then
  # chtypes#320: python3 itself is missing, so even the annotation helper
  # (also python3) cannot run; a static echo is all that is left.
  [ "${GITHUB_ACTIONS:-}" != "true" ] || echo "::warning title=chtypes artifact provenance::unknown — $WHICH suite (python3 not on PATH; provenance cannot be printed)"
  die "python3 is not on PATH; provenance cannot be printed"
fi
python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/$(python3 "$SCRIPTS/abi-v1/majors.py" header "$WHICH")"

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

PROBLEMS=(); SUMMARY=""; PASSED=0; SKIPPED=""
case "$WHICH" in
  python)
    command -v uv >/dev/null 2>&1 || die "uv is not on PATH; the python suite cannot run and must not be reported as passing"
    say "pytest -q -rfs (python/)"
    # -rfs, not just -rs: pytest's -r is a `store`, so the last -r on the
    # command line wins over pyproject.toml's addopts "-ra".
    run "$ROOT/python" uv run --quiet pytest -q -rfs tests
    census_python
    ;;
  ts)
    command -v pnpm >/dev/null 2>&1 || die "pnpm is not on PATH; the ts suite cannot run and must not be reported as passing"
    [ -d "$ROOT/ts/node_modules" ] || die "ts/node_modules is absent — run 'pnpm install --frozen-lockfile && pnpm build' in ts/ first"
    say "vitest run --reporter=verbose (ts/) — every skipped test is listed by name"
    # vitest is invoked DIRECTLY, not through pnpm: pnpm changes the child's
    # environment in a way that makes vitest stop emitting per-test lines
    # (measured on pnpm 12.4.1).
    run "$ROOT/ts" env NO_COLOR=1 ./node_modules/.bin/vitest run --reporter=verbose
    census_ts
    ;;
  rust)
    command -v cargo >/dev/null 2>&1 || die "cargo is not on PATH; the rust suite cannot run and must not be reported as passing"
    say "cargo test --locked (rust/) — a skipped test says SKIP on stderr, by name"
    run "$ROOT/rust" cargo test --locked
    census_rust
    ;;
esac

# chtypes#285 §1b — the fixed count line, derived from $PASSED/$SKIPPED
# above, never a second hand-set count.
echo "chtypes-count suite=$WHICH-no-artifacts ran=${PASSED:-} skipped=${SKIPPED:-0}"

[ -n "$SUMMARY" ] || PROBLEMS+=("no summary line — the runner did not finish; see $LOG")
[ "$RC" -eq 0 ] || PROBLEMS+=("the runner exited $RC")
[ "${PASSED:-0}" -gt 0 ] || PROBLEMS+=("zero tests passed — a suite that ran nothing is not a pass")

echo "  $WHICH census — ${SUMMARY:-<no summary line>}"
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  echo "  VERDICT $WHICH: NOT a pass —"; for p in "${PROBLEMS[@]}"; do echo "    * $p"; done
  exit 1
fi
echo "  VERDICT $WHICH: pass — ${PASSED} test(s) passed${SKIPPED:+, $SKIPPED skipped (each named above)}"
