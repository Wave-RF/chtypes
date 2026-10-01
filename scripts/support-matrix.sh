#!/usr/bin/env bash
# support-matrix.sh — regenerate the generated block of docs/support.md: which
# language versions each binding requires, which platforms the release ships,
# which ClickHouse lines it publishes today, which of those lines upstream
# still supports, and — the SDK-version -> ABI revision -> artifact-build
# mapping a consumer needs after a load-time refusal.
#
#   scripts/support-matrix.sh [--check] [--out <file>]
#   scripts/support-matrix.sh --selftest   prove the ClickHouse-lines link
#                                           (below) renders the same way
#                                           whatever the served index's
#                                           `supported_lines` key says, against
#                                           fixtures this script builds fresh
#                                           every run, never today's real tags
#                                           or served index
#
# Nothing in that block is typed by hand, because every number in it rots on a
# schedule somebody else controls. The language minimums are read from the four
# manifests in this tree (go.mod, pyproject.toml, package.json, Cargo.toml), so
# they cannot disagree with what a build actually enforces. The platforms and
# the ClickHouse lines are read from the live index.json, so they cannot
# disagree with what the host actually serves — the README used to claim seven
# lines while the release published eleven.
#
# The ABI table has two derived halves, from two independent sources:
#   - SDK version -> ABI revision: each binding tags its own release
#     (go/vX.Y.Z, python/vX.Y.Z, ts/vX.Y.Z, rust/vX.Y.Z). This script reads
#     include/chtypes.h's CHS_ABI_REVISION as it stood AT each tag (`git show
#     <tag>:include/chtypes.h`) and refuses to run if the four bindings
#     disagree at a version they all tagged, or if a tag exists for some
#     bindings and not others — that is a real release-process drift, not a
#     formatting choice this script should paper over.
#   - ABI revision -> artifact build, per ClickHouse line and platform: read
#     from the live index.json's `abi_revision` field. That field is written
#     by the artifact producer only from the revision that introduced it
#     onward (currently revision 5); an older revision's rows carry no field
#     at all. Which revision "no field" denotes is NOT hand-typed here either:
#     it is whichever revision the git tags above name that the index does not
#     name explicitly. If that is not exactly one revision, the mapping is
#     ambiguous and this script refuses rather than guess.
#
# --check regenerates into a temporary file and diffs. It is meant to run
# NON-BLOCKING in CI. A blocking check here would redden this repository
# whenever core certifies a new ClickHouse line, which is a cross-repo event
# this repository cannot fix by itself — the same trade core made for
# fetch-fixtures-check, and for the same reason: stale prose is a smaller
# failure than a pipeline that stalls on someone else's commit.
#
# The "ClickHouse lines" section (chtypes#281) no longer renders its own
# Line/Platforms/Support table: the artifact producer's served
# `support-matrix.md` renders that same table from the same `index.json`, in
# the same publish step that writes it, and names that index's own sha256 —
# so it can never drift from what this page would otherwise restate, and this
# script instead links to it. That link is unconditional and does not read
# `supported_lines` at all: whether that key is present, absent or empty on
# the served index changes what the LINKED page's Support column says, never
# whether this page links to it. See [Served, unsupported ClickHouse
# lines](../docs/support.md#served-unsupported-clickhouse-lines) for what the
# states on that linked page mean for a consumer.
#
# Environment: CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev).
# Needs full git history and tags (fetch-depth: 0 / an unshallow, un-single-
# branch clone) for the ABI-revision half — a shallow checkout dies loudly
# with what to run rather than silently omitting the table.
set -euo pipefail
die() { echo "support-matrix: $*" >&2; exit 1; }

