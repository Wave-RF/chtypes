#!/usr/bin/env bash
# candidate-poll-decide.sh — the pure decision logic behind
# .github/workflows/candidate-poller.yml: has the artifact producer's
# abi<N>-candidate index moved since this was last checked, and is one open
# pull request eligible to have ci.yml dispatched on its head branch. Pulled
# out of that workflow's inline shell, same reasoning as
# scripts/readback-decide.sh: this is driven locally against fixtures, with
# no network and no GitHub API, instead of only by a scheduled run against
# the live candidate channel — there is no candidate channel published today
# (revision 7 will be its first real use), so this is the only way to prove
# the logic before it ever sees a real one.
#
#   scripts/candidate-poll-decide.sh index <http-code> <generated-at> <sha256> <prev-state-file-or-''>
#   scripts/candidate-poll-decide.sh pr <state> <same-repo:0|1> <head-revision-or-''> <wanted-revision>
#   scripts/candidate-poll-decide.sh --selftest
#
# ------------------------------------------------------------------ index
#
# <http-code>     the candidate index fetch's HTTP status, as the workflow's
#                 own curl step already classified it. This script only ever
#                 understands 200 and 404 — anything else (a timeout, a 5xx,
#                 a DNS failure) is a real reachability failure that the
#                 CALLER must have already refused loudly, exactly like
#                 scripts/published-lines.sh's own chtypes#262 fix: a 404 is
#                 the host reachably saying "no such release" (nothing to
#                 do), never CHTYPES_SOURCE_UNREACHABLE. Passing this script
#                 anything else is a caller bug, and it dies loudly rather
#                 than silently treating an outage as "unchanged".
# <generated-at>  the candidate index's own generated_at field (empty on 404)
# <sha256>        sha256 of the fetched index.json bytes (empty on 404)
# <prev-state-file-or-''>  a file holding the PREVIOUS "<generated-at>
#                 <sha256>" key, or '' / a path that does not exist — which
#                 means no prior state at all (first run, or the Actions
#                 cache entry aged out past its 7-day no-access eviction,
#                 same model as artifacts-readback.yml's own baseline).
#
# Prints, in the shape a GitHub Actions step appends to $GITHUB_OUTPUT:
#
#   result=absent|seeded|moved|unchanged
#   key=<the new state key, or empty for absent>
#
#   absent      http-code was 404. Nothing to do, never an error — the
#               caller must not treat this as a failure and must leave any
#               stored state untouched.
#   seeded      http-code was 200, and there was no prior state at all. This
#               is not "moved" in the sense that should ever dispatch
#               anything — there is nothing to compare against yet — so the
#               caller records this key as the new baseline and dispatches
#               nothing, the same "seed quietly, act on nothing" contract
#               artifacts-readback.yml's own `seed` step follows.
#   unchanged   http-code was 200, and the key matches the prior state.
#   moved       http-code was 200, and the key differs from the prior state.
#               The caller records the new key and looks for a pull request
#               to dispatch against.
#
# ------------------------------------------------------------------ pr
#
# <state>              the pull request's own `state` field (GitHub always
#                       lowercases it: "open" or "closed").
# <same-repo:0|1>       1 iff the pull request's head repository is THIS
#                       repository — never a fork. The pwn-request rule: this
#                       workflow's GITHUB_TOKEN dispatches a workflow_dispatch
#                       run on ITS OWN ci.yml, on a branch of this repository;
#                       a fork's branch is never that.
# <head-revision-or-''>  the CHS_ABI_REVISION read from that pull request's
#                       HEAD copy of include/chtypes.h, or '' when it could
#                       not be read (no such file, more than one #define, or
#                       the contents API call itself failed).
# <wanted-revision>     this run's own revision, N — scripts/abi-channel.sh's
#                       header_revision() applied to THIS repository's main.
#
# Prints:
#
#   eligible=1|0
#   reason=<why, only set when eligible=0>
#
# Eligible iff state is exactly "open", same-repo is 1, and head-revision
# equals wanted-revision exactly (a non-empty string compare — an unreadable
# header, passed as '', is never eligible, even if wanted-revision were
# somehow empty too).
set -euo pipefail

