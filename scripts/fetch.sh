#!/usr/bin/env bash
# fetch.sh — one published artifact, verified, installed where the loader looks.
#
#   scripts/fetch.sh 25.8                              latest release, cache dest
#   scripts/fetch.sh 25.8.28.1-lts --platform linux-amd64
#   scripts/fetch.sh 25.8 --dest ./chtypes-artifacts   an explicit registry dir
#   scripts/fetch.sh --all --platform linux-amd64 --dest /opt/chtypes/artifacts   every line, for a container
#   scripts/fetch.sh 25.8 --tag v1.2.0                 a specific release (default: the
#                                                      rolling `artifacts` release)
#   scripts/fetch.sh 25.8 --url https://…/download/x   any base URL (or a local dir)
#   scripts/fetch.sh 25.8 --repo owner/name            a GitHub Releases source instead
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
#   0. SHA256SUMS.sig  an ed25519 signature over the exact bytes of SHA256SUMS,
#                      verified under a trusted key BEFORE anything else is read
#                      (docs/fetch.md §4): an unsigned or mis-signed release is
#                      CHTYPES_ARTIFACT_UNTRUSTED, never downloaded around
#   1. index.json   names the asset and records its sha256
#   2. SHA256SUMS   — now known authentic — records the same sha256; the two must agree
#   3. the tarball  is hashed BEFORE it is unpacked; a mismatch aborts
#   4. manifest.json inside it names the library and its sha256; the installed
#      library is re-hashed after the move, in place
#
# Exit codes (docs/fetch.md §6), and the §7 code every failure message names:
#   0 ok · 1 verification failed (CHTYPES_ARTIFACT_UNTRUSTED, _CORRUPT) · 2 usage ·
#   3 source unreachable (CHTYPES_SOURCE_UNREACHABLE) · 4 not published for this
#   platform/line (CHTYPES_ARTIFACT_UNPUBLISHED).
#
# This script is the SDK's, and stands alone: no cache directories need to
# exist, nothing else in this repository is required, `gh` is optional (plain
# curl against the release URL is the fallback), and the release's own
# index.json is the authority on what exists. It installs into the per-user
# artifact cache every SDK here defaults to —
# ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/<minor>/ — which is
# also where a core-repository build lands, so one directory serves both.
#
# Where it fetches from, by default: https://artifacts.wavehouse.dev/<tag>/ —
# the public artifacts host, where <tag> is a release tag or the rolling
# `artifacts` release. `--url` names any other base (a mirror, a local
# directory, a file:// path); `--repo owner/name` (or CHTYPES_RELEASE_REPO)
# switches to that repository's GitHub Releases, which is how the core
# repository's own CI fetches what it just published.
#
# Environment:
#   CHTYPES_ARTIFACTS_URL  the artifacts host (default https://artifacts.wavehouse.dev)
#   CHTYPES_RELEASE_REPO   owner/name: fetch from GitHub Releases instead
#   CHTYPES_TARGET         platform key (default: this host's own <os>-<arch>)
#   XDG_CACHE_HOME         cache root (default ~/.cache)
#   CHTYPES_TRUSTED_KEYS   hex ed25519 public key(s), comma-separated. REPLACES the
#                          embedded release key — for a mirror signed by someone
#                          else, or spec/fixtures/fetch's test key
#   CHTYPES_ALLOW_UNSIGNED 1 skips step 0 with one loud warning naming the source.
#                          Never the default; never silent
#   CHTYPES_DOWNLOAD_TOKEN sent as a bearer token to the artifacts host (optional)
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPTS")"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/chtypes"

