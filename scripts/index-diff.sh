#!/usr/bin/env bash
# index-diff.sh — an outside, read-only before/after check on the served
# rolling index (index.json): the listing every published SDK resolves
# against by default (scripts/fetch.sh and scripts/support-matrix.sh both
# read it). It exists because the artifact producer's only vehicle for
# regenerating that listing today is a hand operation with no diff and no
# log anyone else can read afterwards, and a real bug has already shipped
# from exactly that gap: a regeneration step that OVERWROTE the live index
# instead of unioning with it, which silently drops rows. This script is the
# independent row-set diff that guards against a repeat.
#
#   scripts/index-diff.sh --snapshot [path]   fetch the served index, save it
#                                              as a baseline (default path:
#                                              ./index-snapshot.json)
#   scripts/index-diff.sh --compare <baseline> fetch the CURRENT index and
#                                              report it against <baseline>
#   scripts/index-diff.sh --selftest          prove the dropped-row check
#                                              actually fires, against fixtures
#                                              this script builds itself
#
# What --compare reports, reading only the two documents themselves — never
# anything hard-coded about what they ought to contain:
#   - rows dropped:  (clickhouse_minor, os, arch, build) keys present in the
#     baseline and absent from the current index. THIS IS A HARD FAILURE.
#   - rows added: keys present now that the baseline never had.
#   - abi_revision coverage, before and after, broken down by build: how many
#     of that build's rows carry an abi_revision.
#   - the top-level "unbuildable" array: presence and contents, before and
#     after.
#   - each document's own generated_at.
#
# A dropped row is never reported as a mere diff line. --compare exits
# non-zero and NAMES every vanished key — not a count — on the very line that
# carries the verdict, in this repository's usual style (scripts/check-suite.sh,
# scripts/check-standalone.sh): a summary a census can read off the exit code
# and the output together, nothing else required.
#
# NOT a substitute for scripts/fetch.sh's verification chain (the ed25519
# signature over SHA256SUMS, the sha256 cross-checks down to the installed
# library). This script never verifies a signature, never checks a hash, and
# never downloads anything but the one index.json document on each side. A
# clean run here means "no row vanished from the listing" — nothing about
# whether the listing is authentic. That is fetch.sh's job alone.
#
# Read-only and credential-free: it fetches index.json (twice for --compare,
# once for --snapshot) and writes nothing back to the channel. No registry,
# no artifacts, no downloads beyond that one JSON document each time, so this
# runs anywhere scripts/fetch.sh's own no-artifact jobs run.
#
# Nothing about today's index is hard-coded here — not a row count, not the
# current line or platform set, not a build number. The baseline file IS the
# expectation; this script compares two documents and knows nothing about
# what they ought to contain. --selftest proves the dropped-row rule fires by
# planting a removed row in a fixture this script builds fresh every run,
# never by asserting today's real numbers.
#
# Environment:
#   CHTYPES_ARTIFACTS_URL  the artifacts host (default https://artifacts.wavehouse.dev)
#                          — the same variable scripts/fetch.sh and
#                          scripts/support-matrix.sh already read, so a
#                          mirror or a local fixture directory (a file://
#                          URL — curl serves those directly, no network
#                          needed) works here exactly as it does there.
#                          The document is always fetched from
#                          "${CHTYPES_ARTIFACTS_URL%/}/artifacts/index.json".
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPTS/$(basename "${BASH_SOURCE[0]}")"
die()  { echo "index-diff: $*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
usage() { sed -n '2,64p' "$SELF"; exit 2; }

command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"

ACTION=""
ARG=""
case "${1:-}" in
  --snapshot)
    ACTION=snapshot
    shift
    # The path is OPTIONAL: take the next argument only if there is one and
    # it is not itself a flag.
    if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then
      ARG="$1"; shift
    fi
    ;;
  --compare)
    ACTION=compare
    shift
    [ $# -ge 1 ] || die "--compare needs a baseline file path"
    ARG="$1"; shift
    ;;
  --selftest)
    ACTION=selftest
    shift
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    die "unknown argument: $1 (expected --snapshot [path], --compare <baseline>, or --selftest)"
    ;;