if [ "${1:-}" = "--selftest" ]; then
  [ $# -eq 1 ] || die "--selftest takes no other arguments"
  SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-support-matrix-selftest.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT

  # A minimal fake repo this script can treat as its own root: one manifest
  # per binding (just enough to satisfy each language-minimum regex above),
  # a header, and the four lang/v0.1.0 tags the ABI-revision half reads —
  # never today's real tree's tags, which this script's own --check refuses
  # to run against mid-release (see the module comment), so a selftest tied
  # to them would depend on exactly when it runs. The fixture under test
  # here is the ClickHouse-lines link alone; everything else is held
  # constant across both cases below.
  repo="$tmp/repo"
  mkdir -p "$repo/scripts" "$repo/go" "$repo/python" "$repo/ts" "$repo/rust" "$repo/include" "$repo/docs"
  cp "$SELF" "$repo/scripts/support-matrix.sh"
  printf 'module example\n\ngo 1.21\n' > "$repo/go/go.mod"
  printf '[project]\nname = "example"\nrequires-python = ">=3.9"\n' > "$repo/python/pyproject.toml"
  printf '{"engines": {"node": ">=18.0.0"}}\n' > "$repo/ts/package.json"
  printf '[package]\nname = "example"\nversion = "0.1.0"\nedition = "2021"\nrust-version = "1.70"\n' > "$repo/rust/Cargo.toml"
  printf '#define CHS_ABI_REVISION 5\n' > "$repo/include/chtypes.h"
  git -C "$repo" init -q
  git -C "$repo" config user.email test@example.com
  git -C "$repo" config user.name test
  # Lightweight tags, deliberately: this fixture only needs `git show
  # <tag>:include/chtypes.h` to resolve, and a real contributor's (or this
  # Mac's) ambient tag.gpgSign/commit.gpgSign config must not reach into a
  # throwaway repo this selftest builds and deletes every run.
  git -C "$repo" config tag.gpgSign false
  git -C "$repo" config commit.gpgSign false
  git -C "$repo" add -A
  git -C "$repo" commit -qm initial >/dev/null
  for lang in go python ts rust; do
    git -C "$repo" tag "$lang/v0.1.0"
  done

  # Two served lines, neither carrying an explicit abi_revision — so the
  # one implicit revision this script requires (see the module comment) is
  # exactly the 5 the header and all four tags name.
  served="$tmp/served/artifacts"
  mkdir -p "$served"
  rows='"artifacts": [
      {"os": "linux", "arch": "amd64", "clickhouse_minor": "24.8", "build": 1},
      {"os": "linux", "arch": "amd64", "clickhouse_minor": "26.3", "build": 2}
    ]'

  regen() {
    printf '# support\n\n<!-- BEGIN GENERATED — scripts/support-matrix.sh; do not edit by hand -->\nstub\n<!-- END GENERATED -->\n' \
      > "$repo/docs/support.md"
    CHTYPES_ARTIFACTS_URL="file://$tmp/served" "$repo/scripts/support-matrix.sh" --out "$repo/docs/support.md" >/dev/null
    cat "$repo/docs/support.md"
  }

  # The served support-matrix.md this fixture's link should point at — never
  # fetched by this script (it only links to it), so its content does not
  # matter, only that the URL this script derives matches where it would
  # really be served, alongside index.json under the same /artifacts/ prefix.
  link="file://$tmp/served/artifacts/support-matrix.md"

  assert_link_and_no_table() {
    local out="$1" label="$2"
    echo "$out" | grep -qF "$link" \
      || die "SELFTEST FAILED ($label): the ClickHouse-lines section must link the served support table ($link):
$out"
    echo "$out" | grep -qi 'sha256' \
      || die "SELFTEST FAILED ($label): the link's sentence must say the served table names the index's sha256:
$out"
    if echo "$out" | grep -qF '| Line | Platforms | Support |'; then
      die "SELFTEST FAILED ($label): this page must not render its own Support column any more — that is the served table's job now:
$out"
    fi
    if echo "$out" | grep -qE '^\| `(24\.8|26\.3)` \| all( \| |$)'; then
      die "SELFTEST FAILED ($label): this page must not render its own per-line Line/Platforms row any more:
$out"
    fi
  }

  clickhouse_section() { printf '%s\n' "$1" | sed -n '/^## ClickHouse lines$/,/^## /p'; }

  # Case 1: supported_lines present and names one of the two served lines.
  # The link must appear and no per-line table must render — this page no
  # longer renders a Support state at all, so which lines are IN the array
  # must not change this page's output.
  printf '{"schema": 1, %s, "supported_lines": ["26.3"]}\n' "$rows" > "$served/index.json"
  out_present="$(regen)"
  assert_link_and_no_table "$out_present" "supported_lines present"

  # Case 2: supported_lines absent entirely. Before chtypes#281's trim this
  # was a different rendered shape (no Support column, but still a per-line
  # table); now it must render exactly the same link as case 1 — the
  # decision of what a missing key means is entirely the served table's to
  # make, and this page must not re-derive it or vary its own output on it.
  printf '{"schema": 1, %s}\n' "$rows" > "$served/index.json"
  out_absent="$(regen)"
  assert_link_and_no_table "$out_absent" "supported_lines absent"
  [ "$(clickhouse_section "$out_absent")" = "$(clickhouse_section "$out_present")" ] \
    || die "SELFTEST FAILED: the ClickHouse-lines section must render identically whether supported_lines is present or absent — the served table's link does not vary with it:
