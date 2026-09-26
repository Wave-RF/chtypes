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

# python_abi_case_failed <test-name> <file> — true iff pytest's own "short
# test summary info" reports FAILED for tests/test_abi_revision.py::<test-name>
# in <file> (#112). Companion to abi_case_ran above, not a replacement for
# either existing python ABI-revision assertion below: "SKIPPED" and
# "collected" each prove a different absence (the fixture unset; the case
# never existing), and neither fires for a case that RAN and FAILED, which is
# the shape a real regression takes — pytest's own summary line said so
# already ("the summary reports failures"), but named no case, because
# nothing here had read that line yet. This does.
#
# Requires -rfs on the pytest invocation below, not a bare -rs: pytest's -r
# is a `store`, not an `append`, so the LAST -r on the effective command line
# wins — and pyproject.toml's addopts already carries "-ra". A bare -rs here
# would silently OVERRIDE that and drop the FAILED short-summary line
# entirely (measured: with plain -rs a case that ran and failed left nothing
# but its traceback in the plain log, in a shape pytest documents no promise
# about). That is the well-formed-input fix, same shape as the rust arm's
# separated stdout/stderr capture below (#66) — the census reads a line
# pytest is documented to print, rather than a matcher loosened to go
# spelunking in a traceback for the test's own name.
#
# Anchored on the exact node id at both ends (space or end-of-line after it),
# so a scan of the whole log matches only the one case named — never a
# neighboring FAILED line, and never a same-prefixed test name (see
# --selftest). No libtest-style interleave risk here: this suite runs
# single-threaded, with no xdist, and the short test summary is written once,
# after every test body has already finished — unlike abi_case_ran's
# mid-body progress line, there is nothing for a case's own output to land
# inside of.
python_abi_case_failed() {
  local t="$1" file="$2"
  grep -qE "^FAILED tests/test_abi_revision\.py::$t( |\$)" "$file"
}

