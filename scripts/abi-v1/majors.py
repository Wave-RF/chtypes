#!/usr/bin/env python3
"""majors.py: which ABI major each binding speaks on this branch, read from one file.

    scripts/abi-v1/majors.py get BINDING                 print the binding's major (1 or 2)
    scripts/abi-v1/majors.py layer-dir BINDING           print its generated layer's directory (go/internal/abi2, ...)
    scripts/abi-v1/majors.py header BINDING              print the header of its major (include/chtypes.h, include/v2/chtypes.h)
    scripts/abi-v1/majors.py announce BINDING            print "ABI major tested: N (spec/binding-majors.json@<commit>)"
                                                         to the log and to $GITHUB_STEP_SUMMARY
    scripts/abi-v1/majors.py assert-library BINDING SO   dlopen SO, ask it for chs_abi_version() and chs_build_info(),
                                                         and refuse unless it is the binding's major and that major's fingerprint
    scripts/abi-v1/majors.py assert-stubs BINDING DIR    the same, for the "ok" library of a build-stubs.sh directory
    scripts/abi-v1/majors.py assert-identity BINDING LOG the same, for the identity a binding's own test printed into LOG
                                                         (a line "chtypes_abi_identity binding=<b> abi=<n> fingerprint=<fp>")
    scripts/abi-v1/majors.py assert-layer BINDING        the same, for the identity compiled into the binding: the
                                                         constants of the ONE generated layer of it the tree holds
    scripts/abi-v1/majors.py --selftest                  prove every refusal fires, with a positive control

THE FILE. spec/binding-majors.json maps each of the four bindings to the ABI
major it speaks at this commit, for example {"go": 2, "python": 1, "rust": 1,
"ts": 1} on the `v2` branch while the bindings convert one at a time (public
issue #511). It is the ONE source for that fact: scripts/abi-v1/gen.py runs a
binding's emitter only for the binding's major, scripts/abi-v1/build-stubs.sh
builds the stubs of every major a binding speaks, and every CI job that tests
a binding derives the major here. `main` has no such file: absent, every
binding speaks ABI v1, so main's jobs read 1 without a file to keep. The
file, when present, names exactly the four bindings, each with a major the
generator knows (model.MAJORS); anything else is refused, never defaulted.

WHAT A JOB PRINTS, AND WHAT IT ASSERTS. `announce` prints the major a job is
about to test, and the commit the file was read at, to the log and to the step
summary, so a green check named for ABI v1 visibly says which major it tested.
The assertion is never "the file says 2, so 2 was tested": that would check
the file against itself. `assert-library` and `assert-stubs` ask the library
the job actually hands the binding (its own chs_abi_version() and its
chs_build_info() abi_fingerprint, through a dlopen) and `assert-identity`
reads what the binding's own test printed from its compiled-in constants, and
`assert-layer` reads those constants from the one generated layer of the
binding present in the tree, wherever it is (never from the directory the map
names);
each refuses unless that answer is the binding's major and the fingerprint of
spec/abi-v<major>/abi.json at this commit. That refusal is what fails a job on
`v2` that tests ABI v1 after its binding converted.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import jcs  # noqa: E402
import model as abimodel  # noqa: E402

MAP = "spec/binding-majors.json"
BINDINGS = ("go", "python", "ts", "rust")
DEFAULT_MAJOR = 1
# Each binding's generated ABI layer, per major: the directory gen.py's
# emitter for that binding writes, and the one the binding's own checks
# (check-no-hand-decls.py, check-linked.sh, the conformance scripts) scan.
LAYER_DIRS = {
    "go": "go/internal/abi{n}",
    "python": "python/src/chtypes/_abi{n}",
    "ts": "ts/src/abi{n}",
    "rust": "rust/src/abi{n}",
}
# Each binding's compiled-in ABI identity, in its generated declaration layer:
# (file within the layer directory, version pattern, fingerprint pattern).
LAYER_IDENTITY = {
    "go": ("abi_gen.go", r"^const ChsAbiVersion = (\d+)$", r'^const ChsAbiFingerprint = "(sha256:[0-9a-f]{64})"$'),
    "python": ("_decls.py", r"^CHS_ABI_VERSION = (\d+)$", r'^CHS_ABI_FINGERPRINT = "(sha256:[0-9a-f]{64})"$'),
    "ts": ("decls.gen.ts", r"^export const ABI_VERSION = (\d+);$", r'^export const ABI_FINGERPRINT = "(sha256:[0-9a-f]{64})";$'),
    "rust": ("decls.rs", r"CHS_ABI_VERSION: i32 = (\d+);", r'CHS_ABI_FINGERPRINT: &str =\s*"(sha256:[0-9a-f]{64})"'),
}
IDENTITY_LINE = re.compile(r"chtypes_abi_identity binding=(\w+) abi=(\d+) fingerprint=(sha256:[0-9a-f]{64})")


class MajorsError(Exception):
    pass


@dataclass(frozen=True)
class Majors:
    by_binding: dict[str, int]
    present: bool

    def __getitem__(self, binding: str) -> int:
        if binding not in BINDINGS:
            raise MajorsError(f"{binding!r} is not a binding ({', '.join(BINDINGS)})")
        return self.by_binding[binding]

    def spoken(self) -> list[int]:
        """Every major at least one binding speaks, ascending."""
        return sorted(set(self.by_binding.values()))

    def bindings_of(self, major: int) -> list[str]:
        return [b for b in BINDINGS if self.by_binding[b] == major]


def parse(text: str, where: str = MAP) -> Majors:
    try:
        doc = jcs.loads(text)
    except jcs.JCSError as e:
        raise MajorsError(f"{where}: {e}") from None
    if not isinstance(doc, dict):
        raise MajorsError(f"{where}: must be a JSON object mapping each binding to its ABI major")
    problems = []
    for b in BINDINGS:
        if b not in doc:
            problems.append(f"{where}: missing binding {b!r}; every binding must be named, none is defaulted")
    for k, v in doc.items():
        if k not in BINDINGS:
            problems.append(f"{where}: {k!r} is not a binding ({', '.join(BINDINGS)})")
        elif not isinstance(v, int) or isinstance(v, bool) or v not in abimodel.MAJORS:
            problems.append(f"{where}: {k} = {v!r} is not an ABI major the generator knows {abimodel.MAJORS}")
    if problems:
        raise MajorsError("\n".join(problems))
    return Majors({b: doc[b] for b in BINDINGS}, True)


def load(root: Path = ROOT) -> Majors:
    """The map at `root`, or every binding at ABI v1 when the file is absent (main)."""
    path = root / MAP
    if not path.exists():
        return Majors({b: DEFAULT_MAJOR for b in BINDINGS}, False)
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise MajorsError(f"{MAP}: unreadable: {e}") from None
    return parse(text)


def layer_dir(binding: str, major: int) -> str:
    return LAYER_DIRS[binding].format(n=major)


def header_path(major: int) -> str:
    return "include/chtypes.h" if major == 1 else f"include/v{major}/chtypes.h"


def fingerprint(root: Path, major: int) -> str:
    return jcs.fingerprint((root / abimodel.spec(major).abi_json).read_bytes())


def commit(root: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return p.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown-commit"


def source_label(root: Path, m: Majors) -> str:
    at = commit(root)
    return f"{MAP}@{at}" if m.present else f"{MAP} absent at {at}: every binding speaks ABI v1"


def to_summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{line}\n\n")


def announce(root: Path, binding: str, what: str = "") -> str:
    m = load(root)
    line = f"ABI major tested: {m[binding]} ({source_label(root, m)})"
    head = f"{binding}{f' ({what})' if what else ''}: "
    print(line)
    to_summary(f"**{head}{line}**")
    return line


# ------------------------------------------------------------- the assertion


def identity_problems(root: Path, m: Majors, binding: str, abi: int, fp: str, what: str) -> list[str]:
    """Every way the identity `what` reported (abi, fp) differs from what the
    map says `binding` speaks at this commit. The identity is the LOADED
    library's own answer, or the binding's compiled-in constants; never the
    map's value."""
    want = m[binding]
    problems = []
    if abi != want:
        problems.append(
            f"{binding}: {what} speaks ABI v{abi}, but {MAP} says {binding} speaks ABI v{want} at this commit: "
            f"this job tests ABI v{abi} after (or before) {binding} converted, so it must not pass"
        )
        return problems
    expected = fingerprint(root, want)
    if fp != expected:
        problems.append(
            f"{binding}: {what} carries abi_fingerprint {fp}, but spec/abi-v{want}/abi.json is {expected} at this commit"
        )
    return problems


