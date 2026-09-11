#!/usr/bin/env bash
# support-matrix.sh — regenerate the generated block of docs/support.md: which
# language versions each binding requires, which platforms the release ships,
# and which ClickHouse lines it publishes today.
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
# --check regenerates into a temporary file and diffs. It is meant to run
# NON-BLOCKING in CI. A blocking check here would redden this repository
# whenever core certifies a new ClickHouse line, which is a cross-repo event
# this repository cannot fix by itself — the same trade core made for
# fetch-fixtures-check, and for the same reason: stale prose is a smaller
# failure than a pipeline that stalls on someone else's commit.
#
# Environment: CHTYPES_ARTIFACTS_URL (default https://artifacts.wavehouse.dev).
set -euo pipefail
die() { echo "support-matrix: $*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/docs/support.md"
CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)   CHECK=1 ;;
    --out)     [ $# -ge 2 ] || die "--out needs a value"; OUT="$2"; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *)         die "unknown argument: $1" ;;
  esac
  shift
done
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"
[ -f "$OUT" ] || die "$OUT does not exist — the generated block is written into an existing page"

URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
URL="${URL%/}/artifacts/index.json"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-support.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --retry 3 --max-time 60 "$URL" -o "$WORK/index.json" \
  || die "CHTYPES_SOURCE_UNREACHABLE: could not fetch $URL"

python3 - "$HERE" "$WORK/index.json" "$OUT" "$WORK/out.md" "$URL" <<'PY'
import json, re, sys, pathlib

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

# Language minimums, from the files a build actually enforces.
go_min     = need(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", read("go/go.mod"), "go/go.mod's go directive")
py_min     = need(r'^requires-python\s*=\s*"[>=~^]*\s*([0-9.]+)"', read("python/pyproject.toml"), "python/pyproject.toml requires-python")
node_min   = need(r'"node"\s*:\s*"[>=~^]*\s*([0-9.]+)"', read("ts/package.json"), "ts/package.json engines.node")
rust_min   = need(r'^rust-version\s*=\s*"([0-9.]+)"', read("rust/Cargo.toml"), "rust/Cargo.toml rust-version")
rust_ed    = need(r'^edition\s*=\s*"([0-9]+)"', read("rust/Cargo.toml"), "rust/Cargo.toml edition")

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

w("### Languages")
w("")
w("| Binding | Package | Requires | Loads the artifact with |")
w("|---|---|---|---|")
w("| Go | `github.com/wave-rf/chtypes/go` | Go %s+ | cgo + `dlopen` |" % go_min)
w("| Python | `chtypes` | Python %s+ | stdlib `ctypes` (no build step, no dependencies) |" % py_min)
w("| TypeScript | `@wavehouse/chtypes` | Node %s+, ESM only | `ffi-rs` (prebuilt) |" % node_min)
w("| Rust | `chtypes` | Rust %s+ (edition %s) | `libloading` |" % (rust_min, rust_ed))
w("")
w("### Platforms")
w("")
w("The artifact is native code, so a platform is supported only if the release")
w("publishes a build for it. Today that is:")
w("")
for p in plat_order:
    note = " — development floor, not an oracle: its `long double` makes float parses diverge from a real server" \
           if p.startswith("darwin") else ""
    w("- `%s`%s" % (p, note))
w("")
w("Both loaders are `dlopen`, so all four bindings are Unix-only. There is no")
w("Windows artifact and no 32-bit build.")
w("")
w("### ClickHouse lines")
w("")
w("One artifact per ClickHouse line, each carrying that release's own C++. A")
w("line is supported when it has a committed run of record in the core")
w("repository and the release publishes it:")
w("")
w("| Line | Exact version | Platforms |")
w("|---|---|---|")
for minor in sorted(lines, key=order):
    e = lines[minor]
    have = [p for p in plat_order if p in e["platforms"]]
    marks = "all" if len(have) == len(plat_order) else ", ".join("`%s`" % p for p in have)
    w("| `%s` | `%s` | %s |" % (minor, e["exact"], marks))
w("")
w("Ask for a line, never a nearest match: `for(\"25.8\")` resolves the newest")
w("build of that line and fails if it is absent, rather than quietly handing")
w("back a neighbor whose answers differ.")

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
print("support-matrix: %d line(s), %d platform(s) from %s" % (len(lines), len(platforms), url), file=sys.stderr)
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
