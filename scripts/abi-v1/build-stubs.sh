#!/usr/bin/env bash
# build-stubs.sh: build every stub library in scripts/abi-v1/emit/_stubshared.py's
# variant plan, plus the predicate each one is meant to be checked against,
# then compile and run stubtest.c (the D2 self-test) against the "ok" build.
#
#     scripts/abi-v1/build-stubs.sh --out DIR               every major a binding speaks, into DIR/v<N>/
#     scripts/abi-v1/build-stubs.sh --out DIR --major N     ABI v<N> only, into DIR itself
#     scripts/abi-v1/build-stubs.sh --selftest              the relocation proof, for every major a binding speaks
#
# MAJORS. spec/binding-majors.json (scripts/abi-v1/majors.py) says which ABI
# major each binding speaks; while the bindings convert to ABI v2 one at a
# time (public issue #511) the four speak two majors, and each conformance leg
# loads the stubs of ITS binding's major. So without --major this builds the
# stubs of every major some binding speaks, each from its own description
# (gen.py --major N --render stub) against its own header (include/chtypes.h
# for ABI v1, include/v<N>/chtypes.h after), into DIR/v<N>/. With --major N it
# builds that one major into DIR, the layout below.
#
# DIR (or DIR/v<N>/) ends up holding:
#   DIR/src/stub.c          the rendered C source (gen.py --render stub)
#   DIR/src/predicate.json  the rendered predicate template (__OS__/__ARCH__ placeholders)
#   DIR/<variant>.so        one shared library per emit/_stubshared.py plan(model) entry
#   DIR/stubs.json          {variant: {path, predicate, reason}}, the manifest a future
#                           loader conformance test reads to know what each library is for.
#                           "path" is RELATIVE to the directory holding stubs.json (just the
#                           file name) — never absolute — because the directory this script
#                           writes into (a CI job's $RUNNER_TEMP) is never the directory a
#                           LATER job downloads the same artifact into (spec/abi-v1/schema/
#                           stubs.schema.json documents this; resolve every path by joining
#                           it with dirname(stubs.json), never by trusting it as-is)
#   DIR/stubtest            the compiled D2 self-test binary
#   DIR/ctor-marker.marker  written ONLY if ctor-marker.so is actually dlopen'd (never by
#                           this script itself, which only compiles, never loads, the variants)
#
# This is seconds of work (around 50 tiny compiles) and runs the same way on
# linux-amd64, linux-arm64 and darwin-arm64 (the three required platforms):
# every variant is a native, non-cross compile of the one generated stub.c,
# selected entirely by preprocessor defines (see emit/stub.py's module
# docstring), so there is nothing platform-specific here beyond the OS/ARCH
# this host reports and the one Darwin-only linker flag "unbound" needs.
#
# --selftest proves the "path is relative" contract for real: it builds into
# one temp directory, then COPIES the whole output tree to a second,
# different temp directory (exactly what v1-abi-conformance's artifact
# download does: a different directory, on a different runner) and reruns
# the already-compiled stubtest binary FROM the new location, pointing it at
# the new location — which only passes if every path stubs.json carries
# still resolves there. It never touches the real tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CC="${CC:-cc}"

# The majors spec/binding-majors.json gives the four bindings, ascending.
spoken_majors() {
    python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/scripts/abi-v1")
import majors
print(" ".join(str(n) for n in majors.load().spoken()))
PY
}

main_build() {
    local OUT="$1" MAJOR="${2:-1}"
    mkdir -p "$OUT/src"

    echo "build-stubs.sh: ABI v$MAJOR: rendering stub.c and predicate.json"
    python3 "$HERE/gen.py" --major "$MAJOR" --render stub --out "$OUT/src"

    # This host's OS/ARCH, spelled exactly as emit/stub.py's generated
    # `#if defined(__linux__)` / `#if defined(__aarch64__)` branches pick at
    # compile time, so the predicate build-stubs.sh writes and the build_info
    # the compiled library reports always agree (both ultimately come from
    # the same uname, read once, here).
    local HOST_OS HOST_ARCH
    case "$(uname -s)" in
        Linux) HOST_OS="linux" ;;
        Darwin) HOST_OS="darwin" ;;
        *) echo "build-stubs.sh: unsupported OS $(uname -s)" >&2; exit 2 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) HOST_ARCH="amd64" ;;
        arm64|aarch64) HOST_ARCH="arm64" ;;
        *) echo "build-stubs.sh: unsupported architecture $(uname -m)" >&2; exit 2 ;;
    esac
    echo "build-stubs.sh: host is $HOST_OS-$HOST_ARCH"

    # The "good" predicate every variant but ctor-marker is checked against:
    # predicate.json's __OS__/__ARCH__ filled in, plus glibc_floor on Linux
    # only (D3's loader step 1 is "darwin: skip"). A real loader never reads
    # this file; it is only a future conformance test's input, alongside
    # stubs.json.
    python3 - "$OUT/src/predicate.json" "$HOST_OS" "$HOST_ARCH" "$OUT/predicate.json" <<'PY'