def measure_library(path: str) -> tuple[int, int, str]:
    """(chs_abi_version(), build_info abi, build_info abi_fingerprint), asked
    of the library itself through a dlopen."""
    mode = getattr(os, "RTLD_NOW", 2) | getattr(os, "RTLD_LOCAL", 0)
    lib = ctypes.CDLL(path, mode=mode)
    version_fn = getattr(lib, "chs_abi_version")
    version_fn.restype = ctypes.c_int32
    version_fn.argtypes = []
    info_fn = getattr(lib, "chs_build_info")
    info_fn.restype = ctypes.c_char_p
    info_fn.argtypes = []
    version = int(version_fn())
    raw = info_fn()
    if raw is None:
        raise MajorsError(f"{path}: chs_build_info() returned NULL")
    info = json.loads(raw.decode("ascii"))
    return version, int(info.get("abi", -1)), str(info.get("abi_fingerprint", ""))


def assert_library(root: Path, binding: str, path: str) -> list[str]:
    m = load(root)
    try:
        version, bi_abi, fp = measure_library(path)
    except (OSError, AttributeError, ValueError, MajorsError) as e:
        return [f"{binding}: could not ask {path} for its identity: {e}"]
    problems = identity_problems(root, m, binding, version, fp, f"the loaded library {path} (chs_abi_version() = {version})")
    if not problems and bi_abi != version:
        problems.append(f"{binding}: {path}: chs_build_info() says abi {bi_abi} but chs_abi_version() says {version}")
    if not problems:
        print(
            f"majors: {binding}: the loaded library {path} answers chs_abi_version() = {version} and "
            f"abi_fingerprint {fp}: ABI v{m[binding]}, as {MAP} says"
        )
    return problems