--- present ---
$(clickhouse_section "$out_present")
--- absent ---
$(clickhouse_section "$out_absent")"

  echo "support-matrix: selftest ok — the ClickHouse-lines section links the served support table and renders no Support column (or per-line table) of its own, whether supported_lines is present or absent on the served index"
  exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/docs/support.md"
CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK=1 ;;
    --out)     [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift ;;
    -h|--help) sed -n '2,64p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
  shift
done
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"
command -v git >/dev/null 2>&1 || die "git is not on PATH"
[ -f "$OUT" ] || die "$OUT does not exist — the generated block is written into an existing page"

BASE_URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
BASE_URL="${BASE_URL%/}"
URL="$BASE_URL/artifacts/index.json"
# The artifact producer's served, generated support table (chtypes#281):
# rendered from this same index.json in the same publish step, alongside it
# under the same /artifacts/ prefix. This script never fetches it — only
# links to it as the per-line source, so the ClickHouse-lines section below
# does not restate index.json's per-line platform/support data a second time.
SUPPORT_TABLE_URL="$BASE_URL/artifacts/support-matrix.md"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-support.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --retry 3 --max-time 60 "$URL" -o "$WORK/index.json" \
  || die "CHTYPES_SOURCE_UNREACHABLE: could not fetch $URL"

python3 - "$HERE" "$WORK/index.json" "$OUT" "$WORK/out.md" "$URL" "$SUPPORT_TABLE_URL" <<'PY'
import json, re, subprocess, sys, pathlib

root, index_path, out_path, tmp_path, url, support_table_url = (
    pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3]),
    pathlib.Path(sys.argv[4]), sys.argv[5], sys.argv[6])

def read(rel):
    return (root / rel).read_text(encoding="utf-8")

def need(pattern, text, what):
    m = re.search(pattern, text, re.M)
    if not m:
        sys.exit("support-matrix: could not read %s — the manifest's shape changed; fix this script "
                 "rather than hand-writing the number" % what)
    return m.group(1)

