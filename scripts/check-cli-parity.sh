#!/usr/bin/env bash
# The four chtypes command lines must behave alike. docs/guides/fetch-v1.md
# section 6 and section 8 define the one CLI surface; this runs a fixed set
# of cases against a binding's own CLI and compares a normalized transcript
# (exit status, whether stderr is empty, and the stdout shape) to ONE shared
# expectation file, scripts/cli-parity/expected.txt, which every binding is
# held to. Each binding's CI job runs its own; there is no build of all four
# in one job.
#
# usage:
#   scripts/check-cli-parity.sh go|python|ts|rust   run one binding's CLI and compare
#   scripts/check-cli-parity.sh all                 all four (a local run; needs every toolchain)
#   scripts/check-cli-parity.sh --print <binding>   print the transcript instead of comparing
#   scripts/check-cli-parity.sh --selftest          no toolchain: a fake CLI passes, and each planted mismatch fails
#
# Normalized, and only this: the version string (chtypes <version> becomes
# chtypes <VERSION>), absolute paths (the scratch directory becomes <DIR>), and the help
# text (its wording differs by toolchain; the shape is a usage text on stdout,
# naming the four commands at the top level). Everything else must match exactly.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
expected="$root/scripts/cli-parity/expected.txt"

# Each case: <name> <args...>. Directories are named by the placeholders
# @EMPTY@ (an existing empty cache) and @CACHE@ (the directory `where` prints).
cases() {
  cat <<'CASES'
help --help
help-short -h
help-subcommand fetch --help
help-after-arguments list --offline -h
version --version
list-offline-empty list --offline --cache @EMPTY@
verify-empty verify --cache @EMPTY@
where where --cache @CACHE@
usage-no-arguments
usage-unknown-command frobnicate
usage-short-version -V
usage-base-flag fetch --base x 26.8
usage-list-platform list --platform x
usage-verify-platform verify --platform linux-arm64
usage-where-platform where --platform linux-arm64
usage-fetch-needs-a-version fetch
usage-update-needs-lock fetch --update
usage-update-frozen fetch --update --lock f --frozen
usage-update-offline fetch --update --lock f --offline
CASES
}

# transcript <scratch>: runs every case through "${CLI[@]}" and prints one line each.
transcript() {
  local scratch="$1" name rc out err shape line
  mkdir -p "$scratch/empty" "$scratch/cache"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    # shellcheck disable=SC2086 # the case line is split into words on purpose
    set -- $line
    name="$1"
    shift
    args=()
    for a in "$@"; do
      case "$a" in
        @EMPTY@) args+=("$scratch/empty") ;;
        @CACHE@) args+=("$scratch/cache") ;;
        *) args+=("$a") ;;
      esac
    done
    rc=0
    env -u CHTYPES_CACHE -u CHTYPES_TARGET -u CHTYPES_ARTIFACTS_URL \
      "${CLI[@]}" ${args[@]+"${args[@]}"} >"$scratch/out" 2>"$scratch/err" </dev/null || rc=$?
    # the sentinel keeps a trailing newline, which "exactly plus a newline" is about
    out="$(cat "$scratch/out"; printf x)"
    out="${out%x}"
    if [ -s "$scratch/err" ]; then err=nonempty; else err=empty; fi
    case "$name" in
      help-subcommand | help-after-arguments)
        # argparse shows the subcommand's own usage here, the others the whole text
        shape=WRONG
        if [ -n "$out" ] && printf '%s' "$out" | command grep -q 'chtypes'; then shape=usage; fi
        ;;
      help*)
        shape=WRONG
        # shellcheck disable=SC2015 # a and-or chain, deliberate: any miss leaves WRONG
        if [ -n "$out" ] && printf '%s' "$out" | command grep -q 'fetch' \
          && printf '%s' "$out" | command grep -q 'verify' \
          && printf '%s' "$out" | command grep -q 'list' \
          && printf '%s' "$out" | command grep -q 'where'; then shape=usage; fi
        ;;
      version)
        shape="$(printf '%s' "$out" | sed -E 's/^chtypes [^ ]+$/chtypes <VERSION>/' | tr '\n' '|')"
        ;;
      *)
        shape="$(printf '%s' "$out" | sed -e "s|$scratch/empty|<DIR>|g" -e "s|$scratch/cache|<DIR>|g" | tr '\n' '|')"
        ;;
    esac
    printf '%s | exit=%s | stderr=%s | stdout=%s\n' "$name" "$rc" "$err" "$shape"
  done < <(cases)
}

