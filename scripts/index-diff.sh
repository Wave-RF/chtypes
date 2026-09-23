#!/usr/bin/env bash
# index-diff.sh — an outside, read-only before/after check on the served
# rolling index (index.json) and its signed manifest (SHA256SUMS): the
# listing every published SDK resolves against by default (scripts/fetch.sh
# and scripts/support-matrix.sh both read index.json). It exists because the
# artifact producer's only vehicle for regenerating that listing today is a
# hand operation with no diff and no log anyone else can read afterwards, and
# three real gaps have already been found in it:
#
#   * a regeneration step that OVERWROTE the live index instead of unioning
#     with it, which would silently drop rows;
#   * a carry-forward test written as `endswith(".tar.gz")`, which nearly
#     dropped the release-level sdk-fetch-fixtures.tar.gz asset outright —
#     and a row-set diff over index.json's artifacts[] cannot see that kind
#     of loss at all, because that asset is never a row there. It is a line
#     in SHA256SUMS, and it is what every one of this repository's four
#     bindings' fetch suites run their ABI-revision and fetch-fixture tests
#     against.
#   * a regeneration that republished an existing asset's BYTES — same
#     filename, same row set, same SHA256SUMS line count, only the hash
#     different (issue #173). Neither the row-set diff nor the line-count
#     check can see that at all. The golden set in particular is served, not
#     tracked: every binding's golden test reads it from the registry at run
#     time, so that kind of republish changes what all four suites assert
#     against with no commit in this repository.
#
# This script is the independent check that guards against all three.
#
#   scripts/index-diff.sh --snapshot [path]   fetch the served index AND
#                                              SHA256SUMS, save both — every
#                                              entry's hash included, verbatim
#                                              — as one baseline (default
#                                              path: ./index-snapshot.json)
#   scripts/index-diff.sh --compare <baseline> [--show-reasons]
#                                              fetch the CURRENT index and
#                                              SHA256SUMS, report them
#                                              against <baseline>. By default,
#                                              any served string field that is
#                                              not one of the known
#                                              structural keys (os, arch,
#                                              clickhouse_minor) is printed
#                                              ELIDED -- e.g.
#                                              "reason: <elided, 42 chars>" --
#                                              because that prose is written
#                                              by the artifact producer, this
#                                              repository does not control
#                                              it, and this report is exactly
#                                              what gets pasted onto a public
#                                              issue. --show-reasons prints
#                                              served string fields in full,
#                                              for someone reading locally.
#   scripts/index-diff.sh --selftest          prove every hard-stop rule
#                                              below actually fires, and that
#                                              a build-only reshape does NOT,
#                                              against fixtures this script
#                                              builds itself
#
# What --compare reports, reading only the two sides themselves — never
# anything hard-coded about what they ought to contain:
#
#   - rows TRUE-DROPPED: a (clickhouse_minor, os, arch) triplet served
#     before and entirely absent now — no row for it survives under ANY
#     build. THIS IS A HARD FAILURE, named by exact row.
#   - rows RESHAPED: the same (clickhouse_minor, os, arch) triplet still has
#     at least one row after, but the set of "build" values it carries
#     changed (a row's build key moved, e.g. from absent to a real number).
#     Reported in full, by triplet, with the before and after build values —
#     but this is information, NOT a failure. The 13 rows the index
#     currently carries with no "build" field at all are exactly the shape
#     of row this exists to describe correctly: when they come back keyed
#     under a real build number instead, that is a reshape of an existing
#     triplet, not a loss of one, and must not read as the bug above.
#   - rows added: a (…, build) key that is new and that no reshape above
#     already explains — a genuinely new row.
#   - the signed SHA256SUMS's row for sdk-fetch-fixtures.tar.gz — the
#     release-level fetch-fixtures asset, never an artifacts[] row — present
#     or missing, before and after. Missing it in the CURRENT manifest IS A
#     HARD FAILURE, at the same tier as a true-dropped row, named on its own
#     line. SHA256SUMS's total line count is also reported before/after, but
#     a line-count change alone is information, not a failure.
#   - every OTHER line SHA256SUMS carries, by filename: assets ADDED (a new
#     filename), REMOVED (a filename no longer served), and HASH-CHANGED
#     (same filename, different hash — the bytes behind it were
#     republished). A changed hash is reported PROMINENTLY, named, and is
#     NOT a failure — republishing is routine, and it is exactly the third
#     gap named at the top of this file: the one way a suite's expectations
#     can change with no commit in this repository. Only
#     sdk-fetch-fixtures.tar.gz going missing (above) is a hard stop; a bare
#     add/remove/hash-change anywhere else is reported the same way
#     everything else here is — information, never failure.
#   - abi_revision coverage, before and after, broken down by build: how many
#     of that build's rows carry an abi_revision.
#   - the top-level "unbuildable" array: presence and contents, before and
#     after. Each entry's STRUCTURAL fields (os, arch, clickhouse_minor) are
#     reported plainly -- that is everything the diff itself needs. Any other
#     served string field on an entry -- "reason" today, or whatever
#     free-text field the producer adds next -- is elided by default (e.g.
#     "reason: <elided, 42 chars>"), by SHAPE rather than by name: this
#     script does not enumerate "reason" and call it done, it elides every
#     string field it does not recognize as structural, so a future free-text
#     field lands in the same elided output the first one did. That prose
#     belongs to the artifact producer, not this repository, and this report
#     is exactly what gets pasted onto a public issue. --show-reasons prints
#     it in full, for someone reading locally.
#   - each document's own generated_at.
#
# A hard failure (a true-dropped row, or a missing fixtures manifest row) is
# never reported as a mere diff line. --compare exits non-zero and NAMES
# every one of them — not a count — on the verdict itself, in this
# repository's usual style (scripts/check-suite.sh, scripts/check-standalone.sh):
# a summary a census can read off the exit code and the output together,
# nothing else required.
#
# NOT a substitute for scripts/fetch.sh's verification chain (the ed25519
# signature over SHA256SUMS, the sha256 cross-checks down to the installed
# library). This script never verifies a signature, never checks a hash
# value, and never downloads anything but index.json and the SHA256SUMS text
# file on each side. A clean run here means "no row and no fixtures manifest
# entry vanished from the listing" — nothing about whether the listing is
# authentic. That is fetch.sh's job alone.
#
# Read-only and credential-free: it fetches index.json and SHA256SUMS (twice
# each for --compare, once each for --snapshot) and writes nothing back to
# the channel. No registry, no artifacts, no downloads beyond those two small
# files each time, so this runs anywhere scripts/fetch.sh's own no-artifact
# jobs run.
#
# Nothing about today's index is hard-coded here — not a row count, not the
# current line or platform set, not a build number. The baseline file IS the
# expectation; this script compares two documents and knows nothing about
# what they ought to contain. --selftest proves every rule above fires (and
# that a reshape does NOT) by planting each fault in fixtures this script
# builds fresh every run, never by asserting today's real numbers. In
# particular, this script does NOT normalize a missing "build" field to 0 to
# make a reshape look like "no change" — the producer's own documentation of
# that convention lives outside this repository and is unverified from here;
# reporting the reshape explicitly, by triplet, is what lets a human compare
# it against what they predicted.
#
# A baseline taken by an older copy of this script (or any bare index.json,
# such as a document saved some other way) still works with --compare for
# the row-level checks; it simply has no "before" SHA256SUMS reading, so both
# the line-count check and the per-asset hash diff report the before side as
# unavailable rather than assumed.
#
# Environment:
#   CHTYPES_ARTIFACTS_URL  the artifacts host (default https://artifacts.wavehouse.dev)
#                          — the same variable scripts/fetch.sh and
#                          scripts/support-matrix.sh already read, so a
#                          mirror or a local fixture directory (a file://
#                          URL — curl serves those directly, no network
#                          needed) works here exactly as it does there.
#                          index.json is always fetched from
#                          "${CHTYPES_ARTIFACTS_URL%/}/artifacts/index.json"
#                          and SHA256SUMS from
#                          "${CHTYPES_ARTIFACTS_URL%/}/artifacts/SHA256SUMS".
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPTS/$(basename "${BASH_SOURCE[0]}")"
die()  { echo "index-diff: $*" >&2; exit 1; }
say()  { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
usage() { sed -n '2,156p' "$SELF"; exit 2; }

command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"

ACTION=""
ARG=""
SHOW_REASONS=0
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
    # --show-reasons is OPTIONAL and only meaningful for --compare: opt IN to
    # printing served free-text fields (e.g. "unbuildable"[].reason) in full.
    # Never the default — see the header comment.
    if [ "${1:-}" = "--show-reasons" ]; then
      SHOW_REASONS=1
      shift
    fi
    ;;
  --selftest)
    ACTION=selftest
    shift
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    die "unknown argument: $1 (expected --snapshot [path], --compare <baseline> [--show-reasons], or --selftest)"
    ;;
