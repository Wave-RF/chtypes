#!/usr/bin/env python3
"""check-no-hand-decls.py: no binding declares or resolves a chs_* entry point by hand.

    scripts/abi-v1/check-no-hand-decls.py --scope v1    the v1 FFI directories only (while v0 code coexists)
    scripts/abi-v1/check-no-hand-decls.py --scope all   every binding source and test file (after the cutover)
    scripts/abi-v1/check-no-hand-decls.py --selftest    prove every rule fires, and does not overfire, on a fabricated tree

WHY. In ABI v1 every binding's FFI declarations are generated from
spec/abi-v1/abi.json by scripts/abi-v1/gen.py, and `gen.py --check` proves the
generated files match the description byte for byte. That makes v0's
v0's hand-written declaration check (which could compare only type classes) redundant
by construction, except for one hole: hand-written code that declares or looks
up an entry point itself, bypassing the generated layer. This check closes
that hole. Each binding's rules are the shapes such a bypass must take:

  go      `dlsym(` anywhere, or a cgo call `C.chs_*`
  python  attribute access `.chs_*` (a ctypes CDLL symbol), `getattr(..., "chs_...")`,
          or item access `["chs_..."]`
  ts      a direct `dlsym(`, an ffi-rs `funcName: "chs_..."`, a define-table
          key `chs_...: {`, or `define(` in a file that names any chs_ symbol
  rust    `extern "C"` anywhere, or a libloading lookup `get(b"chs_...")`

So the generated layer exposes its wrappers under names without the `chs_`
prefix, and hand code calls those.

WHAT IS EXEMPT. A file that is one of the paths gen.py's emitters produce for
some ABI major (gen.produced_outputs(major=N)) AND whose first 512 bytes carry
THAT major's banner (scripts/abi-v1/emit/__init__.py's banner_re(N), a real
64-hex fingerprint included): gen.is_generated. A file of one major carrying
another major's banner is not exempt. Membership is the first condition because `gen.py --check` skips
build-output directories when it looks for a banner that no emitter produces, so
the banner alone could be copied into a hand-written file under one of them. Full-line comments (and Python
docstring lines) are dropped before matching, so prose that NAMES a symbol is
not a declaration.

--scope v1 walks the abi1 directories (go/internal/abi1, python/src/chtypes/_abi1,
python/tests/abi1, ts/src/abi1, ts/test/abi1, rust/src/abi1, and
rust/tests/abi1_*.rs); until a binding's v1 lane lands, its directory does not
exist and is reported as such, not silently skipped. --scope all walks every
binding's source and test tree and replaces the v1 scope at the cutover, when
the v0 code is gone.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import majors as bmajors  # noqa: E402
import model as abimodel  # noqa: E402
from emit import BANNER_PREFIX, banner_prefix  # noqa: E402
from gen import is_generated, produced_by_major, produced_outputs  # noqa: E402

EXT = {".go": "go", ".py": "python", ".ts": "ts", ".rs": "rust"}
COMMENT = {"go": ("//", "/*", "*"), "python": ("#",), "ts": ("//", "/*", "*"), "rust": ("//", "/*", "*")}
RULES: dict[str, list[tuple[str, re.Pattern]]] = {
    "go": [
        ("a direct dlsym", re.compile(r"\bdlsym\s*\(")),
        ("a cgo call to a chs_ symbol", re.compile(r"\bC\.chs_\w+")),
    ],
    "python": [
        ("attribute access to a chs_ symbol", re.compile(r"\.\s*chs_\w+")),
        ("getattr of a chs_ symbol", re.compile(r"\bgetattr\s*\([^)]*['\"]chs_")),
        ("item access to a chs_ symbol", re.compile(r"\[\s*['\"]chs_\w+['\"]\s*\]")),
    ],
    "ts": [
        ("a direct dlsym", re.compile(r"\bdlsym\s*\(")),
        ("an ffi-rs funcName naming a chs_ symbol", re.compile(r"\bfuncName\s*:\s*['\"`]chs_")),
        ("an ffi-rs define-table entry for a chs_ symbol", re.compile(r"(?<![\w.])['\"]?chs_\w+['\"]?\s*:\s*\{")),
    ],
    "rust": [
        ('an extern "C" declaration', re.compile(r"\bextern\s+\"C\"")),
        ("a libloading lookup of a chs_ symbol", re.compile(r"\bget\s*(?:::\s*<.*?>)?\s*\(\s*b\"chs_")),
    ],
}
TS_DEFINE = re.compile(r"\bdefine\s*\(")
TS_CHS = re.compile(r"\bchs_\w+")

# Every binding's generated-layer directory at every major the generator
# knows (scripts/abi-v1/majors.py LAYER_DIRS): go/internal/abi2 holds the Go
# layer once go speaks ABI v2 (spec/binding-majors.json).
V1_DIRS = [bmajors.layer_dir(b, n) for b in bmajors.BINDINGS for n in abimodel.MAJORS] + [
    "python/tests/abi1",
    "ts/test/abi1",
]
# The Rust conformance runner of every major (rust/tests/abi<N>_conformance.rs).
V1_GLOBS = [("rust/tests", f"abi{n}_*.rs") for n in abimodel.MAJORS]
ALL_DIRS = ["go", "python/src", "python/tests", "ts/src", "ts/test", "rust/src", "rust/tests"]
# build and dist are walked on purpose: see the exemption note in the module docstring.
SKIP = frozenset({"node_modules", "target", ".venv", "__pycache__", ".pytest_cache"})


def code_lines(binding: str, text: str) -> list[tuple[int, str]]:
    """(line number, line) for every line that is not a full-line comment and,
    in Python, not inside a triple-quoted string that starts a line."""
    out = []
    in_doc: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if binding == "python":
            if in_doc:
                if in_doc in s:
                    in_doc = None
                continue
            for q in ('"""', "'''"):
                if s.startswith(q) or s.startswith(("r" + q, "f" + q)):
                    body = s[s.index(q) + 3 :]
                    if q not in body:
                        in_doc = q
                    break
            else:
                if not s.startswith(COMMENT[binding]):
                    out.append((n, line))
            continue
        if not s.startswith(COMMENT[binding]):
            out.append((n, line))
    return out


