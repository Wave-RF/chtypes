#!/usr/bin/env bash
# build-stubs.sh: build every stub library in scripts/abi-v1/emit/_stubshared.py's
# variant plan, plus the predicate each one is meant to be checked against,
# then compile and run stubtest.c (the D2 self-test) against the "ok" build.
#
#     scripts/abi-v1/build-stubs.sh --out DIR
#
# DIR ends up holding:
#   DIR/src/stub.c          the rendered C source (gen.py --render stub)
#   DIR/src/predicate.json  the rendered predicate template (__OS__/__ARCH__ placeholders)
#   DIR/<variant>.so        one shared library per emit/_stubshared.py plan(model) entry
#   DIR/stubs.json          {variant: {path, predicate, reason}}, the manifest a future
#                           loader conformance test reads to know what each library is for
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
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CC="${CC:-cc}"

OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        *) echo "build-stubs.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
if [ -z "$OUT" ]; then
    echo "usage: build-stubs.sh --out DIR" >&2
    exit 2
fi

mkdir -p "$OUT/src"

echo "build-stubs.sh: rendering stub.c and predicate.json"
python3 "$HERE/gen.py" --render stub --out "$OUT/src"

# This host's OS/ARCH, spelled exactly as emit/stub.py's generated
# `#if defined(__linux__)` / `#if defined(__aarch64__)` branches pick at
# compile time, so the predicate build-stubs.sh writes and the build_info the
# compiled library reports always agree (both ultimately come from the same
# uname, read once, here).
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
# predicate.json's __OS__/__ARCH__ filled in, plus glibc_floor on Linux only
# (D3's loader step 1 is "darwin: skip"). A real loader never reads this
# file; it is only a future conformance test's input, alongside stubs.json.
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

INCLUDE_DIR="$ROOT/include/v1"
MARKER_PATH="$OUT/ctor-marker.marker"
rm -f "$MARKER_PATH"

built=0
STUBS_JSON_ROWS="$OUT/.stubs-rows.jsonl"
: > "$STUBS_JSON_ROWS"

while IFS=$'\t' read -r name defines reason link_allow_undefined; do
    args=(-std=c11 -fPIC -shared -I "$INCLUDE_DIR")
    if [ -n "$defines" ]; then
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
    out_path="$OUT/$name.so"
    "$CC" "${args[@]}" "$OUT/src/stub.c" -o "$out_path"
    built=$((built + 1))

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

    python3 - "$name" "$out_path" "$pred_file" "$reason" >> "$STUBS_JSON_ROWS" <<'PY'
import json, sys
name, path, pred_file, reason = sys.argv[1:5]
with open(pred_file) as f:
    predicate = json.load(f)
print(json.dumps({"name": name, "path": path, "predicate": predicate, "reason": reason}))
PY
done < <(python3 "$HERE/emit/_stubshared.py" --list-variants)

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

echo "build-stubs.sh: built $built stub libraries into $OUT (manifest: $OUT/stubs.json)"

echo "build-stubs.sh: compiling and running the D2 self-test (stubtest.c)"
"$CC" -std=c11 -Wall -Wextra -I "$INCLUDE_DIR" "$HERE/stubtest.c" -o "$OUT/stubtest"
"$OUT/stubtest" "$OUT"
