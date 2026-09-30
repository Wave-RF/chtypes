#!/usr/bin/env bash
# readback-decide.sh — the pure post-or-skip / advance-or-not decision for
# .github/workflows/artifacts-readback.yml's compare step (chtypes#270),
# pulled out of that step's inline shell so the decision can be driven
# locally with stubbed inputs instead of only by a scheduled run against the
# live served index.
#
# THE PROBLEM THIS CLOSES. Every exit-1 compare (a real drop, or the
# sdk-fetch-fixtures.tar.gz manifest row gone missing) posted to the public
# issue and failed the run — correct, because a red scheduled run is the
# alarm and the baseline must never advance on a real loss. But nothing told
# a BRAND NEW drop apart from the same drop still sitting there three runs
# later: a persistent outage re-posted an identical comment on every single
# scheduled run (measured: 2026-09-29T23:56Z and 2026-09-30T05:25Z both
# posted the same verdict for the same served generated_at). This script adds
# exactly one more piece of state — the `generated_at` value the LAST POSTED
# drop comment named, read from its own Actions-cache entry, never mixed into
# the compare baseline — and posts on exit 1 only when the served `after`
# differs from it.
#
#   scripts/readback-decide.sh <ec> <out-file> [<last-drop-file>]
#   scripts/readback-decide.sh --selftest
#
# <ec>              the exit code scripts/index-diff.sh --compare returned.
# <out-file>        a file holding that invocation's captured stdout+stderr.
#                   This script only ever READS it — it never runs
#                   index-diff.sh itself, and never fetches anything.
# <last-drop-file>  optional; a file holding the `generated_at` value the
#                   last POSTED drop comment named. Missing or empty means
#                   "no prior drop is on record" (first run, or the entry
#                   aged out of the Actions cache the same way the baseline's
#                   own does) and is treated exactly like a genuinely new
#                   drop — it posts.
#
# Prints, in the shape a GitHub Actions step appends to $GITHUB_OUTPUT:
#
#   outcome=quiet | post-advance | post-fail | skip-fail | fail-no-post
#   before=<value, or empty>
#   after=<value, or empty>
#   reason=<only set for fail-no-post: bad-ec | no-line | unparseable>
#
# outcome means, to the CALLER (this script never posts a comment, writes a
# cache file, or fails a run itself — it only decides):
#
#   quiet         ec==0, before==after. Post nothing, save nothing, exit 0.
#   post-advance  ec==0, before!=after. Post; only once that succeeds, advance
#                 the baseline to the compared --keep-current file. Exit 0.
#   post-fail     ec==1, and <last-drop-file> is missing/empty/holds a value
#                 other than `after` — a brand new drop, or the first one on
#                 record. Post; only once that succeeds, save `after` to
#                 <last-drop-file>. Then FAIL the run regardless (the compare
#                 baseline is never touched on this path).
#   skip-fail     ec==1, and <last-drop-file> already holds exactly `after`
#                 — the same drop already posted. Post nothing. Still FAIL
#                 the run: the drop has not gone away, and the failed run is
#                 itself the alarm. Do not touch <last-drop-file> (it is
#                 already correct) or the baseline.
#   fail-no-post  ec is neither 0 nor 1 (reason=bad-ec), or no parseable
#                 `  generated_at: before=... after=...` line was found in
#                 <out-file> at all (reason=no-line), or that line was found
#                 but before=/after= could not be extracted from it
#                 (reason=unparseable). This is an outage or a bad argument,
#                 not a finding — scripts/index-diff.sh's own die() fires
#                 before that line is ever printed, and a hard-failure
#                 verdict always prints it first (see the workflow's own
#                 header for why exit 1 cannot be told apart from its exit
#                 code alone). Post nothing; fail the run.
#
# The before=/after= extraction uses the exact same pattern the workflow used
# inline before this script existed, so this extraction changes nothing about
# what counts as a `generated_at:` line.
set -euo pipefail

die() { echo "readback-decide: $*" >&2; exit 1; }