esac
[ $# -eq 0 ] || die "unexpected extra argument(s): $*"

# fetch_index <dest> — index.json, the artifacts[] listing.
fetch_index() {
  local dest="$1" url
  url="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
  url="${url%/}/artifacts/index.json"
  curl -fsSL --retry 3 --max-time 60 "$url" -o "$dest" \
    || die "could not fetch $url (CHTYPES_ARTIFACTS_URL overrides the host; scripts/fetch.sh reads the same variable)"
  python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$dest" \
    || die "$url did not return valid JSON"
}

# fetch_sums <dest> — SHA256SUMS, the signed manifest of every file the
# release actually serves, including release-level assets (like the fetch
# fixtures tarball) that never appear as an index.json artifacts[] row.
fetch_sums() {
  local dest="$1" url
  url="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
  url="${url%/}/artifacts/SHA256SUMS"
  curl -fsSL --retry 3 --max-time 60 "$url" -o "$dest" \
    || die "could not fetch $url (CHTYPES_ARTIFACTS_URL overrides the host; scripts/fetch.sh reads the same variable)"
}

# run_compare <baseline-file> <current-index-file> <current-sums-file> <show-reasons 0|1> —
# prints the full report to stdout; its own exit code (0 clean, 1 a hard
# failure fired) is the caller's verdict.
run_compare() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, sys

baseline_path, current_index_path, current_sums_path = sys.argv[1:4]
show_reasons = sys.argv[4] == "1"

# The one release-level asset this script checks for by name: never an
# artifacts[] row, so the row-set diff below cannot see it at all — it is
# what every binding's fetch suite here runs its fetch-fixture and
# ABI-revision-refusal tests against (docs/guides/fetch.md, scripts/abi-fixtures.sh).
FIXTURES_ASSET = "sdk-fetch-fixtures.tar.gz"


def validate_index(doc, path, label):
    if not isinstance(doc.get("artifacts"), list):
        sys.exit("index-diff: %s (%s) has no 'artifacts' array — this does not look like index.json" % (path, label))
    if doc.get("schema") != 1:
        sys.exit("index-diff: %s (%s) has schema %r, not 1 — this script cannot read it" % (path, label, doc.get("schema")))


def load_baseline(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "index" in raw:
        # The envelope this script's own --snapshot writes: the index
        # document plus the SHA256SUMS lines captured at the same moment.
        idx = raw["index"]
        validate_index(idx, path, "before")
        sums = raw.get("sha256sums")
        if sums is not None and not isinstance(sums, list):
            sys.exit("index-diff: %s carries a 'sha256sums' field that is not a list" % path)
        return idx, sums
    if isinstance(raw, dict) and "artifacts" in raw:
        # A bare index.json used directly as a baseline — a convenience
        # snapshot saved some other way, or one taken by a version of this
        # script that predates SHA256SUMS capture. Still legal; it simply
        # has no "before" reading for the SHA256SUMS checks below.
        validate_index(raw, path, "before")
        return raw, None
    sys.exit("index-diff: %s does not look like a baseline — no 'index' or 'artifacts' key" % path)


before, before_sums = load_baseline(baseline_path)

with open(current_index_path, encoding="utf-8") as f:
    after = json.load(f)
validate_index(after, current_index_path, "after")

with open(current_sums_path, encoding="utf-8") as f:
    after_sums = [line.rstrip("\n") for line in f if line.strip()]


def triplet(a):
    return (a.get("clickhouse_minor"), a.get("os"), a.get("arch"))


def build_label(a):
    b = a.get("build")
    return "none" if b is None else str(b)


def full_key(a):
    return triplet(a) + (build_label(a),)


def fmt_triplet(t):
    minor, os_, arch = t
    return "clickhouse_minor=%s os=%s arch=%s" % (minor, os_, arch)


def fmt_full(k):
    minor, os_, arch, build = k
    return "clickhouse_minor=%s os=%s arch=%s build=%s" % (minor, os_, arch, build)


def build_sort_key(b):
    return (0, int(b)) if b != "none" and b.lstrip("-").isdigit() else (1, b)


before_rows = {full_key(a): a for a in before["artifacts"]}
after_rows = {full_key(a): a for a in after["artifacts"]}

before_by_triplet = {}
for a in before["artifacts"]:
    before_by_triplet.setdefault(triplet(a), set()).add(build_label(a))
after_by_triplet = {}
for a in after["artifacts"]:
    after_by_triplet.setdefault(triplet(a), set()).add(build_label(a))

# Classification runs at the TRIPLET level, per (clickhouse_minor, os,
# arch): a triplet that still has any row after, under any build, is
# reshaped, not dropped — only a triplet with NOTHING left under it is a
# true drop. This is deliberate: the index accumulates builds over time
# (the same triplet legitimately carries many historical build values at
# once), so "a specific build's row vanished" is routine and "the whole
# triplet vanished" is the actual signal a wholesale overwrite would leave.
true_drops = []             # full keys whose triplet is entirely gone now
reshaped = []                # (triplet, removed build labels, added build labels)
reshape_consumed_added = {}  # triplet -> build labels already explained by a reshape

for t, b_builds in before_by_triplet.items():
    a_builds = after_by_triplet.get(t, set())
    if not a_builds:
        for b in b_builds:
            true_drops.append(t + (b,))
        continue
    removed = b_builds - a_builds
    if removed:
        added_here = a_builds - b_builds
        reshaped.append((t, removed, added_here))
        reshape_consumed_added[t] = added_here

added_new = []
for k in after_rows:
    if k in before_rows:
        continue
    t, b = k[:3], k[3]
    if b in reshape_consumed_added.get(t, set()):
        continue
    added_new.append(k)

true_drops.sort(key=fmt_full)
added_new.sort(key=fmt_full)
reshaped.sort(key=lambda r: fmt_triplet(r[0]))


def abi_coverage(doc):
    # {build: (rows carrying abi_revision, total rows)}, so a build's
    # coverage reads as a fraction even when it is 0-of-something.
    cov = {}
    for a in doc["artifacts"]:
        b = build_label(a)
        hit, total = cov.get(b, (0, 0))
        total += 1
        if a.get("abi_revision") is not None:
            hit += 1
        cov[b] = (hit, total)
    return cov


before_cov, after_cov = abi_coverage(before), abi_coverage(after)
all_builds = sorted(set(before_cov) | set(after_cov), key=build_sort_key)


# The "unbuildable" array is written by the artifact producer, and this
# report is exactly what gets pasted onto a public issue — so any served
# STRING field on an entry that this script does not itself need is never
# printed verbatim. Elided BY SHAPE, not by name: only the fields the diff
# actually needs (os, arch, clickhouse_minor) are known structural keys, and
# everything else that turns out to be a string is redacted to its length —
# whether it is "reason" (the one that already leaked two internal tracker
# references) or a free-text field the producer adds next. --show-reasons
# opts back into the full text, for someone reading locally.
UNBUILDABLE_STRUCTURAL_KEYS = ("os", "arch", "clickhouse_minor")


def is_json_scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def describe_shape(v):
    # A safe, content-free description of a value this script does not
    # otherwise know how to elide field-by-field — used for a whole
    # "unbuildable" entry (or the whole array) that is not the documented
    # dict/list shape. Never returns the value's own text.
    if isinstance(v, str):
        return "<elided, %d chars>" % len(v)
    if isinstance(v, list):
        return "<list, %d item%s>" % (len(v), "" if len(v) == 1 else "s")
    if isinstance(v, dict):
        return "<object, %d field%s>" % (len(v), "" if len(v) == 1 else "s")
    if is_json_scalar(v):
        return json.dumps(v)
    return "<%s>" % type(v).__name__


def format_unbuildable_field(key, value, show_reasons):
    if key in UNBUILDABLE_STRUCTURAL_KEYS and is_json_scalar(value):
        # A known structural field: exactly what the diff needs, printed
        # plainly. (If a producer ever puts a list/dict under one of these
        # key names, that is not actually structural — it falls through to
        # the generic, shape-only handling below instead of being trusted.)
        return "%s=%s" % (key, value)
    if isinstance(value, str):
        if show_reasons:
            return "%s=%s" % (key, value)
        return "%s: <elided, %d chars>" % (key, len(value))
    # A non-string, non-structural field: not free text, so its shape (not
    # its content) is safe to report either way.
    return "%s: %s" % (key, describe_shape(value))


def describe_unbuildable_entry(entry, show_reasons):
    if isinstance(entry, dict):
        return ", ".join(
            format_unbuildable_field(k, entry[k], show_reasons) for k in sorted(entry)
        )
    # Not the documented shape (a dict) at all — never echo it raw.
    return describe_shape(entry)


def describe_unbuildable(doc, show_reasons):
    if "unbuildable" not in doc:
        return "(key absent)"
    arr = doc["unbuildable"]
    if not isinstance(arr, list):
        return "present but not a list: %s" % describe_shape(arr)
    if not arr:
        return "present, empty"
    lines = ["%d entr%s:" % (len(arr), "y" if len(arr) == 1 else "ies")]
    for entry in arr:
        lines.append("      - %s" % describe_unbuildable_entry(entry, show_reasons))
    return "\n".join(lines)


def sums_has(lines, name):
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and (parts[1] == name or parts[1] == "*" + name):
            return True
    return False


def parse_sums(lines):
    # {published filename: hex hash}, stripping the optional binary-mode "*"
    # prefix sha256sum puts in front of the filename — the same "name or
    # *name" equivalence sums_has() above already treats as one asset. A
    # later line for the same filename wins, matching how a manifest is
    # actually read (the last line for a name is the live one).
    out = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1][1:] if parts[1].startswith("*") else parts[1]
        out[name] = parts[0]
    return out


fixtures_before = sums_has(before_sums, FIXTURES_ASSET) if before_sums is not None else None
fixtures_after = sums_has(after_sums, FIXTURES_ASSET)

# Per-asset hash diff over the WHOLE manifest — not just the fixtures asset
# above. This is the check issue #173 asked for: a regeneration can replace
# an asset's bytes (a goldens or fixtures republish) while the row set, the
# line count, and even the fixtures-presence check above all stay identical.
# Only reading the hash column catches it. before_assets is None exactly
# when before_sums is (a baseline predating SHA256SUMS capture): reported as
# unavailable, never guessed — the same fallback the line-count check above
# already uses, kept to one style rather than inventing a second.
before_assets = parse_sums(before_sums) if before_sums is not None else None
after_assets = parse_sums(after_sums)
if before_assets is not None:
    assets_added = sorted(set(after_assets) - set(before_assets))
    assets_removed = sorted(set(before_assets) - set(after_assets))
    assets_hash_changed = sorted(
        name for name in (set(before_assets) & set(after_assets))
        if before_assets[name] != after_assets[name]
    )
else:
    assets_added = assets_removed = assets_hash_changed = None

print("index-diff: %s (before) vs %s (after)" % (baseline_path, current_index_path))
print("  generated_at: before=%s  after=%s" % (before.get("generated_at", "(none)"), after.get("generated_at", "(none)")))
print("  rows: before=%d  after=%d" % (len(before_rows), len(after_rows)))

print("  rows added (new key, no reshape explains it): %d" % len(added_new))
for k in added_new:
    print("    + %s" % fmt_full(k))

print("  rows reshaped (same clickhouse_minor/os/arch, build key changed — NOT a failure): %d" % len(reshaped))
for t, removed, added_here in reshaped:
    print("    RESHAPED %s: build %s -> build %s" % (
        fmt_triplet(t),
        "/".join(sorted(removed, key=build_sort_key)),
        "/".join(sorted(added_here, key=build_sort_key)) if added_here else "(none)",
    ))

print("  rows TRUE-DROPPED (clickhouse_minor/os/arch entirely absent now): %d" % len(true_drops))
for k in true_drops:
    print("    - %s" % fmt_full(k))

print("  abi_revision coverage by build:")
for b in all_builds:
    bh, bt = before_cov.get(b, (0, 0))
    ah, at = after_cov.get(b, (0, 0))
    print("    build=%s: before %d/%d  after %d/%d" % (b, bh, bt, ah, at))

print("  unbuildable: before=%s" % describe_unbuildable(before, show_reasons))
print("               after=%s" % describe_unbuildable(after, show_reasons))

print("  SHA256SUMS lines: before=%s  after=%d" % (
    str(len(before_sums)) if before_sums is not None else "(unavailable — this baseline predates SHA256SUMS capture)",
    len(after_sums),
))
print("  %s row: before=%s  after=%s" % (
    FIXTURES_ASSET,
    ("present" if fixtures_before else "MISSING") if fixtures_before is not None else "(unknown)",
    "present" if fixtures_after else "MISSING",
))

if before_assets is None:
    print("  SHA256SUMS per-asset diff: (unavailable — this baseline predates SHA256SUMS capture)")
else:
    print("  SHA256SUMS assets added (new filename): %d" % len(assets_added))
    for name in assets_added:
        print("    + %s" % name)
    print("  SHA256SUMS assets removed (filename no longer served): %d" % len(assets_removed))
    for name in assets_removed:
        print("    - %s" % name)
    print("  SHA256SUMS assets HASH-CHANGED (same filename, different bytes — republished, NOT a failure): %d" % len(assets_hash_changed))
    for name in assets_hash_changed:
        print("    HASH-CHANGED %s" % name)

problems = []
if true_drops:
    problems.append("%d row(s) TRUE-DROPPED (clickhouse_minor/os/arch entirely absent now)" % len(true_drops))
if not fixtures_after:
    problems.append("SHA256SUMS no longer carries a row for %s" % FIXTURES_ASSET)

if problems:
    print("VERDICT: FAIL — %s:" % "; ".join(problems))
    for k in true_drops:
        print("  TRUE DROP %s" % fmt_full(k))
    if not fixtures_after:
        print("  MISSING SHA256SUMS row: %s — every binding's fetch suite here reads this fixture" % FIXTURES_ASSET)
    sys.exit(1)

unchanged = len(set(before_rows) & set(after_rows))
print("VERDICT: ok — nothing was dropped (%d row(s) added, %d reshaped, %d unchanged), and %s is present in SHA256SUMS"
      % (len(added_new), len(reshaped), unchanged, FIXTURES_ASSET))
sys.exit(0)
PY
}