usage() { sed -n '2,12p' "$0"; exit 2; }
die()   { echo "fetch.sh: $*" >&2; exit 1; }
bad_usage() { echo "fetch.sh: $*" >&2; exit 2; }
# fail <CHTYPES_…> <message> — every failure names its §7 code and exits with
# its §6 number, so a caller can tell "the host is down" from "the release is
# forged" from "nothing for this platform" without parsing prose.
fail() {
  local code="$1"; shift
  echo "fetch.sh: $code: $*" >&2
  case "$code" in
    CHTYPES_SOURCE_UNREACHABLE)   exit 3 ;;
    CHTYPES_ARTIFACT_UNPUBLISHED) exit 4 ;;
    *)                            exit 1 ;;
  esac
}
say()   { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

# ------------------------------------------------------------------ arguments
SPELLING=""; PLATFORM=""; DEST=""; TAG=""; BASE_URL=""
REPO="${CHTYPES_RELEASE_REPO:-}"; FORCE=0; ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || bad_usage "--platform needs a value"; PLATFORM="$2"; shift ;;
    # --out is the spelling the CI workflows use (.github/workflows/rigs.yml,
    # verify.yml); --dest is this script's own. They are the same flag.
    --dest|--out) [ $# -ge 2 ] || bad_usage "$1 needs a value";       DEST="$2"; shift ;;
    --tag)      [ $# -ge 2 ] || bad_usage "--tag needs a value";      TAG="$2"; shift ;;
    --url)      [ $# -ge 2 ] || bad_usage "--url needs a value";      BASE_URL="$2"; shift ;;
    --repo)     [ $# -ge 2 ] || bad_usage "--repo needs a value";     REPO="$2"; shift ;;
    --all)      ALL=1 ;;
    --force)    FORCE=1 ;;
    -h|--help)  usage ;;
    -*)         bad_usage "unknown flag: $1" ;;
    *)          [ -z "$SPELLING" ] || bad_usage "more than one version given ($SPELLING, $1)"; SPELLING="$1" ;;
  esac
  shift
done
if [ "$ALL" = 1 ]; then
  # --all takes every line the release publishes for the platform. Which lines
  # exist is a question index.json answers, so a caller never restates the list.
  [ -z "$SPELLING" ] || bad_usage "--all installs every published line; drop the version argument ($SPELLING)"
  SPELLING="--all"
fi
[ -n "$SPELLING" ] || { echo "fetch.sh: a ClickHouse version spelling is required (or --all)" >&2; usage; }
[ -n "$BASE_URL" ] && [ -n "$TAG" ] && bad_usage "--url names a full base; --tag selects a release on the artifacts host (or in --repo) — pass one"

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
  *) bad_usage "not a known platform key: $PLATFORM (linux|darwin)-(arm64|amd64)" ;;
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
elif command -v uv >/dev/null 2>&1 && [ -f "${CHTYPES_CORE_DIR:-$ROOT/../core}/ci/resolve-version.py" ]; then
  # A developer with the core repository beside this one gets its full
  # version resolver (Docker digests, moving tags); a consumer without it gets
  # the local normalisation below, and index.json is the authority either way.
  RESOLVED="$(uv run --no-project python "${CHTYPES_CORE_DIR:-$ROOT/../core}/ci/resolve-version.py" "$SPELLING" 2>/dev/null || true)"
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
  [ -n "$WANT_LINE" ] || bad_usage "cannot make a ClickHouse version out of '$SPELLING'"
  echo "fetch.sh: resolve-version.py unavailable; taking '$SPELLING' as line $WANT_LINE" >&2
fi

# ------------------------------------------------------------ where to fetch
WORK="$(mktemp -d "${TMPDIR:-/tmp}/chtypes-fetch-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# No --url and no repository named: the public artifacts host, under the
# requested tag or the rolling release. A repository (--repo / the env) means
# GitHub Releases; an explicit --url is taken as given.
ARTIFACTS_URL="${CHTYPES_ARTIFACTS_URL:-https://artifacts.wavehouse.dev}"
if [ -z "$BASE_URL" ] && [ -z "$REPO" ]; then
  BASE_URL="${ARTIFACTS_URL%/}/${TAG:-artifacts}"
fi
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
  [ -n "$REPO" ] || bad_usage "cannot tell which GitHub repo to fetch from — pass --repo owner/name, set CHTYPES_RELEASE_REPO, or use --url"
fi

