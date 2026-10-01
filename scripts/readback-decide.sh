#!/usr/bin/env bash
# readback-decide.sh — the pure post-or-skip / advance-or-not decision for
# .github/workflows/artifacts-readback.yml's compare step (chtypes#270),
# pulled out of that step's inline shell so the decision can be driven
# locally with stubbed inputs instead of only by a scheduled run against the
# live served index.
#
# THE PROBLEM THIS CLOSES. Every exit-1 compare (a row key removed, a
# SHA256SUMS filename removed — including, but no longer limited to,
# sdk-fetch-fixtures.tar.gz going missing — or, since the channel went
# append-only, a tarball's bytes changing too; see scripts/index-diff.sh's
# own header for the exact append-only rule, chtypes#283) posted to the
# public issue and failed the run — correct, because a red scheduled run is
# the alarm and the baseline must never advance on a real loss. This script
# itself never re-derives WHICH of those fired — it only reads index-diff.sh's
# own `generated_at:` line, so a tighter or looser failure rule there changes
# nothing here (pinned below). But nothing told
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
#
# --- the seed-time decision (telling a genuine first run from a lost
# baseline) ---
#
#   scripts/readback-decide.sh --seed <read-ok 0|1> <found 0|1> [<prior-ts>]
#
# This is the SEPARATE pure decision for the workflow's `seed` step, which
# only ever runs on a cache miss. An `actions: write` dispatch token (the
# artifact producer's, scoped to `actions: write` alone — filed
# producer-side) can DELETE the Actions-cache baseline, and a deleted
# baseline looks identical, from `cache-matched-key` alone, to this being
# the very first run ever. The workflow tells them apart by asking whether
# #73 already carries a comment this workflow posted (every comment it
# posts carries a first-line `<!-- artifacts-readback: generated_at <v> -->`
# marker) — see the workflow's own header for the full reasoning.
#
# <read-ok>    1 if the workflow's one paginated read of #73's prior
#              comments succeeded and was parseable, 0 if it did not (a
#              non-2xx response, or output `gh api --jq` could not parse).
#              0 ALWAYS wins over <found>/<prior-ts>, whatever they say —
#              an outage must never be read as "no prior comment", because
#              that would silently take the quiet first-run path.
# <found>      1 if, and only if, read-ok=1 AND at least one prior marked
#              comment was found. Ignored when read-ok=0.
# <prior-ts>   the newest prior marked comment's `generated_at` value.
#              Required when found=1 (a usage error otherwise); ignored
#              otherwise.
#
# Prints, in the same $GITHUB_OUTPUT shape as decide() above:
#
#   outcome=seed-quiet | seed-loud-fail | fail-closed
#   prior_generated_at=<value, or empty>
#
#   seed-quiet      read-ok=1, found=0. A genuine first run: the caller
#                   snapshots a fresh baseline, posts nothing, exits 0.
#   seed-loud-fail  read-ok=1, found=1. The cache is gone despite this
#                   workflow having run before — the baseline was LOST, not
#                   absent. The caller still snapshots a fresh baseline (so
#                   the NEXT run has something to compare against), but
#                   posts the loss to #73 — naming prior_generated_at, since
#                   removals between then and now cannot be proven absent by
#                   this run — and fails the run (exit 1) on purpose.
#   fail-closed     read-ok=0. The read itself failed, so nothing here is
#                   known: not whether a prior comment exists, and not what
#                   it would have named. The caller seeds nothing and posts
#                   nothing (there is no generated_at to name honestly) and
#                   just fails — the same "an outage must never read as a
#                   quiet index" reasoning decide()'s own fail-no-post
#                   already uses, one layer up.
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