if [ "$ACTION" = snapshot ]; then
  DEST="${ARG:-index-snapshot.json}"
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-index-diff-snapshot.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT
  say "fetching the served index and SHA256SUMS -> $DEST"
  fetch_index "$WORK/index.json"
  fetch_sums "$WORK/SHA256SUMS"
  python3 - "$WORK/index.json" "$WORK/SHA256SUMS" "$DEST" <<'PY'
import json, sys

index_path, sums_path, dest_path = sys.argv[1:4]
doc = json.load(open(index_path, encoding="utf-8"))
sums_lines = [line.rstrip("\n") for line in open(sums_path, encoding="utf-8") if line.strip()]

with open(dest_path, "w", encoding="utf-8") as f:
    json.dump({"format": 2, "index": doc, "sha256sums": sums_lines}, f)

fixtures_present = any(
    len(parts) >= 2 and parts[1] in ("sdk-fetch-fixtures.tar.gz", "*sdk-fetch-fixtures.tar.gz")
    for parts in (line.split() for line in sums_lines)
)
print("index-diff: snapshot saved: %s" % dest_path)
print("  generated_at: %s" % doc.get("generated_at", "(none)"))
print("  schema: %s" % doc.get("schema", "(none)"))
print("  rows: %d" % len(doc.get("artifacts", []) or []))
print("  SHA256SUMS lines: %d (sdk-fetch-fixtures.tar.gz: %s)" % (
    len(sums_lines), "present" if fixtures_present else "MISSING"))