esac
[ $# -eq 0 ] || die "unexpected extra argument(s): $*"

# fetch_index <dest> — the one JSON document this script ever reads.
fetch_index() {
  local dest="$1" url
  url="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
  url="${url%/}/artifacts/index.json"
  curl -fsSL --retry 3 --max-time 60 "$url" -o "$dest" \
    || die "could not fetch $url (CHTYPES_ARTIFACTS_URL overrides the host; scripts/fetch.sh reads the same variable)"
  python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$dest" \
    || die "$url did not return valid JSON"
}

# run_compare <before-file> <after-file> — prints the full report to stdout;
# its own exit code (0 clean, 1 a row was dropped) is the caller's verdict.
run_compare() {
  python3 - "$1" "$2" <<'PY'
import json, sys

before_path, after_path = sys.argv[1], sys.argv[2]


def load(path, label):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc.get("artifacts"), list):
        sys.exit("index-diff: %s (%s) has no 'artifacts' array — this does not look like index.json" % (path, label))
    if doc.get("schema") != 1:
        sys.exit("index-diff: %s (%s) has schema %r, not 1 — this script cannot read it" % (path, label, doc.get("schema")))
    return doc


before = load(before_path, "before")
after = load(after_path, "after")


def key(a):
    # (clickhouse_minor, os, arch, build) is the row identity this check
    # tracks. A row with no "build" field (the index carries some, from
    # before builds were numbered) keys on build=None consistently on both
    # sides, rather than being silently dropped from consideration.
    return (a.get("clickhouse_minor"), a.get("os"), a.get("arch"), a.get("build"))


def fmt_key(k):
    minor, os_, arch, build = k
    return "clickhouse_minor=%s os=%s arch=%s build=%s" % (minor, os_, arch, "none" if build is None else build)


before_rows = {key(a): a for a in before["artifacts"]}
after_rows = {key(a): a for a in after["artifacts"]}

dropped = sorted(set(before_rows) - set(after_rows), key=fmt_key)
added = sorted(set(after_rows) - set(before_rows), key=fmt_key)


def build_of(a):
    b = a.get("build")
    return "none" if b is None else str(b)


def abi_coverage(doc):
    # {build: (rows carrying abi_revision, total rows)}, so a build's
    # coverage reads as a fraction even when it is 0-of-something.
    cov = {}
    for a in doc["artifacts"]:
        b = build_of(a)
        hit, total = cov.get(b, (0, 0))
        total += 1
        if a.get("abi_revision") is not None:
            hit += 1
        cov[b] = (hit, total)
    return cov


before_cov, after_cov = abi_coverage(before), abi_coverage(after)


def build_sort_key(b):
    return (0, int(b)) if b != "none" and b.lstrip("-").isdigit() else (1, b)


all_builds = sorted(set(before_cov) | set(after_cov), key=build_sort_key)


def describe_unbuildable(doc):
    if "unbuildable" not in doc:
        return "(key absent)"
    arr = doc["unbuildable"]
    if not isinstance(arr, list):
        return "present but not a list: %r" % (arr,)
    if not arr:
        return "present, empty"
    return "%d entr%s: %s" % (len(arr), "y" if len(arr) == 1 else "ies", json.dumps(arr, sort_keys=True))


print("index-diff: %s (before) vs %s (after)" % (before_path, after_path))
print("  generated_at: before=%s  after=%s" % (before.get("generated_at", "(none)"), after.get("generated_at", "(none)")))
print("  rows: before=%d  after=%d" % (len(before_rows), len(after_rows)))
print("  rows added:   %d" % len(added))
for k in added:
    print("    + %s" % fmt_key(k))
print("  rows dropped: %d" % len(dropped))
for k in dropped:
    print("    - %s" % fmt_key(k))
print("  abi_revision coverage by build:")
for b in all_builds:
    bh, bt = before_cov.get(b, (0, 0))
    ah, at = after_cov.get(b, (0, 0))
    print("    build=%s: before %d/%d  after %d/%d" % (b, bh, bt, ah, at))
print("  unbuildable: before=%s" % describe_unbuildable(before))
print("               after=%s" % describe_unbuildable(after))

if dropped:
    print("VERDICT: FAIL — %d row(s) dropped from the index (present in the baseline, absent now):" % len(dropped))
    for k in dropped:
        print("  DROPPED %s" % fmt_key(k))
    sys.exit(1)

print("VERDICT: ok — nothing was dropped (%d row(s) added, %d row(s) unchanged)" % (len(added), len(before_rows)))
sys.exit(0)
PY
}

if [ "$ACTION" = snapshot ]; then
  DEST="${ARG:-index-snapshot.json}"
  say "fetching the served index -> $DEST"
  fetch_index "$DEST"
  python3 - "$DEST" <<'PY'
import json, sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
print("index-diff: snapshot saved: %s" % sys.argv[1])
print("  generated_at: %s" % doc.get("generated_at", "(none)"))
print("  schema: %s" % doc.get("schema", "(none)"))
print("  rows: %d" % len(doc.get("artifacts", []) or []))
PY
  exit 0
fi

if [ "$ACTION" = compare ]; then
  BASELINE="$ARG"
  [ -f "$BASELINE" ] || die "baseline file does not exist: $BASELINE (scripts/index-diff.sh --snapshot writes one)"
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-index-diff.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT
  CURRENT="$WORK/current.json"
  say "fetching the current index"
  fetch_index "$CURRENT"
  rc=0
  run_compare "$BASELINE" "$CURRENT" || rc=$?
  exit "$rc"
fi

if [ "$ACTION" = selftest ]; then
  # A fixture channel this script builds itself, never anything read off the
  # real (or any earlier) index.json — the negative-control discipline of
  # scripts/lint-prose.sh --selftest and scripts/check-abi-decls.py --selftest:
  # prove the checker fires on a planted fault, using input it constructed,
  # not input it was handed the answer for.
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-index-diff-selftest.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/served/artifacts" "$tmp/regen-ok/artifacts" "$tmp/regen-dropped/artifacts"

  python3 - "$tmp" <<'PY'