# PARITY_OK_RE (#77) — the rust arm's OTHER census carries the same
# end-anchor shape as abi_case_ran above, and reads it off the very same
# kind of merged capture: the suite-wide $PLAIN is `2>&1 | tee`'d in run()
# below, so anything a parity test writes to stderr mid-body can land inside
# libtest's own "test NAME ... ok" progress line (stdout) and break the `$`
# anchor — see abi_case_ran's comment for the three shapes actually measured
# on #66 (same libtest engine, same mechanism). Nothing in rust/tests/parity.rs
# writes to stderr TODAY, which is why this has never fired — the exposure
# was latent, found while fixing #66 and filed separately as #77 because it
# is a different check. It is worse in one respect: this census is a COUNT
# against a THRESHOLD ($PARITY_MIN, below), not a boolean about one named
# case, so an interleave silently UNDERCOUNTS rather than naming a specific
# case as unproven — the failure message would say the parity suite is short
# of cases, sending the next reader to the parity tests, which are fine.
# Factored out (like abi_case_ran) so --selftest and the real census below
# drive the exact same pattern — no re-implemented regex.
PARITY_OK_RE='^test (parity_manifest|rust_(exposes|answers)|the_(rust|go_copy))[a-z_]* \.\.\. ok$'

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

  # PARITY_OK_RE (#77) — same three interleave shapes as above, driven
  # through the SAME regex the rust arm's census counts with (no
  # re-implemented pattern), spelled with real parity test names
  # (rust/tests/parity.rs) in place of the ABI-fixture's. Unlike
  # abi_case_ran this is a COUNT, not a boolean, so each assertion below
  # reads grep -c's own number rather than its exit status.
  printf 'test parity_manifest_meets_its_own_floors ... ok\ntest rust_exposes_every_capability_the_contract_assigns_it ... ok\ntest the_rust_cli_offers_every_contract_subcommand ... ok\n' \
    > "$tmp/parity-clean.log"
  N="$(grep -cE "$PARITY_OK_RE" "$tmp/parity-clean.log")"
  [ "$N" -eq 3 ] || { echo "SELFTEST FAILED: 3 clean parity lines counted as $N, not 3" >&2; exit 1; }

  # Shape 1 (#61's shape on #66): verdict present, junk appended after "ok".
  printf 'test rust_answers_the_same_values_as_the_other_bindings ... okABI fixture: ran matching_revision_loads\n' \
    > "$tmp/parity-shape1.log"
  N="$(grep -cE "$PARITY_OK_RE" "$tmp/parity-shape1.log" || true)"
  [ "$N" -eq 0 ] || { echo "SELFTEST FAILED: parity shape 1 (ok present, line not ended) counted as $N, not 0 — the anchor must stay strict" >&2; exit 1; }

  # Shape 2 (#65's shape on #66): verdict displaced — no "ok" on the line at all.
  printf 'test the_go_copy_of_the_manifest_is_byte_identical ... ABI fixture: ran matching_revision_loads\n' \
    > "$tmp/parity-shape2.log"
  N="$(grep -cE "$PARITY_OK_RE" "$tmp/parity-shape2.log" || true)"
  [ "$N" -eq 0 ] || { echo "SELFTEST FAILED: parity shape 2 (no ok on the line) counted as $N, not 0" >&2; exit 1; }

  # Shape 3 (#71's shape on #66): line PREFIXED, breaking the ^ anchor too,
  # and carrying no verdict.
  printf 'ABI fixture: ran matching_revision_loadstest the_rust_unlisted_allowlist_has_not_rotted ... \n' \
    > "$tmp/parity-shape3.log"
  N="$(grep -cE "$PARITY_OK_RE" "$tmp/parity-shape3.log" || true)"
  [ "$N" -eq 0 ] || { echo "SELFTEST FAILED: parity shape 3 (line prefixed, no verdict) counted as $N, not 0 — this would go quiet on a real failure" >&2; exit 1; }

  # Threshold behavior (#77): this census is a COUNT against PARITY_MIN
  # (6, below), not a boolean about one case, so a fix that only repairs the
  # matcher and leaves the counting/threshold wiring broken would still turn
  # the gate into a no-op. Simulate the ORIGINAL exposure at scale: six real
  # passing tests (one per test name this pattern recognizes), every one
  # corrupted the same way (shape 2), as would land in the merged $PLAIN if
  # every parity test wrote to stderr mid-body. If the count silently
  # cleared the >= 6 threshold anyway, the fix would have quietly turned the
  # gate off; it must instead undercount to 0, and that count must then fail
  # the threshold comparison the real census applies.
  : > "$tmp/parity-undercount.log"
  for t in parity_manifest_meets_its_own_floors parity_manifest_is_fully_declared \
           the_go_copy_of_the_manifest_is_byte_identical rust_exposes_every_capability_the_contract_assigns_it \
           rust_answers_the_same_values_as_the_other_bindings the_rust_cli_offers_every_contract_subcommand; do
    printf 'test %s ... ABI fixture: ran matching_revision_loads\n' "$t" >> "$tmp/parity-undercount.log"
  done
  N="$(grep -cE "$PARITY_OK_RE" "$tmp/parity-undercount.log" || true)"
  [ "$N" -eq 0 ] || { echo "SELFTEST FAILED: 6 interleaved parity lines counted as $N, not 0 — corrupted lines must never clear the threshold" >&2; exit 1; }
  [ "$N" -lt 6 ] || { echo "SELFTEST FAILED: an under-count of $N was read as meeting PARITY_MIN=6 — the threshold gate would be a no-op" >&2; exit 1; }

  # python_abi_case_failed (#112) — pin that a case which RAN and FAILED is
  # read off pytest's own "short test summary info" FAILED line, the shape a
  # real regression takes and the one the two existing python ABI-revision
  # assertions (SKIPPED, collected) cannot see (neither fires for a case
  # that ran and failed). Positive case first, using the exact line measured
  # from a planted refusal-guard defect (a guard replaced with one that can
  # never be true — issue #112's own reproduction).
  printf 'FAILED tests/test_abi_revision.py::test_wrong_revision_is_refused - Failed: DID NOT RAISE RegistryError\n' \
    > "$tmp/pyabi-failed.log"
  python_abi_case_failed test_wrong_revision_is_refused "$tmp/pyabi-failed.log" \
    || { echo "SELFTEST FAILED: a genuine FAILED summary line for the case was read as not-failed" >&2; exit 1; }

  # Negative control: an ordinary clean run (pytest -q names no PASSED line
  # at all, so a log with nothing FAILED in it must never trip this).
  printf '122 passed, 20 skipped in 2.52s\n' > "$tmp/pyabi-clean.log"
  python_abi_case_failed test_wrong_revision_is_refused "$tmp/pyabi-clean.log" \
    && { echo "SELFTEST FAILED: a clean run with no FAILED line was read as failed" >&2; exit 1; }

  # Negative control: the case genuinely did not run (fixture unset — this
  # is what the existing SKIPPED assertion above already catches). This
  # matcher must stay quiet on that shape too, so the two assertions cover
  # distinct failure modes rather than one masking the other.
  printf 'SKIPPED [1] tests/test_abi_revision.py:56: no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset\n' \
    > "$tmp/pyabi-skipped.log"
  python_abi_case_failed test_wrong_revision_is_refused "$tmp/pyabi-skipped.log" \
    && { echo "SELFTEST FAILED: a SKIPPED line was read as a FAILED one" >&2; exit 1; }

  # Negative control: a FAILED line for an unrelated test must not satisfy
  # the case this census is naming — a census that lit up on ANY failure
  # would stop pointing at the ABI-revision guarantee specifically.
  printf 'FAILED tests/test_registry.py::test_something_else - AssertionError\n' \
    > "$tmp/pyabi-other.log"
  python_abi_case_failed test_wrong_revision_is_refused "$tmp/pyabi-other.log" \
    && { echo "SELFTEST FAILED: an unrelated test's FAILED line was read as this case having failed" >&2; exit 1; }

  # Negative control: the anchor must stop at the test name's own end, not
  # just its start — a same-prefixed neighbor (a real pytest node id shape:
  # a longer test name sharing this one's prefix) must not satisfy it either,
  # or the census would name the wrong case as the ABI-revision guarantee
  # while a different test entirely was what actually failed.
  printf 'FAILED tests/test_abi_revision.py::test_wrong_revision_is_refused_and_something_else - AssertionError\n' \
    > "$tmp/pyabi-prefix.log"
  python_abi_case_failed test_wrong_revision_is_refused "$tmp/pyabi-prefix.log" \
    && { echo "SELFTEST FAILED: a same-prefixed neighboring test name was read as this case" >&2; exit 1; }

  # The other case name must be matched independently — the loop in the
  # python arm below calls this once per case, and a matcher that only
  # ever recognized one hardcoded name would silently stop covering the
  # control.
  printf 'FAILED tests/test_abi_revision.py::test_matching_revision_loads - AssertionError\n' \
    > "$tmp/pyabi-control-failed.log"
  python_abi_case_failed test_matching_revision_loads "$tmp/pyabi-control-failed.log" \
    || { echo "SELFTEST FAILED: the matching_revision_loads control's own FAILED line was not recognized" >&2; exit 1; }

  # scripts/lib/provenance.py (chtypes#190) — the registry provenance printer
  # both this script and check-standalone.sh call before running a suite.
  # Its own --selftest builds a fake registry (a matching-revision manifest,
  # a mismatched one, and a manifest-less scratch directory) and, critically,
  # rewrites a TEMP COPY of the header's CHS_ABI_REVISION and re-asserts that
  # the WARNING moves with it — so a version of the printer that hard-codes
  # the pinned revision instead of reading the header fails this, not just a
  # missing feature.
  python3 "$SCRIPTS/lib/provenance.py" --selftest \
    || { echo "SELFTEST FAILED: scripts/lib/provenance.py --selftest" >&2; exit 1; }

  echo "check-suite: selftest ok — the ABI-fixture matcher stays strict on all three #66 interleave shapes and a genuine non-run, still reads a clean line as RUN, the parity census matcher (#77) stays strict on the same three shapes, counts a clean line, and still fails its threshold on an undercount, the python ABI-revision FAILED-line matcher (#112) names a case that ran and failed without tripping on a clean run, a skip, an unrelated failure, or a same-prefixed neighbor, and scripts/lib/provenance.py's own selftest passed (#190)"
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

