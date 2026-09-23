#!/usr/bin/env bash
# support-matrix.sh — regenerate the generated block of docs/support.md: which
# language versions each binding requires, which platforms the release ships,
# which ClickHouse lines it publishes today, and — the SDK-version -> ABI
# revision -> artifact-build mapping a consumer needs after a load-time
# refusal.
#
#   scripts/support-matrix.sh [--check] [--out <file>]
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
# Environment: CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev).
# Needs full git history and tags (fetch-depth: 0 / an unshallow, un-single-
# branch clone) for the ABI-revision half — a shallow checkout dies loudly
# with what to run rather than silently omitting the table.
set -euo pipefail
die() { echo "support-matrix: $*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/docs/support.md"
CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK=1 ;;
    --out)     [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
  shift
done
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"
command -v git >/dev/null 2>&1 || die "git is not on PATH"
[ -f "$OUT" ] || die "$OUT does not exist — the generated block is written into an existing page"

URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
URL="${URL%/}/artifacts/index.json"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-support.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --retry 3 --max-time 60 "$URL" -o "$WORK/index.json" \
  || die "CHTYPES_SOURCE_UNREACHABLE: could not fetch $URL"

python3 - "$HERE" "$WORK/index.json" "$OUT" "$WORK/out.md" "$URL" <<'PY'
import json, re, subprocess, sys, pathlib

root, index_path, out_path, tmp_path, url = (pathlib.Path(sys.argv[1]), sys.argv[2],
                                             pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4]), sys.argv[5])

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
    entry = lines.setdefault(minor, {"exact": a["clickhouse_version"], "platforms": set()})
    entry["platforms"].add(plat)
    # The index keeps every patch row ever published; the newest one names the line.
    if tuple(int(p) for p in re.findall(r"\d+", a["clickhouse_version"])) > \
       tuple(int(p) for p in re.findall(r"\d+", entry["exact"])):
        entry["exact"] = a["clickhouse_version"]

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
    note = " — see [macOS is a development floor, not an oracle](limitations.md#macos-is-a-development-floor-not-an-oracle)" \
           if p.startswith("darwin") else ""
    w("- `%s`%s" % (p, note))
w("")
w("Both loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows artifact and no 32-bit build.")
w("")
w("## ClickHouse lines")
w("")
w("One artifact per ClickHouse line, each carrying that release's own C++. A line is supported when it has a committed run of record in the core repository and the release publishes it:")
w("")
w("| Line | Exact version | Platforms |")
w("|---|---|---|")
for minor in sorted(lines, key=order):
    e = lines[minor]
    have = [p for p in plat_order if p in e["platforms"]]
    marks = "all" if len(have) == len(plat_order) else ", ".join("`%s`" % p for p in have)
    w("| `%s` | `%s` | %s |" % (minor, e["exact"], marks))
w("")
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
w("Read from the live index: revision %d is whichever revision the served `abi_revision` field never names (see above); every other revision listed is read directly off that field. A cell reads **not published** when the index carries no row at all for that (line, platform, revision) combination — never a guess at what the build number would be." % implicit_revision)
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
elif excluded_pairs:
    w("Every line/platform pairing above either has a build for every revision in this table, or is excluded by design (marked above) rather than merely not yet built.")
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
