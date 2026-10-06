#!/usr/bin/env python3
"""gen.py: generate the chtypes ABI v1 layer from its description.

    scripts/abi-v1/gen.py --write         regenerate every output from spec/abi-v1/
    scripts/abi-v1/gen.py --check         fail if any output differs from what --write would write
    scripts/abi-v1/gen.py --fingerprint   print CHS_ABI_FINGERPRINT
    scripts/abi-v1/gen.py --extract-v0    rewrite spec/abi-v1/v0-symbols.json from the released tags
    scripts/abi-v1/gen.py --selftest      prove every refusal fires, on temporary copies of the tree

    scripts/abi-v1/gen.py --major 2 --write|--check|--fingerprint
                                          the same, for ABI v2 (UNSTABLE): spec/abi-v2/ and its own outputs

MAJORS. Without --major, everything here is ABI v1 and behaves exactly as it
did before ABI v2 existed: the same inputs, outputs, banners and messages.
`--major N` reads spec/abi-vN/ instead and runs only the emitters that
generate for N (emit/__init__.py, MAJORS), into N's own paths: for ABI v2,
include/v2/chtypes.h, spec/abi-v2/generated/exports.txt and the block of
docs/reference/abi-v2.md. Each major's banner names its own command and
description, so one major's stale-file scan never sees, flags or deletes
another's outputs. From generation 2 on, the description is also held to its
generation's rules (model.py, generation_rule_problems). The generator keeps
its path under scripts/abi-v1/ because ABI v1's banner, which every v1 output
carries, names that path: moving it would change every v1 output. --selftest
proves both majors whatever --major says, so it refuses --major.

WHAT IT DOES. spec/abi-v1/abi.json is the ABI: types, enums, constants,
functions, the build_info shape, the document vocabularies. The SDK owns it
(decision D1.5) and its fingerprint, `sha256:` + the sha256 of its RFC 8785
canonical form (D1.1, scripts/abi-v1/jcs.py), is the ABI's identity. Every
other file that states the ABI is GENERATED from it by the emitters under
scripts/abi-v1/emit/, one module per output family, discovered by file name:
the C header include/chtypes.h, the export list, the generated block of
docs/reference/abi-v1.md, and each binding's declaration layer, stub and
conformance cases as those emitters land. Python 3.11+, standard library only.

WHAT --check REFUSES, each named with the file and, for a drifted output, the
first differing line:

  * an output that differs from the generator's, or is missing;
  * a file carrying the generated banner that no emitter produces any more
    (stale; --write deletes it);
  * a description outside the canonicalizable subset (non-ASCII, a float, an
    integer at or beyond 2**53, a duplicate key) or failing its JSON Schema;
  * a cross-reference the schema cannot express (model.py's third layer):
    among them a FIRM function that names a provisional handle or enum, a
    docs.md section missing for a symbol or present for none, a v0 name
    reused with a different signature while sdk.json's reuse_v0_names is
    false (D1.3), and ANY reused v0 name without the chs_abi_revision
    tombstone;
  * a generated file under a binding's source tree that matches the security
    carve-out's verification needles, read from scripts/policy-merge-check.py
    itself (its binding_of and verification_hits), so a generated file can
    never silently become verification code that no carve-out entry covers.

--selftest proves each of those fires, by planting it in a temporary copy of
the inputs and running --check there, and proves --write is deterministic
(two runs, identical bytes) and leaves hand-written text outside a generated
block alone. It then does the same for ABI v2 beside ABI v1 in one copy:
neither major's --write or --check touches the other's outputs, and each
generation-2 rule refuses a planted violation. It never touches the real tree.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import emit  # noqa: E402
import jcs  # noqa: E402
import model as abimodel  # noqa: E402
from emit.header import ProseError  # noqa: E402

PMC = "scripts/policy-merge-check.py"
# Directories never walked for stale generated files: build and dependency
# output, VCS metadata, other worktrees.
SKIP_DIRS = frozenset(
    {".git", "node_modules", "target", ".venv", "dist", "build", ".worktrees", ".claude", "__pycache__", ".bin"}
)


# ------------------------------------------------------------------- outputs


def command(major: int) -> str:
    """How to run this generator for one major, as its messages print it."""
    return "scripts/abi-v1/gen.py" if major == 1 else f"scripts/abi-v1/gen.py --major {major}"


def build(root: Path, major: int = 1) -> tuple[abimodel.Model, list[emit.Output]]:
    model = abimodel.load(root, major)
    outs: list[emit.Output] = []
    seen: dict[str, str] = {}
    for mod in emit.discover(major):
        for o in mod.outputs(model):
            key = f"{o.path}#{o.block}" if o.block else o.path
            if key in seen:
                raise abimodel.ModelError([f"{o.path}: produced by both {seen[key]} and {mod.__name__}"])
            seen[key] = mod.__name__
            outs.append(o)
    return model, outs


def splice(path: str, existing: str, name: str, body: str) -> str:
    begin, end = emit.block_markers(name)
    lines = existing.split("\n")
    b = [i for i, line in enumerate(lines) if line.strip() == begin]
    e = [i for i, line in enumerate(lines) if line.strip() == end]
    if len(b) != 1 or len(e) != 1 or b[0] >= e[0]:
        raise abimodel.ModelError(
            [
                f"{path}: needs exactly one {begin!r} line followed by exactly one {end!r} line (found {len(b)} and {len(e)})"
            ]
        )
    # A blank line before the end marker: Markdown formatters (dprint) require
    # one between a paragraph and an HTML comment.
    return "\n".join(lines[: b[0] + 1] + body.rstrip("\n").split("\n") + [""] + lines[e[0] :])


def expected_files(root: Path, outs: list[emit.Output], major: int = 1) -> dict[str, str]:
    """path -> the complete text the tree should hold."""
    banner_re = emit.banner_re(major)
    want: dict[str, str] = {}
    for o in outs:
        if o.content is not None:
            if not banner_re.search(o.content[:512]):
                raise abimodel.ModelError([f"{o.path}: the emitter's output has no banner in its first 512 bytes"])
            want[o.path] = o.content
            continue
        base = want.get(o.path)
        if base is None:
            try:
                base = (root / o.path).read_text(encoding="utf-8")
            except OSError:
                raise abimodel.ModelError(
                    [f"{o.path}: the generated block {o.block!r} needs this hand-written file to exist"]
                ) from None
        want[o.path] = splice(o.path, base, o.block, f"{o.body}")
    return want


def produced_outputs(root: Path = ROOT, major: int = 1) -> frozenset[str]:
    """The paths one major's emitters produce whole (the files that carry a banner), as repository-relative POSIX paths; ABI v1's by default.

    The three source checks that exempt generated files (check-no-hand-decls.py, check-no-error-code-table.py, check-quoting-passthrough.py) exempt a file only if it is in this set AND carries the banner. Membership, not the banner alone, because the stale-banner scan in check() skips SKIP_DIRS, so a hand-written file with a copied banner under such a directory would pass both. The set comes from the repository this script lives in, so a check run over a planted tree still names real output paths.
    """
    _, outs = build(root, major)
    return frozenset(o.path for o in outs if o.content is not None)


def banner_files(root: Path, major: int = 1) -> list[str]:
    """Every file whose first 512 bytes carry a real generated banner of this
    major's (another major's banner never matches: emit.banner_prefix)."""
    banner_re = emit.banner_re(major)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                with open(path, "rb") as f:
                    head = f.read(512)
            except OSError:
                continue
            if banner_re.search(head.decode("utf-8", "replace")):
                found.append(path.relative_to(root).as_posix())
    return found


def load_carve_out(root: Path):
    """scripts/policy-merge-check.py, imported for its binding_of and
    verification_hits: the same derivation the required check runs."""
    path = root / PMC
    spec = importlib.util.spec_from_file_location("_abi_v1_policy_merge_check", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise abimodel.ModelError([f"{PMC}: cannot load it, and the verification-needle check needs it"])
    mod = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their module through
    # sys.modules while the class bodies execute.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[spec.name]
        raise
    return mod


def needle_problems(root: Path, want: dict[str, str]) -> list[str]:
    pmc = load_carve_out(root)
    out = []
    for path, text in sorted(want.items()):
        g = pmc.binding_of(path)
        if g is None:
            continue
        hits = pmc.verification_hits(g.binding, text)
        if hits:
            out.append(
                f"{path}: a generated {g.binding} file matches the verification needle {hits[0]!r} "
                f"({PMC}); generated code must never be verification code (spell a digest `sha256:<hex>`)"
            )
    return out


def first_difference(want: str, have: str) -> str:
    a, b = want.split("\n"), have.split("\n")
    for i, (x, y) in enumerate(zip(a, b, strict=False), 1):
        if x != y:
            return f"line {i}: expected {x[:120]!r}, found {y[:120]!r}"
    i = min(len(a), len(b)) + 1
    return f"line {i}: " + ("the tree's copy ends early" if len(a) > len(b) else "the tree's copy has extra lines")


def check(root: Path, major: int = 1) -> list[str]:
    try:
        _, outs = build(root, major)
        want = expected_files(root, outs, major)
    except abimodel.ModelError as e:
        return e.problems
    except ProseError as e:
        return [str(e)]
    problems = []
    for path, text in sorted(want.items()):
        try:
            have = (root / path).read_text(encoding="utf-8")
        except OSError:
            problems.append(f"{path}: missing; run {command(major)} --write")
            continue
        if have != text:
            problems.append(
                f"{path}: differs from the generator's output ({first_difference(text, have)}); "
                f"edit {abimodel.spec(major).dir}/ and run {command(major)} --write, never the output"
            )
    produced = set(want)
    for path in banner_files(root, major):
        if path not in produced:
            problems.append(
                f"{path}: stale; it carries the generated banner but no emitter produces it (--write deletes it)"
            )
    try:
        problems += needle_problems(root, want)
    except abimodel.ModelError as e:
        problems += e.problems
    return problems


def write(root: Path, major: int = 1) -> list[str]:
    _, outs = build(root, major)
    want = expected_files(root, outs, major)
    for path, text in want.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    removed = []
    for path in banner_files(root, major):
        if path not in want:
            (root / path).unlink()
            removed.append(path)
    return removed


def render(root: Path, emitter: str, out: Path, major: int = 1) -> list[str]:
    """Write one emitter's build-time files under `out`; return their paths."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", emitter):
        raise ValueError(f"{emitter!r} is not an emitter module name")
    model = abimodel.load(root, major)
    mod = {m.__name__.rsplit(".", 1)[-1]: m for m in emit.discover(major)}.get(emitter)
    if mod is None:
        if major != 1 and (HERE / "emit" / f"{emitter}.py").is_file():
            raise ValueError(f"emit/{emitter}.py does not generate for ABI v{major} (its MAJORS)")
        raise ValueError(f"no emitter scripts/abi-v1/emit/{emitter}.py")
    fn = getattr(mod, "render_files", None)
    if not callable(fn):
        raise ValueError(f"emit/{emitter}.py defines no render_files(model)")
    written = []
    base = out.resolve()
    for rel, text in sorted(fn(model).items()):
        target = (base / rel).resolve()
        if base not in target.parents:
            raise ValueError(f"emit/{emitter}.py: {rel!r} escapes the output directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(str(target))
    return written


# ---------------------------------------------------------------- v0 symbols


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _version_key(tag: str) -> tuple:
    binding, _, version = tag.partition("/v")
    return tuple(int(p) for p in version.split(".")), binding


def parse_prototypes(text: str, where: str) -> dict[str, tuple[str, list[str]]]:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    protos: dict[str, tuple[str, list[str]]] = {}
    for m in re.finditer(r"CHS_API\s+([^;{}]+?)\b(chs_\w+)\s*\(([^;{}]*?)\)\s*;", text, flags=re.S):
        ret, name, params = m.group(1), m.group(2), m.group(3)
        if "(" in params or "..." in params:
            raise SystemExit(f"{where}: {name}: a function-pointer or variadic parameter; refusing to guess")
        plist = [p.strip() for p in params.split(",") if p.strip()]
        types = []
        if plist != ["void"]:
            for p in plist:
                d = abimodel.norm_c(p)
                mm = re.match(r"^(.*?)\s*\b([A-Za-z_]\w*)$", d)
                types.append(abimodel.norm_c(mm.group(1)) if mm and mm.group(1) else d)
        if name in protos:
            raise SystemExit(f"{where}: {name} declared twice")
        protos[name] = (abimodel.norm_c(ret), types)
    if not protos:
        raise SystemExit(f"{where}: no CHS_API prototypes")
    return protos


def _dump(obj, indent: int = 0) -> str:
    """JSON with objects indented and arrays of scalars kept on one line."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{pad}  {json.dumps(k)}: {_dump(v, indent + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        items = [f"{pad}  {_dump(v, indent + 1)}" for v in obj]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(obj)


def extract_v0(root: Path, major: int = 1) -> None:
    tags = sorted((t for t in _git(root, "tag", "-l", "*/v0.*").split() if t), key=_version_key)
    if not tags:
        raise SystemExit("--extract-v0: no */v0.* tags; fetch them (git fetch --tags)")
    sigs: dict[str, dict[tuple, list[str]]] = {}
    for tag in tags:
        text = _git(root, "show", f"{tag}:include/chtypes.h")
        for name, (ret, params) in parse_prototypes(text, f"{tag}:include/chtypes.h").items():
            sigs.setdefault(name, {}).setdefault((ret, tuple(params)), []).append(tag)
    doc = {
        "_note": "Every chs_* prototype in include/chtypes.h at every released tag (the four bindings, v0.x), "
        "extracted by scripts/abi-v1/gen.py --extract-v0. Types are normalized; parameter names are dropped. "
        "The D1.3 check compares each described function against every v0 signature of its name.",
        "tags": tags,
        "symbols": {
            name: [{"returns": r, "params": list(p), "tags": ts} for (r, p), ts in sorted(by.items())]
            for name, by in sorted(sigs.items())
        },
    }
    text = _dump(doc) + "\n"
    jcs.loads(text)  # the model reads it strictly: prove it can
    v0_json = abimodel.spec(major).v0_json
    (root / v0_json).write_text(text, encoding="utf-8")
    print(f"--extract-v0: {len(sigs)} symbols across {len(tags)} tags -> {v0_json}")


# ------------------------------------------------------------------ selftest


JCS_VECTORS = [
    ('{"b":1,"a":2}', '{"a":2,"b":1}'),
    (' { "a" : [ 1 , 2 ] , "c" : { } , "d" : [ ] } ', '{"a":[1,2],"c":{},"d":[]}'),
    ('{"z":[3,1,{"y":null,"x":true}],"a":false}', '{"a":false,"z":[3,1,{"x":true,"y":null}]}'),
    ('{"b":0,"B":0,"_":0,"1":0,"a_b":0,"ab":0}', '{"1":0,"B":0,"_":0,"a_b":0,"ab":0,"b":0}'),
    ("[0,-0,9007199254740991,-9007199254740991,-1]", "[0,0,9007199254740991,-9007199254740991,-1]"),
    ('{"s":"\\u0000\\u001f\\b\\f\\n\\r\\t\\"\\\\\\/\\u007f~ "}', '{"s":"\\u0000\\u001f\\b\\f\\n\\r\\t\\"\\\\/\x7f~ "}'),
    ('{"e":"\\u0041\\u000B"}', '{"e":"A\\u000b"}'),
]
JCS_REFUSALS = [
    ("1.0", "floating-point"),
    ("[1e2]", "floating-point"),
    ('{"a":NaN}', "constant"),
    ("[Infinity]", "constant"),
    ('{"\\u00e9":1}', "non-ASCII key"),
    ('["\\u00e9"]', "non-ASCII string"),
    ('{"a":1,"a":2}', "duplicate object key"),
    ("9007199254740992", "outside"),
    ("-9007199254740992", "outside"),
    (b"\xff", "not UTF-8"),
]


def _selftest_jcs() -> list[str]:
    fails = []
    for text, want in JCS_VECTORS:
        got = jcs.canonicalize(jcs.loads(text)).decode("utf-8")
        if got != want:
            fails.append(f"jcs: {text!r} canonicalized to {got!r}, RFC 8785 says {want!r}")
    for text, why in JCS_REFUSALS:
        try:
            jcs.loads(text)
            fails.append(f"jcs: {text!r} was accepted; it must refuse ({why})")
        except jcs.JCSError as e:
            if why not in str(e):
                fails.append(f"jcs: {text!r} refused for {e}, expected {why!r}")
    want = "sha256:" + hashlib.sha256(b'{"a":1}').hexdigest()
    if jcs.fingerprint(b'{ "a" : 1 }') != want:
        fails.append("jcs: the fingerprint is not sha256 over the canonical bytes")
    return fails


def _selftest_validator() -> list[str]:
    v = abimodel.validate
    cases = [
        ({"a": 1}, {"type": "object", "required": ["a", "b"]}, "missing required 'b'"),
        ({"a": 1}, {"type": "object", "additionalProperties": False}, "not allowed"),
        (True, {"const": 1}, "must be 1"),
        (1, {"enum": [True]}, "is not one of"),
        ("x1", {"pattern": "^[a-z]+$"}, "does not match"),
        ([1, 1], {"uniqueItems": True}, "duplicate item"),
        ({"k": "s"}, {"oneOf": [{"type": "object"}, {"required": ["k"]}]}, "matches 2 oneOf"),
        ({"K": 1}, {"propertyNames": {"pattern": "^[a-z]+$"}}, "does not match"),
        (5, {"$ref": "#/$defs/s", "$defs": {"s": {"type": "string"}}}, "expected string"),
    ]
    fails = []
    for inst, sch, frag in cases:
        errs = v(inst, sch)
        if not any(frag in e for e in errs):
            fails.append(f"validate: {inst!r} against {sch!r} gave {errs!r}, expected one naming {frag!r}")
    if v({"a": [1, "x"]}, {"properties": {"a": {"type": "array"}}}):
        fails.append("validate: a valid instance was refused")
    if not abimodel.schema_keyword_problems({"if": {}}):
        fails.append("validate: an unimplemented keyword (`if`) was not refused")
    return fails


def _selftest_build_info(model: abimodel.Model) -> list[str]:
    """The description's build_info schema accepts the example the artifact
    producer published for chs_build_info() (an unknown extra field
    included, since a new field must not be a refusal) and refuses each
    malformation a loader must refuse."""
    good = {
        "schema": 1,
        "abi": 1,
        "abi_fingerprint": "sha256:" + "4b" * 32,
        "clickhouse_version": "26.8.15.10",
        "channel": "lts",
        "clickhouse_minor": "26.8",
        "clickhouse_commit": "a" * 40,
        "core_commit": "b" * 40,
        "build": "20261001.183455",
        "inputs_sha256": "c" * 64,
        "os": "linux",
        "arch": "arm64",
        "toolchain": {"cc": "clang 21"},
        "capabilities": {
            "input_formats": ["JSONEachRow", "CSV"],
            "export_formats": ["JSONCompactEachRow"],
            "doc_flags": ["values", "transforms", "defaults"],
            "features": ["default_generators"],
        },
        "a_later_field": "accepted",
    }
    fails = []
    errs = abimodel.validate(good, model.build_info_schema)
    if errs:
        fails.append(f"build_info: the published example was refused: {errs}")
    bad = {
        "a missing field": lambda d: d.pop("build"),
        "schema 2": lambda d: d.update(schema=2),
        "a build spelled as a date": lambda d: d.update(build="2026-10-01"),
        "an uppercase fingerprint": lambda d: d.update(abi_fingerprint="sha256:" + "4B" * 32),
        "a fingerprint without its prefix": lambda d: d.update(abi_fingerprint="4b" * 32),
        "abi as a string": lambda d: d.update(abi="1"),
        "capabilities without doc_flags": lambda d: d["capabilities"].pop("doc_flags"),
        "capabilities without features": lambda d: d["capabilities"].pop("features"),
        "a feature that is not a string": lambda d: d["capabilities"].update(features=[1]),
    }
    for what, mutate in bad.items():
        d = json.loads(json.dumps(good))
        mutate(d)
        if not abimodel.validate(d, model.build_info_schema):
            fails.append(f"build_info: {what} was accepted")
    return fails


def _selftest_byte_strings(model: abimodel.Model) -> list[str]:
    """The byte_strings rule as the description's own document schemas
    enforce it: one form or the other, never both, a name always present,
    base64 only in its standard alphabet."""
    good = {
        "outcome": "accepted",
        "input_span": {"off": 0, "len": 6},
        "unknown_fields": [{"name": "u"}, {"name_b64": "/w=="}],
        "cols": [
            {"name": "s", "null": False, "stored_b64": "/wCA", "input_b64": "/wCA", "value_b64": "/wCA"},
            {"name_b64": "/2s=", "null": False, "stored": "x", "value_b64": "eA=="},
        ],
        "err_b64": "/w==",
    }
    schema = model.documents["row"].schema
    fails = []
    errs = abimodel.validate(good, schema)
    if errs:
        fails.append(f"byte_strings: a well-formed row document was refused: {errs}")
    bad = {
        "a rendering in both forms": lambda d: d["cols"][0].update(stored="x"),
        "a message in both forms": lambda d: d.update(err="boom"),
        "a column with no name in either form": lambda d: d["cols"][0].pop("name"),
        "a column name in both forms": lambda d: d["cols"][1].update(name="k"),
        "an unknown field as a bare string": lambda d: d["unknown_fields"].append("raw"),
        "base64 outside the standard alphabet": lambda d: d["cols"][0].update(stored_b64="_-8"),
        "unpadded base64": lambda d: d["cols"][0].update(value_b64="/wC"),
    }
    for what, mutate in bad.items():
        d = json.loads(json.dumps(good))
        mutate(d)
        if not abimodel.validate(d, schema):
            fails.append(f"byte_strings: {what} was accepted")
    batch = model.documents["batch"].schema
    gb = {
        "rows": [good],
        "unconsumed": [],
        "framing": {"bom_skipped": None, "container": None, "header": {"consumed": True, "lines": 1, "names": [{"name_b64": "/w=="}]}},
        "engine_rows": [[{"name": "s", "null": False, "stored_b64": "/wCA", "value_b64": "/wCA"}]],
    }
    errs = abimodel.validate(gb, batch)
    if errs:
        fails.append(f"byte_strings: a well-formed batch document was refused: {errs}")
    gb["engine_rows"] = [{"s": "x"}]
    if not abimodel.validate(gb, batch):
        fails.append("byte_strings: an engine row keyed by column name (rev-6's shape) was accepted")
    return fails


def _copy_inputs(src: Path, dst: Path, outs: list[emit.Output], extra: tuple[str, ...] = ()) -> None:
    paths = {"spec/abi-v1", "scripts/abi-v1", PMC, *extra} | {o.path for o in outs}
    for rel in sorted(paths):
        s, d = src / rel, dst / rel
        if s.is_dir():
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__"))
        elif s.is_file():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)


def _run(root: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(root / "scripts/abi-v1/gen.py"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return p.returncode, p.stdout + p.stderr


def _edit(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"selftest plant: {old!r} not found in {path}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def _json_edit(path: Path, fn) -> None:
    """Mutate a JSON file through its parsed form, so a plant never depends
    on how the file happens to be formatted."""
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _fn_entry(data: dict, name: str) -> dict:
    for f in data["functions"]:
        if f["name"] == name:
            return f
    raise AssertionError(f"selftest plant: no function {name!r}")


def _drop_section(path: Path, name: str) -> None:
    text = path.read_text(encoding="utf-8")
    pat = re.compile(r"^### `?" + re.escape(name) + r"`?\n.*?(?=^#{2,3} )", re.S | re.M)
    if not pat.search(text):
        raise AssertionError(f"selftest plant: no section {name!r} in {path}")
    path.write_text(pat.sub("", text, count=1), encoding="utf-8")


def _tree_digest(root: Path, paths: list[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.encode() + b"\0" + (root / p).read_bytes() + b"\0")
    return h.hexdigest()


def _selftest_tree() -> list[str]:
    fails: list[str] = []
    _, outs = build(ROOT)
    with tempfile.TemporaryDirectory(prefix="abi-v1-selftest-") as tmp:
        pristine = Path(tmp) / "pristine"
        _copy_inputs(ROOT, pristine, outs)

        # --write is deterministic, and --check accepts what it wrote.
        rc, log = _run(pristine, "--write")
        if rc:
            return [f"--write failed on a copy of the tree:\n{log}"]
        paths = sorted({o.path for o in outs})
        first = _tree_digest(pristine, paths)
        rc, log = _run(pristine, "--write")
        if rc or _tree_digest(pristine, paths) != first:
            fails.append("--write run twice did not produce byte-identical outputs")
        rc, log = _run(pristine, "--check")
        if rc:
            return fails + [f"--check refused what --write had just written:\n{log}"]

        n = 0

        def plant(what: str, mutate, expect: str | None, args: tuple[str, ...] = ("--check",)) -> None:
            nonlocal n
            n += 1
            work = Path(tmp) / f"plant-{n}"
            shutil.copytree(pristine, work)
            try:
                mutate(work)
            except AssertionError as e:
                fails.append(f"plant {n} ({what}): could not be planted: {e}")
                return
            rc, log = (0, "")
            for a in args:
                rc, log = _run(work, a)
            if expect is None:
                if rc != 0:
                    fails.append(f"plant {n} ({what}): expected --check to pass, it failed:\n{log}")
            elif rc == 0:
                fails.append(f"plant {n} ({what}): --check passed; it must refuse")
            elif expect not in log:
                fails.append(f"plant {n} ({what}): refused, but not for {expect!r}:\n{log}")

        abi, sdk, docs = "spec/abi-v1/abi.json", "spec/abi-v1/sdk.json", "spec/abi-v1/docs.md"

        # A hand edit to any generated file is drift.
        for o in outs:
            if o.content is not None:
                plant(f"hand edit to {o.path}", lambda w, p=o.path: _edit(w / p, "\n", "\n/* hand */\n"), o.path)
            else:
                begin, _ = emit.block_markers(o.block)
                plant(
                    f"hand edit inside the block of {o.path}",
                    lambda w, p=o.path, b=begin: _edit(w / p, b + "\n", b + "\nhand-written\n"),
                    o.path,
                )
                plant(
                    f"hand edit outside the block of {o.path} is allowed",
                    lambda w, p=o.path, b=begin: _edit(w / p, b, "Hand-written prose stays.\n\n" + b),
                    None,
                )
                plant(
                    f"a missing end marker in {o.path}",
                    lambda w, p=o.path, blk=o.block: _edit(w / p, emit.block_markers(blk)[1], ""),
                    "exactly one",
                )

        plant("a schema violation", lambda w: _json_edit(w / abi, lambda d: d.update(surprise=1)), "abi.schema.json")
        plant(
            "a float in the description", lambda w: _json_edit(w / abi, lambda d: d.update(abi=1.0)), "floating-point"
        )
        plant(
            "a non-ASCII string in the description",
            lambda w: _json_edit(w / abi, lambda d: d["library"].update(stem="libchtyp\u00e9s")),
            "non-ASCII",
        )
        plant("a duplicate key", lambda w: _edit(w / abi, "{", '{\n  "abi": 1,'), "duplicate object key")
        plant(
            "an unimplemented schema keyword",
            lambda w: _json_edit(w / "spec/abi-v1/schema/abi.schema.json", lambda d: d.update({"if": {}})),
            "not implemented",
        )

        def collide(w: Path) -> None:
            _json_edit(w / abi, lambda d: _fn_entry(d, "chs_back_quote").update(name="chs_quote_identifier"))
            _edit(w / docs, "### chs_back_quote\n", "### chs_quote_identifier\n")

        def reuse_off(w: Path) -> None:
            _json_edit(w / sdk, lambda d: d.update(reuse_v0_names=False))

        def collide_off(w: Path) -> None:
            collide(w)
            reuse_off(w)

        plant(
            "a v0 name reused with a different signature while reuse_v0_names is false",
            collide_off,
            "reuses a v0 name with a different signature",
        )

        def collide_on(w: Path) -> None:
            collide(w)
            _json_edit(w / sdk, lambda d: d.update(reuse_v0_names=True))

        plant("the same reuse while reuse_v0_names is true", collide_on, None, ("--write", "--check"))

        def no_tombstone(w: Path) -> None:
            _json_edit(
                w / abi,
                lambda d: d.update(functions=[f for f in d["functions"] if f["name"] != abimodel.TOMBSTONE]),
            )
            _drop_section(w / docs, abimodel.TOMBSTONE)
            _json_edit(w / sdk, lambda d: d.update(reuse_v0_names=True))

        plant("v0 names reused without the tombstone", no_tombstone, "without the chs_abi_revision tombstone")

        def wrong_tombstone(w: Path) -> None:
            _json_edit(w / abi, lambda d: d["constants"][abimodel.TOMBSTONE_CONSTANT].update(value=7))

        plant("a tombstone that does not return 1001", wrong_tombstone, "= 1001")
        plant(
            "a missing docs.md section",
            lambda w: _drop_section(w / docs, "chs_buf_len"),
            "no `### chs_buf_len` section",
        )
        plant(
            "a docs.md section for nothing",
            lambda w: _edit(w / docs, "### chs_buf_len\n", "### chs_nonexistent\n\nProse.\n\n### chs_buf_len\n"),
            "does not define",
        )
        plant(
            "prose that would close a C comment",
            lambda w: _edit(w / docs, "### chs_buf_len\n\n", "### chs_buf_len\n\nA */ inside.\n\n"),
            "cannot sit inside a C comment",
        )

        def firm_on_provisional(w: Path) -> None:
            # Nothing is provisional once the ABI is confirmed, so plant a
            # provisional handle (and its free, whose markers must match) with
            # a legend entry, and leave chs_schema_describe FIRM.
            def mutate(d: dict) -> None:
                d["handles"]["chs_schema"]["provisional"] = ["A99"]
                _fn_entry(d, "chs_schema_free")["provisional"] = ["A99"]

            _json_edit(w / abi, mutate)
            _json_edit(w / sdk, lambda d: d["provisional_markers"].update(A99="a planted marker"))

        plant(
            "a FIRM function on a provisional handle",
            firm_on_provisional,
            "references the provisional handle chs_schema",
        )
        plant(
            "an undefined provisional marker",
            lambda w: _json_edit(w / abi, lambda d: _fn_entry(d, "chs_shutdown").update(provisional=["A99"])),
            "'A99' has no entry",
        )
        plant(
            "a call that reads a handle but is not `shared`",
            lambda w: _json_edit(w / abi, lambda d: _fn_entry(d, "chs_preview_row").update(thread="handle_serial")),
            "its thread class must be `shared`",
        )
        plant(
            "a free that is not `handle_serial`",
            lambda w: _json_edit(w / abi, lambda d: _fn_entry(d, "chs_filter_free").update(thread="shared")),
            "a handle's free is thread class `handle_serial`",
        )
        plant(
            "`shared` on a call that takes no handle",
            lambda w: _json_edit(w / abi, lambda d: _fn_entry(d, "chs_quote_string").update(thread="shared")),
            "applies only to a call that takes a handle",
        )
        def drop_sibling(w: Path) -> None:
            def mutate(d: dict) -> None:
                del d["documents"]["row"]["schema"]["$defs"]["col"]["properties"]["stored_b64"]

            _json_edit(w / abi, mutate)

        plant("a byte field with no _b64 sibling", drop_sibling, "must be a standard-base64 string")

        def unlisted(w: Path) -> None:
            _json_edit(w / abi, lambda d: d["documents"]["row"]["byte_fields"].remove("cols[].wire"))

        plant("a _b64 member no byte_fields path names", unlisted, "but no byte_fields path names 'wire'")

        def no_rule(w: Path) -> None:
            def mutate(d: dict) -> None:
                d["documents"]["filter_result"]["schema"]["$defs"]["filter_error"].pop("allOf")

            _json_edit(w / abi, mutate)

        plant("a byte field whose object has no one-of-two rule", no_rule, "has no rule allowing exactly (or at most) one")
        plant(
            "a byte field that resolves to nothing",
            lambda w: _json_edit(w / abi, lambda d: d["documents"]["batch"]["byte_fields"].append("rows[].nowhere")),
            "does not resolve",
        )
        plant(
            "a thread class the model does not describe",
            lambda w: _json_edit(
                w / "spec/abi-v1/schema/abi.schema.json",
                lambda d: d["$defs"]["function"]["properties"]["thread"]["enum"].append("lockstep"),
            ),
            "differs from the classes model.py describes",
        )

        def needle(w: Path) -> None:
            (w / "scripts/abi-v1/emit" / "zz_selftest_needle.py").write_text(
                "from . import Output, banner\n\n\n"
                "def outputs(model):\n"
                "    lit = '\"' + 'ab' * 32 + '\"'\n"
                "    return [Output('go/internal/abi1/selftest_gen.go', content=f'// {banner(model)}\\n"
                "package abi1\\n\\nconst k = {lit}\\n')]\n"
            )

        plant(
            "a generated binding file matching a verification needle",
            needle,
            "verification needle",
            ("--write", "--check"),
        )

        def stale(w: Path) -> None:
            fp = jcs.fingerprint((w / abi).read_bytes())
            p = w / "go/internal/abi1/old_gen.go"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"// {emit.BANNER_PREFIX} (CHS_ABI_FINGERPRINT {fp}) — DO NOT EDIT\npackage abi1\n")

        plant("a stale generated file", stale, "stale")
        plant("--write removes a stale generated file", stale, None, ("--write", "--check"))

        # --render writes an emitter's build-time files, and only under --out.
        work = Path(tmp) / "render"
        shutil.copytree(pristine, work)
        (work / "scripts/abi-v1/emit" / "zz_selftest_render.py").write_text(
            "def outputs(model):\n    return []\n\n\n"
            "def render_files(model):\n    return {'sub/stub.c': '/* ' + model.fingerprint + ' */\\n'}\n"
        )
        out_dir = Path(tmp) / "render-out"
        rc, log = _run(work, "--render", "zz_selftest_render", "--out", str(out_dir))
        made = out_dir / "sub" / "stub.c"
        if rc or not made.is_file() or jcs.fingerprint((work / abi).read_bytes()) not in made.read_text():
            fails.append(f"--render did not write the emitter's build-time file:\n{log}")
        rc, log = _run(work, "--check")
        if rc:
            fails.append(f"an emitter with only build-time files broke --check:\n{log}")
        (work / "scripts/abi-v1/emit" / "zz_selftest_render.py").write_text(
            "def outputs(model):\n    return []\n\n\ndef render_files(model):\n    return {'../escape.c': ''}\n"
        )
        rc, log = _run(work, "--render", "zz_selftest_render", "--out", str(out_dir))
        if rc == 0 or "escapes the output directory" not in log:
            fails.append(f"--render wrote outside --out:\n{log}")
    return fails


def _selftest_v2() -> list[str]:
    """ABI v2 beside ABI v1 in one tree: neither major's --write or --check
    reads, flags or deletes the other's outputs, and every generation-2 rule
    gen.py enforces refuses a planted violation."""
    fails: list[str] = []
    _, outs1 = build(ROOT, 1)
    _, outs2 = build(ROOT, 2)
    v1_paths = sorted({o.path for o in outs1})
    v2_paths = sorted({o.path for o in outs2})
    v2 = abimodel.spec(2)
    abi, schema, sdk, docs = v2.abi_json, v2.abi_schema, v2.sdk_json, v2.docs_md
    with tempfile.TemporaryDirectory(prefix="abi-v2-selftest-") as tmp:
        pristine = Path(tmp) / "pristine"
        _copy_inputs(ROOT, pristine, outs1 + outs2, extra=(v2.dir,))

        # The two majors share one tree and never touch each other's outputs.
        v1_before = _tree_digest(pristine, v1_paths)
        rc, log = _run(pristine, "--major", "2", "--write")
        if rc:
            return [f"--major 2 --write failed on a copy of the tree:\n{log}"]
        if _tree_digest(pristine, v1_paths) != v1_before:
            fails.append("--major 2 --write changed an ABI v1 output")
        v2_first = _tree_digest(pristine, v2_paths)
        rc, log = _run(pristine, "--major", "2", "--write")
        if rc or _tree_digest(pristine, v2_paths) != v2_first:
            fails.append("--major 2 --write run twice did not produce byte-identical outputs")
        rc, log = _run(pristine, "--check")
        if rc:
            fails.append(f"ABI v1's --check refused a tree that also holds ABI v2's outputs:\n{log}")
        rc, log = _run(pristine, "--write")
        if rc or not all((pristine / p).is_file() for p in v2_paths) or _tree_digest(pristine, v2_paths) != v2_first:
            fails.append(f"ABI v1's --write removed or changed an ABI v2 output:\n{log}")
        rc, log = _run(pristine, "--major", "2", "--check")
        if rc:
            return fails + [f"--major 2 --check refused what --major 2 --write had just written:\n{log}"]

        n = 0

        def plant(what: str, mutate, expect: str | None, runs=(("--major", "2", "--check"),)) -> None:
            nonlocal n
            n += 1
            work = Path(tmp) / f"plant-{n}"
            shutil.copytree(pristine, work)
            try:
                mutate(work)
            except AssertionError as e:
                fails.append(f"v2 plant {n} ({what}): could not be planted: {e}")
                return
            rc, log = (0, "")
            for argv in runs:
                rc, log = _run(work, *argv)
            if expect is None:
                if rc != 0:
                    fails.append(f"v2 plant {n} ({what}): expected a pass, it failed:\n{log}")
            elif rc == 0:
                fails.append(f"v2 plant {n} ({what}): passed; it must refuse")
            elif expect not in log:
                fails.append(f"v2 plant {n} ({what}): refused, but not for {expect!r}:\n{log}")

        plant(
            "a hand edit to the v2 header",
            lambda w: _edit(w / "include/v2/chtypes.h", "\n", "\n/* hand */\n"),
            "include/v2/chtypes.h",
        )

        def stale(major: int):
            def mutate(w: Path) -> None:
                fp = jcs.fingerprint((w / abimodel.spec(major).abi_json).read_bytes())
                (w / f"stale-v{major}.txt").write_text(
                    f"# {emit.banner_prefix(major)} (CHS_ABI_FINGERPRINT {fp}) — DO NOT EDIT\n"
                )

            return mutate

        plant("a stale ABI v2 output", stale(2), "stale")
        plant("a stale ABI v2 output is not ABI v1's business", stale(2), None, (("--check",),))
        plant("a stale ABI v1 output is not ABI v2's business", stale(1), None)

        # The description says it is generation 2, in its schema and in the model.
        plant("abi 1 in the v2 description", lambda w: _json_edit(w / abi, lambda d: d.update(abi=1)), "abi.schema.json")

        def abi_one_unpinned(w: Path) -> None:
            _json_edit(w / abi, lambda d: d.update(abi=1))
            _json_edit(w / schema, lambda d: d["properties"].update(abi={"type": "integer", "minimum": 1}))

        plant("abi 1, with the schema no longer pinning it", abi_one_unpinned, "this is the description of ABI v2")

        def no_stability(w: Path) -> None:
            _json_edit(w / abi, lambda d: d.pop("stability"))
            _json_edit(w / schema, lambda d: d["required"].remove("stability"))

        plant("no stability, with the schema no longer requiring it", no_stability, "declares `stability`")

        # The rules are stated, every one.
        plant("no ## Rules section", lambda w: _edit(w / docs, "## Rules\n", ""), "no `## Rules` section")
        plant("a rule dropped", lambda w: _edit(w / docs, "(r4)", "(rX)", count=99), "does not state (r4)")

        # (r2) no result schema closes an object.
        plant(
            "(r2) a document schema closed to new fields",
            lambda w: _json_edit(w / abi, lambda d: d["documents"]["row"]["schema"].update(additionalProperties=False)),
            "documents.row.schema closes an object",
        )
        plant(
            "(r2) build_info closed to new fields",
            lambda w: _json_edit(
                w / abi,
                lambda d: d["build_info"]["schema"]["properties"]["capabilities"].update(additionalProperties=False),
            ),
            "build_info.schema/properties/capabilities closes an object",
        )

        # (r3) no described value takes the unknown(n) member's name.
        plant(
            "(r3) a vocabulary value spelled unknown",
            lambda w: _json_edit(w / abi, lambda d: d["enums"]["default_kind"]["values"].append({"value": "unknown"})),
            "enum default_kind: the value 'unknown' takes the name (r3) reserves",
        )
        plant(
            "(r3) an int32 value named *_UNKNOWN",
            lambda w: _json_edit(
                w / abi,
                lambda d: d["enums"]["chs_format"]["values"].append(
                    {"name": "CHS_FORMAT_UNKNOWN", "value": 99, "ch_name": "Unknown"}
                ),
            ),
            "enum chs_format: the value 'CHS_FORMAT_UNKNOWN' takes the name (r3) reserves",
        )

        # (r4) generation 1's published codes keep their names and numbers.
        def renumber(d: dict) -> None:
            for v in d["enums"]["chs_status"]["values"]:
                if v["name"] == "CHS_INTERNAL":
                    v["value"] = 5

        plant("(r4) a published status renumbered", lambda w: _json_edit(w / abi, renumber), "CHS_INTERNAL = 4 is published")
        plant(
            "(r4) a published error code dropped",
            lambda w: _json_edit(w / sdk, lambda d: d["errors"]["codes"].pop("artifact_corrupt")),
            "errors.codes artifact_corrupt = 'CHTYPES_ARTIFACT_CORRUPT' is published",
        )
        plant(
            "(r4) a published error code reused",
            lambda w: _json_edit(w / sdk, lambda d: d["errors"]["codes"].update(later="CHTYPES_ARTIFACT_CORRUPT")),
            "reuses the published code 'CHTYPES_ARTIFACT_CORRUPT'",
        )
        plant(
            "(r4) generation 1's description unreadable",
            lambda w: (w / abimodel.spec(1).abi_json).unlink(),
            "(r4) compares against generation 1's published codes",
        )

        # Locking drops the UNSTABLE notes, and the outputs follow the description.
        def lock(w: Path) -> None:
            _json_edit(w / abi, lambda d: d.update(stability="locked"))

        plant("a locked description generates and checks", lock, None, (("--major", "2", "--write"), ("--major", "2", "--check")))
        work = Path(tmp) / "locked"
        shutil.copytree(pristine, work)
        lock(work)
        rc, log = _run(work, "--major", "2", "--write")
        header = (work / "include/v2/chtypes.h").read_text(encoding="utf-8")
        page = (work / "docs/reference/abi-v2.md").read_text(encoding="utf-8")
        if rc or " * UNSTABLE: " in header or "| stability             | locked" not in page:
            fails.append(f"a locked description still generated the UNSTABLE notes, or no `locked` row:\n{log}")
        if " * UNSTABLE: " not in (pristine / "include/v2/chtypes.h").read_text(encoding="utf-8"):
            fails.append("the unstable description's header carries no UNSTABLE note")
    return fails


def selftest() -> int:
    fails = _selftest_jcs() + _selftest_validator()
    if not fails:
        try:
            m = abimodel.load(ROOT)
            fails += _selftest_build_info(m)
            fails += _selftest_byte_strings(m)
        except abimodel.ModelError as e:
            fails += [f"the tree's own description does not load: {p}" for p in e.problems]
    if not fails:
        fails += _selftest_tree()
    if not fails:
        fails += _selftest_v2()
    for f in fails:
        print(f"gen.py --selftest: FAIL {f}", file=sys.stderr)
    if fails:
        return 1
    print(
        "gen.py --selftest: ok: RFC 8785 vectors and refusals, the schema validator, --write deterministic, "
        "the byte_strings rule, and every planted drift, schema, JCS, D1.3, tombstone, docs, prose, marker, thread-class, "
        "byte-field, needle and stale case refused; and ABI v2 beside it: neither major touches the other's outputs, "
        "and every generation-2 rule (abi == major, stability, the Rules section, r2, r3, r4) refused"
    )
    return 0


# ---------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate every output")
    mode.add_argument("--check", action="store_true", help="fail on any drift (CI)")
    mode.add_argument("--fingerprint", action="store_true", help="print CHS_ABI_FINGERPRINT")
    mode.add_argument(
        "--extract-v0",
        action="store_true",
        help="rewrite the major's v0-symbols.json (spec/abi-v1/v0-symbols.json by default) from git tags",
    )
    mode.add_argument("--selftest", action="store_true", help="prove every refusal fires, for every major")
    mode.add_argument(
        "--render",
        metavar="EMITTER",
        help="write emit/EMITTER.py's build-time (never committed) files under --out, e.g. the test stub's source",
    )
    ap.add_argument("--out", type=Path, help="the directory --render writes into")
    ap.add_argument(
        "--major",
        type=int,
        choices=abimodel.MAJORS,
        help="the ABI major to generate: its description under spec/abi-vN/ and its own outputs (default 1)",
    )
    args = ap.parse_args(argv)

    if args.selftest:
        if args.major is not None:
            ap.error("--selftest proves every major at once; drop --major")
        return selftest()
    major = args.major or 1
    sp = abimodel.spec(major)
    if args.render:
        if args.out is None:
            ap.error("--render needs --out DIR")
        try:
            written = render(ROOT, args.render, args.out, major)
        except (abimodel.ModelError, ProseError, ValueError) as e:
            for p in getattr(e, "problems", [str(e)]):
                print(f"gen.py --render: {p}", file=sys.stderr)
            return 1
        for p in written:
            print(p)
        return 0
    if args.extract_v0:
        extract_v0(ROOT, major)
        return 0
    if args.fingerprint:
        try:
            print(jcs.fingerprint((ROOT / sp.abi_json).read_bytes()))
        except (OSError, jcs.JCSError) as e:
            print(f"gen.py: {sp.abi_json}: {e}", file=sys.stderr)
            return 1
        return 0
    if args.write:
        try:
            removed = write(ROOT, major)
        except abimodel.ModelError as e:
            for p in e.problems:
                print(f"gen.py --write: {p}", file=sys.stderr)
            return 1
        except ProseError as e:
            print(f"gen.py --write: {e}", file=sys.stderr)
            return 1
        for p in removed:
            print(f"gen.py --write: removed stale {p}")
        problems = check(ROOT, major)
        for p in problems:
            print(f"gen.py --write: {p}", file=sys.stderr)
        if problems:
            return 1
        print(f"gen.py --write: ok ({jcs.fingerprint((ROOT / sp.abi_json).read_bytes())})")
        return 0
    problems = check(ROOT, major)
    for p in problems:
        print(f"gen.py --check: {p}", file=sys.stderr)
    if problems:
        print(f"gen.py --check: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"gen.py --check: ok: every output matches {sp.dir}/ ({jcs.fingerprint((ROOT / sp.abi_json).read_bytes())})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
