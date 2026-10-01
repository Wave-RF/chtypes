#!/usr/bin/env bash
# published-lines.sh — the newest -lts line and the newest -stable line the
# artifacts release publishes for one platform, read from the live index.json,
# and a key that changes exactly when those rows do.
#
#   scripts/published-lines.sh [--platform <os-arch>] [--tag <t>]
#                               [--oldest-at-revision <N> | --served-at-revision <N>]
#   scripts/published-lines.sh --selftest   prove the newest BUILD in a line
#                                            wins — not the first row in the
#                                            index, and not a lexical sort —
#                                            that --oldest-at-revision skips
#                                            a line stuck at an older
#                                            revision rather than picking it,
#                                            and that --served-at-revision
#                                            lists every line actually at a
#                                            given revision, oldest first
#
# Prints `lines=<lts-minor> <stable-minor>` and `key=<sha256>`, one per line —
# the shape a GitHub Actions step appends to $GITHUB_OUTPUT — so CI never
# hard-codes a line: the release's own index is the authority on what exists,
# and a cache keyed on `key` is reused until the release changes either row.
# Which row was chosen, and why, goes to stderr.
#
# A line carries several builds. The line itself is still the newest -lts and
# the newest -stable minor; WITHIN a line, the row picked is the newest BUILD
# by the numeric components of clickhouse_version, never list order and never
# a string sort (26.7.10.6 must beat 26.7.9.12).
#
# --oldest-at-revision <N> (chtypes#281 item 2) adds a THIRD line to `lines=`:
# the OLDEST clickhouse_minor this index publishes for the platform whose own
# winning row carries abi_revision == N — this is what .github/workflows/
# ci.yml's `artifacts` job fetches alongside the two above, replacing a line
# that used to be hand-typed (24.8). A line that stops receiving new ABI
# revisions (a "served, unsupported" line, chtypes#281 item 1) simply stops
# qualifying here on its own the first time N moves past what it was last
# relinked to — never docs/support.md's `supported_lines`: the question this
# answers is "is this line actually fetchable at N", which a retired line can
# still answer yes to right up until a relink leaves it behind, and
# `supported_lines` never changes that answer either way. Fails loudly,
# naming N and the platform, when nothing qualifies.
#
# --served-at-revision <N> is a SEPARATE standalone mode (never combined with
# --oldest-at-revision, and ignores the normal lts/stable picks entirely):
# prints `served=<m1> <m2> ...`, EVERY clickhouse_minor this index publishes
# for the platform whose own winning row carries abi_revision == N, oldest
# first — never just one line. CI's `artifacts` job runs this once per run
# and exports the result as CHTYPES_SERVED_LINES_AT_REVISION, so each
# binding's quoting-boundary test can tell "nothing below the documented
# boundary is served this run" (genuinely impossible — skip) apart from
# "something below the boundary IS served, but this run did not load it" (a
# CI configuration bug — fail). Unlike --oldest-at-revision, an EMPTY result
# is not an error here: it is information the caller may legitimately act on.
#
# Nothing is verified here, on purpose: this only CHOOSES lines and names a
# key. scripts/fetch.sh re-reads the release under its ed25519 signature and
# checks every hash before it installs a byte, so a forged index could at
# most pick a different line for fetch.sh to verify, or a key that misses
# the cache.
#
# Environment: CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev),
# CHTYPES_TARGET (the platform; default this host's <os>-<arch>).
set -euo pipefail
die() { echo "published-lines: $*" >&2; exit 1; }

# fail <CHTYPES_...> <message> — mirrors scripts/fetch.sh's own fail(): names
# the docs/guides/fetch.md §7 code and exits with its §6 number, so a caller
# (the `lines` step in .github/workflows/ci.yml) can tell "not published" from
# "unreachable" without parsing prose, exactly as it already can for fetch.sh.
fail() {
  local code="$1"; shift
  echo "published-lines: $code: $*" >&2
  case "$code" in
    CHTYPES_SOURCE_UNREACHABLE)   exit 3 ;;
    CHTYPES_ARTIFACT_UNPUBLISHED) exit 4 ;;
    *)                            exit 1 ;;
  esac
}