die() { echo "candidate-poll-decide: $*" >&2; exit 1; }

decide_index() {
  local code="$1" generated_at="$2" sha256="$3" prev_file="$4"
  if [ "$code" = 404 ]; then
    printf 'result=absent\nkey=\n'
    return 0
  fi
  [ "$code" = 200 ] || die "index only understands http-code 200 or 404 — a real reachability failure (got $code) must be refused by the caller before this is ever invoked, never turned into a quiet 'nothing to do'"
  local key="$generated_at $sha256"
  local prev=""
  if [ -n "$prev_file" ] && [ -f "$prev_file" ]; then
    prev="$(cat "$prev_file")"
  fi
  if [ -z "$prev" ]; then
    printf 'result=seeded\nkey=%s\n' "$key"
  elif [ "$prev" = "$key" ]; then
    printf 'result=unchanged\nkey=%s\n' "$key"
  else
    printf 'result=moved\nkey=%s\n' "$key"
  fi
}

decide_pr() {
  local state="$1" same_repo="$2" head_rev="$3" wanted_rev="$4"
  if [ "$state" != "open" ]; then
    printf 'eligible=0\nreason=state is %s, not open\n' "$state"
    return 0
  fi
  if [ "$same_repo" != 1 ]; then
    printf 'eligible=0\nreason=the head is a fork; never dispatched\n'
    return 0
  fi
  if [ -z "$head_rev" ]; then
    printf 'eligible=0\nreason=could not read CHS_ABI_REVISION from the head include/chtypes.h\n'
    return 0
  fi
  if [ "$head_rev" != "$wanted_rev" ]; then
    printf 'eligible=0\nreason=head revision %s does not match the polled revision %s\n' "$head_rev" "$wanted_rev"
    return 0
  fi
  printf 'eligible=1\nreason=\n'
}

# --------------------------------------------------------------- selftest