PY
  exit 0
fi

if [ "$ACTION" = compare ]; then
  BASELINE="$ARG"
  [ -f "$BASELINE" ] || die "baseline file does not exist: $BASELINE (scripts/index-diff.sh --snapshot writes one)"
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-index-diff.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT
  say "fetching the current index and SHA256SUMS"
  fetch_index "$WORK/current-index.json"
  fetch_sums "$WORK/current-SHA256SUMS"
  rc=0
  run_compare "$BASELINE" "$WORK/current-index.json" "$WORK/current-SHA256SUMS" "$SHOW_REASONS" || rc=$?
  exit "$rc"
fi

if [ "$ACTION" = selftest ]; then
  # Fixture channels this script builds itself, never anything read off the
  # real (or any earlier) index.json or SHA256SUMS — the negative-control
  # discipline of scripts/lint-prose.sh --selftest and
  # scripts/check-abi-decls.py --selftest: prove the checker fires on a
  # planted fault, using input it constructed, not input it was handed the
  # answer for.
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-index-diff-selftest.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p \
    "$tmp/served/artifacts" \
    "$tmp/regen-ok/artifacts" \
    "$tmp/regen-dropped/artifacts" \
    "$tmp/regen-reshaped/artifacts" \
    "$tmp/regen-fixtures-dropped/artifacts" \
    "$tmp/regen-unbuildable-reason/artifacts" \
    "$tmp/regen-hash-changed/artifacts"

  # The needle for the elision selftest below comes from scripts/lint-public.sh
  # --print-rules, FETCHED LIVE — never a hand-written list (issue #159
  # recorded what hand-copied needles cost: four invented patterns, two of
  # which were not needles at all).
  NEEDLE="$("$SCRIPTS/lint-public.sh" --print-rules | awk -F'\t' '$1 == "LITERAL" { print $2; exit }')"
  [ -n "$NEEDLE" ] || die "could not obtain a needle from scripts/lint-public.sh --print-rules"

  NEEDLE="$NEEDLE" python3 - "$tmp" <<'PY'
