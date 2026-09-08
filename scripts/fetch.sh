#!/usr/bin/env bash
# fetch.sh — one published artifact, verified, installed where the loader looks.
#
#   scripts/fetch.sh 25.8                              latest release, cache dest
#   scripts/fetch.sh 25.8.28.1-lts --platform linux-amd64
#   scripts/fetch.sh 25.8 --dest ./chtypes-artifacts   an explicit registry dir
#   scripts/fetch.sh --all --platform linux-amd64 --dest /opt/chtypes/artifacts   every line, for a container
#   scripts/fetch.sh 25.8 --tag chtypes-v1.2.0         a specific release
#   scripts/fetch.sh 25.8 --url https://…/download/x   any base URL (or a local dir)
#
# (--out is an alias for --dest, and --all takes every line the release
# publishes for the platform; both are the spellings .github/workflows use.)
#
# Installs into <dest>/<clickhouse_minor>/, which is exactly the layout
# chtypes.NewRegistry scans: one directory per version, each holding the
# manifest.json that names the library to dlopen. Point a Registry at <dest>
# after this and the version is live.
#
# The verification chain, in order, because a fetch that installs a corrupted
# 300 MB library and reports success is the worst outcome available here:
#
#   1. index.json   names the asset and records its sha256
#   2. SHA256SUMS   independently records the same sha256 — the two must agree
#   3. the tarball  is hashed BEFORE it is unpacked; a mismatch aborts
#   4. manifest.json inside it names the library and its sha256; the installed
#      library is re-hashed after the move, in place
#
# This script is the SDK's, and stands alone: no cache directories need to
# exist, nothing else in this repository is required, `gh` is optional (plain
# curl against the release URL is the fallback), and the release's own
# index.json is the authority on what exists. It installs into the per-user
# artifact cache every SDK here defaults to —
# ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/<minor>/ — which is
# also where a core-repository build lands, so one directory serves both.
#
# Environment:
#   CHTYPES_RELEASE_REPO   owner/name to fetch from (default: gh's inference)
#   CHTYPES_TARGET         platform key (default: this host's own <os>-<arch>)
#   XDG_CACHE_HOME         cache root (default ~/.cache)
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes"

usage() { sed -n '2,12p' "$0"; exit 2; }
die()   { echo "fetch.sh: $*" >&2; exit 1; }
say()   { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

# ------------------------------------------------------------------ arguments
SPELLING=""; PLATFORM=""; DEST=""; TAG=""; BASE_URL=""
REPO="${CHTYPES_RELEASE_REPO:-}"; FORCE=0; ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || die "--platform needs a value"; PLATFORM="$2"; shift ;;
    # --out is the spelling the CI workflows use (.github/workflows/rigs.yml,
    # verify.yml); --dest is this script's own. They are the same flag.
    --dest|--out) [ $# -ge 2 ] || die "$1 needs a value";       DEST="$2"; shift ;;
    --tag)      [ $# -ge 2 ] || die "--tag needs a value";      TAG="$2"; shift ;;
    --url)      [ $# -ge 2 ] || die "--url needs a value";      BASE_URL="$2"; shift ;;
    --repo)     [ $# -ge 2 ] || die "--repo needs a value";     REPO="$2"; shift ;;
    --all)      ALL=1 ;;
    --force)    FORCE=1 ;;
    -h|--help)  usage ;;
    -*)         die "unknown flag: $1" ;;
    *)          [ -z "$SPELLING" ] || die "more than one version given ($SPELLING, $1)"; SPELLING="$1" ;;
  esac
  shift
done
if [ "$ALL" = 1 ]; then
  # --all takes every line the release publishes for the platform. Which lines
  # exist is a question index.json answers, so a caller never restates the list.
  [ -z "$SPELLING" ] || die "--all installs every published line; drop the version argument ($SPELLING)"
  SPELLING="--all"
fi
[ -n "$SPELLING" ] || { echo "fetch.sh: a ClickHouse version spelling is required (or --all)" >&2; usage; }
[ -n "$BASE_URL" ] && [ -n "$TAG" ] && die "--url and --tag name two different sources; pass one"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# ------------------------------------------------------- platform and dest
# The platform defaults to THIS host's own <os>-<arch> (CHTYPES_TARGET
# overrides it): a consumer fetches the library its process will dlopen. A
# fetch for another platform — Linux artifacts on a Mac, for a container — is
# legitimate and is printed loudly so it is never mistaken for a native one.
HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in arm64|aarch64) HOST_ARCH=arm64 ;; x86_64|amd64) HOST_ARCH=amd64 ;; *) HOST_ARCH="$(uname -m)" ;; esac
if [ -z "$PLATFORM" ]; then
  PLATFORM="${CHTYPES_TARGET:-$HOST_OS-$HOST_ARCH}"