def stubs_ok_library(stubs_dir: str) -> str:
    manifest = Path(stubs_dir) / "stubs.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    return str(Path(stubs_dir) / doc["variants"]["ok"]["path"])


def assert_identity_log(root: Path, binding: str, log: str) -> list[str]:
    m = load(root)
    text = Path(log).read_text(encoding="utf-8", errors="replace")
    found = [x for x in IDENTITY_LINE.finditer(text) if x.group(1) == binding]
    if len(found) != 1:
        return [f"{binding}: {log} carries {len(found)} chtypes_abi_identity line(s) for {binding}, expected exactly 1"]
    abi, fp = int(found[0].group(2)), found[0].group(3)
    problems = identity_problems(root, m, binding, abi, fp, f"the binding's compiled-in identity ({log})")
    if not problems:
        print(f"majors: {binding}: the binding is compiled for ABI v{abi}, {fp}: as {MAP} says")
    return problems


def layer_identities(root: Path, binding: str) -> list[tuple[str, int, str]]:
    """(layer file, version, fingerprint) for every generated layer of
    `binding` present under `root`, at any major the generator knows."""
    name, vpat, fpat = LAYER_IDENTITY[binding]
    found = []
    for n in abimodel.MAJORS:
        path = root / layer_dir(binding, n) / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        v = re.search(vpat, text, re.M)
        f = re.search(fpat, text, re.M)
        if not v or not f:
            raise MajorsError(f"{path.relative_to(root)}: no compiled-in ABI version and fingerprint found")
        found.append((path.relative_to(root).as_posix(), int(v.group(1)), f.group(1)))
    return found


def assert_layer(root: Path, binding: str) -> list[str]:
    m = load(root)
    found = layer_identities(root, binding)
    if len(found) != 1:
        return [f"{binding}: the tree holds {len(found)} generated layer(s) of {binding} ({[f for f, _, _ in found]}); exactly one is compiled"]
    rel, abi, fp = found[0]
    problems = identity_problems(root, m, binding, abi, fp, f"the binding's compiled-in identity ({rel})")
    if not problems:
        print(f"majors: {binding}: {rel} compiles in ABI v{abi}, {fp}: as {MAP} says")
    return problems


# -------------------------------------------------------------------- selftest

_FAKE_LIB = """
#include <stdint.h>
int32_t chs_abi_version(void) { return ABI; }
const char *chs_build_info(void) { return "{\\"schema\\":1,\\"abi\\":" ABI_S ",\\"abi_fingerprint\\":\\"" FP "\\"}"; }
"""