# A registry PATH is not a fingerprint: the same path holds different builds
# on different days, and a machine that also builds artifacts can hold
# several producer commits worth at once (chtypes#190). Before the suite
# runs, say what is actually in the registry it will use — one line per
# artifact line's manifest fields, the goldens file's identity, and a loud
# (never fatal) WARNING when a line's ABI revision does not match this
# binding's own header. Mirrors the first entry of the documented registry
# search path (docs/guides/artifacts.md), same as check-standalone.sh: an
# explicit $CHTYPES_REGISTRY if set, else the per-user cache.
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH; provenance cannot be printed"
if [ "$NO_ARTIFACTS" -eq 1 ]; then
  python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/include/chtypes.h"
else
  REG="${CHTYPES_REGISTRY:-}"
  if [ -z "$REG" ]; then
    _os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$(uname -m)" in x86_64|amd64) _arch=amd64 ;; arm64|aarch64) _arch=arm64 ;; *) _arch="$(uname -m)" ;; esac
    REG="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/artifacts/$_os-$_arch"
  fi
  python3 "$SCRIPTS/lib/provenance.py" --header "$ROOT/include/chtypes.h" --registry "$REG"
fi

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
    say "pytest -q -rfs (python/)"
    # -rfs, not just -rs: pytest's -r is a `store`, not an `append` — the last
    # -r on the effective command line wins over pyproject.toml's addopts
    # "-ra", so a bare -rs here would silently DROP the "short test summary
    # info" FAILED lines the ABI-revision census below depends on (measured:
    # with plain -rs a case that ran and failed left no FAILED line anywhere
    # in the plain log — only its traceback, which the golden/registry
    # censuses below do not parse). Adding f keeps both: skip lines other
    # assertions in this arm already read, and now failure lines too.
    run "$ROOT/python" uv run --quiet pytest -q -rfs tests
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
      # failure in the summary is the two cases having passed. Neither of
      # these two checks fires for a case that RAN and FAILED — the shape a
      # real regression takes (#112) — so that shape is read separately,
      # below, off the same -rfs short summary the SKIPPED check already
      # reads. The SKIPPED and collected checks are NOT replaced: they catch
      # a case never running at all, which python_abi_case_failed cannot.
      ! grep -qF 'no ABI revision fixture' "$PLAIN" || PROBLEMS+=("the ABI-revision cases SKIPPED although \$CHTYPES_ABI_FIXTURES is set")
      ABI_N="$( (cd "$ROOT/python" && uv run --quiet pytest -q --collect-only tests/test_abi_revision.py 2>/dev/null) \
               | grep -cE '^tests/test_abi_revision\.py::test_(wrong_revision_is_refused|matching_revision_loads)$' || true)"
      [ "${ABI_N:-0}" -eq 2 ] || PROBLEMS+=("the ABI-revision handshake was not proven here: python collected ${ABI_N:-0} of its 2 cases")
      for t in test_wrong_revision_is_refused test_matching_revision_loads; do
        ! python_abi_case_failed "$t" "$PLAIN" \
          || PROBLEMS+=("the ABI-revision case $t did not pass although \$CHTYPES_ABI_FIXTURES is set")
      done
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
    # PARITY_OK_RE (defined above, shared with --selftest) has the same
    # end-anchor shape as abi_case_ran, and #77 found it reads the same kind
    # of exposed input: the suite-wide $PLAIN above is the ordinary run()
    # helper's `2>&1 | tee` capture, so anything rust/tests/parity.rs writes
    # to stderr mid-body could land inside libtest's own "test NAME ... ok"
    # progress line (stdout) and break the `$` anchor — see abi_case_ran's
    # comment for the three shapes #66 actually measured (same libtest
    # engine, same mechanism). Nothing in parity.rs writes to stderr TODAY,
    # so this has never fired, but the fix does not depend on that staying
    # true, and this census is worse when it does fire: it is a COUNT
    # against a THRESHOLD rather than a boolean about one named case, so an
    # interleave would silently UNDERCOUNT instead of naming an unproven
    # case — a red that sends the next reader to the parity tests, which are
    # fine (#77).
    #
    # THE FIX, same shape as #66: the parity tests live in their own binary
    # (`cargo test --test parity`), so exactly as #66 did for the ABI
    # fixture, it is run again here, alone, with stdout and stderr captured
    # to SEPARATE files and concatenated only after both are complete —
    # which cannot itself introduce an interleave. PARITY_N is read off that
    # concatenation, never off the suite-wide, 2>&1-merged $PLAIN above
    # (which still runs these tests too, as part of the ordinary suite, and
    # is not relied on here).
    PARITY_STDOUT="$LOG_DIR/rust-parity.stdout.log"
    PARITY_STDERR="$LOG_DIR/rust-parity.stderr.log"
    PARITY_PLAIN="$LOG_DIR/rust-parity.plain.log"
    ( cd "$ROOT/rust" && cargo test --locked --test parity ) \
      > "$PARITY_STDOUT" 2> "$PARITY_STDERR" || true
    cat "$PARITY_STDOUT" "$PARITY_STDERR"
    sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' "$PARITY_STDOUT" "$PARITY_STDERR" > "$PARITY_PLAIN"
    PARITY_N="$(grep -cE "$PARITY_OK_RE" "$PARITY_PLAIN" || true)"
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