# decide <ec> <out-file> [<last-drop-file>] — prints outcome=/before=/after=/
# reason= to stdout (see the header above). Exits non-zero only on a usage
# error (a missing <out-file>); a normal decision, however dire the outcome,
# always returns 0 — the caller reads `outcome`, never this script's own
# exit code.
decide() {
  local ec="$1" out_file="$2" last_drop_file="${3:-}"
  [ -f "$out_file" ] || die "no such out-file: $out_file"

  if [ "$ec" != 0 ] && [ "$ec" != 1 ]; then
    printf 'outcome=fail-no-post\nbefore=\nafter=\nreason=bad-ec\n'
    return 0
  fi

  local line before after
  line="$(grep -m1 '^  generated_at:' "$out_file" || true)"
  if [ -z "$line" ]; then
    printf 'outcome=fail-no-post\nbefore=\nafter=\nreason=no-line\n'
    return 0
  fi
  before="$(printf '%s\n' "$line" | sed -n 's/^  generated_at: before=\(.*\)  after=.*/\1/p')"
  after="$(printf '%s\n' "$line" | sed -n 's/^  generated_at: before=.*  after=\(.*\)/\1/p')"
  if [ -z "$before" ] || [ -z "$after" ]; then
    printf 'outcome=fail-no-post\nbefore=\nafter=\nreason=unparseable\n'
    return 0
  fi

  if [ "$ec" = 0 ]; then
    if [ "$before" = "$after" ]; then
      printf 'outcome=quiet\nbefore=%s\nafter=%s\nreason=\n' "$before" "$after"
    else
      printf 'outcome=post-advance\nbefore=%s\nafter=%s\nreason=\n' "$before" "$after"
    fi
    return 0
  fi

  # ec == 1 — the one branch this script adds state to: was THIS after
  # already the subject of the last posted drop comment?
  local last_drop=""
  if [ -n "$last_drop_file" ] && [ -f "$last_drop_file" ]; then
    last_drop="$(cat "$last_drop_file")"
  fi
  if [ -n "$last_drop" ] && [ "$last_drop" = "$after" ]; then
    printf 'outcome=skip-fail\nbefore=%s\nafter=%s\nreason=\n' "$before" "$after"
  else
    printf 'outcome=post-fail\nbefore=%s\nafter=%s\nreason=\n' "$before" "$after"
  fi
}

# --------------------------------------------------------------- selftest