def _build_fake(cc: str, out: Path, abi: int, fp: str) -> None:
    src = out.with_suffix(".c")
    src.write_text(_FAKE_LIB, encoding="utf-8")
    subprocess.run(
        [cc, "-shared", "-fPIC", f"-DABI={abi}", f'-DABI_S="{abi}"', f'-DFP="{fp}"', str(src), "-o", str(out)],
        check=True,
    )


def selftest() -> int:
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="majors-selftest-") as tmp:
        root = Path(tmp) / "tree"
        for major in abimodel.MAJORS:
            d = root / abimodel.spec(major).dir
            d.mkdir(parents=True)
            shutil.copy2(ROOT / abimodel.spec(major).abi_json, d / "abi.json")

        def write_map(obj) -> None:
            (root / MAP).parent.mkdir(parents=True, exist_ok=True)
            (root / MAP).write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

        # The map's own refusals: a missing binding, an unknown key, a major
        # the generator does not know, a value that is not an integer.
        for what, obj, frag in (
            ("a map missing a binding", {"go": 2, "python": 1, "ts": 1}, "missing binding 'rust'"),
            ("an unknown key", {"go": 2, "python": 1, "ts": 1, "rust": 1, "java": 1}, "'java' is not a binding"),
            ("an unknown major", {"go": 3, "python": 1, "ts": 1, "rust": 1}, "go = 3"),
            ("a string major", {"go": "2", "python": 1, "ts": 1, "rust": 1}, "go = '2'"),
            ("a duplicate key", '{"go": 2, "go": 1, "python": 1, "ts": 1, "rust": 1}', "duplicate"),
            ("not an object", "[2]", "must be a JSON object"),
        ):
            write_map(obj)
            try:
                load(root)
                fails.append(f"{what}: the map was accepted")
            except MajorsError as e:
                if frag not in str(e):
                    fails.append(f"{what}: refused, but not for {frag!r}: {e}")
        # Absent: main's case, every binding at ABI v1.
        (root / MAP).unlink()
        if load(root).by_binding != {b: 1 for b in BINDINGS} or load(root).present:
            fails.append("an absent map did not read as ABI v1 for every binding")

        # The assertion, on the identity a library or binding reports.
        fp1, fp2 = fingerprint(root, 1), fingerprint(root, 2)
        write_map({"go": 2, "python": 1, "ts": 1, "rust": 1})
        m = load(root)
        if identity_problems(root, m, "go", 2, fp2, "control"):
            fails.append("positive control: a v2 identity for go:2 was refused")
        if not identity_problems(root, m, "go", 1, fp1, "a v1 stub"):
            fails.append("go:2 accepted a library that answers ABI v1")
        if not identity_problems(root, m, "go", 2, fp1, "a v2 library with v1's fingerprint"):
            fails.append("go:2 accepted a v2 library carrying another fingerprint")
        if identity_problems(root, m, "python", 1, fp1, "control"):
            fails.append("positive control: python:1 refused a v1 identity")
        if not identity_problems(root, m, "python", 2, fp2, "a v2 stub for a v1 binding"):
            fails.append("python:1 accepted a library that answers ABI v2")

        # The same through a REAL dlopen of a compiled library, and through a
        # binding's printed identity line. A C compiler is required: a
        # selftest that cannot build its plants must fail, not skip.
        cc = shutil.which(os.environ.get("CC", "cc")) or shutil.which("cc")
        if cc is None:
            fails.append("no C compiler (cc) on PATH; the dlopen half of the selftest cannot run")
        else:
            libs = Path(tmp) / "libs"
            libs.mkdir()
            ext = ".dylib" if sys.platform == "darwin" else ".so"
            lib1, lib2 = libs / f"abi1{ext}", libs / f"abi2{ext}"
            try:
                _build_fake(cc, lib1, 1, fp1)
                _build_fake(cc, lib2, 2, fp2)
            except subprocess.CalledProcessError as e:
                fails.append(f"could not compile the planted libraries: {e}")
            else:
                if assert_library(root, "go", str(lib2)):
                    fails.append("positive control: go:2 refused a loaded library that answers ABI v2")
                p = assert_library(root, "go", str(lib1))
                if not p or "speaks ABI v1" not in p[0]:
                    fails.append(f"go:2 with a loaded v1 library was not refused for the major: {p}")
                if assert_library(root, "rust", str(lib1)):
                    fails.append("positive control: rust:1 refused a loaded library that answers ABI v1")
                (root / MAP).unlink()
                p = assert_library(root, "go", str(lib2))
                if not p:
                    fails.append("with no map (main) go accepted a loaded library that answers ABI v2")
                write_map({"go": 2, "python": 1, "ts": 1, "rust": 1})
        log = Path(tmp) / "go-test.log"
        log.write_text(f"=== RUN TestABIIdentity\n    chtypes_abi_identity binding=go abi=2 fingerprint={fp2}\n")
        if assert_identity_log(root, "go", str(log)):
            fails.append("positive control: a printed go v2 identity was refused for go:2")
        log.write_text(f"    chtypes_abi_identity binding=go abi=1 fingerprint={fp1}\n")
        if not assert_identity_log(root, "go", str(log)):
            fails.append("a printed go v1 identity was accepted for go:2")
        log.write_text("PASS\n")
        if not assert_identity_log(root, "go", str(log)):
            fails.append("a log with no identity line was accepted")
        # The compiled-in identity of the one layer present, wherever it is.
        write_map({"go": 2, "python": 1, "ts": 1, "rust": 1})
        layer = root / layer_dir("go", 2) / "abi_gen.go"
        layer.parent.mkdir(parents=True)
        layer.write_text(f'package abi2\n\nconst ChsAbiVersion = 2\n\nconst ChsAbiFingerprint = "{fp2}"\n')
        if assert_layer(root, "go"):
            fails.append("positive control: go:2 refused its own ABI v2 layer")
        old = root / layer_dir("go", 1) / "abi_gen.go"
        old.parent.mkdir(parents=True)
        old.write_text(f'package abi1\n\nconst ChsAbiVersion = 1\n\nconst ChsAbiFingerprint = "{fp1}"\n')
        if not assert_layer(root, "go"):
            fails.append("two Go layers in one tree (a v1 layer left behind) were accepted")
        layer.unlink()
        p = assert_layer(root, "go")
        if not p or "speaks ABI v1" not in p[0]:
            fails.append(f"go:2 with only an ABI v1 layer compiled was not refused for the major: {p}")
        write_map({"go": 2, "python": 1, "ts": 1})
        try:
            assert_identity_log(root, "go", str(log))
            fails.append("a map missing a binding reached the assertion")
        except MajorsError:
            pass
    for f in fails:
        print(f"majors.py --selftest: FAIL {f}", file=sys.stderr)
    if fails:
        return 1
    print(
        "majors.py --selftest: ok: a map missing a binding, an unknown key, an unknown major, a string, a duplicate and "
        "a non-object are refused; an absent map reads ABI v1 for all; go:2 refuses a loaded v1 library, a v2 library "
        "with v1's fingerprint, a printed v1 identity, a compiled v1 layer and two layers at once, and the positive controls pass"
    )
    return 0