def problems_in(path: Path, rel: str, produced: dict[int, frozenset[str]] | None = None) -> list[str]:
    if produced is None:
        produced = produced_by_major()
    binding = EXT.get(path.suffix)
    if binding is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [f"{rel}: unreadable; a checker that cannot read a file must not pass it"]
    if is_generated(rel, text, produced):
        return []
    found = []
    lines = code_lines(binding, text)
    for n, line in lines:
        for what, rx in RULES[binding]:
            if rx.search(line):
                found.append(f"{rel}:{n}: {what}: {line.strip()[:120]}")
    if binding == "ts":
        code = "\n".join(line for _, line in lines)
        if TS_DEFINE.search(code) and TS_CHS.search(code):
            n = next(n for n, line in lines if TS_DEFINE.search(line))
            found.append(f"{rel}:{n}: an ffi-rs define( in a file that names a chs_ symbol")
    return found


def files_in(root: Path, scope: str) -> tuple[list[tuple[Path, str]], list[str]]:
    files: list[tuple[Path, str]] = []
    absent: list[str] = []
    dirs = V1_DIRS if scope == "v1" else ALL_DIRS
    for d in dirs:
        base = root / d
        if not base.is_dir():
            absent.append(d)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames if x not in SKIP)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix in EXT:
                    files.append((p, p.relative_to(root).as_posix()))
    if scope == "v1":
        for d, pattern in V1_GLOBS:
            base = root / d
            matched = sorted(base.glob(pattern)) if base.is_dir() else []
            if not matched:
                absent.append(f"{d}/{pattern}")
            files += [(p, p.relative_to(root).as_posix()) for p in matched]
    return files, absent


def run(root: Path, scope: str, quiet: bool = False, produced: dict[int, frozenset[str]] | None = None) -> int:
    files, absent = files_in(root, scope)
    if produced is None:
        produced = produced_by_major()
    found = []
    for p, rel in files:
        found += problems_in(p, rel, produced)
    if not quiet:
        for f in found:
            print(f"check-no-hand-decls: {f}", file=sys.stderr)
        if absent:
            print(f"check-no-hand-decls: not present yet ({scope} scope): {', '.join(absent)}")
    if found:
        if not quiet:
            print(
                f"check-no-hand-decls: {len(found)} hand-written chs_ declaration(s) or lookup(s); "
                "declare entry points in spec/abi-v1/abi.json and call the generated layer",
                file=sys.stderr,
            )
        return 1
    if not quiet:
        print(
            f"check-no-hand-decls: ok ({scope} scope): {len(files)} file(s) scanned, none declares a chs_ entry point by hand"
        )
    return 0