fi
case "$PLATFORM" in
  linux-arm64|linux-amd64|darwin-arm64|darwin-amd64) ;;
  *) die "not a known platform key: $PLATFORM (linux|darwin)-(arm64|amd64)" ;;
esac
if [ "${PLATFORM%%-*}" != "$HOST_OS" ]; then
  echo "fetch.sh: note — fetching $PLATFORM artifacts on a $HOST_OS host." >&2
  echo "          They are for a $PLATFORM process (a container, usually), not this one." >&2
  echo "          Pass --platform $HOST_OS-$HOST_ARCH for a library this host can dlopen." >&2
fi

# The per-user cache, keyed by platform — the one directory every SDK's
# NewRegistry / Registry / default_registry_dir agrees on.
[ -n "$DEST" ] || DEST="$CACHE_ROOT/artifacts/$PLATFORM"
mkdir -p "$DEST"

# ------------------------------------------------------- resolve the version
# resolve-version.py is the single place that knows that 25.8, 25.8.28.1,
# v25.8.28.1-lts and a Docker digest are three views of ONE record (its
# docstring records the bug that cost a whole scoring column). Use it when it is
# available; when it is not — bare runner, no uv — fall back to normalising the
# spelling here and let the release's index.json be the authority on what exists.
WANT_LINE=""; WANT_EXACT=""
# Did the CALLER name a patch, or a line? It matters, and resolve-version.py
# cannot answer it: asked for the line `25.8` it helpfully expands to the patch
# the moving tag points at today (25.8.30.16-lts here), which is often NEWER
# than the patch that was built and published. Expanding a line, then treating
# the expansion as a hard requirement, would refuse a perfectly good release;
# treating a patch the caller typed as a soft preference would silently install
# a different one. So the caller's own spelling decides which it is.
STRICT_EXACT=0
case "$(printf '%s' "${SPELLING#v}" | sed -E 's/-(lts|stable|prestable|testing)$//')" in
  *.*.*.*) STRICT_EXACT=1 ;;
esac
if [ "$ALL" = 1 ]; then
  # Nothing to resolve: index.json is the list.
  WANT_LINE="every published line"; STRICT_EXACT=0
elif command -v uv >/dev/null 2>&1 && [ -f "${CHTYPES_CORE_DIR:-$ROOT/../chtypes-core}/ci/resolve-version.py" ]; then
  # A developer with the core repository beside this one gets its full
  # version resolver (Docker digests, moving tags); a consumer without it gets
  # the local normalisation below, and index.json is the authority either way.
  RESOLVED="$(uv run --no-project python "${CHTYPES_CORE_DIR:-$ROOT/../chtypes-core}/ci/resolve-version.py" "$SPELLING" 2>/dev/null || true)"
  if [ -n "$RESOLVED" ]; then
    IFS='|' read -r WANT_LINE WANT_EXACT <<EOF
$(printf '%s' "$RESOLVED" | python3 -c 'import json,sys
d = json.load(sys.stdin)
ex = d.get("exact") or ""
ch = d.get("channel") or ""
print("%s|%s" % (d["line"], ("%s-%s" % (ex, ch)) if ex and ch else ex))')
EOF
  fi
fi
if [ -z "$WANT_LINE" ]; then
  # v25.8.28.1-lts -> line 25.8, exact 25.8.28.1-lts;  25.8 -> line 25.8 only.
  S="${SPELLING#v}"
  BARE="$(printf '%s' "$S" | sed -E 's/-(lts|stable|prestable|testing)$//')"
  case "$BARE" in
    *.*.*.*) WANT_EXACT="$S" ;;
    *)       WANT_EXACT="" ;;
  esac
  WANT_LINE="$(printf '%s' "$BARE" | cut -d. -f1,2)"
  [ -n "$WANT_LINE" ] || die "cannot make a ClickHouse version out of '$SPELLING'"
  echo "fetch.sh: resolve-version.py unavailable; taking '$SPELLING' as line $WANT_LINE" >&2
fi

