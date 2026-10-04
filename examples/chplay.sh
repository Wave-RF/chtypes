#!/usr/bin/env bash
# chplay.sh — run the chtypes playground tours.
#
#   ./chplay.sh              run every language whose toolchain is installed
#   ./chplay.sh go           run one (any of: go python ts rust)
#   ./chplay.sh go rust      run a subset
#   ./chplay.sh --list       show what would run, and with which toolchain
#   ./chplay.sh --require-all  a missing toolchain is a FAILURE, not a skip
#   ./chplay.sh --locked       every tour honors its committed lockfile
#
# Each tour runs against a real library from the v1 cache. Before a tour runs,
# this script fetches one line through THAT binding's own command line
# (`chtypes fetch <line>`: resolve over OCI, verify the signed statement, unpack
# into the cache), so a tour exercises the same fetch path a user gets. The line
# is $CHPLAY_LINE (default: the newest line the registry publishes); $CHPLAY_FETCH_ARGS adds flags to the fetch,
# for example `--offline` on a machine whose cache is already seeded. The cache
# is the binding's default (${XDG_CACHE_HOME:-~/.cache}/chtypes/v1), or the
# directory $CHTYPES_CACHE names. The tours need the network only for that
# fetch. No Docker, no ClickHouse server.
#
# A missing toolchain is a SKIP with instructions, never a failure. A tour
# that crashes is a failure and makes this script exit nonzero.
#
# THE TWO CI FLAGS, and why they exist. Interactively a skip is the right
# answer: you should not need four toolchains to see one tour. In CI it is
# the wrong answer twice over — a run where all four silently skipped exits
# 0 and proves nothing, and a tour resolved against a freshly-solved
# dependency set cannot catch a committed lockfile that has drifted. That
# is what let examples/rust/Cargo.lock sit at 0.1.0 through the whole 0.1.1
# release with every gate green. --require-all turns a skip into a failure;
# --locked makes each tour use its lockfile as committed and fail if it no
# longer resolves. CI passes both; neither changes what a tour prints.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The v1 cache every binding shares, and the line each tour's fetch asks for.
# Neither is read from include/chtypes.h: ABI v1 has no revision number, and
# each binding checks its own ABI fingerprint when it loads a library.
CACHE_ROOT="${CHTYPES_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/v1}"
# Without $CHPLAY_LINE, the line is the newest two-part line the registry
# publishes, read from its tags/list, so the tours follow what is actually served
# instead of a number written here (a hard-coded line that a registry does not
# carry yet would turn the required `examples` job red). The registry is the first
# base in $CHTYPES_ARTIFACTS_URL, else the default every binding uses. If the
# listing cannot be read (no network, or an --offline cache), 26.8 is the fallback,
# and a fetch of a line the registry lacks still fails loudly.
newest_published_line() {
  local base="${CHTYPES_ARTIFACTS_URL:-https://registry.wavehouse.dev/chtypes/v1}"
  base="${base%%,*}"
  # A client splices /v2/ right after the scheme and authority (fetch-v1.md §2).
  local scheme="${base%%://*}" rest="${base#*://}"
  local authority="${rest%%/*}" path="${rest#*/}"
  [ "$scheme" = "http" ] || [ "$scheme" = "https" ] || return 1
  curl -fsS --max-time 20 -A "chplay/1" \
    "$scheme://$authority/v2/$path/tags/list" 2>/dev/null |
    python3 -c '
import json, sys
tags = json.load(sys.stdin).get("tags") or []
lines = sorted({t for t in tags if t.count(".") == 1 and all(p.isdigit() for p in t.split("."))},
               key=lambda t: tuple(int(p) for p in t.split(".")))
print(lines[-1] if lines else "")
' 2>/dev/null
}
if [ -n "${CHPLAY_LINE:-}" ]; then
  LINE="$CHPLAY_LINE"
else
  LINE="$(newest_published_line || true)"
  [ -n "$LINE" ] || LINE=26.8
fi
# Every tour prints this many numbered section banners when it ran in full.
EXPECTED_SECTIONS=17
ALL_LANGS=(go python ts rust)