get_file() { # get_file <asset-name> <destination>  ->  0 fetched · 1 absent (404, no such file) · 2 unreachable
  # "Absent" and "unreachable" are different verdicts: a release with no
  # SHA256SUMS.sig is UNTRUSTED, a host that cannot be reached is not.
  local name="$1" out="$2" src code
  rm -f "$out"
  case "$SOURCE_KIND" in
    url)
      case "$BASE_URL" in
        http://*|https://*)
          code="$(curl -sSL --retry 3 --retry-delay 1 -o "$out" -w '%{http_code}' \
                    ${CHTYPES_DOWNLOAD_TOKEN:+-H "Authorization: Bearer $CHTYPES_DOWNLOAD_TOKEN"} \
                    "$BASE_URL/$name" 2>"$WORK/.curl.err")" || code="000"
          case "$code" in
            200) return 0 ;;
            404) rm -f "$out"; return 1 ;;
            *)   rm -f "$out"; echo "fetch.sh: $BASE_URL/$name -> HTTP $code $(tr -d '\n' < "$WORK/.curl.err")" >&2; return 2 ;;
          esac ;;
        *)
          src="${BASE_URL#file://}"
          [ -d "$src" ] || return 2
          [ -f "$src/$name" ] || return 1
          cp "$src/$name" "$out" ;;
      esac ;;
    gh)
      # No --tag means the latest release, which is gh's own default here.
      local args=(--repo "$REPO" --pattern "$name" --dir "$(dirname "$out")" --clobber)
      if [ -n "$TAG" ]; then gh release download "$TAG" "${args[@]}" >/dev/null 2>&1 || true
      else gh release download "${args[@]}" >/dev/null 2>&1 || true; fi
      [ -f "$(dirname "$out")/$name" ] || return 1
      [ "$(dirname "$out")/$name" = "$out" ] || mv "$(dirname "$out")/$name" "$out" ;;
    http)
      if [ -n "$TAG" ]; then src="https://github.com/$REPO/releases/download/$TAG/$name"
      else src="https://github.com/$REPO/releases/latest/download/$name"; fi
      code="$(curl -sSL --retry 3 --retry-delay 1 -o "$out" -w '%{http_code}' "$src" 2>"$WORK/.curl.err")" || code="000"
      case "$code" in
        200) return 0 ;;
        404) rm -f "$out"; return 1 ;;
        *)   rm -f "$out"; echo "fetch.sh: $src -> HTTP $code $(tr -d '\n' < "$WORK/.curl.err")" >&2; return 2 ;;
      esac ;;
  esac
}

SOURCE_DESC="$BASE_URL"
[ "$SOURCE_KIND" = url ] || SOURCE_DESC="$REPO@${TAG:-latest} (via $SOURCE_KIND)"
if [ "$ALL" = 1 ]; then say "every published ClickHouse line, $PLATFORM -> $DEST"
else say "ClickHouse $SPELLING -> line $WANT_LINE${WANT_EXACT:+ (exact $WANT_EXACT)}, $PLATFORM"; fi
say "source $SOURCE_DESC"

# --------------------------------------------------------- step 0: the signature
# docs/fetch.md §3 step 0 and §4. SHA256SUMS is fetched first and NOTHING — not
# index.json — is read until its ed25519 signature verifies under a trusted
# key: the embedded release key, or exactly the keys CHTYPES_TRUSTED_KEYS
# names. The verifier is RFC 8032 in stdlib Python, because this script stands
# alone and python3 ships no ed25519. CHTYPES_ALLOW_UNSIGNED=1 skips the step
# with one loud warning naming the source, and is never the default.
RELEASE_PUBLIC_KEY="fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc"   # key id deb275922dbff76e
verify_signature() { # verify_signature <SHA256SUMS> <SHA256SUMS.sig> -> prints the verdict; exit 0 iff a trusted key verifies
  python3 - "$1" "$2" "${CHTYPES_TRUSTED_KEYS:-$RELEASE_PUBLIC_KEY}" "${CHTYPES_TRUSTED_KEYS:+CHTYPES_TRUSTED_KEYS}" <<'PY'
import base64, hashlib, sys
sums_path, sig_path, keys, keysrc = sys.argv[1:]
def refuse(why): print(why); sys.exit(1)
# ---- ed25519 verification, RFC 8032 over Python integers (the reference
# vector in docs/fetch.md §4 and RFC 8032's test 1 are checked at every run).
p = 2**255 - 19
q = 2**252 + 27742317777372353535851937790883648493
d = (-121665 * pow(121666, p - 2, p)) % p
def sha512(s): return hashlib.sha512(s).digest()
def inv(x): return pow(x, p - 2, p)
def add(P, Q):
    X1, Y1, Z1, T1 = P; X2, Y2, Z2, T2 = Q
    A = (Y1 - X1) * (Y2 - X2) % p; B = (Y1 + X1) * (Y2 + X2) % p
    C = 2 * T1 * T2 * d % p; D = 2 * Z1 * Z2 % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)
def mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1: Q = add(Q, P)
        P = add(P, P); s >>= 1
    return Q
def recover_x(y, sign):
    x2 = (y * y - 1) * inv(d * y * y + 1) % p
    if x2 == 0: return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p: x = x * pow(2, (p - 1) // 4, p) % p
    if (x * x - x2) % p: return None
    if (x & 1) != sign: x = p - x
    return x
By = 4 * inv(5) % p; Bx = recover_x(By, 0); B = (Bx, By, 1, Bx * By % p)
def decode(s):
    if len(s) != 32: return None
    y = int.from_bytes(s, "little"); sign = y >> 255; y &= (1 << 255) - 1
    if y >= p: return None
    x = recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % p)
def verify(pub, msg, sig):
    if len(sig) != 64 or len(pub) != 32: return False
    A = decode(pub); R = decode(sig[:32])
    if A is None or R is None: return False
    s = int.from_bytes(sig[32:], "little")
    if s >= q: return False
    h = int.from_bytes(sha512(sig[:32] + pub + msg), "little") % q
    P1 = mul(s, B); P2 = add(R, mul(h, A))
    return (P1[0] * P2[2] - P2[0] * P1[2]) % p == 0 and (P1[1] * P2[2] - P2[1] * P1[2]) % p == 0
def keyid(pub): return hashlib.sha256(pub).hexdigest()[:16]
RELEASE = bytes.fromhex("fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc")
REF_SIG = bytes.fromhex("0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04")
RFC_PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_SIG = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
if not (keyid(RELEASE) == "deb275922dbff76e" and verify(RELEASE, b"hello\n", REF_SIG)
        and not verify(RELEASE, b"hellp\n", REF_SIG) and verify(RFC_PUB, b"", RFC_SIG)):
    refuse("the embedded ed25519 verifier failed its self-test — refusing to trust any verdict from it")
# ---- the trusted set: the embedded release key, or exactly what the override names
trusted = []
for k in keys.replace(";", ",").split(","):
    k = k.strip().lower()
    if not k: continue
    try: raw = bytes.fromhex(k)
    except ValueError: raw = b""
    if len(raw) != 32: refuse("CHTYPES_TRUSTED_KEYS entry %r is not a 64-hex ed25519 public key" % k)
    trusted.append(raw)
if not trusted: refuse("CHTYPES_TRUSTED_KEYS is set but names no key")
# ---- the signature file: exactly the two lines of §4
msg = open(sums_path, "rb").read()
lines = open(sig_path, "rb").read().decode("utf-8", "replace").split("\n")
if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
    refuse("SHA256SUMS.sig is not the two-line format (an 'untrusted comment:' line, then base64)")
try: sig = base64.b64decode(lines[1].strip(), validate=True)
except Exception: refuse("SHA256SUMS.sig's second line is not base64")
if len(sig) != 64: refuse("SHA256SUMS.sig carries %d signature bytes, not 64" % len(sig))
# The comment's key id is UNTRUSTED — a hint for which key to try first, never a verdict.
hint = lines[0].rsplit(" ", 1)[-1].strip()
for pub in sorted(trusted, key=lambda k: keyid(k) != hint):
    if verify(pub, msg, sig):
        print("ed25519 key %s (%s)" % (keyid(pub), "from " + keysrc if keysrc else "the release key"))
        sys.exit(0)
refuse("the signature (header names key %s) verifies under none of the %d trusted key(s): %s"
       % (hint, len(trusted), ", ".join(keyid(k) for k in trusted)))