# ------------------------------------------------------------------------ main


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("get", "layer-dir", "header"):
        sub.add_parser(name).add_argument("binding", choices=BINDINGS)
    a = sub.add_parser("announce")
    a.add_argument("binding", choices=BINDINGS)
    a.add_argument("--what", default="", help="what this job tests, for the summary line")
    for name, arg in (("assert-library", "library"), ("assert-stubs", "stubs_dir"), ("assert-identity", "log")):
        p = sub.add_parser(name)
        p.add_argument("binding", choices=BINDINGS)
        p.add_argument(arg)
    sub.add_parser("assert-layer").add_argument("binding", choices=BINDINGS)
    args = ap.parse_args(argv)
    try:
        m = load(ROOT)
        if args.cmd == "get":
            print(m[args.binding])
            return 0
        if args.cmd == "layer-dir":
            print(layer_dir(args.binding, m[args.binding]))
            return 0
        if args.cmd == "header":
            print(header_path(m[args.binding]))
            return 0
        if args.cmd == "announce":
            announce(ROOT, args.binding, args.what)
            return 0
        if args.cmd == "assert-library":
            problems = assert_library(ROOT, args.binding, args.library)
        elif args.cmd == "assert-stubs":
            problems = assert_library(ROOT, args.binding, stubs_ok_library(args.stubs_dir))
        elif args.cmd == "assert-layer":
            problems = assert_layer(ROOT, args.binding)
        else:
            problems = assert_identity_log(ROOT, args.binding, args.log)
    except MajorsError as e:
        print(f"majors.py: {e}", file=sys.stderr)
        return 1
    for p in problems:
        print(f"::error::majors.py: {p}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"majors.py: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