selftest() {
  local tmp fail=0
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-readback-decide-selftest.XXXXXX")"
  # No RETURN trap here on purpose: a trap set with `trap ... RETURN` fires on
  # every LATER function return in this shell too, not just this one's — it
  # would fire again when main() itself returns, by which point $tmp is out
  # of scope and `set -u` turns that into an unbound-variable error. Clean up
  # by hand at each explicit return below instead.

  # write_out <path> <before> <after-or-''>  — a fixture exactly shaped like
  # scripts/index-diff.sh's own printed line; after='' omits the line
  # entirely, standing in for a fetch failure (die() fired first).
  write_out() {
    local path="$1" before="$2" after="$3"
    if [ -z "$after" ]; then
      printf 'some unrelated output\nno comparison line here\n' > "$path"
    else
      printf 'index-diff: comparing...\n  generated_at: before=%s  after=%s\nVERDICT: ...\n' "$before" "$after" > "$path"
    fi
  }

  # check <name> <expected-outcome> <ec> <before> <after> <last-drop-or-''>
  check() {
    local name="$1" want="$2" ec="$3" before="$4" after="$5" last_drop="$6"
    local out_file lastdrop_file got_outcome
    out_file="$tmp/out.txt"
    write_out "$out_file" "$before" "$after"
    lastdrop_file=""
    if [ -n "$last_drop" ]; then
      lastdrop_file="$tmp/lastdrop.txt"
      printf '%s' "$last_drop" > "$lastdrop_file"
    fi
    local got
    got="$(decide "$ec" "$out_file" "$lastdrop_file")"
    got_outcome="$(printf '%s\n' "$got" | sed -n 's/^outcome=//p')"
    if [ "$got_outcome" != "$want" ]; then
      echo "SELFTEST FAILED ($name): wanted outcome=$want, got:" >&2
      printf '%s\n' "$got" >&2
      fail=1
      return
    fi
    echo "readback-decide: selftest ok — $name"
  }

  # 1. A first drop — no last-drop file at all — posts (and, per the caller's
  #    own contract, still fails: this script's outcome is what makes that
  #    happen).
  check "a first drop (no prior last-drop entry) posts" post-fail 1 "2026-09-29T23:00:00Z" "2026-09-29T23:34:28Z" ""

  # 2. An identical repeat — the last-drop file already names this exact
  #    after — does not post, but the outcome still carries the caller's
  #    instruction to fail.
  check "an identical repeat does not re-post" skip-fail 1 "2026-09-29T23:00:00Z" "2026-09-29T23:34:28Z" "2026-09-29T23:34:28Z"

  # 3. A drop at a NEW generated_at — the last-drop file names an older
  #    value — posts again.
  check "a drop at a new generated_at posts again" post-fail 1 "2026-09-29T23:34:28Z" "2026-09-30T06:00:00Z" "2026-09-29T23:34:28Z"

  # 4. A fetch failure — no generated_at line at all — posts nothing
  #    (fail-no-post), whatever the last-drop file says.
  check "a fetch failure (no generated_at line) posts nothing" fail-no-post 1 "" "" "2026-09-29T23:34:28Z"

  # 5. A bad exit code (neither 0 nor 1) also posts nothing, distinctly
  #    reasoned from the no-line case above.
  out5="$tmp/out5.txt"
  write_out "$out5" "2026-09-29T23:00:00Z" "2026-09-29T23:34:28Z"
  got5="$(decide 2 "$out5" "")"
  reason5="$(printf '%s\n' "$got5" | sed -n 's/^reason=//p')"
  if [ "$(printf '%s\n' "$got5" | sed -n 's/^outcome=//p')" = fail-no-post ] && [ "$reason5" = bad-ec ]; then
    echo "readback-decide: selftest ok — a bad exit code posts nothing, reason=bad-ec"
  else
    echo "SELFTEST FAILED (bad exit code): got:" >&2; printf '%s\n' "$got5" >&2; fail=1
  fi

  # 6. exit 0 behavior is untouched by any of the above: unchanged stays
  #    quiet, and a genuine move still posts and (per the caller) advances —
  #    the last-drop file must play no part in either.
  check "exit 0, unchanged: quiet, regardless of any stale last-drop entry" quiet 0 "2026-09-29T23:00:00Z" "2026-09-29T23:00:00Z" "2026-09-28T00:00:00Z"
  check "exit 0, moved: post-advance, regardless of any stale last-drop entry" post-advance 0 "2026-09-29T23:00:00Z" "2026-09-30T06:00:00Z" "2026-09-28T00:00:00Z"

  # 7. A missing <out-file> is a usage error, not a decision. Run in a
  #    subshell: die() calls exit, and decide() is a function in THIS shell
  #    — an unwrapped call would take the whole selftest down with it.
  if (decide 1 "$tmp/does-not-exist.txt" "") >/dev/null 2>&1; then
    echo "SELFTEST FAILED (missing out-file): decide() should have failed" >&2
    fail=1
  else
    echo "readback-decide: selftest ok — a missing out-file is a usage error, not a decision"
  fi

  if [ "$fail" -ne 0 ]; then
    rm -rf "$tmp"
    return 1
  fi
  echo "readback-decide: selftest ok — a first drop posts, an identical repeat skips but still fails, a new generated_at posts again, a fetch failure posts nothing, a bad exit code posts nothing with its own reason, and exit 0's quiet/post-advance split is untouched by the last-drop file"
  rm -rf "$tmp"
  return 0
}

main() {
  if [ "${1:-}" = "--selftest" ]; then
    selftest
    return $?
  fi
  [ $# -ge 2 ] || die "usage: readback-decide.sh <ec> <out-file> [<last-drop-file>]  (or --selftest)"
  decide "$1" "$2" "${3:-}"
}

main "$@"