import json, sys
src, os_, arch, dst = sys.argv[1:5]
with open(src) as f:
    pred = json.load(f)
pred["os"] = os_
pred["arch"] = arch
if os_ == "linux":
    pred["glibc_floor"] = "2.29"
with open(dst, "w") as f:
    json.dump(pred, f, indent=2, sort_keys=True)
    f.write("\n")
PY

    local INCLUDE_DIR="$ROOT/include"
    [ "$MAJOR" = "1" ] || INCLUDE_DIR="$ROOT/include/v$MAJOR"
    local MARKER_PATH="$OUT/ctor-marker.marker"
    rm -f "$MARKER_PATH"

    local built=0
    local STUBS_JSON_ROWS="$OUT/.stubs-rows.jsonl"
    : > "$STUBS_JSON_ROWS"

    # "|", not a tab: see emit/_stubshared.py's _main() for why a tab
    # silently drops ok/ok-b's empty "defines" field and shifts every later
    # field by one under bash's `read`.
    local name defines reason link_allow_undefined
    while IFS='|' read -r name defines reason link_allow_undefined; do
        local args=(-std=c11 -fPIC -shared -I "$INCLUDE_DIR")
        if [ -n "$defines" ]; then
            local items item
            IFS=',' read -ra items <<< "$defines"
            for item in "${items[@]}"; do
                [ -n "$item" ] && args+=("-D$item")
            done
        fi
        if [ "$name" = "ctor-marker" ]; then
            args+=("-DCHS_STUB_CTOR_MARKER_PATH=\"$MARKER_PATH\"")
        fi
        if [ "$link_allow_undefined" = "1" ] && [ "$HOST_OS" = "darwin" ]; then
            # shellcheck disable=SC2054  # one -Wl argument with embedded commas (the linker's own
            # sub-option syntax), not three array elements
            args+=(-Wl,-undefined,dynamic_lookup)
        fi
        local rel_path="$name.so"
        local out_path="$OUT/$rel_path"
        "$CC" "${args[@]}" "$OUT/src/stub.c" -o "$out_path"
        built=$((built + 1))

        local pred_file
        if [ "$name" = "ctor-marker" ]; then
            pred_file="$OUT/predicate-ctor-marker.json"
            python3 - "$OUT/predicate.json" "$pred_file" <<'PY'
import json, sys
src, dst = sys.argv[1:3]
with open(src) as f:
    pred = json.load(f)
pred["glibc_floor"] = "99.0"
with open(dst, "w") as f:
    json.dump(pred, f, indent=2, sort_keys=True)
    f.write("\n")
PY
        else
            pred_file="$OUT/predicate.json"
        fi

        # "path" is RELATIVE to the directory holding stubs.json (just the
        # file name: every variant's .so sits next to it, never in a
        # subdirectory) — never $out_path. v1-abi-stubs writes stubs.json
        # under its own $RUNNER_TEMP, but v1-abi-conformance downloads the
        # artifact into a DIFFERENT directory on a DIFFERENT runner; an
        # absolute path baked in here would be stale there (measured: 117 of
        # 119 cases failed dlopen with "No such file" before this fix).
        # stubs.schema.json documents the contract; the stub self-test below
        # never reads stubs.json at all (it builds "$OUT/ok.so" etc. itself),
        # so it needed no change — see --selftest instead, which proves the
        # contract by relocating the whole tree and rerunning it there.
        python3 - "$name" "$rel_path" "$pred_file" "$reason" >> "$STUBS_JSON_ROWS" <<'PY'
import json, sys
name, path, pred_file, reason = sys.argv[1:5]
with open(pred_file) as f:
    predicate = json.load(f)
print(json.dumps({"name": name, "path": path, "predicate": predicate, "reason": reason}))
PY
    done < <(python3 "$HERE/emit/_stubshared.py" --list-variants --major "$MAJOR")

    python3 - "$STUBS_JSON_ROWS" "$OUT/stubs.json" <<'PY'