import json, os, sys

tmp = sys.argv[1]
needle = os.environ["NEEDLE"]


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


def write_sums(rel, rows, include_fixtures=True, goldens_hash=None, fixtures_hash=None):
    lines = ["%s  %s" % ("2" * 64, r["file"]) for r in rows]
    lines.append("%s  sdk-goldens.json" % (goldens_hash or "3" * 64))
    if include_fixtures:
        lines.append("%s  sdk-fetch-fixtures.tar.gz" % (fixtures_hash or "4" * 64))
    with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


write("served/artifacts/index.json", doc(ROWS, "2026-01-01T00:00:00Z"))
write_sums("served/artifacts/SHA256SUMS", ROWS)

# regen-ok: a correct regeneration — every old row still present, unioned
# with one new one, plus a top-level unbuildable entry to exercise that
# report.
regen_ok_rows = list(ROWS) + [row("26.2", "linux", "amd64", build=2000, abi=5)]
write("regen-ok/artifacts/index.json", doc(
    regen_ok_rows, "2026-01-02T00:00:00Z",
    unbuildable=[{"clickhouse_minor": "99.9", "reason": "no LTS build yet"}],
))
write_sums("regen-ok/artifacts/SHA256SUMS", regen_ok_rows)

# regen-dropped: a TRUE DROP — the bug this script exists to catch — one
# triplet's only row silently missing (an "--assemble" that overwrote rather
# than unioned), nothing else disturbed.
regen_dropped_rows = [r for r in ROWS if not (r["clickhouse_minor"] == "25.3" and r["os"] == "darwin")]
regen_dropped_rows.append(row("26.2", "linux", "amd64", build=2000, abi=5))
write("regen-dropped/artifacts/index.json", doc(regen_dropped_rows, "2026-01-02T00:00:00Z"))
write_sums("regen-dropped/artifacts/SHA256SUMS", regen_dropped_rows)