# run_binding <binding> <scratch>: the transcript of one real CLI.
run_binding() {
  local binding="$1" scratch="$2"
  case "$binding" in
    go)
      (cd "$root/go" && go build -o "$scratch/chtypes-go" ./cmd/chtypes)
      CLI=("$scratch/chtypes-go")
      ;;
    python) CLI=(uv run --quiet --locked --project "$root/python" python -m chtypes) ;;
    ts)
      [ -f "$root/ts/dist/cli.js" ] || { echo "check-cli-parity: ts/dist/cli.js is missing; run 'pnpm build' in ts first" >&2; return 2; }
      CLI=(node "$root/ts/dist/cli.js")
      ;;
    rust) CLI=(cargo run --locked --quiet --manifest-path "$root/rust/Cargo.toml" --bin chtypes --) ;;
    fake) CLI=(bash "$scratch/fake-cli.sh") ;;
    *) echo "check-cli-parity: unknown binding '$binding'" >&2; return 2 ;;
  esac
  transcript "$scratch/work"
}

compare() { # compare <binding> <scratch>
  local binding="$1" scratch="$2" got="$2/transcript-$1.txt"
  run_binding "$binding" "$scratch" >"$got"
  if diff -u "$expected" "$got" >"$scratch/diff-$binding.txt"; then
    echo "check-cli-parity: $binding matches scripts/cli-parity/expected.txt ($(wc -l <"$got" | tr -d ' ') cases)"
    return 0
  fi
  echo "check-cli-parity: $binding DIFFERS from scripts/cli-parity/expected.txt:" >&2
  cat "$scratch/diff-$binding.txt" >&2
  return 1
}

# A fake CLI that follows the contract; FAKE_BREAK plants one mismatch.
write_fake() {
  cat >"$1" <<'FAKE'
brk="${FAKE_BREAK:-}"
usage="usage: chtypes fetch|verify|list|where"
bad() { echo "chtypes: usage" >&2; exit 2; }
case "${1:-}" in
  "") bad ;;
esac
for a in "$@"; do
  if [ "$a" = "-h" ] || [ "$a" = "--help" ]; then
    if [ "$brk" = help-stderr ]; then echo "$usage" >&2; exit 2; fi
    echo "$usage"; exit 0
  fi
done
case "$1" in
  --version)
    if [ "$brk" = version-bare ]; then echo "1.2.3"; else echo "chtypes 1.2.3"; fi
    exit 0 ;;
  fetch)
    shift
    update=0; lock=0; frozen=0; offline=0; n=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --update) update=1 ;; --lock) lock=1; shift ;; --frozen) frozen=1 ;; --offline) offline=1 ;;
        --base) shift; bad ;;
        -*) bad ;;
        *) n=$((n + 1)) ;;
      esac
      shift
    done
    if [ "$update" = 1 ] && { [ "$lock" = 0 ] || [ "$frozen" = 1 ] || [ "$offline" = 1 ]; }; then bad; fi
    [ "$update" = 1 ] || [ "$n" -gt 0 ] || bad
    exit 0 ;;
  verify | list | where)
    cmd="$1"; shift
    cache=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --cache) cache="$2"; shift ;;
        --offline) [ "$cmd" = list ] || bad ;;
        --platform) [ "$brk" = platform-accepted ] || bad; shift ;;
        *) bad ;;
      esac
      shift
    done
    if [ "$cmd" = where ]; then echo "$cache"; fi
    # A verify that verified nothing says so on stderr (public issue #486).
    if [ "$cmd" = verify ]; then echo "chtypes: verified 0 builds under $cache" >&2; fi
    if [ "$cmd" = list ] && [ "$brk" = list-header ]; then echo "installed ($cache):"; fi
    exit 0 ;;
esac
bad
FAKE
}

selftest() {
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' EXIT
  write_fake "$scratch/fake-cli.sh"
  # Positive control: the well-behaved fake must match, or every failure below
  # would prove nothing.
  FAKE_BREAK="" compare fake "$scratch" >/dev/null || { echo "check-cli-parity --selftest: the conforming fake CLI does not match the expectation" >&2; cat "$scratch/diff-fake.txt" >&2; exit 1; }
  local brk
  for brk in help-stderr version-bare list-header platform-accepted; do
    if FAKE_BREAK="$brk" compare fake "$scratch" >/dev/null 2>&1; then
      echo "check-cli-parity --selftest: the planted mismatch '$brk' was NOT caught" >&2
      exit 1
    fi
    echo "check-cli-parity --selftest: planted '$brk' is caught"
  done
  echo "check-cli-parity --selftest: ok"
}

main() {
  case "${1:-}" in
    --selftest)
      selftest
      ;;
    --print)
      scratch="$(mktemp -d)"
      trap 'rm -rf "$scratch"' EXIT
      run_binding "${2:?binding}" "$scratch"
      ;;
    all)
      local rc=0 b
      scratch="$(mktemp -d)"
      trap 'rm -rf "$scratch"' EXIT
      for b in go python ts rust; do compare "$b" "$scratch" || rc=1; done
      exit "$rc"
      ;;
    go | python | ts | rust)
      scratch="$(mktemp -d)"
      trap 'rm -rf "$scratch"' EXIT
      compare "$1" "$scratch"
      ;;
    *)
      sed -n '2,18p' "${BASH_SOURCE[0]}" >&2
      exit 2
      ;;
  esac
}
main "$@"