# ------------------------------------------------------------ where to fetch
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-fetch-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

SOURCE_KIND=""
if [ -n "$BASE_URL" ]; then
  SOURCE_KIND=url
elif command -v gh >/dev/null 2>&1; then
  SOURCE_KIND=gh
else
  SOURCE_KIND=http
fi
if [ "$SOURCE_KIND" != url ] && [ -z "$REPO" ]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  [ -n "$REPO" ] || die "cannot tell which GitHub repo to fetch from — pass --repo owner/name, set CHTYPES_RELEASE_REPO, or use --url"
fi

get_file() { # get_file <asset-name> <destination>
  local name="$1" out="$2" src
  case "$SOURCE_KIND" in
    url)
      case "$BASE_URL" in
        file://*) src="${BASE_URL#file://}/$name"
                  [ -f "$src" ] || return 1
                  cp "$src" "$out" ;;
        http://*|https://*)
                  curl -fsSL --retry 3 --retry-delay 1 -o "$out" "$BASE_URL/$name" ;;
        *)        src="$BASE_URL/$name"
                  [ -f "$src" ] || return 1
                  cp "$src" "$out" ;;
      esac ;;
    gh)
      # No --tag means the latest release, which is gh's own default here.
      local args=(--repo "$REPO" --pattern "$name" --dir "$(dirname "$out")" --clobber)
      if [ -n "$TAG" ]; then gh release download "$TAG" "${args[@]}" >/dev/null
      else gh release download "${args[@]}" >/dev/null; fi
      [ -f "$(dirname "$out")/$name" ] || return 1
      [ "$(dirname "$out")/$name" = "$out" ] || mv "$(dirname "$out")/$name" "$out" ;;
    http)
      if [ -n "$TAG" ]; then
        curl -fsSL --retry 3 --retry-delay 1 -o "$out" \
          "https://github.com/$REPO/releases/download/$TAG/$name"
      else
        curl -fsSL --retry 3 --retry-delay 1 -o "$out" \
          "https://github.com/$REPO/releases/latest/download/$name"
      fi ;;
  esac
}

SOURCE_DESC="$BASE_URL"
[ "$SOURCE_KIND" = url ] || SOURCE_DESC="$REPO@${TAG:-latest} (via $SOURCE_KIND)"
if [ "$ALL" = 1 ]; then say "every published ClickHouse line, $PLATFORM -> $DEST"
else say "ClickHouse $SPELLING -> line $WANT_LINE${WANT_EXACT:+ (exact $WANT_EXACT)}, $PLATFORM"; fi
say "source $SOURCE_DESC"

# --------------------------------------------------------------- pick the asset
get_file index.json "$WORK/index.json" || die "no index.json at $SOURCE_DESC"
# The selection runs in its own assignment rather than inside the here-doc that
# feeds `read`: a here-doc nested inside a command substitution inside a
# here-doc parses, but it hides python's own error message, and that message is
# the useful half when a release simply has no artifact for what was asked.
SELECTED="$(python3 - "$WORK/index.json" "${PLATFORM%%-*}" "${PLATFORM##*-}" "$WANT_LINE" "$WANT_EXACT" "$STRICT_EXACT" "$ALL" <<'PY'
import json, sys
path, os_, arch, line, exact, strict, want_all = sys.argv[1:]
strict, want_all = strict == "1", want_all == "1"
doc = json.load(open(path))
if doc.get("schema") != 1:
    sys.exit("index.json schema %r is not 1 — this fetch.sh cannot read it" % doc.get("schema"))
arts = [a for a in doc["artifacts"] if a["os"] == os_ and a["arch"] == arch]
if not arts:
    sys.exit("the release has nothing for %s-%s (it has: %s)"
             % (os_, arch, ", ".join(sorted({"%s-%s" % (a["os"], a["arch"])
                                             for a in doc["artifacts"]})) or "nothing"))

def vkey(a):
    return [int(p) for p in a["clickhouse_version"].split("-")[0].split(".")]

if want_all:
    # One per minor line — a release should not carry two patches of a line, but
    # if it does, the newer one is the one to install.
    best = {}
    for a in arts:
        cur = best.get(a["clickhouse_minor"])
        if cur is None or vkey(a) > vkey(cur):
            best[a["clickhouse_minor"]] = a
    hit = sorted(best.values(), key=lambda a: [int(p) for p in a["clickhouse_minor"].split(".")])
else:
    hit = [a for a in arts if exact and a["clickhouse_version"] == exact]
    if strict and not hit:
        sys.exit("you asked for exactly ClickHouse %s on %s-%s and this release does "
                 "not publish it (it has: %s).\n"
                 "          Ask for the line (%s) to take what was published."
                 % (exact, os_, arch, ", ".join(a["clickhouse_version"] for a in arts), line))
    if not hit:
        hit = [a for a in arts if a["clickhouse_minor"] == line]
    if not hit:
        sys.exit("no artifact for ClickHouse line %s on %s-%s (it has: %s)"
                 % (line, os_, arch, ", ".join(a["clickhouse_version"] for a in arts)))
    # More than one patch on a line can only happen if a release shipped two;
    # take the newest by version number rather than by list order.
    hit = [sorted(hit, key=vkey)[-1]]

for a in hit:
    for k in ("file", "sha256", "bytes", "clickhouse_version", "clickhouse_minor", "library", "library_sha256"):
        if not a.get(k):
            sys.exit("index.json entry for %s is missing %s" % (a.get("file"), k))
    print("|".join([a["file"], a["sha256"], str(a["bytes"]), a["clickhouse_version"],
                    a["clickhouse_minor"], a["library"], a["library_sha256"]]))
PY
)" || die "index.json has nothing to install for this request"
[ -n "$SELECTED" ] || die "could not select anything from index.json (see the message above)"