# regen-reshaped: the shape a correct regeneration gives the rows that
# CURRENTLY have no "build" field at all — the same triplet comes back keyed
# under a real build number instead. Same triplet, nothing lost: must be
# reported as a reshape, never as a drop.
regen_reshaped_rows = [
    row("24.8", "linux", "amd64", build=5000) if (r["clickhouse_minor"] == "24.8" and r.get("build") is None) else r
    for r in ROWS
]
write("regen-reshaped/artifacts/index.json", doc(regen_reshaped_rows, "2026-01-02T00:00:00Z"))
write_sums("regen-reshaped/artifacts/SHA256SUMS", regen_reshaped_rows)

# regen-fixtures-dropped: every index row is UNCHANGED — only the release
# manifest's row for the fetch-fixtures asset vanishes. Isolates the
# SHA256SUMS guard from the row-level checks entirely.
write("regen-fixtures-dropped/artifacts/index.json", doc(ROWS, "2026-01-02T00:00:00Z"))
write_sums("regen-fixtures-dropped/artifacts/SHA256SUMS", ROWS, include_fixtures=False)

# regen-unbuildable-reason: every index row is UNCHANGED — only a new
# "unbuildable" entry appears, whose free-text "reason" carries a needle from
# scripts/lint-public.sh --print-rules (the same shape as the two internal
# tracker references that were about to reach a public issue verbatim). Its
# structural fields (os, arch, clickhouse_minor) are set too, so the default
# output can be checked to still carry THOSE while eliding the reason.
write("regen-unbuildable-reason/artifacts/index.json", doc(
    ROWS, "2026-01-02T00:00:00Z",
    unbuildable=[{
        "clickhouse_minor": "88.1",
        "os": "linux",
        "arch": "amd64",
        "reason": "blocked pending %s cleanup" % needle,
    }],
))
write_sums("regen-unbuildable-reason/artifacts/SHA256SUMS", ROWS)