PY
}
fetch_release_file() { # fetch_release_file <name>: a release-level file the source MUST have
  local rc=0
  get_file "$1" "$WORK/$1" || rc=$?
  case "$rc" in
    0) ;;
    1) fail CHTYPES_SOURCE_UNREACHABLE "no $1 at $SOURCE_DESC — not a chtypes release" ;;
    *) fail CHTYPES_SOURCE_UNREACHABLE "could not fetch $1 from $SOURCE_DESC" ;;
  esac
}
# ------------------------------------- the release metadata, and the one retry
#
# A publish into the rolling release is THREE objects — SHA256SUMS,
# SHA256SUMS.sig, index.json — and object storage gives no way to swap them
# atomically. They are uploaded in that order, so an old index read against new
# sums still cross-checks; the only genuinely unsafe window is between the sums
# and the signature that covers them, one small object wide and seconds long.
#
# Exactly two symptoms fall in that window: the signature does not verify, and
# index.json disagrees with SHA256SUMS. Both are retried, because a moment later
# the publish has landed and the three agree. Nothing else is: a tarball whose
# hash is wrong (below) is the release lying about a byte, not a half-finished
# upload, and it refuses at once. When the attempts run out these refuse exactly
# as loudly as they did before, with the same code — a retry buys ten seconds,
# it never converts a refusal into an install.
# Only a real HTTP source can be mid-publish. A file:// fixture or a directory
# is whatever it is, so it refuses on the first look, exactly as it always has —
# which is also why the spec/fixtures/fetch suites stay instant.
METADATA_ATTEMPTS="${CHTYPES_METADATA_ATTEMPTS:-3}"
METADATA_RETRY_DELAY="${CHTYPES_METADATA_RETRY_DELAY:-4}"
case "$BASE_URL" in
  http://*|https://*) ;;
  *) METADATA_ATTEMPTS=1 ;;
esac

# Prints nothing and returns 0 when the three objects agree; on a window
# symptom, sets WINDOW_CODE/WINDOW_MSG and returns 1. A non-window failure
# still calls fail() and exits from inside here.
try_metadata() {
  WINDOW_CODE=""; WINDOW_MSG=""
  fetch_release_file SHA256SUMS
  SIG_STATUS=""
  if [ "${CHTYPES_ALLOW_UNSIGNED:-}" = 1 ]; then
    echo "fetch.sh: WARNING signature verification is OFF (CHTYPES_ALLOW_UNSIGNED=1)." >&2
    echo "          Nothing proves $SOURCE_DESC is what Wave RF published: every hash" >&2
    echo "          below only shows the download matched what THAT source claims." >&2
    echo "          Unset CHTYPES_ALLOW_UNSIGNED for anything but a test." >&2
    SIG_STATUS="NOT VERIFIED (CHTYPES_ALLOW_UNSIGNED=1)"
  else
    local rc=0
    get_file SHA256SUMS.sig "$WORK/SHA256SUMS.sig" || rc=$?
    case "$rc" in
      0) ;;
      1) fail CHTYPES_ARTIFACT_UNTRUSTED "$SOURCE_DESC has no SHA256SUMS.sig — an unsigned release is never installed (CHTYPES_ALLOW_UNSIGNED=1 overrides, for a test)" ;;
      *) fail CHTYPES_SOURCE_UNREACHABLE "could not fetch SHA256SUMS.sig from $SOURCE_DESC" ;;
    esac
    if ! SIG_STATUS="$(verify_signature "$WORK/SHA256SUMS" "$WORK/SHA256SUMS.sig")"; then
      WINDOW_CODE=CHTYPES_ARTIFACT_UNTRUSTED
      WINDOW_MSG="$SOURCE_DESC: $SIG_STATUS — NOT reading the release"
      return 1
    fi
    say "SHA256SUMS.sig verified: $SIG_STATUS"
  fi

  fetch_release_file index.json

  # The whole-release cross-check, hoisted here from the per-asset one below so
  # that a disagreement is caught while re-fetching all three still fixes it.
  local disagreement
  disagreement="$(python3 - "$WORK/index.json" "$WORK/SHA256SUMS" <<'PY_XCHECK'
import json, sys
index_path, sums_path = sys.argv[1:3]
sums = {}
for line in open(sums_path, encoding="utf-8", errors="replace"):
    parts = line.split()
    if len(parts) >= 2:
        sums[parts[1].lstrip("*")] = parts[0]
try:
    rows = json.load(open(index_path)).get("artifacts", [])
except Exception as exc:
    print("index.json is not readable JSON: %s" % exc)
    raise SystemExit(0)
for r in rows:
    name, want = r.get("file"), r.get("sha256")
    if not name or not want or name not in sums:
        continue                      # a row the sums do not mention is not a disagreement
    if sums[name] != want:
        print("index.json says %s is %s but SHA256SUMS says %s" % (name, want, sums[name]))
        raise SystemExit(0)
