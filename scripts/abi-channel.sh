#!/usr/bin/env bash
# abi-channel.sh — which artifact channel the `artifacts` job fetches from,
# THIS run, decided from the header and the served index alone (chtypes#232).
#
#   scripts/abi-channel.sh [--header PATH] [--platform <os-arch>]
#   scripts/abi-channel.sh --selftest
#
# WHY. Until now, a PR that moves include/chtypes.h's CHS_ABI_REVISION ahead
# of what the release serves was expected-red on `artifacts` for its whole
# life: every served library refuses the new revision, on purpose (see the
# abi-fixtures job's own header for how a broken refusal hid inside exactly
# that expected-red state once). The artifact producer now publishes signed
# candidate builds for a pending revision under the tag `abi<N>-candidate`.
# This script is what decides, per run, whether `artifacts` should read that
# candidate channel instead of the served one — never a repository variable,
# because a variable applies to every branch and `main` would then point at
# a candidate.
#
# THE DECISION, in order:
#
#   0. CHTYPES_ARTIFACTS_TAG explicitly set (a plain repository variable,
#      unrelated to ABI revisions — e.g. #85) STILL WINS, unconditionally:
#      no header read beyond the sanity check below, no served-index fetch,
#      no comparison, and it can never fail this step. Someone set that
#      variable on purpose; second-guessing it is not this script's job.
#   1. Otherwise, the header revision: read from THIS repository's
#      include/chtypes.h. It must match exactly once, or this refuses to
#      guess and fails.
#   2. The served revision: from the served channel's index.json, restricted
#      to the given platform, this takes the lines scripts/published-lines.sh
#      picks for that platform PLUS 24.8, picks each line's winning row by
#      scripts/fetch.sh's own rank (ClickHouse version numerically, then
#      wrapper build), and reads that row's abi_revision. All the chosen
#      rows must agree, and every one of them must carry an abi_revision, or
#      this cannot say what the served revision is and refuses to guess.
#   3. header == served: the default channel (empty tag), same as before
#      this script existed. header > served: `abi<header>-candidate`, for
#      BOTH scripts/published-lines.sh and scripts/fetch.sh. header < served:
#      this refuses and fails, naming both numbers — that direction should
#      never happen outside a revert, and guessing a channel for it would be
#      exactly the silent-fallback failure this script exists to prevent.
#
# Prints, in the shape a GitHub Actions step appends to $GITHUB_OUTPUT:
#
#   tag=<empty, or a channel tag>      pass to both scripts' own --tag
#   mode=<explicit|default|candidate>  explicit: CHTYPES_ARTIFACTS_TAG won;
#                                      default: header == served; candidate:
#                                      header > served, reading the tag above
#   revision=<header's ABI revision>
#   served=<served revision, or empty in explicit mode — not read there>
#
# The line saying which channel was chosen, and why, goes to stderr (a
# workflow log shows both streams), because "chosen the default channel, on
# purpose" needs to be as visible in a green run as a candidate choice is —
# this is the read that answers "did revision 5 == 5 actually get checked,
# or did nothing check it."
#
# Never falls back to the served channel on a candidate fetch failure — that
# is the caller's job (the `artifacts` job's own verdict step), reading
# scripts/fetch.sh's exit code (3 CHTYPES_SOURCE_UNREACHABLE, 4
# CHTYPES_ARTIFACT_UNPUBLISHED) against this script's `mode` output, because
# only scripts/fetch.sh's own attempt can tell "no candidate published yet"
# apart from "the host is down" — this script never fetches a candidate
# tag's index.json itself, only the served one.
#
# Environment: CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev),
# CHTYPES_ARTIFACTS_TAG (an explicit override — see step 0 above).
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
die() { echo "abi-channel: $*" >&2; exit 1; }
say() { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

# header_revision <header-path> — CHS_ABI_REVISION, read exactly the way
# .github/workflows/ci.yml's `abi` job already does, so the two can never
# disagree on how to parse the same line. Dies unless it matches exactly once.
header_revision() {
  local header="$1" rev
  [ -f "$header" ] || die "no header at $header"
  rev="$(sed -n 's/^#define CHS_ABI_REVISION \([0-9][0-9]*\)$/\1/p' "$header")"
  local n
  n="$(printf '%s\n' "$rev" | grep -c . || true)"
  [ "$n" -eq 1 ] || die "$header defines CHS_ABI_REVISION $n time(s) (wanted exactly 1): $(printf '%s' "$rev" | tr '\n' ' ')"
  printf '%s' "$rev"
}

# served_revision <index.json> <platform> <line>...  — prints exactly one of:
#   ok <revision>
#   missing <space-separated lines with no usable abi_revision>
#   disagree <line1>=<rev1> <line2>=<rev2> ...
# to stdout; the row chosen for each line, and why, goes to stderr. The
# per-line winner is picked by the SAME rank scripts/fetch.sh's own asset
# selection uses — version numerically, then wrapper build — so this can
# never name a different row than what a fetch of that line would install.
served_revision() {
  local index="$1" platform="$2"
  shift 2
  python3 - "$index" "$platform" "$@" <<'PY'
import json, re, sys
index_path, platform = sys.argv[1], sys.argv[2]
lines = sys.argv[3:]
doc = json.load(open(index_path, encoding="utf-8"))
if doc.get("schema") != 1:
    sys.exit("abi-channel: index.json schema %r is not 1 — this script cannot read it" % doc.get("schema"))
os_, _, arch = platform.partition("-")
rows = [a for a in doc.get("artifacts", []) if a.get("os") == os_ and a.get("arch") == arch]

def vkey(a):
    return tuple(int(p) for p in a["clickhouse_version"].split("-", 1)[0].split("."))

def build_of(a):
    # Exactly scripts/fetch.sh's build_of(): the row's own "build" field when
    # it is a positive int, else the -b<N> the file name ends with, else 0.
    b = a.get("build")
    if isinstance(b, int) and b > 0:
        return b
    m = re.search(r"-b([0-9]+)\.tar\.gz$", a.get("file", ""))
    return int(m.group(1)) if m else 0

def rank(a):
    return (vkey(a), build_of(a))

revisions = {}
missing = []
for line in lines:
    of_line = [a for a in rows if a.get("clickhouse_minor") == line]
    if not of_line:
        sys.exit("abi-channel: the served channel publishes no %s row for %s" % (line, platform))
    winner = max(of_line, key=rank)
    rev = winner.get("abi_revision")
    print("abi-channel: served %s -> %s (%s), abi_revision=%r" % (
        line, winner.get("clickhouse_version"), winner.get("file"), rev), file=sys.stderr)
    if not isinstance(rev, int):
        missing.append(line)
    else:
        revisions[line] = rev

if missing:
    print("missing %s" % " ".join(missing))
    sys.exit(0)

distinct = sorted(set(revisions.values()))
if len(distinct) != 1:
    print("disagree %s" % " ".join("%s=%d" % (l, revisions[l]) for l in lines))
    sys.exit(0)

print("ok %d" % distinct[0])
PY
}

# resolve_lines <url> <tag> <platform> — the lines this run checks the served
# revision against: scripts/published-lines.sh's own two picks for <platform>
# under <tag> (the served/default tag when called for real; a fixture tag in
# --selftest), plus 24.8, the oldest line the release publishes and the one
# with no abi_revision at all before it existed. Never re-implemented here —
# published-lines.sh is the one place that decides which two lines those are.
resolve_lines() {
  local url="$1" tag="$2" platform="$3" out lines
  out="$(CHTYPES_ARTIFACTS_URL="$url" "$SCRIPTS/published-lines.sh" --platform "$platform" --tag "$tag" 2>/dev/null)" \
    || die "scripts/published-lines.sh could not pick the served channel's two lines for $platform"
  lines="$(printf '%s\n' "$out" | sed -n 's/^lines=//p')"
  [ -n "$lines" ] || die "scripts/published-lines.sh printed no 'lines=' for $platform"
  printf '%s 24.8' "$lines"
}

# choose <header-rev> <served-status-line> — the three-way decision (or the
# two refusals). served-status-line is served_revision()'s stdout, unparsed.
# Prints `mode=`/`tag=`/`served=` to stdout; the human-readable reason (this
# run's own log line: "did revision N == N actually get checked") to stderr.
# Dies on disagreement, a missing abi_revision, or header < served.
choose() {
  local header_rev="$1" status_line="$2" kind rest served
  kind="${status_line%% *}"
  rest="${status_line#* }"
  case "$kind" in
    missing)
      die "the served channel's chosen line(s) [$rest] carry no abi_revision at all — refusing to guess whether header revision $header_rev is ahead of or behind the served channel"
      ;;
    disagree)
      die "the served channel's chosen lines disagree on abi_revision ($rest) — refusing to guess whether header revision $header_rev is ahead of or behind the served channel"
      ;;
    ok)
      served="$rest"
      ;;
    *)
      die "served_revision printed something this script does not understand: $status_line"
      ;;
  esac
  if [ "$header_rev" -eq "$served" ]; then
    say "channel: header revision $header_rev matches the served channel's revision $served — using the default channel"
    printf 'mode=default\ntag=\nserved=%s\n' "$served"
  elif [ "$header_rev" -gt "$served" ]; then
    say "channel: header revision $header_rev is AHEAD of the served channel's revision $served — using the artifact producer's abi${header_rev}-candidate channel"
    printf 'mode=candidate\ntag=abi%s-candidate\nserved=%s\n' "$header_rev" "$served"
  else
    die "the header's ABI revision ($header_rev) is BEHIND the served channel's revision ($served) — header=$header_rev served=$served. This should not happen outside a revert of include/chtypes.h; the header and the served channel disagree about which direction is newer."
  fi
}