def git(*args):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("support-matrix: `git %s` failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout

# Language minimums, from the files a build actually enforces.
go_min     = need(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", read("go/go.mod"), "go/go.mod's go directive")
py_min     = need(r'^requires-python\s*=\s*"[>=~^]*\s*([0-9.]+)"', read("python/pyproject.toml"), "python/pyproject.toml requires-python")
node_min   = need(r'"node"\s*:\s*"[>=~^]*\s*([0-9.]+)"', read("ts/package.json"), "ts/package.json engines.node")
rust_min   = need(r'^rust-version\s*=\s*"([0-9.]+)"', read("rust/Cargo.toml"), "rust/Cargo.toml rust-version")
rust_ed    = need(r'^edition\s*=\s*"([0-9]+)"', read("rust/Cargo.toml"), "rust/Cargo.toml edition")

# SDK version -> ABI revision, from each binding's own release tags and the
# header AS IT STOOD at each tag — never from a hand-typed list. A shallow
# checkout (no tags) fails loudly here rather than silently skipping the table.
LANGS = ("go", "python", "ts", "rust")
VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
REVISION_RE = re.compile(r"^#define\s+CHS_ABI_REVISION\s+(\d+)\s*$", re.M)

tags_by_lang = {}
for lang in LANGS:
    versions = set()
    for line in git("tag", "--list", "%s/v*" % lang).splitlines():
        line = line.strip()
        if not line:
            continue
        m = VERSION_RE.match(line[len(lang) + 1:])
        if m:
            versions.add(m.group(1))
    tags_by_lang[lang] = versions

if not all(tags_by_lang.values()):
    sys.exit("support-matrix: no <lang>/vX.Y.Z release tags found for %s — this needs full git history "
             "and tags (fetch-depth: 0 for a CI checkout; `git fetch --tags` for a shallow local clone)"
             % ", ".join(lang for lang in LANGS if not tags_by_lang[lang]))

all_versions = set().union(*tags_by_lang.values())
common_versions = set.intersection(*tags_by_lang.values())
uneven = {v: sorted(lang for lang in LANGS if v not in tags_by_lang[lang])
          for v in all_versions if v not in common_versions}
if uneven:
    sys.exit("support-matrix: a release tag exists for some bindings and not others at %r — the four "
             "bindings release together (see this repository's CLAUDE.md), so this is a real drift, "
             "not a formatting choice this script should paper over" % uneven)

def version_key(v):
    return tuple(int(p) for p in v.split("."))

def abi_revision_at_tag(lang, version):
    tag = "%s/v%s" % (lang, version)
    text = git("show", "%s:include/chtypes.h" % tag)
    m = REVISION_RE.search(text)
    if not m:
        sys.exit("support-matrix: %s's include/chtypes.h has no CHS_ABI_REVISION line — "
                 "the header's shape changed; fix this script rather than hand-writing the number" % tag)
    return int(m.group(1))

version_revision = {}
for v in sorted(common_versions, key=version_key):
    seen = {lang: abi_revision_at_tag(lang, v) for lang in LANGS}
    if len(set(seen.values())) != 1:
        sys.exit("support-matrix: the four bindings disagree on the ABI revision at v%s: %r — "
                 "that is a real cross-binding drift" % (v, seen))
    version_revision[v] = seen[LANGS[0]]

doc = json.load(open(index_path, encoding="utf-8"))
if doc.get("schema") != 1:
    sys.exit("support-matrix: index.json schema %r is not 1 — this script cannot read it" % doc.get("schema"))
rows = doc.get("artifacts", [])
if not rows:
    sys.exit("support-matrix: %s publishes no artifacts — refusing to write an empty matrix" % url)

platforms, lines = set(), {}
for a in rows:
    plat = "%s-%s" % (a["os"], a["arch"])
    platforms.add(plat)
    minor = a["clickhouse_minor"]
    # The exact patch is deliberately not tracked here: it changes on every
    # upstream patch release, and a column of it made this generated page churn
    # without anything a reader acts on changing. It lives in each artifact's
    # manifest.json and in the served index.json (`clickhouse_version`).
    lines.setdefault(minor, {"platforms": set()})["platforms"].add(plat)

def order(minor):
    return tuple(int(p) for p in minor.split("."))

plat_order = sorted(platforms, key=lambda p: (p.split("-")[0], p.split("-")[1]))
out = []
w = out.append

w("## Languages")
w("")
w("| Binding | Package | Requires | Loads the artifact with |")
w("|---|---|---|---|")
w("| Go | `github.com/wave-rf/chtypes/go` | Go %s+ | cgo + `dlopen` |" % go_min)
w("| Python | `chtypes` | Python %s+ | stdlib `ctypes` (no build step, no dependencies) |" % py_min)
w("| TypeScript | `@wavehouse/chtypes` | Node %s+, ESM only | `ffi-rs` (prebuilt) |" % node_min)
w("| Rust | `chtypes` | Rust %s+ (edition %s) | `libloading` |" % (rust_min, rust_ed))
w("")
w("## Platforms")
w("")
w("The artifact is native code, so a platform is supported only if the release publishes a build for it. Today that is:")
w("")
for p in plat_order:
    note = " — see [macOS artifacts are for development; Linux is the reference](limitations.md#macos-artifacts-are-for-development-linux-is-the-reference)" \
           if p.startswith("darwin") else ""
    w("- `%s`%s" % (p, note))
w("")
w("Both loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows artifact and no 32-bit build.")
w("")
w("## ClickHouse lines")
w("")
w("One artifact per ClickHouse line, each carrying that release's own C++. A line is published once it has passed the artifact producer's comparison against a real server and the release includes it. Separately, chtypes supports a ClickHouse line exactly as long as upstream does — see [Served, unsupported ClickHouse lines](#served-unsupported-clickhouse-lines) below for what a line reads once upstream's own support for it ends.")
w("")

# Per-line platform coverage and support state (chtypes#281) used to be a
# Line/Platforms/Support table rendered here, re-reading the same
# `supported_lines` array the artifact producer's own served
# `support-matrix.md` already renders a table from — straight off this same
# `index.json`, in the same publish step that writes it, and naming that
# index's own sha256. Two generated copies of the same per-line facts can
# only drift, so this page links the served one instead of rendering its
# own; which lines are IN `supported_lines`, or whether the key is present
# at all, changes what the LINKED page says, never whether this page links
# to it — see --selftest, which proves the link is the same either way.
w("Per-line platform coverage and which lines are currently supported are rendered by the artifact producer directly from `index.json`, in the same publish step that writes it and naming that index's own sha256 — so they can never drift from what this page would otherwise have to restate: the [served support table](%s)." % support_table_url)
w("")
w("The exact ClickHouse patch each line is built from is in its artifact's `manifest.json` and in the served `index.json` (`clickhouse_version`); it moves with every upstream patch release, so it is not repeated here.")
w("Ask for a line, never a nearest match: `for(\"25.8\")` resolves the newest build of that line and fails if it is absent, rather than quietly handing back a neighbor whose answers differ.")
w("")
w("## ABI revisions")
w("")
w("An SDK build speaks exactly one ABI revision and refuses, at load, any artifact reporting a different one — naming both numbers. **Both revisions can be served on the same rolling channel at once**, including during a cutover, so which artifact build an installed SDK version actually needs is not always \"whatever `for()` resolves to\" any more; the two tables below answer that.")
w("")

# ABI revision each index row carries EXPLICITLY. Currently only the revision
# that introduced the field (5) is ever written; an absent field is older.
index_explicit_revisions = sorted(set(a["abi_revision"] for a in rows if "abi_revision" in a))

# Which revision(s) the git tags name that the index never writes explicitly.
# The producer's contract (per chtypes issue #73) is that the field starts
# at the revision that introduced it and is never written for anything
# older — so exactly one revision should be missing from the index's own
# vocabulary. More or fewer than one means that contract no longer holds and
# this script must not guess which one "absent" means.
implicit_revisions = sorted(set(version_revision.values()) - set(index_explicit_revisions))
if len(implicit_revisions) != 1:
    sys.exit("support-matrix: expected exactly one ABI revision that the index never writes explicitly "
             "(the producer's contract for an absent `abi_revision`), found %r — the tagged versions speak "
             "%r and the index explicitly names %r; this script refuses to guess which absent revision means "
             "which number" % (implicit_revisions, sorted(set(version_revision.values())), index_explicit_revisions))
implicit_revision = implicit_revisions[0]
all_revisions = sorted(set(index_explicit_revisions) | {implicit_revision})

w("### Which ABI revision an SDK version speaks")
w("")
w("Read from this repository's own release tags and `include/chtypes.h` as it stood at each — not typed here. All four bindings tag the same version number together and were checked to agree on the revision at every tag that exists for all four.")
w("")
w("| SDK version | Speaks ABI revision |")
w("|---|---|")
for v in sorted(version_revision, key=version_key):
    w("| `%s` | %d |" % (v, version_revision[v]))
w("")
w("### Which artifact build satisfies each revision, per ClickHouse line and platform")
w("")
w("Read from the live index: revision %d is whichever revision the served `abi_revision` field never names (see above); every other revision listed is read directly off that field. A cell reads **not published** when the index carries no row at all for that (line, platform, revision) combination at a revision the index writes explicitly — a gap a consumer already on that revision cannot work around. A cell in the Revision %d column reads **—** instead: the index carries no row there either, but some lines predate that baseline revision and some postdate it, and this script does not read build timestamps to guess which — it reports only that there is no such build, never a guess at what the build number would be or at why one is missing." % (implicit_revision, implicit_revision))
w("")

per_combo = {}
for a in rows:
    key = (a["clickhouse_minor"], "%s-%s" % (a["os"], a["arch"]))
    per_combo.setdefault(key, []).append(a)

def revision_of(a):
    return a["abi_revision"] if "abi_revision" in a else implicit_revision

# index.json's top-level `unbuildable` array (chtypes#150, chtypes#73) names
# every (os, arch, clickhouse_minor) pairing the artifact producer has
# decided will NEVER get a build — a permanent, by-design gap, not one a
# later regeneration fills. Only the FACT of exclusion is taken from it —
# which (line, platform) pairs — never its `reason` field: that string names
# internal issues in the private sibling repository and core repository's own
# infrastructure, and this repository is public. scripts/lint-public.sh
# cannot catch every possible spelling of that leak, so the rule is: do not
# read `reason` here, ever, for any purpose, and do not "fix" this to pass it
# through — the wording below is this script's own, derived only from which
# pairings are excluded.
excluded_pairs = set()
for e in doc.get("unbuildable", []):
    excluded_pairs.add((e["clickhouse_minor"], "%s-%s" % (e["os"], e["arch"])))

# Cells set while walking the table, purely so the closing summary below can
# describe what it saw without recomputing it — never used to decide what a
# cell itself renders.
dash_cells = []

def newest_build_cell(candidates, minor=None, plat=None, r=None):
    if candidates:
        with_build = [a for a in candidates if isinstance(a.get("build"), int)]
        if with_build:
            newest = max(with_build, key=lambda a: a["build"])
            return "`%d`" % newest["build"]
        return "*(pre-relink, no build number)*"
    if minor is not None and (minor, plat) in excluded_pairs:
        # A designed exclusion, not a gap that regenerating the index could
        # ever fill. Named by which OTHER platforms of this same line and
        # revision DO have a build — computed from the table's own data, not
        # a hardcoded "linux" — so this stays true if the excluded platform
        # or the surviving ones ever change.
        other_oses = sorted({
            pp.split("-", 1)[0] for pp in plat_order if pp != plat
            and (minor, pp) not in excluded_pairs
            and any(revision_of(a) == r for a in per_combo.get((minor, pp), []))
        })
        if other_oses:
            return "*(%s only, by design)*" % "/".join(other_oses)
        return "*(excluded by design — no platform in this table has a build at this revision for this line)*"
    if r is not None and r == implicit_revision:
        # No row at all for this (line, platform) at the baseline revision,
        # and not an excluded pairing either. chtypes#165: this is NOT the
        # same as a real gap (below) — a real gap is a revision the index
        # writes explicitly, where "no row" means a consumer on that revision
        # is stuck. Here, at the one revision the index never names, "no row"
        # is equally consistent with the line predating this baseline (never
        # rebuilt for it) or postdating it (never existed under it), and nothing
        # the index states says which. "not published" would assert the former
        # is coming; this script does not know that, and does not guess from
        # build timestamps (the trap chtypes#165 named) — so it says only that
        # there is nothing here.
        dash_cells.append((minor, plat))
        return "—"
    return "not published"

header_cells = ["Line", "Platform"] + ["Revision %d" % r for r in all_revisions]
w("| " + " | ".join(header_cells) + " |")
w("|" + "---|" * len(header_cells))
for minor in sorted(lines, key=order):
    for p in plat_order:
        group = per_combo.get((minor, p), [])
        cells = [newest_build_cell([a for a in group if revision_of(a) == r], minor, p, r) for r in all_revisions]
        w("| `%s` | `%s` | %s |" % (minor, p, " | ".join(cells)))
w("")

# Any line/platform pairing missing a build for a revision NEWER than the
# implicit baseline is a real gap a consumer on that revision cannot work
# around — call it out explicitly rather than let the table's "not published"
# cells speak for themselves. Computed from the same data as the table, not
# a name typed here: which pairing this is can and does change release to
# release. A pairing excluded BY DESIGN (above) already reads that way in its
# own cell and is never listed here too — this section is only for a gap
# nobody has explained.
gaps = []
for r in all_revisions:
    if r == implicit_revision:
        continue
    missing = [(minor, p) for minor in sorted(lines, key=order) for p in plat_order
               if newest_build_cell([a for a in per_combo.get((minor, p), []) if revision_of(a) == r], minor, p, r) == "not published"]
    if missing:
        gaps.append((r, missing))

if gaps:
    w("⚠️ Not every line/platform pairing above has a build for every revision. A gap here means a consumer already on that revision cannot load that line on that platform at all — pinning a different build will not help, because the index carries no such build:")
    w("")
    for r, missing in gaps:
        names = ", ".join("`%s` on `%s`" % (minor, p) for minor, p in missing)
        w("- **Revision %d**: no build for %s." % (r, names))
else:
    reasons = []
    if excluded_pairs:
        reasons.append("is excluded by design (marked above)")
    if dash_cells:
        reasons.append("reads **—** at revision %d, where the index carries no row for it at all and this script does not guess why (see above)" % implicit_revision)
    if reasons:
        w("Every line/platform pairing above either has a build for every revision the index writes explicitly, or " + ", or ".join(reasons) + " — never merely \"not yet published\" without one of those reasons.")
    else:
        w("Every line/platform pairing above has a build for every revision in this table.")

block = "\n".join(out)
BEGIN = "<!-- BEGIN GENERATED — scripts/support-matrix.sh; do not edit by hand -->"
END = "<!-- END GENERATED -->"
page = out_path.read_text(encoding="utf-8")
if BEGIN not in page or END not in page:
    sys.exit("support-matrix: %s has no generated block — expected the %s / %s markers"
             % (out_path, BEGIN, END))
head, rest = page.split(BEGIN, 1)
_, tail = rest.split(END, 1)
tmp_path.write_text(head + BEGIN + "\n\n" + block + "\n\n" + END + tail, encoding="utf-8")
print("support-matrix: %d line(s), %d platform(s), %d SDK version(s), %d ABI revision(s) from %s"
      % (len(lines), len(platforms), len(version_revision), len(all_revisions), url), file=sys.stderr)
PY

if [ "$CHECK" = 1 ]; then
  if diff -u "$OUT" "$WORK/out.md" > "$WORK/diff.txt"; then
    echo "support-matrix: $OUT is current"
    exit 0
  fi
  echo "support-matrix: $OUT is STALE — regenerate with scripts/support-matrix.sh" >&2
  sed -n '1,80p' "$WORK/diff.txt" >&2
  exit 1
fi
cp "$WORK/out.md" "$OUT"
echo "support-matrix: wrote $OUT"