PY_XCHECK
)" || disagreement="could not cross-check index.json against SHA256SUMS"
  if [ -n "$disagreement" ]; then
    WINDOW_CODE=CHTYPES_ARTIFACT_CORRUPT
    WINDOW_MSG="$disagreement — the release disagrees with itself; not installing it"
    return 1
  fi
}

attempt=1
while :; do
  try_metadata && break
  if [ "$attempt" -ge "$METADATA_ATTEMPTS" ]; then
    fail "$WINDOW_CODE" "$WINDOW_MSG"
  fi
  echo "fetch.sh: $WINDOW_CODE on attempt $attempt/$METADATA_ATTEMPTS — this is what a release" >&2
  echo "          being published looks like from outside; retrying in ${METADATA_RETRY_DELAY}s" >&2
  sleep "$METADATA_RETRY_DELAY"
  attempt=$((attempt + 1))
done

# --------------------------------------------------------------- pick the asset
# The listing names the artifacts' licence (Elastic License 2.0); say so once,
# before a byte of library moves — the SDK is Apache 2.0, the artifact is not.
LIC="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("license","") + " " + d.get("license_url",""))' "$WORK/index.json" 2>/dev/null || true)"
[ -n "${LIC% }" ] && echo "fetch.sh: artifacts are licensed under ${LIC% } — LICENSE and NOTICE ship beside them" >&2
# The selection runs in its own assignment rather than inside the here-doc that
# feeds `read`: a here-doc nested inside a command substitution inside a
# here-doc parses, but it hides python's own error message, and that message is
# the useful half when a release simply has no artifact for what was asked.
SELECTED="$(python3 - "$WORK/index.json" "${PLATFORM%%-*}" "${PLATFORM##*-}" "$WANT_LINE" "$WANT_EXACT" "$STRICT_EXACT" "$ALL" 2>"$WORK/.select.err" <<'PY'
import json, sys
path, os_, arch, line, exact, strict, want_all = sys.argv[1:]
strict, want_all = strict == "1", want_all == "1"
doc = json.load(open(path))
if doc.get("schema") != 1:
    sys.exit("corrupt: index.json schema %r is not 1 — this fetch.sh cannot read it" % doc.get("schema"))
arts = [a for a in doc["artifacts"] if a["os"] == os_ and a["arch"] == arch]
if not arts:
    sys.exit("unpublished: the release has nothing for %s-%s (it has: %s)"
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
        sys.exit("unpublished: you asked for exactly ClickHouse %s on %s-%s and this release does "
                 "not publish it (it has: %s).\n"
                 "          Ask for the line (%s) to take what was published."
                 % (exact, os_, arch, ", ".join(a["clickhouse_version"] for a in arts), line))
    if not hit:
        hit = [a for a in arts if a["clickhouse_minor"] == line]
    if not hit:
        sys.exit("unpublished: no artifact for ClickHouse line %s on %s-%s (it has: %s)"
                 % (line, os_, arch, ", ".join(a["clickhouse_version"] for a in arts)))
    # More than one patch on a line can only happen if a release shipped two;
    # take the newest by version number rather than by list order.
    hit = [sorted(hit, key=vkey)[-1]]

for a in hit:
    for k in ("file", "sha256", "bytes", "clickhouse_version", "clickhouse_minor", "library", "library_sha256"):
        if not a.get(k):
            sys.exit("corrupt: index.json entry for %s is missing %s" % (a.get("file"), k))
    print("|".join([a["file"], a["sha256"], str(a["bytes"]), a["clickhouse_version"],
                    a["clickhouse_minor"], a["library"], a["library_sha256"]]))
PY
)" || {
  # The selector's own message is the useful half; its first word is the code.
  case "$(head -1 "$WORK/.select.err")" in
    "unpublished: "*) fail CHTYPES_ARTIFACT_UNPUBLISHED "$(sed '1s/^unpublished: //' "$WORK/.select.err")" ;;
    "corrupt: "*)     fail CHTYPES_ARTIFACT_CORRUPT     "$(sed '1s/^corrupt: //' "$WORK/.select.err")" ;;
    *)                cat "$WORK/.select.err" >&2; fail CHTYPES_ARTIFACT_CORRUPT "index.json at $SOURCE_DESC could not be used for this request" ;;
  esac
}
[ -n "$SELECTED" ] || fail CHTYPES_ARTIFACT_UNPUBLISHED "index.json selected nothing for this request"