run() {
  local header="$1" platform="$2" url="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
  local header_rev
  header_rev="$(header_revision "$header")"

  if [ -n "${CHTYPES_ARTIFACTS_TAG:-}" ]; then
    say "channel: vars.CHTYPES_ARTIFACTS_TAG is explicitly set to '$CHTYPES_ARTIFACTS_TAG' — using it directly; header revision $header_rev is not compared against anything served"
    printf 'tag=%s\nmode=explicit\nrevision=%s\nserved=\n' "$CHTYPES_ARTIFACTS_TAG" "$header_rev"
    return 0
  fi

  local lines work index out
  lines="$(resolve_lines "$url" artifacts "$platform")"
  work="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-abi-channel.XXXXXX")"
  trap 'rm -rf "$work"' RETURN
  index="$work/index.json"
  curl -fsSL --retry 3 --max-time 60 "${url%/}/artifacts/index.json" -o "$index" \
    || die "CHTYPES_SOURCE_UNREACHABLE: could not fetch ${url%/}/artifacts/index.json"
  # shellcheck disable=SC2086 # $lines is a script-controlled, space-joined list of bare version strings
  out="$(served_revision "$index" "$platform" $lines)"
  local decision
  decision="$(choose "$header_rev" "$out")"
  printf '%s\nrevision=%s\n' "$decision" "$header_rev"
}