selftest() {
  # Not `local`: the EXIT trap below runs after this function has returned
  # (the case statement's own `exit $?` triggers it), by which point a
  # `local` variable is already out of scope under `set -u` — the exact trap
  # scripts/readback-decide.sh's own selftest() documents.
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-candidate-poll-decide-selftest.XXXXXX")"
  local fail=0
  trap 'rm -rf "$tmp"' EXIT

  check_index() {  # check_index <name> <want-result> <code> <gen-at> <sha> <prev-content-or-''>
    local name="$1" want="$2" code="$3" gen="$4" sha="$5" prev="$6"
    local prev_file="" out got rc=0
    if [ -n "$prev" ]; then
      prev_file="$tmp/prev-$RANDOM.txt"
      printf '%s' "$prev" > "$prev_file"
    fi
    out="$(decide_index "$code" "$gen" "$sha" "$prev_file")" || rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "SELFTEST FAILED ($name): decide_index exited $rc unexpectedly: $out" >&2; fail=1; return
    fi
    got="$(printf '%s\n' "$out" | sed -n 's/^result=//p')"
    if [ "$got" != "$want" ]; then
      echo "SELFTEST FAILED ($name): wanted result=$want, got: $out" >&2; fail=1; return
    fi
    echo "candidate-poll-decide: selftest ok — $name"
  }

  # A 404 is nothing to do, never an error, whatever the prior state was.
  check_index "a 404 on the candidate index is 'absent', never an error" absent 404 "" "" ""
  check_index "a 404 is absent even with a prior baseline on record" absent 404 "" "" "2026-09-30T00:00:00Z deadbeef"

  # No prior state at all: seed quietly, distinct from a real 'moved'.
  check_index "a 200 with no prior state at all is 'seeded'" seeded 200 "2026-09-30T00:00:00Z" "deadbeef" ""

  # Same key as before: unchanged.
  check_index "a 200 whose key matches the prior state is 'unchanged'" unchanged 200 "2026-09-30T00:00:00Z" "deadbeef" "2026-09-30T00:00:00Z deadbeef"

  # A different generated_at, or a different sha256 at the same generated_at
  # (a republish under the same timestamp), both count as moved.
  check_index "a 200 whose generated_at differs from the prior state is 'moved'" moved 200 "2026-09-30T01:00:00Z" "deadbeef" "2026-09-30T00:00:00Z deadbeef"
  check_index "a 200 whose sha256 differs at the same generated_at is 'moved'" moved 200 "2026-09-30T00:00:00Z" "cafef00d" "2026-09-30T00:00:00Z deadbeef"

  # A real reachability failure must never collapse into 'absent' or
  # 'unchanged' — it is the caller's to refuse before this is ever reached,
  # and this script must die loudly if it is invoked anyway (in a subshell:
  # die() calls exit, which would otherwise take the whole selftest down).
  if (decide_index 500 "" "" "") >/dev/null 2>&1; then
    echo "SELFTEST FAILED: decide_index accepted http-code 500 as though it were 404 or 200" >&2; fail=1
  else
    echo "candidate-poll-decide: selftest ok — an http-code other than 200/404 (500) dies rather than being read as 'nothing to do'"
  fi

  check_pr() {  # check_pr <name> <want-eligible> <state> <same-repo> <head-rev> <wanted-rev> [<reason-substring>]
    local name="$1" want="$2" state="$3" same_repo="$4" head_rev="$5" wanted="$6" reason_grep="${7:-}"
    local out got_eligible got_reason
    out="$(decide_pr "$state" "$same_repo" "$head_rev" "$wanted")"
    got_eligible="$(printf '%s\n' "$out" | sed -n 's/^eligible=//p')"
    if [ "$got_eligible" != "$want" ]; then
      echo "SELFTEST FAILED ($name): wanted eligible=$want, got: $out" >&2; fail=1; return
    fi
    if [ -n "$reason_grep" ]; then
      got_reason="$(printf '%s\n' "$out" | sed -n 's/^reason=//p')"
      case "$got_reason" in
        *"$reason_grep"*) ;;
        *) echo "SELFTEST FAILED ($name): reason '$got_reason' did not contain '$reason_grep'" >&2; fail=1; return ;;
      esac
    fi
    echo "candidate-poll-decide: selftest ok — $name"
  }

  # The exact matrix the workflow's own header asks for: moved vs unchanged
  # is index's job (above); here, no matching PR (wrong revision), a fork
  # PR, and a PR at another revision.
  check_pr "an open, same-repo PR at the wanted revision is eligible" 1 open 1 6 6
  check_pr "a fork PR is never eligible, whatever its revision" 0 open 0 6 6 fork
  check_pr "a PR at another revision is not eligible" 0 open 1 5 6 "does not match"
  check_pr "a closed PR is not eligible" 0 closed 1 6 6 "not open"
  check_pr "a PR whose header could not be read (empty head-revision) is not eligible" 0 open 1 "" 6 "could not read"
  check_pr "no matching PR at all (wrong revision AND it happens to be a fork) still reports the first-checked reason" 0 open 0 5 6 fork

  [ "$fail" -eq 0 ] || { echo "candidate-poll-decide: selftest FAILED" >&2; return 1; }
  echo "candidate-poll-decide: selftest ok — index: absent/seeded/unchanged/moved are each distinct and a non-200/404 code dies rather than guessing; pr: open+same-repo+matching-revision is the only eligible shape, and a fork, a closed PR, a revision mismatch and an unreadable header are each refused with their own reason"
  return 0
}

case "${1:-}" in
  --selftest)
    selftest
    exit $?
    ;;
  index)
    shift
    [ $# -eq 4 ] || die "usage: candidate-poll-decide.sh index <http-code> <generated-at> <sha256> <prev-state-file-or-''>"
    decide_index "$@"
    ;;
  pr)
    shift
    [ $# -eq 4 ] || die "usage: candidate-poll-decide.sh pr <state> <same-repo:0|1> <head-revision-or-''> <wanted-revision>"
    decide_pr "$@"
    ;;
  *)
    die "usage: candidate-poll-decide.sh index <http-code> <generated-at> <sha256> <prev-state-file-or-''> | pr <state> <same-repo:0|1> <head-revision-or-''> <wanted-revision> | --selftest"
    ;;
esac