# classify_index_fetch <http-code> <url> — maps the index fetch's HTTP outcome
# to the same two codes scripts/fetch.sh's own get_file()/fetch_release_file()
# distinguish (chtypes#262): 200 is silently fine; 404 means the host is
# REACHABLE and answers "no such release" — CHTYPES_ARTIFACT_UNPUBLISHED, exit
# 4, never CHTYPES_SOURCE_UNREACHABLE; anything else (a timeout, a DNS
# failure, a 5xx — curl reports these as "000" or leaves a non-2xx code) is a
# real reachability failure, exit 3. A function, not inlined, so --selftest
# can drive it without a network call.
classify_index_fetch() {
  local code="$1" url="$2"
  case "$code" in
    200) return 0 ;;
    404) fail CHTYPES_ARTIFACT_UNPUBLISHED "no index.json at $url — nothing is published under this tag" ;;
    *)   fail CHTYPES_SOURCE_UNREACHABLE "could not fetch $url (HTTP $code)" ;;
  esac
}

# pick_lines <index.json> <platform> <url-label> [<oldest-at-revision>]
#
# Reads <index.json>, restricts to <platform> (os-arch), and prints
# `lines=`/`key=` to stdout and the chosen row for each kind, labeled, to
# stderr. <url-label> is only for the error messages. Shared between the real
# run (fed the curled index) and --selftest (fed a planted fixture), so there
# is exactly one definition of "which row wins".
#
# <oldest-at-revision>, when non-empty (chtypes#281 item 2), adds a THIRD
# pick: the OLDEST clickhouse_minor on this platform whose own winning row
# (same rank as the two above — ClickHouse version numerically, then wrapper
# build) carries abi_revision equal to it. Grouped and ranked across EVERY
# line, not just -lts/-stable, and filtered by revision rather than by
# channel suffix — the replacement for a hand-typed oldest line (24.8), which
# worked only as long as that one name kept receiving every new revision.
# Fails loudly, naming the revision and the platform, when nothing qualifies.
pick_lines() {
  python3 - "$1" "$2" "$3" "${4:-}" <<'PY'
import hashlib, json, re, sys
path, platform, url = sys.argv[1:4]
oldest_revision = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
doc = json.load(open(path, encoding="utf-8"))
if doc.get("schema") != 1:
    sys.exit("published-lines: index.json schema %r is not 1 — this script cannot read it" % doc.get("schema"))
os_, _, arch = platform.partition("-")
rows = [a for a in doc.get("artifacts", []) if a.get("os") == os_ and a.get("arch") == arch]
if not rows:
    sys.exit("published-lines: %s publishes nothing for %s" % (url, platform))

def line_order(a):
    # Which ClickHouse minor a row belongs to. This decides which LINE is
    # newest — unchanged from before: only the build search below is new.
    return tuple(int(p) for p in a["clickhouse_minor"].split("."))

def build_order(a):
    # The full ClickHouse version, numerically. Never a string sort: it gets
    # 26.7.10.6 vs 26.7.9.12 backwards ('1' < '9' as characters). The
    # trailing "-lts"/"-stable" carries no digits, so splitting on the first
    # "-" before parsing drops it cleanly.
    return tuple(int(p) for p in a["clickhouse_version"].split("-", 1)[0].split("."))

def build_of(a):
    # scripts/fetch.sh's own build_of(): the row's own "build" field when it
    # is a positive int, else the -b<N> the file name ends with, else 0. Only
    # used for the oldest-at-revision pick below, to break a tie WITHIN one
    # line the same way the two picks above already do via build_order (which
    # only orders by clickhouse_version and cannot see a bare rebuild).
    b = a.get("build")
    if isinstance(b, int) and b > 0:
        return b
    m = re.search(r"-b([0-9]+)\.tar\.gz$", a.get("file", ""))
    return int(m.group(1)) if m else 0

picked = []
for suffix in ("lts", "stable"):
    of_kind = [a for a in rows if a["clickhouse_version"].endswith("-" + suffix)]
    if not of_kind:
        sys.exit("published-lines: no -%s line is published for %s" % (suffix, platform))
    # Pick the newest LINE by minor, exactly as before, then narrow to the
    # rows of that one line and pick the newest BUILD within it. Every row of
    # a line ties on line_order, so leaving the choice there — as the old code
    # did — hands back an arbitrary member instead of the one that will
    # actually be fetched.
    newest_minor = max(of_kind, key=line_order)["clickhouse_minor"]
    same_line = [a for a in of_kind if a["clickhouse_minor"] == newest_minor]
    picked.append(max(same_line, key=build_order))

if oldest_revision is not None:
    by_minor = {}
    for a in rows:
        minor = a.get("clickhouse_minor")
        if minor is None:
            continue
        by_minor.setdefault(minor, []).append(a)
    at_revision = []
    for minor, group in by_minor.items():
        winner = max(group, key=lambda a: (build_order(a), build_of(a)))
        if winner.get("abi_revision") == oldest_revision:
            at_revision.append((minor, winner))
    if not at_revision:
        sys.exit("published-lines: no line %s publishes for %s carries abi_revision %d in its winning row"
                  % (url, platform, oldest_revision))
    picked.append(min(at_revision, key=lambda item: line_order(item[1]))[1])

for a in picked:
    print("published-lines: %s -> %s  (%s, sha256 %s…)" % (
        a["clickhouse_minor"], a["clickhouse_version"], a["file"], a["sha256"][:12]), file=sys.stderr)
key = hashlib.sha256(json.dumps(picked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print("lines=%s" % " ".join(a["clickhouse_minor"] for a in picked))
print("key=%s" % key)
PY
}

# pick_served_at_revision <index.json> <platform> <revision>
#
# chtypes#281 rework: EVERY clickhouse_minor this index publishes for
# <platform> whose own winning row (same rank as pick_lines() above) carries
# abi_revision == <revision> — sorted oldest first. Unlike
# --oldest-at-revision (which answers "which ONE line"), this answers "which
# lines, in total, does the resolved channel serve at this SDK's revision" —
# the question each binding's quoting-boundary test needs to tell "nothing
# below the documented boundary is served this run" (skip) apart from
# "something below the boundary IS served, but this run did not load it" (a
# CI configuration bug, not a library fact — fail). Prints `served=<m1> <m2>
# ...` (possibly empty, never an error on its own: an empty result is real
# information a caller may act on) to stdout, and each qualifying row to
# stderr. Only the index itself being unreadable is an error here.
pick_served_at_revision() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, re, sys
path, platform, revision = sys.argv[1], sys.argv[2], sys.argv[3]
revision = int(revision)
doc = json.load(open(path, encoding="utf-8"))
if doc.get("schema") != 1:
    sys.exit("published-lines: index.json schema %r is not 1 — this script cannot read it" % doc.get("schema"))
os_, _, arch = platform.partition("-")
rows = [a for a in doc.get("artifacts", []) if a.get("os") == os_ and a.get("arch") == arch]

def line_order(a):
    return tuple(int(p) for p in a["clickhouse_minor"].split("."))

def build_order(a):
    return tuple(int(p) for p in a["clickhouse_version"].split("-", 1)[0].split("."))

def build_of(a):
    b = a.get("build")
    if isinstance(b, int) and b > 0:
        return b
    m = re.search(r"-b([0-9]+)\.tar\.gz$", a.get("file", ""))
    return int(m.group(1)) if m else 0

by_minor = {}
for a in rows:
    minor = a.get("clickhouse_minor")
    if minor is None:
        continue
    by_minor.setdefault(minor, []).append(a)

served = []
for minor, group in by_minor.items():
    winner = max(group, key=lambda a: (build_order(a), build_of(a)))
    if winner.get("abi_revision") == revision:
        served.append((minor, winner))
served.sort(key=lambda item: line_order(item[1]))

for minor, row in served:
    print("published-lines: served at abi_revision %d -> %s (%s)" % (
        revision, minor, row.get("clickhouse_version")), file=sys.stderr)
print("served=%s" % " ".join(minor for minor, _ in served))
PY
}

if [ "${1:-}" = "--selftest" ]; then
  # Both bugs a regression could reintroduce, proven against a planted index:
  #   1. "first row wins" — the oldest build of each line is listed first,
  #      matching the real index shape that triggered #89.
  #   2. "string sort wins" — 26.7.9.12 sorts ABOVE 26.7.10.6 as text, so a
  #      lexical comparison is wrong in the direction most likely to be missed.
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-lines-selftest.XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
  cat > "$tmp/index.json" <<'JSON'
{
 "schema": 1,
 "artifacts": [
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.8", "clickhouse_version": "26.8.2.7-lts",
   "file": "chtypes-26.8.2.7-lts-linux-amd64.tar.gz", "sha256": "aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.8", "clickhouse_version": "26.8.4.11-lts",
   "file": "chtypes-26.8.4.11-lts-linux-amd64.tar.gz", "sha256": "bbbb0000bbbb0000bbbb0000bbbb0000bbbb0000bbbb0000bbbb0000bbbb0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.8", "clickhouse_version": "26.8.5.13-lts",
   "file": "chtypes-26.8.5.13-lts-linux-amd64.tar.gz", "sha256": "cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000cccc0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.8", "clickhouse_version": "26.8.6.5-lts",
   "file": "chtypes-26.8.6.5-lts-linux-amd64.tar.gz", "sha256": "dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.3.19-stable",
   "file": "chtypes-26.7.3.19-stable-linux-amd64.tar.gz", "sha256": "eeee0000eeee0000eeee0000eeee0000eeee0000eeee0000eeee0000eeee0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.6.57-stable",
   "file": "chtypes-26.7.6.57-stable-linux-amd64.tar.gz", "sha256": "ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.8.15-stable",
   "file": "chtypes-26.7.8.15-stable-linux-amd64.tar.gz", "sha256": "11110000111100001111000011110000111100001111000011110000111100"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.9.12-stable",
   "file": "chtypes-26.7.9.12-stable-linux-amd64.tar.gz", "sha256": "22220000222200002222000022220000222200002222000022220000222200"},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.10.6-stable",
   "file": "chtypes-26.7.10.6-stable-linux-amd64.tar.gz", "sha256": "33330000333300003333000033330000333300003333000033330000333300"}
 ]
}
JSON
  out="$(pick_lines "$tmp/index.json" "linux-amd64" "selftest" 2>"$tmp/err")" \
    || { echo "SELFTEST FAILED: pick_lines exited non-zero" >&2; cat "$tmp/err" >&2; exit 1; }
  err="$(cat "$tmp/err")"
  # The line selection must be unchanged: newest -lts minor, newest -stable minor.
  printf '%s\n' "$out" | grep -qx 'lines=26.8 26.7' \
    || { echo "SELFTEST FAILED: lines= should be '26.8 26.7', got: $(printf '%s' "$out" | grep '^lines=')" >&2; exit 1; }
  # The newest BUILD of each line must be the one named on stderr...
  printf '%s\n' "$err" | grep -q '26.8 -> 26.8.6.5-lts' \
    || { echo "SELFTEST FAILED: did not choose the newest -lts build (26.8.6.5-lts): $err" >&2; exit 1; }
  printf '%s\n' "$err" | grep -q '26.7 -> 26.7.10.6-stable' \
    || { echo "SELFTEST FAILED: did not choose the newest -stable build — 26.7.10.6 must beat 26.7.9.12: $err" >&2; exit 1; }
  # ...and not the oldest (list-order bug) or the lexically-larger one (string-sort bug).
  printf '%s\n' "$err" | grep -q '26.8.2.7-lts' \
    && { echo "SELFTEST FAILED: chose the oldest -lts row instead of the newest build" >&2; exit 1; }
  printf '%s\n' "$err" | grep -q '26.7.9.12-stable' \
    && { echo "SELFTEST FAILED: chose 26.7.9.12 over 26.7.10.6 — a lexical, not numeric, comparison" >&2; exit 1; }
  echo "published-lines: selftest ok — newest build wins within a line, not list order, not a lexical sort"

  # chtypes#262: a 404 on the index is a REACHABLE host saying "no such
  # release" — CHTYPES_ARTIFACT_UNPUBLISHED, exit 4 — never
  # CHTYPES_SOURCE_UNREACHABLE. Proven against a bogus non-200/404 code too,
  # so the two cannot collapse into each other.
  out=""; rc=0
  out="$(classify_index_fetch 404 "https://example.invalid/abi6-candidate/index.json" 2>&1)" || rc=$?
  [ "$rc" -eq 4 ] || { echo "SELFTEST FAILED: a 404 should exit 4 (CHTYPES_ARTIFACT_UNPUBLISHED), got $rc: $out" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q CHTYPES_ARTIFACT_UNPUBLISHED \
    || { echo "SELFTEST FAILED: a 404's message should name CHTYPES_ARTIFACT_UNPUBLISHED: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — a 404 on the index is CHTYPES_ARTIFACT_UNPUBLISHED, exit 4"

  rc=0
  out="$(classify_index_fetch 000 "https://example.invalid/abi6-candidate/index.json" 2>&1)" || rc=$?
  [ "$rc" -eq 3 ] || { echo "SELFTEST FAILED: an unreachable host should exit 3 (CHTYPES_SOURCE_UNREACHABLE), got $rc: $out" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q CHTYPES_SOURCE_UNREACHABLE \
    || { echo "SELFTEST FAILED: an unreachable host's message should name CHTYPES_SOURCE_UNREACHABLE: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — a real reachability failure stays CHTYPES_SOURCE_UNREACHABLE, exit 3, never collapsed into the 404 case"

  rc=0
  out="$(classify_index_fetch 500 "https://example.invalid/artifacts/index.json" 2>&1)" || rc=$?
  [ "$rc" -eq 3 ] || { echo "SELFTEST FAILED: a 500 should also be CHTYPES_SOURCE_UNREACHABLE (exit 3), got $rc: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — a 5xx is CHTYPES_SOURCE_UNREACHABLE too, not treated as unpublished"

  # -- pick_lines()'s --oldest-at-revision pick (chtypes#281 item 2): the
  #    replacement for a hand-typed oldest line (24.8). 24.8 here is stuck at
  #    revision 5 (what a retired line looks like once a relink moves past
  #    it); 25.3 and the two newest (-lts/-stable) lines are at revision 6.
  cat > "$tmp/revisions-index.json" <<'JSON'
{
 "schema": 1,
 "artifacts": [
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "24.8", "clickhouse_version": "24.8.1.1-lts",
   "file": "chtypes-24.8.1.1-lts-linux-amd64.tar.gz", "sha256": "77770000777700007777000077770000777700007777000077770000777700", "build": 1, "abi_revision": 5},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "25.3", "clickhouse_version": "25.3.2.2-lts",
   "file": "chtypes-25.3.2.2-lts-linux-amd64.tar.gz", "sha256": "88880000888800008888000088880000888800008888000088880000888800", "build": 1, "abi_revision": 6},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.7", "clickhouse_version": "26.7.10.6-stable",
   "file": "chtypes-26.7.10.6-stable-linux-amd64.tar.gz", "sha256": "33330000333300003333000033330000333300003333000033330000333300", "build": 1, "abi_revision": 6},
  {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.8", "clickhouse_version": "26.8.6.5-lts",
   "file": "chtypes-26.8.6.5-lts-linux-amd64.tar.gz", "sha256": "dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000dddd0000", "build": 1, "abi_revision": 6}
 ]
}
JSON
  # (a) the oldest line present at the target revision is picked outright,
  #     when it IS the oldest line overall: at revision 5, only 24.8 qualifies.
  out="$(pick_lines "$tmp/revisions-index.json" "linux-amd64" "selftest" "5" 2>/dev/null)" \
    || { echo "SELFTEST FAILED: pick_lines --oldest-at-revision 5 exited non-zero" >&2; exit 1; }
  printf '%s\n' "$out" | grep -qx 'lines=26.8 26.7 24.8' \
    || { echo "SELFTEST FAILED: at revision 5, lines= should end in 24.8 (the only line there), got: $(printf '%s' "$out" | grep '^lines=')" >&2; exit 1; }
  echo "published-lines: selftest ok — oldest-at-revision picks a line that is actually present at the given revision"

  # (b) the oldest line OVERALL (24.8) is stuck at an older revision (5) —
  #     skipped — and the next-oldest line that IS at revision 6 (25.3) is
  #     picked instead, never 24.8 and never one of the two newest.
  out="$(pick_lines "$tmp/revisions-index.json" "linux-amd64" "selftest" "6" 2>/dev/null)" \
    || { echo "SELFTEST FAILED: pick_lines --oldest-at-revision 6 exited non-zero" >&2; exit 1; }
  printf '%s\n' "$out" | grep -qx 'lines=26.8 26.7 25.3' \
    || { echo "SELFTEST FAILED: at revision 6, lines= should end in 25.3 (24.8 is stuck at revision 5), got: $(printf '%s' "$out" | grep '^lines=')" >&2; exit 1; }
  echo "published-lines: selftest ok — a line stuck at an older revision is skipped, and the next-oldest line at the target revision is picked"

  # (c) no line anywhere carries the given revision: fails loudly, naming it
  #     — the same "stay loud" discipline as every other refusal here, never
  #     a silent fallback to the newest two lines alone.
  rc=0
  out="$(pick_lines "$tmp/revisions-index.json" "linux-amd64" "selftest" "99" 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || { echo "SELFTEST FAILED: pick_lines --oldest-at-revision 99 should fail when nothing carries it, got success: $out" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q 'abi_revision 99' \
    || { echo "SELFTEST FAILED: the failure should name the revision that matched nothing: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — no line at the given revision fails loudly, naming the revision, rather than silently dropping the third pick"

  # -- pick_served_at_revision() (chtypes#281 rework): the FULL list of lines
  #    at a given revision, oldest first — what CHTYPES_SERVED_LINES_AT_REVISION
  #    carries, on the SAME fixture (24.8@5, 25.3@6, 26.7@6, 26.8@6).
  out="$(pick_served_at_revision "$tmp/revisions-index.json" "linux-amd64" "6" 2>/dev/null)" \
    || { echo "SELFTEST FAILED: pick_served_at_revision 6 exited non-zero" >&2; exit 1; }
  [ "$out" = "served=25.3 26.7 26.8" ] \
    || { echo "SELFTEST FAILED: served-at-revision 6 should be '25.3 26.7 26.8' (24.8 is stuck at revision 5), got: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — served-at-revision lists every line at the given revision, oldest first, excluding one stuck at an older revision"

  out="$(pick_served_at_revision "$tmp/revisions-index.json" "linux-amd64" "5" 2>/dev/null)" \
    || { echo "SELFTEST FAILED: pick_served_at_revision 5 exited non-zero" >&2; exit 1; }
  [ "$out" = "served=24.8" ] \
    || { echo "SELFTEST FAILED: served-at-revision 5 should be '24.8' (the only line there), got: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — served-at-revision lists a single line when only one qualifies"

  # An empty result is NOT an error — it is information a caller may act on
  # (CI's derivation step fails the job itself if it wants mandatory output;
  # this function's own job is only to report what is actually true).
  out="$(pick_served_at_revision "$tmp/revisions-index.json" "linux-amd64" "99" 2>/dev/null)" \
    || { echo "SELFTEST FAILED: pick_served_at_revision 99 should succeed with an empty result, not fail" >&2; exit 1; }
  [ "$out" = "served=" ] \
    || { echo "SELFTEST FAILED: served-at-revision 99 should be empty (nothing is at revision 99), got: $out" >&2; exit 1; }
  echo "published-lines: selftest ok — served-at-revision succeeds with an empty list rather than erroring, when nothing qualifies"

  exit 0
fi

PLATFORM="${CHTYPES_TARGET:-}"; TAG="artifacts"; OLDEST_REVISION=""; SERVED_REVISION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform)            [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM="$2"; shift ;;
    --tag)                 [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift ;;
    --oldest-at-revision)  [ $# -ge 2 ] || die "--oldest-at-revision needs a value"; OLDEST_REVISION="$2"; shift ;;
    --served-at-revision)  [ $# -ge 2 ] || die "--served-at-revision needs a value"; SERVED_REVISION="$2"; shift ;;
    -h|--help)  sed -n '2,37p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done
if [ -n "$OLDEST_REVISION" ] && [ -n "$SERVED_REVISION" ]; then
  die "--oldest-at-revision and --served-at-revision are two different standalone modes — pass only one"
fi
if [ -z "$PLATFORM" ]; then
  _os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in x86_64|amd64) _arch=amd64 ;; arm64|aarch64) _arch=arm64 ;; *) _arch="$(uname -m)" ;; esac
  PLATFORM="$_os-$_arch"
fi
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"

URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
URL="${URL%/}/$TAG/index.json"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-lines.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
# -f dropped (chtypes#262): with it, curl fails identically on a 404 and on a
# real network error, and this script cannot then tell "not published" from
# "unreachable" apart. -w prints the status so classify_index_fetch can.
HTTP_CODE="$(curl -sSL --retry 3 --retry-delay 1 --max-time 60 -o "$WORK/index.json" -w '%{http_code}' "$URL")" \
  || HTTP_CODE="000"
classify_index_fetch "$HTTP_CODE" "$URL"

if [ -n "$SERVED_REVISION" ]; then
  pick_served_at_revision "$WORK/index.json" "$PLATFORM" "$SERVED_REVISION"
else
  pick_lines "$WORK/index.json" "$PLATFORM" "$URL" "$OLDEST_REVISION"
fi