# ------------------------------------------------------------------ selftest
if [ "${1:-}" = "--selftest" ]; then
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-abi-channel-selftest.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  fail=0

  # A fixture index.json with one row per line, each carrying the given
  # abi_revision (or none at all, when omitted from the map). Uses the same
  # two synthetic lines published-lines.sh's own --selftest already proves
  # "newest build wins" against, plus 24.8, so this never has to re-derive
  # which build within a line is served — it only has to say what THAT row's
  # abi_revision is. Never read off the real, live index.
  write_index() {  # write_index <path> <rev-for-26.8> <rev-for-26.7> <rev-for-24.8>  ('-' = omit)
    python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, sys
path, r1, r2, r3 = sys.argv[1:5]
def row(minor, version, rev):
    d = {"os": "linux", "arch": "amd64", "clickhouse_minor": minor,
         "clickhouse_version": version, "file": "chtypes-%s-linux-amd64.tar.gz" % version,
         "sha256": "0" * 64, "build": 1}
    if rev != "-":
        d["abi_revision"] = int(rev)
    return d
doc = {"schema": 1, "artifacts": [
    row("26.8", "26.8.6.5-lts", r1),
    row("26.7", "26.7.10.6-stable", r2),
    row("24.8", "24.8.1.1-lts", r3),
]}
json.dump(doc, open(path, "w"))
PY
  }

  check() {  # check <name> <expected 'ok'|'fail'> <expected-grep-or-''> -- <cmd...>
    local name="$1" want="$2" grep_for="$3"; shift 3
    [ "$1" = "--" ] && shift
    local out rc=0
    out="$("$@" 2>&1)" || rc=$?
    if [ "$want" = ok ]; then
      if [ "$rc" -ne 0 ]; then
        echo "SELFTEST FAILED ($name): expected success, got exit $rc:" >&2; echo "$out" >&2; fail=1; return
      fi
    else
      if [ "$rc" -eq 0 ]; then
        echo "SELFTEST FAILED ($name): expected failure, got success:" >&2; echo "$out" >&2; fail=1; return
      fi
    fi
    if [ -n "$grep_for" ] && ! printf '%s\n' "$out" | grep -qF "$grep_for"; then
      echo "SELFTEST FAILED ($name): output did not contain '$grep_for':" >&2; echo "$out" >&2; fail=1; return
    fi
    echo "abi-channel: selftest ok — $name"
  }

  write_header() {  # write_header <path> <revision-or-'none'|'two'>
    case "$2" in
      none) : > "$1" ;;
      two)  printf '#define CHS_ABI_REVISION 5\n#define CHS_ABI_REVISION 6\n' > "$1" ;;
      *)    printf '#define CHS_ABI_REVISION %s\n' "$2" > "$1" ;;
    esac
  }

  # -- header_revision() itself: exactly-one-match discipline --------------
  h="$tmp/h.h"
  write_header "$h" 5
  check "header revision: single #define reads back" ok "5" -- bash -c "source '$0' --lib; header_revision '$h'"
  write_header "$h" none
  check "header revision: zero #define lines fails" fail "0 time(s)" -- bash -c "source '$0' --lib; header_revision '$h'"
  write_header "$h" two
  check "header revision: two #define lines fails" fail "2 time(s)" -- bash -c "source '$0' --lib; header_revision '$h'"

  # -- served_revision() + choose(): the derivation table -------------------
  idx="$tmp/index.json"

  write_header "$h" 5
  write_index "$idx" 5 5 5
  check "equal: header 5 == served 5 -> default channel" ok "mode=default" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 5 \"\$out\""
  check "equal: no tag is emitted" ok "tag=" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 5 \"\$out\""

  write_header "$h" 6
  check "ahead: header 6 > served 5 -> abi6-candidate" ok "tag=abi6-candidate" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 6 \"\$out\""
  check "ahead: mode=candidate" ok "mode=candidate" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 6 \"\$out\""

  check "behind: header 4 < served 5 fails, naming both numbers" fail "header=4 served=5" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 4 \"\$out\""

  write_index "$idx" 5 6 5
  check "disagreeing rows: fails, naming the disagreement" fail "disagree on abi_revision" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 5 \"\$out\""

  write_index "$idx" 5 - 5
  check "a row missing abi_revision: fails, naming the line" fail "carry no abi_revision" -- \
    bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose 5 \"\$out\""

  # -- explicit tag: wins outright, even over what would otherwise be a
  #    hard failure (header behind served) — proving it short-circuits
  #    BEFORE the comparison, never merely after it.
  write_index "$idx" 5 5 5
  write_header "$h" 4
  out="$(CHTYPES_ARTIFACTS_TAG=my-mirror bash "$0" --header "$h" --platform linux-amd64 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -q '^tag=my-mirror$' && printf '%s\n' "$out" | grep -q '^mode=explicit$'; then
    echo "abi-channel: selftest ok — explicit tag overriding: wins even though header 4 < served 5 would otherwise fail"
  else
    echo "SELFTEST FAILED (explicit tag overriding): exit=$rc" >&2; echo "$out" >&2; fail=1
  fi

  # -- the output follows a header edit: same served rows, revision alone
  #    changes -> default flips to candidate and back, on the temp header's
  #    OWN bytes, never a cached or hand-set value.
  write_index "$idx" 5 5 5
  write_header "$h" 5
  m1="$(bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose \$(header_revision '$h') \"\$out\"" | sed -n 's/^mode=//p')"
  write_header "$h" 7
  m2="$(bash -c "source '$0' --lib; out=\$(served_revision '$idx' linux-amd64 26.8 26.7 24.8); choose \$(header_revision '$h') \"\$out\"" | sed -n 's/^mode=//p')"
  if [ "$m1" = default ] && [ "$m2" = candidate ]; then
    echo "abi-channel: selftest ok — the output follows the temp header's own number ($m1 at rev 5, $m2 at rev 7), not a cached value"
  else
    echo "SELFTEST FAILED (output follows header edit): got '$m1' then '$m2', wanted 'default' then 'candidate'" >&2; fail=1
  fi

  [ "$fail" -eq 0 ] || exit 1
  echo "abi-channel: selftest ok — all cases passed"
  exit 0
fi

# --lib: source this file's functions without running anything (used by the
# selftest's own subshells above to call header_revision/served_revision/choose
# directly, against fixtures it built, rather than a second reimplementation
# of this file's logic).
if [ "${1:-}" = "--lib" ]; then
  return 0 2>/dev/null || exit 0
fi

# ------------------------------------------------------------------ real run
HEADER="$ROOT/include/chtypes.h"
PLATFORM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --header)   [ $# -ge 2 ] || die "--header needs a path"; HEADER="$2"; shift ;;
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM="$2"; shift ;;
    -h|--help)  sed -n '2,10p' "$0"; exit 0 ;;
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

run "$HEADER" "$PLATFORM"