import json, os, sys

tmp = sys.argv[1]


def row(minor, os_, arch, build=None, abi=None):
    r = {
        "os": os_,
        "arch": arch,
        "clickhouse_minor": minor,
        "clickhouse_version": minor + ".1.1-lts",
        "file": "chtypes-%s-%s-%s.tar.gz" % (minor, os_, arch),
        "sha256": "0" * 64,
        "library": "libchtypes.so" if os_ == "linux" else "libchtypes.dylib",
        "library_sha256": "1" * 64,
        "bytes": 12345,
    }
    if build is not None:
        r["build"] = build
    if abi is not None:
        r["abi_revision"] = abi
    return r


ROWS = [
    row("24.8", "linux", "amd64"),                # no "build" field at all
    row("25.3", "linux", "amd64", build=1000, abi=5),
    row("25.3", "darwin", "arm64", build=1000, abi=5),
    row("25.8", "linux", "amd64", build=1000),     # a build with no abi_revision yet
]


def doc(rows, generated_at, unbuildable=None):
    d = {
        "schema": 1,
        "generated_at": generated_at,
        "license": "Elastic License 2.0",
        "license_url": "https://example.invalid/license",
        "artifacts": rows,
    }
    if unbuildable is not None:
        d["unbuildable"] = unbuildable
    return d


def write(rel, d):
    with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
        json.dump(d, f)


write("served/artifacts/index.json", doc(ROWS, "2026-01-01T00:00:00Z"))

# regen-ok: a correct regeneration — every old row still present, unioned
# with one new one, plus a top-level unbuildable entry to exercise that report.
regen_ok_rows = list(ROWS) + [row("26.2", "linux", "amd64", build=2000, abi=5)]
write("regen-ok/artifacts/index.json", doc(
    regen_ok_rows, "2026-01-02T00:00:00Z",
    unbuildable=[{"clickhouse_minor": "99.9", "reason": "no LTS build yet"}],
))

# regen-dropped: the bug this script exists to catch — the same union, but
# one existing row silently missing (an "--assemble" that overwrote rather
# than unioned).
regen_dropped_rows = [r for r in ROWS if not (r["clickhouse_minor"] == "25.3" and r["os"] == "darwin")]
regen_dropped_rows.append(row("26.2", "linux", "amd64", build=2000, abi=5))
write("regen-dropped/artifacts/index.json", doc(regen_dropped_rows, "2026-01-02T00:00:00Z"))
PY

  fail() { echo "SELFTEST FAILED: $1" >&2; [ -z "${2:-}" ] || echo "$2" >&2; exit 1; }

  # 1) --snapshot against the fixture "served" channel.
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/served" "$SELF" --snapshot "$tmp/baseline.json" 2>&1)" \
    || fail "--snapshot did not succeed against a clean fixture channel" "$out"
  [ -f "$tmp/baseline.json" ] || fail "--snapshot did not write $tmp/baseline.json" "$out"

  # 2) --compare against the SAME channel: must exit 0 and say plainly that
  #    nothing was dropped.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/served" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "--compare against an UNCHANGED channel exited $rc; it must exit 0" "$out"
  case "$out" in
    *"nothing was dropped"*) ;;
    *) fail "an unchanged compare did not say plainly that nothing was dropped" "$out" ;;
  esac
  echo "  unchanged channel: exit 0, \"nothing was dropped\" — as required"

  # 3) --compare against a channel that correctly UNIONED (kept every old row
  #    and added one): still exit 0, the new row is reported as added, and
  #    the unbuildable array's contents are reported.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-ok" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "a correct union regeneration was reported as a failure (exit $rc)" "$out"
  case "$out" in
    *"clickhouse_minor=26.2"*) ;;
    *) fail "the added row was not reported" "$out" ;;
  esac
  case "$out" in
    *"99.9"*"no LTS build yet"*) ;;
    *) fail "the unbuildable array's contents were not reported" "$out" ;;
  esac
  echo "  correct union regeneration: exit 0, the added row and the unbuildable array are both reported"

  # 4) --compare against a channel that DROPPED a row: the one outcome that
  #    must never pass quietly. Exit non-zero, and name the exact key.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-dropped" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || fail "the planted dropped row was NOT caught — exited 0" "$out"
  case "$out" in
    *"DROPPED clickhouse_minor=25.3 os=darwin arch=arm64 build=1000"*) ;;
    *) fail "the planted drop fired (exit $rc) but did not name the row" "$out" ;;
  esac
  echo "  planted drop (clickhouse_minor=25.3 os=darwin arch=arm64 build=1000): caught, exit $rc, row named"

  echo "index-diff: selftest ok — an unchanged channel passes, a union regeneration passes, and a dropped row is caught by name"
  exit 0
fi
