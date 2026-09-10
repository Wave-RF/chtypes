#!/usr/bin/env bash
# published-lines.sh — the newest -lts line and the newest -stable line the
# artifacts release publishes for one platform, read from the live index.json,
# and a key that changes exactly when those two rows do.
#
#   scripts/published-lines.sh [--platform <os-arch>] [--tag <t>]
#
# Prints `lines=<lts-minor> <stable-minor>` and `key=<sha256>`, one per line —
# the shape a GitHub Actions step appends to $GITHUB_OUTPUT — so CI never
# hard-codes a line: the release's own index is the authority on what exists,
# and a cache keyed on `key` is reused until the release changes either row.
# Which row was chosen, and why, goes to stderr.
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

PLATFORM="${CHTYPES_TARGET:-}"; TAG="artifacts"
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM="$2"; shift ;;
    --tag)      [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift ;;
    -h|--help)  sed -n '2,21p' "$0"; exit 0 ;;
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

python3 - "$WORK/index.json" "$PLATFORM" "$URL" <<'PY'
import hashlib, json, sys
path, platform, url = sys.argv[1:4]
doc = json.load(open(path, encoding="utf-8"))
if doc.get("schema") != 1:
    sys.exit("published-lines: index.json schema %r is not 1 — this script cannot read it" % doc.get("schema"))
os_, _, arch = platform.partition("-")
rows = [a for a in doc.get("artifacts", []) if a.get("os") == os_ and a.get("arch") == arch]
if not rows:
    sys.exit("published-lines: %s publishes nothing for %s" % (url, platform))

def release_order(a):
    return tuple(int(p) for p in a["clickhouse_minor"].split("."))

picked = []
for suffix in ("lts", "stable"):
    of_kind = [a for a in rows if a["clickhouse_version"].endswith("-" + suffix)]
    if not of_kind:
        sys.exit("published-lines: no -%s line is published for %s" % (suffix, platform))
    picked.append(max(of_kind, key=release_order))
for a in picked:
    print("published-lines: %s -> %s  (%s, sha256 %s…)" % (
        a["clickhouse_minor"], a["clickhouse_version"], a["file"], a["sha256"][:12]), file=sys.stderr)
key = hashlib.sha256(json.dumps(picked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
print("lines=%s" % " ".join(a["clickhouse_minor"] for a in picked))
print("key=%s" % key)
PY