PLANTS = [
    # (relative path, content, expected to be flagged)
    ("go/internal/abi1/loader.go", "package abi1\n\nfunc f() { C.chs_preview_row(nil) }\n", True),
    (
        "go/internal/abi1/probe.go",
        'package abi1\n\n/*\n#include <dlfcn.h>\nvoid *p(void *h) { return dlsym(h, "x"); }\n*/\nimport "C"\n',
        True,
    ),
    (
        "go/internal/abi1/commented.go",
        "package abi1\n\n// C.chs_preview_row is reached through the generated table.\n",
        False,
    ),
    ("python/src/chtypes/_abi1/_loader.py", "def f(lib):\n    lib.chs_preview_row.restype = None\n", True),
    ("python/src/chtypes/_abi1/_resolve.py", "def f(lib):\n    return getattr(lib, 'chs_abi_version')\n", True),
    ("python/src/chtypes/_abi1/_item.py", "def f(lib):\n    return lib['chs_abi_version']\n", True),
    (
        "python/src/chtypes/_abi1/_doc.py",
        '"""Calls lib.chs_preview_row through the generated layer."""\n\n# lib.chs_x\nX = 1\n',
        False,
    ),
    ("ts/src/abi1/loader.ts", "export const f = (h: unknown) => dlsym(h, 'chs_abi_version');\n", True),
    (
        "ts/src/abi1/call.ts",
        "load({ library: 'l', funcName: 'chs_buf_len', retType: 0, paramsType: [], paramsValue: [] });\n",
        True,
    ),
    ("ts/src/abi1/table.ts", "export const t = { chs_buf_len: { library: 'l' } };\n", True),
    ("ts/src/abi1/define.ts", "define({\n  x: { library: 'l' },\n});\nconst n = 'chs_buf_len';\n", True),
    (
        "ts/src/abi1/fine.ts",
        "// dlsym( is never called here\nexport const len = (b: { len(): number }) => b.len();\n",
        False,
    ),
    ("rust/src/abi1/loader.rs", 'extern "C" {\n    fn chs_abi_version() -> i32;\n}\n', True),
    ("rust/src/abi1/lookup.rs", 'fn f(l: &Library) { let _ = unsafe { l.get::<F>(b"chs_abi_version\\0") }; }\n', True),
    ("rust/tests/abi1_conformance.rs", '// extern "C" lives in the generated layer\nfn main() {}\n', False),
    # Outside the v1 directories: ignored by --scope v1, caught by --scope all.
    ("go/chtypes/old.go", "package chtypes\n\nfunc f() { C.chs_rows(nil) }\n", "all"),
]