# SHA256SUMS is release-level: fetched once, whether one version is being
# installed or all of them.
get_file SHA256SUMS "$WORK/SHA256SUMS" || die "no SHA256SUMS at $SOURCE_DESC"

# install_one <row> — one index.json row, from the release to <dest>/<minor>/.
install_one() {
  local ASSET ASSET_SHA ASSET_BYTES A_VER A_MINOR A_LIB A_LIBSHA
  IFS='|' read -r ASSET ASSET_SHA ASSET_BYTES A_VER A_MINOR A_LIB A_LIBSHA <<EOF
$1
EOF
  [ -n "$ASSET" ] && [ -n "$ASSET_SHA" ] && [ -n "$A_LIBSHA" ] \
    || die "malformed index row: $1"
  say "$ASSET  ($ASSET_BYTES bytes, ClickHouse $A_VER, library $A_LIB)"
  if [ "$ALL" = 0 ] && [ -n "$WANT_EXACT" ] && [ "$A_VER" != "$WANT_EXACT" ]; then
    echo "fetch.sh: note — line $WANT_LINE points at $WANT_EXACT upstream today;" >&2
    echo "          this release publishes $A_VER for that line, and that is what was installed." >&2
  fi

  local INSTALL="$DEST/$A_MINOR"

  # Already installed and intact? Say so instead of re-downloading 300 MB. The
  # test is the same one the install path ends with, so "already there" is a
  # verified claim and not an assumption about a directory's existence.
  local HAVE
  if [ "$FORCE" = 0 ] && [ -f "$INSTALL/manifest.json" ] && [ -f "$INSTALL/$A_LIB" ]; then
    HAVE="$(sha256_of "$INSTALL/$A_LIB")"
    if [ "$HAVE" = "$A_LIBSHA" ]; then
      say "already installed and verified: $INSTALL/$A_LIB"
      echo "$INSTALL"
      return 0
    fi
    echo "fetch.sh: $INSTALL/$A_LIB is present but hashes $HAVE (want $A_LIBSHA) — replacing" >&2
  fi

  # ---------------------------------------- the tarball, hashed before unpacking
  local SUMS_SHA
  SUMS_SHA="$(awk -v f="$ASSET" '$2 == f || $2 == "*" f {print $1}' "$WORK/SHA256SUMS" | head -1)"
  [ -n "$SUMS_SHA" ] || die "SHA256SUMS has no line for $ASSET"
  [ "$SUMS_SHA" = "$ASSET_SHA" ] \
    || die "index.json says $ASSET is $ASSET_SHA but SHA256SUMS says $SUMS_SHA — the release disagrees with itself; do not install it"

  say "downloading $ASSET"
  rm -f "$WORK/$ASSET"
  get_file "$ASSET" "$WORK/$ASSET" || die "could not download $ASSET from $SOURCE_DESC"
  local GOT_BYTES GOT_SHA
  GOT_BYTES="$(wc -c < "$WORK/$ASSET" | tr -d ' ')"
  GOT_SHA="$(sha256_of "$WORK/$ASSET")"
  [ "$GOT_BYTES" = "$ASSET_BYTES" ] || die "$ASSET is $GOT_BYTES bytes, index.json says $ASSET_BYTES"
  [ "$GOT_SHA" = "$ASSET_SHA" ] \
    || die "$ASSET FAILED its sha256: got $GOT_SHA, want $ASSET_SHA — NOT unpacking it"
  say "sha256 verified before unpacking: $GOT_SHA"

  # ----------------------------------------------------------------- install
  local UNPACK="$WORK/unpack.$A_MINOR"
  rm -rf "$UNPACK"; mkdir -p "$UNPACK"
  tar -xzf "$WORK/$ASSET" -C "$UNPACK"
  [ -f "$UNPACK/manifest.json" ] || die "$ASSET contains no manifest.json at its root"
  local MANIFEST M_LIB M_VER M_MINOR M_SHA
  # clickhouse_minor is absent from the first Linux artifacts (they predate the
  # field); deriving it from clickhouse_version is what minorOf() in
  # go/chtypes/multiversion.go does, so the install path agrees with the loader.
  MANIFEST="$(python3 - "$UNPACK/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
ver = m.get("clickhouse_version") or ""
minor = m.get("clickhouse_minor") or ".".join(ver.split(".")[:2])
for k in ("library", "library_sha256"):
    if not m.get(k):
        sys.exit("manifest.json inside the tarball is missing %s" % k)
print("|".join([m["library"], ver, minor, m["library_sha256"]]))
PY
)" || die "manifest.json inside $ASSET is not usable"
  IFS='|' read -r M_LIB M_VER M_MINOR M_SHA <<EOF