# install_one <row> — one index.json row, from the release to <dest>/<minor>/.
install_one() {
  local ASSET ASSET_SHA ASSET_BYTES A_VER A_MINOR A_LIB A_LIBSHA
  IFS='|' read -r ASSET ASSET_SHA ASSET_BYTES A_VER A_MINOR A_LIB A_LIBSHA <<EOF
$1
EOF
  [ -n "$ASSET" ] && [ -n "$ASSET_SHA" ] && [ -n "$A_LIBSHA" ] \
    || fail CHTYPES_ARTIFACT_CORRUPT "malformed index row: $1"
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
  [ -n "$SUMS_SHA" ] || fail CHTYPES_ARTIFACT_CORRUPT "SHA256SUMS has no line for $ASSET"
  [ "$SUMS_SHA" = "$ASSET_SHA" ] \
    || fail CHTYPES_ARTIFACT_CORRUPT "index.json says $ASSET is $ASSET_SHA but SHA256SUMS says $SUMS_SHA — the release disagrees with itself; not installing it"

  say "downloading $ASSET"
  rm -f "$WORK/$ASSET"
  get_file "$ASSET" "$WORK/$ASSET" || fail CHTYPES_SOURCE_UNREACHABLE "could not download $ASSET from $SOURCE_DESC"
  local GOT_BYTES GOT_SHA
  GOT_BYTES="$(wc -c < "$WORK/$ASSET" | tr -d ' ')"
  GOT_SHA="$(sha256_of "$WORK/$ASSET")"
  [ "$GOT_BYTES" = "$ASSET_BYTES" ] || fail CHTYPES_ARTIFACT_CORRUPT "$ASSET is $GOT_BYTES bytes, index.json says $ASSET_BYTES"
  [ "$GOT_SHA" = "$ASSET_SHA" ] \
    || fail CHTYPES_ARTIFACT_CORRUPT "$ASSET FAILED its sha256: got $GOT_SHA, want $ASSET_SHA — NOT unpacking it"
  say "sha256 verified before unpacking: $GOT_SHA"

  # ----------------------------------------------------------------- install
  local UNPACK="$WORK/unpack.$A_MINOR"
  rm -rf "$UNPACK"; mkdir -p "$UNPACK"
  tar -xzf "$WORK/$ASSET" -C "$UNPACK"
  [ -f "$UNPACK/manifest.json" ] || fail CHTYPES_ARTIFACT_CORRUPT "$ASSET contains no manifest.json at its root"
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
)" || fail CHTYPES_ARTIFACT_CORRUPT "manifest.json inside $ASSET is not usable"
  IFS='|' read -r M_LIB M_VER M_MINOR M_SHA <<EOF
$MANIFEST
EOF
  [ -n "$M_LIB" ] || fail CHTYPES_ARTIFACT_CORRUPT "manifest.json inside $ASSET did not yield a library name"
  # The index is a convenience; the manifest is the artifact's own claim about
  # itself. They must agree, or the index was built from a different artifact.
  [ "$M_LIB" = "$A_LIB" ] && [ "$M_VER" = "$A_VER" ] && [ "$M_MINOR" = "$A_MINOR" ] && [ "$M_SHA" = "$A_LIBSHA" ] \
    || fail CHTYPES_ARTIFACT_CORRUPT "manifest.json inside $ASSET disagrees with index.json ($M_LIB/$M_VER/$M_MINOR vs $A_LIB/$A_VER/$A_MINOR)"
  [ -f "$UNPACK/$M_LIB" ] || fail CHTYPES_ARTIFACT_CORRUPT "$ASSET names library $M_LIB but does not contain it"

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
    fail CHTYPES_ARTIFACT_CORRUPT "installed $INSTALL/$M_LIB hashes $FINAL_SHA, manifest says $M_SHA — the install is bad"
  fi
  say "installed and verified"
  printf '    %s\n' "$INSTALL/" >&2
  ( cd "$INSTALL" && ls -l ) >&2
  printf '    %s sha256 %s\n' "$M_LIB" "$FINAL_SHA" >&2
  printf '    release signature: %s\n' "$SIG_STATUS" >&2
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
