#!/usr/bin/env bash
# published-lines.sh — the newest -lts line and the newest -stable line the
# artifacts release publishes for one platform, read from the live index.json,
# and a key that changes exactly when those two rows do.
#
#   scripts/published-lines.sh [--platform <os-arch>] [--tag <t>]
#   scripts/published-lines.sh --selftest   prove the newest BUILD in a line
#                                            wins — not the first row in the
#                                            index, and not a lexical sort
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

# pick_lines <index.json> <platform> <url-label>
#
# Reads <index.json>, restricts to <platform> (os-arch), and prints
# `lines=`/`key=` to stdout and the chosen row for each kind, labeled, to
# stderr. <url-label> is only for the error messages. Shared between the real
# run (fed the curled index) and --selftest (fed a planted fixture), so there
# is exactly one definition of "which row wins".
pick_lines() {
  python3 - "$1" "$2" "$3" <<'PY'
import hashlib, json, sys
path, platform, url = sys.argv[1:4]
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
for a in picked:
    print("published-lines: %s -> %s  (%s, sha256 %s…)" % (
        a["clickhouse_minor"], a["clickhouse_version"], a["file"], a["sha256"][:12]), file=sys.stderr)
key = hashlib.sha256(json.dumps(picked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print("lines=%s" % " ".join(a["clickhouse_minor"] for a in picked))
print("key=%s" % key)
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
  exit 0
fi

PLATFORM="${CHTYPES_TARGET:-}"; TAG="artifacts"
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM="$2"; shift ;;
    --tag)      [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift ;;
    -h|--help)  sed -n '2,27p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done
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
curl -fsSL --retry 3 --max-time 60 "$URL" -o "$WORK/index.json" \
  || die "CHTYPES_SOURCE_UNREACHABLE: could not fetch $URL"

pick_lines "$WORK/index.json" "$PLATFORM" "$URL"
