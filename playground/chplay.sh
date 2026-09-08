#!/usr/bin/env bash
# chplay.sh — run the chtypes playground tours.
#
#   ./chplay.sh              run every language whose toolchain is installed
#   ./chplay.sh go           run one (any of: go python ts rust)
#   ./chplay.sh go rust      run a subset
#   ./chplay.sh --list       show what would run, and with which toolchain
#
# Every tour is OFFLINE: it needs only that language's toolchain plus the
# artifacts in the registry (scripts/fetch.sh, or a core-repository build).
# No Docker, no ClickHouse server, no network.
#
# A missing toolchain is a SKIP with instructions, never a failure. A tour
# that crashes is a failure and makes this script exit nonzero.
#
# The one thing here that DOES want a server — go/ingest-demo — is not run by
# this script at all; see go/ingest-demo/README.md (the optional online demo).
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The registry: $CHTYPES_REGISTRY, else the per-user artifact cache every SDK
# here defaults to (scripts/fetch.sh installs there; a core-repo build lands
# there too). <arch> is spelled the artifact way.
_arch="$(uname -m)"; case "$_arch" in x86_64|amd64) _arch=amd64 ;; arm64|aarch64) _arch=arm64 ;; esac
REGISTRY="${CHTYPES_REGISTRY:-${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/artifacts/$(uname -s | tr '[:upper:]' '[:lower:]')-$_arch}"
ALL_LANGS=(go python ts rust)

bold()  { printf '\033[1m%s\033[0m' "$1"; }
say()   { printf '%s\n' "$*"; }

usage() {
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---------------------------------------------------------------- artifacts
#
# Everything below needs at least one built artifact. Check once, up front,
# with an actionable message — not four times with a stack trace each.
artifact_count() {
  local n=0 d
  for d in "$REGISTRY"/*/; do
    [ -e "${d}libchtypes.dylib" ] || [ -e "${d}libchtypes.so" ] && n=$((n + 1))
  done
  echo "$n"
}

require_artifacts() {
  if [ ! -d "$REGISTRY" ] || [ "$(artifact_count)" -eq 0 ]; then
    say "chplay: no chtypes artifacts under $REGISTRY"
    say ""
    say "  Fetch one first (verified, into the per-user cache):"
    say "      ../scripts/fetch.sh 25.8"
    say "  or build one in the core repository (chtypes-core: just vendor 25.8)."
    say ""
    say "  Or point \$CHTYPES_REGISTRY at a directory that has them."
    exit 1
  fi
}

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
  local core="${CHTYPES_CORE_DIR:-$HERE/../../core}"
  if [ -n "${CHTYPES_LIB_BUILD:-}" ] || [ -d "$core/lib/build" ]; then
    local build="${CHTYPES_LIB_BUILD:-$core/lib/build}"
    (cd "$HERE/go" && CGO_LDFLAGS="-L$build -Wl,-rpath,$build" CHTYPES_LIB_BUILD="$build" go run -tags chtypes_linked .)
  else
    (cd "$HERE/go" && go run .)
  fi
}

have_python() { command -v uv >/dev/null 2>&1 && return 0
  SKIP_REASON="uv not found — brew install uv (or https://docs.astral.sh/uv); or skip it: ./chplay.sh go ts rust"; return 1; }
run_python() { (cd "$HERE/python" && uv run demo.py); }

have_ts() {
  command -v node >/dev/null 2>&1 || { SKIP_REASON="node not found — brew install node; or skip it: ./chplay.sh go python rust"; return 1; }
  command -v pnpm >/dev/null 2>&1 || { SKIP_REASON="pnpm not found — corepack enable pnpm (or brew install pnpm); or skip it: ./chplay.sh go python rust"; return 1; }
  return 0
}
run_ts() {
  (cd "$HERE/ts" &&
    { [ -d node_modules ] || pnpm install --silent; } &&
    node demo.mjs)
}

have_rust() { command -v cargo >/dev/null 2>&1 && return 0
  SKIP_REASON="rust toolchain not found — install rustup (https://rustup.rs); or skip it: ./chplay.sh go python ts"; return 1; }
run_rust() { (cd "$HERE/rust" && cargo run --quiet); }

# ------------------------------------------------------------------ driving

langs=()
for arg in "$@"; do
  case "$arg" in
  -h | --help) usage ;;
  --list)
    require_artifacts
    say "artifacts: $(artifact_count) version(s) under $REGISTRY"
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

require_artifacts
say "chplay: $(artifact_count) artifact version(s) under $REGISTRY"
[ -n "${CHTYPES_VERSION:-}" ] && say "chplay: CHTYPES_VERSION=$CHTYPES_VERSION (tours will select it)"

logdir="$(mktemp -d "${TMPDIR:-/tmp}/chplay.XXXXXX")"
trap 'rm -rf "$logdir"' EXIT

declare -a results=()
failed=0

for l in "${langs[@]}"; do
  SKIP_REASON=""
  if ! "have_$l"; then
    say ""
    say "$(bold "-- $l: SKIPPED") — $SKIP_REASON"
    results+=("$l: skipped — $SKIP_REASON")
    continue
  fi
  say ""
  say "$(bold "-- $l: running")  ($HERE/$l)"
  log="$logdir/$l.out"
  if "run_$l" 2>&1 | tee "$log"; then
    rc=0
  else
    rc=$?
  fi
  # Never trust the exit code alone: count the numbered section banners the
  # tour actually printed. 16 means the whole tour ran (15 and 16 — the
  # revision-3 export and filter sections — PRINT even when they degrade on
  # an artifact that predates the surface).
  sections=$(grep -c '^=== ' "$log" 2>/dev/null || true)
  if [ "$rc" -eq 0 ] && [ "${sections:-0}" -ge 16 ]; then
    results+=("$l: ok — $sections/16 sections ran")
  elif [ "$rc" -eq 0 ]; then
    results+=("$l: FAILED — exited 0 but only ${sections:-0}/16 sections printed")
    failed=1
  else
    results+=("$l: FAILED — exit $rc after ${sections:-0}/16 sections")
    failed=1
  fi
done

say ""
say "$(bold '== chplay summary ==')"
for r in "${results[@]}"; do say "  $r"; done
exit "$failed"