def selftest() -> int:
    fails = []
    fake = "sha256:" + "0" * 64
    with tempfile.TemporaryDirectory(prefix="abi-v1-hand-decls-") as tmp:
        root = Path(tmp)
        for rel, content, _ in PLANTS:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        # A generated file is exempt; one carrying only the banner's SHAPE (no
        # real fingerprint) is not.
        gen = root / "go/internal/abi1/abi_gen.go"
        gen.write_text(
            f"// {BANNER_PREFIX} (CHS_ABI_FINGERPRINT {fake}) — DO NOT EDIT\npackage abi1\n\nfunc g() {{ C.chs_buf_len(nil) }}\n"
        )
        forged = root / "go/internal/abi1/forged_gen.go"
        forged.write_text(
            f"// {BANNER_PREFIX} (CHS_ABI_FINGERPRINT sha256:...) DO NOT EDIT\npackage abi1\n\nfunc h() {{ C.chs_buf_len(nil) }}\n"
        )

        # The same real banner on a hand-written file under a build-output
        # directory inside a binding: not a produced path, so not exempt, and
        # the walk must reach it.
        built = root / "go/internal/abi1/build/loader_gen.go"
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_text(gen.read_text().replace("func g()", "func b()"))
        # What the emitters produce, standing in for the real set (the real
        # outputs, so a real generated file stays exempt) plus the generated file above.
        produced = {1: produced_outputs(major=1) | {"go/internal/abi1/abi_gen.go"}, 2: produced_outputs(major=2)}
        # ABI v2's banner on ABI v2's own produced path is exempt; ABI v1's
        # banner on that path, or v2's banner on a v1 path, is not.
        v2_gen = root / "go/internal/abi2/abi_gen.go"
        v2_gen.parent.mkdir(parents=True, exist_ok=True)
        v2_gen.write_text(
            f"// {banner_prefix(2)} (CHTYPES_FP) — DO NOT EDIT\npackage abi2\n\nfunc g() {{ C.chs_buf_len(nil) }}\n".replace(
                "CHTYPES_FP", "CHS_ABI_FINGERPRINT " + fake
            )
        )
        produced[2] = produced[2] | {"go/internal/abi2/abi_gen.go"}
        v1_on_v2 = root / "go/internal/abi2/crossed_gen.go"
        v1_on_v2.write_text(gen.read_text().replace("package abi1", "package abi2"))
        produced[2] = produced[2] | {"go/internal/abi2/crossed_gen.go"}

        flagged = {}
        for scope in ("v1", "all"):
            files, _ = files_in(root, scope)
            flagged[scope] = {rel for p, rel in files if problems_in(p, rel, produced)}
        for rel, _, expect in PLANTS:
            in_v1 = rel in flagged["v1"]
            in_all = rel in flagged["all"]
            if expect is True and not (in_v1 and in_all):
                fails.append(f"{rel}: a planted hand declaration was not caught")
            if expect is False and (in_v1 or in_all):
                fails.append(f"{rel}: a comment or a non-declaration was flagged")
            if expect == "all" and (in_v1 or not in_all):
                fails.append(f"{rel}: outside the v1 directories it must be caught by --scope all only")
        if "go/internal/abi1/abi_gen.go" in flagged["v1"]:
            fails.append("a file carrying the real generated banner was not exempt")
        if "go/internal/abi1/forged_gen.go" not in flagged["v1"]:
            fails.append("a file carrying only the banner's shape was exempted")
        if "go/internal/abi1/build/loader_gen.go" not in flagged["v1"]:
            fails.append("a hand-written file carrying the real banner under build/ was exempted or never walked")
        if "go/internal/abi2/abi_gen.go" in flagged["v1"] or "go/internal/abi2/abi_gen.go" in flagged["all"]:
            fails.append("ABI v2's generated file, carrying ABI v2's banner at an ABI v2 output path, was not exempt")
        if "go/internal/abi2/crossed_gen.go" not in flagged["all"]:
            fails.append("ABI v1's banner on an ABI v2 output path was exempted")
        # A produced path with its banner removed is an ordinary hand-written file.
        stripped = gen.read_text().split("\n", 1)[1]
        gen.write_text(stripped)
        files, _ = files_in(root, "v1")
        if "go/internal/abi1/abi_gen.go" not in {rel for p, rel in files if problems_in(p, rel, produced)}:
            fails.append("a banner-less copy of a generated file was exempted")
        gen.write_text(f"// {BANNER_PREFIX} (CHS_ABI_FINGERPRINT {fake}) — DO NOT EDIT\npackage abi1\n\nfunc g() {{ C.chs_buf_len(nil) }}\n")
        if run(root, "v1", quiet=True, produced=produced) != 1:
            fails.append("run() did not exit 1 on a tree with hand declarations")

        empty = root / "empty"
        empty.mkdir()
        if run(empty, "v1", quiet=True, produced=produced) != 0:
            fails.append("a tree with no v1 directory yet must pass, reporting them absent")
    for f in fails:
        print(f"check-no-hand-decls --selftest: FAIL {f}", file=sys.stderr)
    if fails:
        return 1
    print(
        "check-no-hand-decls: selftest ok: every rule fires in all four bindings, comments and generated files (ABI v1's and ABI v2's, each by its own banner) are exempt, a forged or crossed banner is not, and the scopes differ as documented"
    )
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scope", choices=("v1", "all"))
    mode.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return run(ROOT, args.scope)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