# Both default off: the interactive run is the forgiving one.
REQUIRE_ALL=0
LOCKED=0

bold()  { printf '\033[1m%s\033[0m' "$1"; }
say()   { printf '%s\n' "$*"; }

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ------------------------------------------------------------------- fetch
#
# Per language, `fetch_<lang>` runs that binding's own command line from its own
# directory. A failed fetch fails that language's tour; it is never a skip.

# shellcheck disable=SC2086  # CHPLAY_FETCH_ARGS is a word list on purpose
fetch_go() { (cd "$HERE/../go" && go run ./cmd/chtypes fetch $CHPLAY_FETCH_ARGS "$LINE"); }
# shellcheck disable=SC2086
fetch_python() { (cd "$HERE/../python" && uv run python -m chtypes fetch $CHPLAY_FETCH_ARGS "$LINE"); }
# shellcheck disable=SC2086
fetch_ts() { (cd "$HERE/../ts" && pnpm install --frozen-lockfile --silent && pnpm build >/dev/null && node dist/cli.js fetch $CHPLAY_FETCH_ARGS "$LINE"); }
# shellcheck disable=SC2086
fetch_rust() { (cd "$HERE/../rust" && cargo run --quiet --locked --bin chtypes -- fetch $CHPLAY_FETCH_ARGS "$LINE"); }

# ---------------------------------------------------------------- languages
#
# Per language: have <lang> answers "is the toolchain here" (and says how to
# get it when not); run <lang> runs the tour from its own directory.

have_go() { command -v go >/dev/null 2>&1 && return 0
  SKIP_REASON="go toolchain not found — brew install go (or https://go.dev/dl); or skip it: ./chplay.sh python ts rust"; return 1; }
# The Go tour's two statically linked sections need the core repository's
# lib/build on CGO_LDFLAGS and the chtypes_linked tag; with the sibling present
# both are set here, otherwise the tour runs dlopen-only and those sections
# say so.
run_go() {
  # No default that walks OUT of this repository: a public clone has no
  # sibling checkout, and a path that only resolves on a maintainer's
  # laptop is worse than no path. Set CHTYPES_LIB_BUILD (or
  # CHTYPES_CORE_DIR) to opt in; absent, the linked sections skip.
  local core="${CHTYPES_CORE_DIR:-}"
  if [ -n "${CHTYPES_LIB_BUILD:-}" ] || { [ -n "$core" ] && [ -d "$core/lib/build" ]; }; then
    local build="${CHTYPES_LIB_BUILD:-$core/lib/build}"
    (cd "$HERE/go" && CGO_LDFLAGS="-L$build -Wl,-rpath,$build" CHTYPES_LIB_BUILD="$build" go run -tags chtypes_linked .)
  else
    (cd "$HERE/go" && go run .)
  fi
}

have_python() { command -v uv >/dev/null 2>&1 && return 0
  SKIP_REASON="uv not found — brew install uv (or https://docs.astral.sh/uv); or skip it: ./chplay.sh go ts rust"; return 1; }
run_python() {
  if [ "$LOCKED" -eq 1 ]; then (cd "$HERE/python" && uv run --locked demo.py)
  else (cd "$HERE/python" && uv run demo.py); fi
}

have_ts() {
  command -v node >/dev/null 2>&1 || { SKIP_REASON="node not found — brew install node; or skip it: ./chplay.sh go python rust"; return 1; }
  command -v pnpm >/dev/null 2>&1 || { SKIP_REASON="pnpm not found — corepack enable pnpm (or brew install pnpm); or skip it: ./chplay.sh go python rust"; return 1; }
  return 0
}
run_ts() {
  if [ "$LOCKED" -eq 1 ]; then
    (cd "$HERE/ts" && pnpm install --frozen-lockfile --silent && pnpm --silent demo)
  else
    (cd "$HERE/ts" &&
      { [ -d node_modules ] || pnpm install --silent; } &&
      pnpm --silent demo)
  fi
}