import json, sys
rows_path, out_path = sys.argv[1:3]
rows = {}
with open(rows_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        rows[row["name"]] = {"path": row["path"], "predicate": row["predicate"], "reason": row["reason"]}
with open(out_path, "w") as f:
    json.dump({"schema": 1, "variants": rows}, f, indent=2, sort_keys=True)
    f.write("\n")
PY
    rm -f "$STUBS_JSON_ROWS"

    echo "build-stubs.sh: ABI v$MAJOR: built $built stub libraries into $OUT (manifest: $OUT/stubs.json)"

    echo "build-stubs.sh: compiling and running the D2 self-test (stubtest.c)"
    "$CC" -std=c11 -Wall -Wextra -pthread -I "$INCLUDE_DIR" "$HERE/stubtest.c" -o "$OUT/stubtest"
    "$OUT/stubtest" "$OUT"
}

selftest() {
    local major
    for major in $(spoken_majors); do
        selftest_major "$major"
    done
}

selftest_major() {
    local a b MAJOR="$1"
    a="$(mktemp -d)"
    echo "build-stubs.sh --selftest: ABI v$MAJOR: building into $a"
    main_build "$a" "$MAJOR" >&2

    echo "build-stubs.sh --selftest: every stubs.json path must be relative"
    python3 - "$a/stubs.json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
bad = sorted(n for n, v in doc["variants"].items() if v["path"].startswith("/"))
if bad:
    sys.exit("build-stubs.sh --selftest: FAIL: absolute path(s) in stubs.json: " + ", ".join(bad))
PY

    # Relocate the WHOLE output tree to a second, different directory —
    # exactly what v1-abi-conformance's artifact download does to
    # v1-abi-stubs's output — and prove every stubs.json path still resolves
    # there, by rerunning the (already-built, unmodified) stubtest binary
    # from its new location, pointed at its new location. This is the actual
    # regression this selftest exists for: a build-time absolute path would
    # pass build_stubs.sh's own run (at "$a") and only break once an
    # artifact crosses a job boundary, so the proof has to include a real
    # relocation, not just "stubs.json parses".
    b="$(mktemp -d)/relocated"
    echo "build-stubs.sh --selftest: relocating to $b and rerunning stubtest from there"
    cp -R "$a" "$b"
    python3 - "$b/stubs.json" "$b" <<'PY'
import json, os, sys
stubs_json, base = sys.argv[1:3]
doc = json.load(open(stubs_json))
missing = [(n, os.path.join(base, v["path"])) for n, v in doc["variants"].items()
           if not os.path.isfile(os.path.join(base, v["path"]))]
if missing:
    sys.exit(f"build-stubs.sh --selftest: FAIL: after relocating to {base}, missing: {missing}")
PY
    "$b/stubtest" "$b"

    # The stubs are this major's: the ok library answers its own generation.
    python3 - "$b/stubs.json" "$b" "$MAJOR" <<'PY'
import ctypes, json, os, sys
stubs_json, base, major = sys.argv[1], sys.argv[2], int(sys.argv[3])
ok = os.path.join(base, json.load(open(stubs_json))["variants"]["ok"]["path"])
lib = ctypes.CDLL(ok, mode=os.RTLD_NOW | os.RTLD_LOCAL)
fn = getattr(lib, "chs_abi_version")
fn.restype = ctypes.c_int32
if fn() != major:
    sys.exit(f"build-stubs.sh --selftest: FAIL: the ABI v{major} ok stub answers chs_abi_version() = {fn()}")
PY

    rm -rf "$a" "$(dirname "$b")"
    echo "build-stubs.sh --selftest: ABI v$MAJOR: ok: stubs.json paths are relative, resolve after relocation, the D2 self-test still passes from the relocated directory, and the ok stub answers ABI v$MAJOR"
}

if [ "${1:-}" = "--selftest" ]; then
    selftest
    exit 0
fi

OUT=""
ONE_MAJOR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --major) ONE_MAJOR="$2"; shift 2 ;;
        *) echo "build-stubs.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
if [ -z "$OUT" ]; then
    echo "usage: build-stubs.sh --out DIR [--major N] | build-stubs.sh --selftest" >&2
    exit 2
fi
if [ -n "$ONE_MAJOR" ]; then
    main_build "$OUT" "$ONE_MAJOR"
    exit 0
fi
for major in $(spoken_majors); do
    main_build "$OUT/v$major" "$major"
done
echo "build-stubs.sh: built the stubs of ABI v$(spoken_majors | sed 's/ /, v/g') (spec/binding-majors.json) under $OUT/v<N>/"