# regen-hash-changed: the exact gap issue #173 reported — the row set AND the
# SHA256SUMS line count are both UNCHANGED; only two assets' BYTES differ
# (new hash, same filename), the shape a routine goldens/fixtures republish
# takes. Neither the row-set diff nor the line-count check can see this at
# all; only reading the hash column can.
write("regen-hash-changed/artifacts/index.json", doc(ROWS, "2026-01-02T00:00:00Z"))
write_sums(
    "regen-hash-changed/artifacts/SHA256SUMS", ROWS,
    goldens_hash="9" * 64, fixtures_hash="8" * 64,
)
PY

  fail() { echo "SELFTEST FAILED: $1" >&2; [ -z "${2:-}" ] || echo "$2" >&2; exit 1; }
  must_not_contain() { # must_not_contain <output> <needle> <what>
    if printf '%s\n' "$1" | grep -qF -- "$2"; then
      fail "$3" "$1"
    fi
  }

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
  #    the unbuildable array's structural fields are reported — but its
  #    free-text "reason" is ELIDED by default, never the raw producer prose
  #    (that is the whole point of this issue; see check 7 below for the
  #    dedicated elision proof against a live-fetched needle).
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-ok" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "a correct union regeneration was reported as a failure (exit $rc)" "$out"
  case "$out" in
    *"clickhouse_minor=26.2"*) ;;
    *) fail "the added row was not reported" "$out" ;;
  esac
  case "$out" in
    *"clickhouse_minor=99.9"*) ;;
    *) fail "the unbuildable array's structural field was not reported" "$out" ;;
  esac
  must_not_contain "$out" "no LTS build yet" "the unbuildable array's free-text reason was reported VERBATIM by default (must be elided)"
  echo "  correct union regeneration: exit 0, the added row and the unbuildable array's structural fields are reported, reason elided"

  # 4) --compare against a channel that RESHAPED one row (same triplet, only
  #    the build key changed — exactly the transition the 13 build-less rows
  #    in the real index are expected to get): must exit 0, must name the
  #    triplet with its before/after build values, and must NOT be reported
  #    as a drop of any kind.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-reshaped" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "a build-only reshape was reported as a failure (exit $rc)" "$out"
  case "$out" in
    *"RESHAPED clickhouse_minor=24.8 os=linux arch=amd64: build none -> build 5000"*) ;;
    *) fail "the reshape was not reported by triplet with its before/after build values" "$out" ;;
  esac
  must_not_contain "$out" "TRUE DROP " "a build-only reshape was also reported as a TRUE DROP"
  echo "  build-only reshape (clickhouse_minor=24.8 os=linux arch=amd64, build none -> 5000): exit 0, reported as RESHAPED, not dropped"

  # 5) --compare against a channel that TRUE-DROPPED an entire triplet (no
  #    build covers it any more): the one outcome that must never pass
  #    quietly. Exit non-zero, and name it as a TRUE DROP.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-dropped" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || fail "the planted TRUE DROP was NOT caught — exited 0" "$out"
  case "$out" in
    *"TRUE DROP clickhouse_minor=25.3 os=darwin arch=arm64 build=1000"*) ;;
    *) fail "the TRUE DROP fired (exit $rc) but did not name the row" "$out" ;;
  esac
  echo "  TRUE DROP (clickhouse_minor=25.3 os=darwin arch=arm64 build=1000): caught, exit $rc, row named"

  # 6) --compare against a channel where ONLY the SHA256SUMS fixtures row
  #    vanished (every index row unchanged): the hard stop this addition
  #    exists for — a row-set diff over index.json alone cannot see this at
  #    all. Exit non-zero, name the asset, and keep it isolated from the
  #    row-level checks.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-fixtures-dropped" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -ne 0 ] || fail "a missing SHA256SUMS fixtures row was NOT caught — exited 0" "$out"
  case "$out" in
    *"MISSING SHA256SUMS row: sdk-fetch-fixtures.tar.gz"*) ;;
    *) fail "the missing fixtures row fired (exit $rc) but did not name the asset" "$out" ;;
  esac
  must_not_contain "$out" "TRUE DROP " "a fixtures-only failure was also reported as a row-level TRUE DROP"
  echo "  SHA256SUMS fixtures row (sdk-fetch-fixtures.tar.gz) missing: caught, exit $rc, asset named, isolated from row-level checks"

  # 7) --compare against a channel whose only change is a new "unbuildable"
  #    entry with a "reason" carrying a needle from
  #    scripts/lint-public.sh --print-rules: the DEFAULT output must not
  #    carry the needle anywhere, must still report the entry's structural
  #    fields plainly, and must say the reason was elided (by length); with
  #    --show-reasons the needle must appear in full. Exit 0 either way — an
  #    unbuildable entry is information, never a hard failure.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-unbuildable-reason" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "a new unbuildable entry alone was reported as a failure (exit $rc)" "$out"
  case "$out" in
    *"arch=amd64, clickhouse_minor=88.1, os=linux"*) ;;
    *) fail "the unbuildable entry's structural fields were not reported plainly" "$out" ;;
  esac
  case "$out" in
    *"reason: <elided, "*) ;;
    *) fail "the unbuildable entry's reason was not reported as elided" "$out" ;;
  esac
  must_not_contain "$out" "$NEEDLE" "the DEFAULT --compare output carried a served free-text field's needle verbatim: $NEEDLE"
  echo "  unbuildable reason (needle \"$NEEDLE\" from lint-public.sh --print-rules): elided by default, structural fields (os, arch, clickhouse_minor) shown plainly"

  rc=0
  out_shown="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-unbuildable-reason" "$SELF" --compare "$tmp/baseline.json" --show-reasons 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "--show-reasons changed the exit code for the same channel (exit $rc)" "$out_shown"
  case "$out_shown" in
    *"$NEEDLE"*) ;;
    *) fail "--show-reasons did not print the full served reason text (needle: $NEEDLE)" "$out_shown" ;;
  esac
  must_not_contain "$out_shown" "<elided, " "--show-reasons still elided the reason field"
  echo "  --show-reasons: the full reason text is printed, needle present, same exit code"

  # 8) --compare against a channel where the row set AND the SHA256SUMS line
  #    count are both UNCHANGED — only two assets' hashes differ (same
  #    filename, new bytes): the exact gap issue #173 reported, which a
  #    row-set diff and a line-count check both miss entirely. Must exit 0 (a
  #    changed hash alone is never a failure) and must NAME each asset, and
  #    must not be reported as added, removed, or a TRUE DROP of any kind.
  rc=0
  out="$(CHTYPES_ARTIFACTS_URL="file://$tmp/regen-hash-changed" "$SELF" --compare "$tmp/baseline.json" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "an asset hash change alone was reported as a failure (exit $rc)" "$out"
  case "$out" in
    *"SHA256SUMS assets HASH-CHANGED (same filename, different bytes — republished, NOT a failure): 2"*) ;;
    *) fail "the hash-changed count was not reported as 2" "$out" ;;
  esac
  case "$out" in
    *"    HASH-CHANGED sdk-goldens.json"*) ;;
    *) fail "the changed sdk-goldens.json hash was not named" "$out" ;;
  esac
  case "$out" in
    *"    HASH-CHANGED sdk-fetch-fixtures.tar.gz"*) ;;
    *) fail "the changed sdk-fetch-fixtures.tar.gz hash was not named" "$out" ;;
  esac
  case "$out" in
    *"SHA256SUMS assets added (new filename): 0"*) ;;
    *) fail "a hash-only change was also reported as an asset addition" "$out" ;;
  esac
  case "$out" in
    *"SHA256SUMS assets removed (filename no longer served): 0"*) ;;
    *) fail "a hash-only change was also reported as an asset removal" "$out" ;;
  esac
  must_not_contain "$out" "TRUE DROP " "an asset hash change alone was also reported as a TRUE DROP"
  must_not_contain "$out" "MISSING SHA256SUMS row" "an asset hash change alone was also reported as a missing SHA256SUMS row"
  case "$out" in
    *"nothing was dropped"*) ;;
    *) fail "an asset hash change alone did not read as a clean verdict" "$out" ;;
  esac
  echo "  SHA256SUMS asset hash changed, row set and line count unchanged (sdk-goldens.json, sdk-fetch-fixtures.tar.gz): exit 0, both named HASH-CHANGED, not added/removed/dropped"

  echo "index-diff: selftest ok — unchanged/union/reshaped channels pass, a true triplet drop is caught by name, a missing SHA256SUMS fixtures row is caught by name, a served unbuildable reason is elided by default and shown only with --show-reasons, and a same-filename hash change is named without being treated as a failure"
  exit 0
fi