have_rust() { command -v cargo >/dev/null 2>&1 && return 0
  SKIP_REASON="rust toolchain not found — install rustup (https://rustup.rs); or skip it: ./chplay.sh go python ts"; return 1; }
run_rust() {
  if [ "$LOCKED" -eq 1 ]; then (cd "$HERE/rust" && cargo run --quiet --locked)
  else (cd "$HERE/rust" && cargo run --quiet); fi
}

# ------------------------------------------------------------------ driving

langs=()
for arg in "$@"; do
  case "$arg" in
  -h | --help) usage ;;
  --require-all) REQUIRE_ALL=1 ;;
  --locked) LOCKED=1 ;;
  --list)
    say "cache: $CACHE_ROOT   line: $LINE"
    for l in "${ALL_LANGS[@]}"; do
      SKIP_REASON=""
      if "have_$l"; then say "  $l: ready"; else say "  $l: would skip — $SKIP_REASON"; fi
    done
    exit 0
    ;;
  go | python | ts | rust) langs+=("$arg") ;;
  *)
    say "chplay: unknown language '$arg' (choose from: ${ALL_LANGS[*]})"
    exit 2
    ;;
  esac
done
[ ${#langs[@]} -eq 0 ] && langs=("${ALL_LANGS[@]}")

CHPLAY_FETCH_ARGS="${CHPLAY_FETCH_ARGS:-}"
say "chplay: cache $CACHE_ROOT, fetching line $LINE through each binding's own CLI"
[ "$REQUIRE_ALL" -eq 1 ] && say "chplay: --require-all — a missing toolchain fails this run"
[ "$LOCKED" -eq 1 ] && say "chplay: --locked — every tour must resolve its committed lockfile"
[ -n "${CHTYPES_VERSION:-}" ] && say "chplay: CHTYPES_VERSION=$CHTYPES_VERSION (tours will select it)"

logdir="$(mktemp -d "${TMPDIR:-/tmp}/chplay.XXXXXX")"
trap 'rm -rf "$logdir"' EXIT

declare -a results=()
failed=0

for l in "${langs[@]}"; do
  SKIP_REASON=""
  if ! "have_$l"; then
    if [ "$REQUIRE_ALL" -eq 1 ]; then
      # A gate that skipped everything exits 0 and proves nothing.
      say ""
      say "$(bold "-- $l: FAILED") — toolchain missing, and --require-all forbids a skip"
      say "   $SKIP_REASON"
      results+=("$l: FAILED — toolchain missing under --require-all")
      failed=1
    else
      say ""
      say "$(bold "-- $l: SKIPPED") — $SKIP_REASON"
      results+=("$l: skipped — $SKIP_REASON")
    fi
    continue
  fi
  say ""
  say "$(bold "-- $l: running")  ($HERE/$l)"
  log="$logdir/$l.out"
  if ! { "fetch_$l" 2>&1 | tee "$log"; }; then
    results+=("$l: FAILED — \`$l\` CLI fetch of line $LINE failed")
    failed=1
    continue
  fi
  if "run_$l" 2>&1 | tee "$log"; then
    rc=0
  else
    rc=$?
  fi
  # Never trust the exit code alone: count the numbered section banners the
  # tour actually printed. EXPECTED_SECTIONS means the whole tour ran.
  sections=$(grep -c '^=== ' "$log" 2>/dev/null || true)
  if [ "$rc" -eq 0 ] && [ "${sections:-0}" -ge "$EXPECTED_SECTIONS" ]; then
    results+=("$l: ok — $sections/$EXPECTED_SECTIONS sections ran")
  elif [ "$rc" -eq 0 ]; then
    results+=("$l: FAILED — exited 0 but only ${sections:-0}/$EXPECTED_SECTIONS sections printed")
    failed=1
  else
    results+=("$l: FAILED — exit $rc after ${sections:-0}/$EXPECTED_SECTIONS sections")
    failed=1
  fi
done

say ""
say "$(bold '== chplay summary ==')"
for r in "${results[@]}"; do say "  $r"; done
exit "$failed"