# decide_seed <read-ok 0|1> <found 0|1> [<prior-ts>] — the seed-step decision
# (see the header above). <prior-ts> is required when found=1 (a usage
# error otherwise, exactly like decide()'s own missing-out-file check); it
# is ignored otherwise, including when read-ok=0.
decide_seed() {
  local read_ok="$1" found="$2" prior_ts="${3:-}"

  if [ "$read_ok" != 1 ]; then
    printf 'outcome=fail-closed\nprior_generated_at=\n'
    return 0
  fi

  if [ "$found" = 1 ]; then
    [ -n "$prior_ts" ] || die "decide_seed: found=1 but no <prior-ts> given"
    printf 'outcome=seed-loud-fail\nprior_generated_at=%s\n' "$prior_ts"
  else
    printf 'outcome=seed-quiet\nprior_generated_at=\n'
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

  # 8. THE PIN for chtypes#283: this script's decision must not depend on
  #    WHICH of index-diff.sh's failure classes fired — only on the
  #    `generated_at:` line and the exit code. Fixture text shaped like the
  #    REAL output of the tighter, append-only verdict (a REMOVED row key —
  #    the wording #283 introduced, never the old "TRUE-DROPPED"/"RESHAPED,
  #    NOT a failure" text this script was never supposed to key off of
  #    either), not the generic placeholder write_out() above uses elsewhere
  #    in this file. If a future change to index-diff.sh's wording ever made
  #    this script parse the verdict line itself instead of the
  #    `generated_at:` line, this is what would catch it.
  out8="$tmp/out8.txt"
  cat > "$out8" <<'EOF'
index-diff: /tmp/baseline.json (before) vs /tmp/current-index.json (after)
  generated_at: before=2026-09-30T00:00:00Z  after=2026-09-30T01:00:00Z
  rows: before=4  after=3
  rows added (new key): 0
  rows REMOVED (row key present before, absent after — HARD FAILURE: the channel is append-only, so this can never happen legitimately): 1
    REMOVED clickhouse_minor=25.3 os=darwin arch=arm64: build 1000 no longer present (remaining: none — triplet entirely gone)
VERDICT: FAIL — 1 row key(s) REMOVED (present before, absent after):
  REMOVED clickhouse_minor=25.3 os=darwin arch=arm64 build=1000
EOF
  got8="$(decide 1 "$out8" "")"
  outcome8="$(printf '%s\n' "$got8" | sed -n 's/^outcome=//p')"
  before8="$(printf '%s\n' "$got8" | sed -n 's/^before=//p')"
  after8="$(printf '%s\n' "$got8" | sed -n 's/^after=//p')"
  if [ "$outcome8" = post-fail ] && [ "$before8" = "2026-09-30T00:00:00Z" ] && [ "$after8" = "2026-09-30T01:00:00Z" ]; then
    echo "readback-decide: selftest ok — a real append-only REMOVED-row verdict (#283's tighter failure, never the old TRUE-DROPPED/RESHAPED wording) still decides post-fail from the generated_at line alone, whatever index-diff.sh's verdict prose says"
  else
    echo "SELFTEST FAILED (real append-only verdict shape): wanted outcome=post-fail before=2026-09-30T00:00:00Z after=2026-09-30T01:00:00Z, got:" >&2
    printf '%s\n' "$got8" >&2
    fail=1
  fi

  # --- decide_seed(): telling a genuine first run from a lost baseline ---

  # check_seed <name> <expected-outcome> <read-ok> <found> <prior-ts-or-''>
  #            <expected-prior-generated-at-or-''>
  check_seed() {
    local name="$1" want="$2" read_ok="$3" found="$4" prior_ts="$5" want_prior="$6"
    local got got_outcome got_prior
    got="$(decide_seed "$read_ok" "$found" "$prior_ts")"
    got_outcome="$(printf '%s\n' "$got" | sed -n 's/^outcome=//p')"
    got_prior="$(printf '%s\n' "$got" | sed -n 's/^prior_generated_at=//p')"
    if [ "$got_outcome" != "$want" ] || [ "$got_prior" != "$want_prior" ]; then
      echo "SELFTEST FAILED ($name): wanted outcome=$want prior_generated_at=$want_prior, got:" >&2
      printf '%s\n' "$got" >&2
      fail=1
      return
    fi
    echo "readback-decide: selftest ok — $name"
  }

  # 9. read ok, no prior marked comment on #73: a genuine first run, seeds
  #    quietly.
  check_seed "seed: read ok, no prior comment -> seed-quiet" seed-quiet 1 0 "" ""

  # 10. read ok, a prior marked comment exists: the cache is gone despite
  #     this workflow having run before -- seed loud and fail, naming the
  #     prior comment's generated_at.
  check_seed "seed: read ok, prior comment found -> seed-loud-fail, names it" \
    seed-loud-fail 1 1 "2026-09-29T23:34:28Z" "2026-09-29T23:34:28Z"

  # 11 (THE read-failure case). The #73 read itself failed: this must be
  #     fail-closed, never seed-quiet — an outage must never be read as "no
  #     prior comment".
  check_seed "seed: read failed, no comment on record -> fail-closed, not seed-quiet" \
    fail-closed 0 0 "" ""

  # 12. read-ok=0 must win even when `found`/`prior-ts` say a comment WAS
  #     found — read-ok is checked first and ignores the rest, so a caller
  #     bug that sets found=1 alongside a failed read still fails closed
  #     rather than posting a claim built on an unread comment.
  check_seed "seed: read failed, found=1 anyway -> still fail-closed (read-ok wins)" \
    fail-closed 0 1 "2026-09-29T23:34:28Z" ""

  # 13. found=1 with no prior-ts is a usage error (a caller bug), not a
  #     decision — symmetrical with decide()'s own missing-out-file check
  #     (case 7, above). Run in a subshell: die() calls exit.
  if (decide_seed 1 1 "") >/dev/null 2>&1; then
    echo "SELFTEST FAILED (seed: found=1 with no prior-ts): decide_seed() should have failed" >&2
    fail=1
  else
    echo "readback-decide: selftest ok — seed: found=1 with no prior-ts is a usage error, not a decision"
  fi

  if [ "$fail" -ne 0 ]; then
    rm -rf "$tmp"
    return 1
  fi
  echo "readback-decide: selftest ok — a first drop posts, an identical repeat skips but still fails, a new generated_at posts again, a fetch failure posts nothing, a bad exit code posts nothing with its own reason, exit 0's quiet/post-advance split is untouched by the last-drop file, a real append-only (#283) REMOVED-row verdict still decides post-fail, and decide_seed tells a genuine first run from a lost baseline from a failed #73 read, with read-ok taking precedence over found/prior-ts"
  rm -rf "$tmp"
  return 0
}

main() {
  if [ "${1:-}" = "--selftest" ]; then
    selftest
    return $?
  fi
  if [ "${1:-}" = "--seed" ]; then
    shift
    [ $# -ge 2 ] || die "usage: readback-decide.sh --seed <read-ok 0|1> <found 0|1> [<prior-ts>]"
    decide_seed "$1" "$2" "${3:-}"
    return $?
  fi
  [ $# -ge 2 ] || die "usage: readback-decide.sh <ec> <out-file> [<last-drop-file>]  (or --seed ... / --selftest)"
  decide "$1" "$2" "${3:-}"
}

main "$@"