$MANIFEST
EOF
  [ -n "$M_LIB" ] || die "manifest.json inside $ASSET did not yield a library name"
  # The index is a convenience; the manifest is the artifact's own claim about
  # itself. They must agree, or the index was built from a different artifact.
  [ "$M_LIB" = "$A_LIB" ] && [ "$M_VER" = "$A_VER" ] && [ "$M_MINOR" = "$A_MINOR" ] && [ "$M_SHA" = "$A_LIBSHA" ] \
    || die "manifest.json inside $ASSET disagrees with index.json ($M_LIB/$M_VER/$M_MINOR vs $A_LIB/$A_VER/$A_MINOR)"
  [ -f "$UNPACK/$M_LIB" ] || die "$ASSET names library $M_LIB but does not contain it"

  # Move into place through a sibling temp directory: an interrupted install must
  # never leave a half-populated <minor>/ for NewRegistry to dlopen.
  mkdir -p "$DEST"
  local NEWDIR="$DEST/.$A_MINOR.incoming.$$" OLDDIR="$DEST/.$A_MINOR.replaced.$$"
  rm -rf "$NEWDIR" "$OLDDIR"
  mv "$UNPACK" "$NEWDIR"
  if [ -e "$INSTALL" ]; then mv "$INSTALL" "$OLDDIR"; fi
  mv "$NEWDIR" "$INSTALL"
  rm -rf "$OLDDIR"
  rm -f "$WORK/$ASSET"

  # --------------------------------------------------- verify where it landed
  # Re-hash the installed file, in place. Everything up to here proves the bytes
  # were right somewhere else.
  local FINAL_SHA
  FINAL_SHA="$(sha256_of "$INSTALL/$M_LIB")"
  if [ "$FINAL_SHA" != "$M_SHA" ]; then
    die "installed $INSTALL/$M_LIB hashes $FINAL_SHA, manifest says $M_SHA — the install is bad"
  fi
  say "installed and verified"
  printf '    %s\n' "$INSTALL/" >&2
  ( cd "$INSTALL" && ls -l ) >&2
  printf '    %s sha256 %s\n' "$M_LIB" "$FINAL_SHA" >&2
  echo "    ClickHouse $M_VER — chtypes.NewRegistry(\"$DEST\") will now serve $M_MINOR" >&2
  echo "$INSTALL"
}

INSTALLED=0
while IFS= read -r row; do
  [ -n "$row" ] || continue
  install_one "$row"
  INSTALLED=$((INSTALLED + 1))
done <<EOF
$SELECTED
EOF
[ "$INSTALLED" -gt 0 ] || die "index.json selected nothing to install"
if [ "$ALL" = 1 ]; then say "$INSTALLED version(s) installed into $DEST"; fi
