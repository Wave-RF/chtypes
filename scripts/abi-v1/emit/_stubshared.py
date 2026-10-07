"""_stubshared.py: facts shared between emit/stub.py, emit/cases.py and
build-stubs.sh, so the shipped stub libraries and the cases that describe
them can never silently drift apart.

Starts with "_": emit.discover() skips it. It defines no outputs() or
render_files() and is never an emitter by itself.

HEAD_BYTES is the number of leading bytes of a bytes_in argument the stub's
echo reports as `head_hex`, next to `len` and `sha256`; emit/stub.py (which
generates the C that computes these at run time) and emit/cases.py (which
precomputes the expected values with hashlib, at generation time) both read
this one constant.

THE VARIANT PLAN. plan(model) is the single list of stub libraries
build-stubs.sh must build: the happy path (ok, ok-b), each loader-refusal
shape from the plan's table (abi version, build_info, fingerprint, the
process-level traps), and one missing-<sym> per exported symbol. Three
consumers need the identical list and none of them is allowed to recompute
it independently:

  * build-stubs.sh (bash) gets it as "|"-delimited lines by running this
    file as a script: python3 scripts/abi-v1/emit/_stubshared.py
    --list-variants — one line per variant: name, a comma-joined list of
    `MACRO` or `MACRO=value` defines, the expected loader-refusal reason
    (sdk.json's vocabulary, or "accepted" for ok/ok-b), and
    link_allow_undefined ("0"/"1"). "|", not a tab: bash's `read` collapses
    a run of IFS whitespace (which a tab counts as), silently dropping
    ok/ok-b's empty `defines` field and shifting every later field by one;
    "|" is a strict single-character delimiter there;
  * emit/stub.py reads VARIANT_DEFINE_SYMBOL to know which preprocessor guard
    name a described symbol's omission uses (it emits the guard around every
    function's definition);
  * emit/cases.py's loader cases enumerate plan(model) directly, so a case
    exists for exactly the libraries build-stubs.sh actually produces.

Run standalone (`__main__`), this file adds its own parent directory
(scripts/abi-v1) to sys.path so it can `import model`, exactly as gen.py
does; imported as `emit._stubshared` from another emitter, that is already
on sys.path courtesy of gen.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent  # scripts/abi-v1/emit
SCRIPTS_ABI_V1 = HERE.parent  # scripts/abi-v1
ROOT = SCRIPTS_ABI_V1.parent.parent  # repository root

HEAD_BYTES = 16

# DOCUMENT MODE: the byte_strings rule as the stub models it. A call to one of
# DOC_FUNCTIONS whose `param` begins DOC_PREFIX returns a small document of
# that kind instead of its echo, built from the rest of the parameter (the
# "payload") by the template below. The template is the ONE source of the
# document's layout: emit/stub.py compiles it to C, and doc_render() renders
# it in Python for emit/cases.py's expectation. What the two do NOT share is
# the rule itself: the C side decides UTF-8 validity and spells base64 with
# its own code (emit/stub.py's preamble), the Python side with the standard
# library, and a conformance case compares the two documents exactly. So a
# case proves the stub's rule, and every binding's strict JSON decoding of a
# document that carries `_b64` members and `value_b64`, end to end.
#
# Template steps:
#   ("raw", text)          JSON text, ASCII, emitted verbatim;
#   ("len",)               the whole parameter's length, in decimal;
#   ("member", key, src)   the byte_strings rule: "key":"<text>" for valid
#                          UTF-8, "key_b64":"<base64>" otherwise;
#   ("b64", src)           a JSON string holding src's standard base64;
# where src is ("lit", bytes) or ("payload", suffix_bytes): the payload with
# the suffix appended.
DOC_PREFIX = b"!D:"
_COL = ("lit", b"s")
DOC_TEMPLATES = {
    "row": [
        ("raw", '{"outcome":"accepted","input_span":{"off":0,"len":'),
        ("len",),
        ("raw", '},"unknown_fields":[],"unsupported_settings":[],"cols":[{'),
        ("member", "name", _COL),
        ("raw", ","),
        ("member", "type", ("lit", b"String")),
        ("raw", ',"src":"input",'),
        ("member", "input", ("payload", b"")),
        ("raw", ","),
        ("member", "stored", ("payload", b"")),
        ("raw", ',"null":false,"value_b64":'),
        ("b64", ("payload", b"")),
        ("raw", '}],"transformed":[]}'),
    ],
    "discovery": [
        ("raw", '{"columns":[{'),
        ("member", "name", ("payload", b"")),
        ("raw", ',"type":"String","default_kind":"","default_expression":"",'),
        ("member", "declaration", ("payload", b" String")),
        ("raw", "}],"),
        ("member", "columns_sql", ("payload", b" String")),
        ("raw", "}"),
    ],
}
# function -> (its bytes_in parameter that carries the prefix, the template)
DOC_FUNCTIONS = {
    "chs_preview_row": ("body", "row"),
    "chs_discover_columns": ("rows", "discovery"),
}


def _doc_src(src: tuple, payload: bytes) -> bytes:
    return src[1] if src[0] == "lit" else payload + src[1]


def doc_render(template: str, param: bytes) -> dict:
    """The document the stub returns for `param` (which starts DOC_PREFIX),
    rendered in Python from the same template the stub's C is compiled from."""
    import base64
    import json

    payload = param[len(DOC_PREFIX) :]
    out: list[str] = []
    for step in DOC_TEMPLATES[template]:
        op = step[0]
        if op == "raw":
            out.append(step[1])
        elif op == "len":
            out.append(str(len(param)))
        elif op == "member":
            data = _doc_src(step[2], payload)
            try:
                out.append(json.dumps(step[1]) + ":" + json.dumps(data.decode("utf-8")))
            except UnicodeDecodeError:
                out.append(json.dumps(step[1] + "_b64") + ":" + json.dumps(base64.b64encode(data).decode("ascii")))
        elif op == "b64":
            out.append(json.dumps(base64.b64encode(_doc_src(step[1], payload)).decode("ascii")))
        else:
            raise ValueError(f"DOC_TEMPLATES: unknown step {op!r}")
    return json.loads("".join(out))


# The one-CREATE rule (chs_schema_create takes exactly one CREATE TABLE
# statement), as the stub models it and as the case that probes it expects
# it: emit/stub.py generates the C check from this dict and emit/cases.py the
# case, so the two can never disagree. The stub is a test double, not
# ClickHouse: it refuses when a `;` is followed by any byte other than ASCII
# whitespace or another `;`, with no notion of quoting or comments. That is
# enough to prove a binding passes the whole statement through unsplit and
# maps the refusal to its schema-error class with every field intact; what a
# real library answers is ClickHouse's own parser's refusal of a second
# statement in one query (the code and name below are that refusal's, read
# from ClickHouse's parseQuery source, not measured here).
ONE_CREATE = {
    "fn": "chs_schema_create",
    "param": "create_table",
    "status": "CHS_REJECTED",
    "ch_code": 62,
    "ch_name": "SYNTAX_ERROR",
    "message": "Multi-statements are not allowed",
}

# The image zone's process-once rule (chs_initialize), as the stub models it
# and as the cases that probe it expect it: emit/stub.py generates the C from
# this dict, and emit/cases.py (the call-level cases) and emit/setup_cases.py
# (the public-API setup case) read the same values, so the three can never
# disagree. The library's rule, which the stub restates:
#
#   * a zone ClickHouse's DateLUT cannot load is refused with CHS_REJECTED and
#     ClickHouse's own code, and commits NOTHING: a later, different, good
#     zone on the same image is accepted (`measured` on darwin-arm64
#     26.8.15.10, calling chs_initialize directly: "Not/AZone" answered
#     CHS_REJECTED with code 36, then "Europe/Berlin" answered CHS_OK on the
#     same image);
#   * once a zone is accepted, the same spelling again is CHS_OK and a
#     different spelling is CHS_INVALID_ARGUMENT (the same measurement's
#     control: "Europe/Berlin" then "UTC").
#
# The stub is a test double, not ClickHouse: it refuses exactly one sentinel
# spelling, `bad_zone`, and accepts every other one. The code is the one the
# measurement above returned, and the name is ClickHouse's own name for that
# code; the two messages are the stub's stand-ins, not ClickHouse's text. A
# status injection (`!S:`) is answered before this rule and commits nothing.
IMAGE_ZONE = {
    "fn": "chs_initialize",
    "param": "timezone",
    "bad_zone": "Not/AZone",
    "status": "CHS_REJECTED",
    "ch_code": 36,
    "ch_name": "BAD_ARGUMENTS",
    "message": "Cannot load time zone Not/AZone",
    "conflict_status": "CHS_INVALID_ARGUMENT",
    "conflict_message": "the image zone is already set, to a different spelling",
}

# The image zone probe: the one way a test reads back the zone an image was set
# up with, through a binding's PUBLIC API. A call to `fn` whose `param` is
# exactly `probe` returns, as its output, the zone the image holds
# (IMAGE_ZONE's committed spelling, empty when none was committed yet),
# instead of its echo. The setup cases (emit/setup_cases.py) use it to prove an
# open ran step 7 under the zone the caller recorded, not under the empty
# setup. A real library has no such call: this is the stub's alone.
ZONE_PROBE = {
    "fn": "chs_type_validate",
    "param": "type_expr",
    "out": "out",
    "probe": "!Z:",
}

# The symbol a loader checks before any other (step 3): it gets its own named
# variant ("no-abi-version", reason "not_v1") rather than folding into the
# generic missing-<sym> sweep, because a real loader distinguishes "this is
# not a v1 artifact at all" from "a v1 artifact is missing one symbol".
ABI_VERSION_SYMBOL = "chs_abi_version"

# PM ruling: chs_build_info (and every other handshake symbol except
# chs_abi_version — chs_clickhouse_version included) is NOT folded into
# not_v1 when absent. chs_abi_version answering 1 is what makes a library
# "an ABI v1+ artifact" at all (step 3); if it is present and correct, the
# library genuinely IS one, so refusing it as not_v1 over a DIFFERENT
# missing symbol would be a false message. sdk.json already has the reason
# that fits — missing_symbol plus the symbol's own name — raised at
# whichever step first needs to resolve that symbol (step 4 for
# chs_build_info, since step 4 is the first to call it), not only at step
# 6's generic sweep. not_v1 stays reserved for chs_abi_version alone.


def omit_define(symbol: str) -> str:
    """The preprocessor guard macro that omits `symbol`'s definition from the
    compiled stub. emit/stub.py wraps every function body in
    `#if !defined(<this>)`; build-stubs.sh defines exactly one of these per
    missing-<sym> (and no-abi-version) variant."""
    return f"CHS_STUB_OMIT_{symbol}"


@dataclass(frozen=True)
class Variant:
    name: str
    defines: tuple[tuple[str, str | None], ...]  # (MACRO, value-or-None)
    reason: str  # sdk.json loader-refusal vocabulary, or "accepted"
    predicate_overrides: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    link_allow_undefined: bool = False  # "unbound": the .so may link with an unresolved symbol

    def define_args(self) -> list[str]:
        """`-D` arguments for the C compiler, in a fixed order."""
        return [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in self.defines]


# Rule r7, variant "filter-observable" (-DCHS_STUB_FILTER_OBSERVABLE):
# chs_preview_batch answers one of these two batch documents, by whether its
# filter reached it. rows_passed is the only difference.
FILTER_OBSERVABLE_DOCS = {
    "filter": '{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_passed":1,"rows_cut":0}',
    "none": '{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_passed":0,"rows_cut":0}',
}


def plan(model) -> list[Variant]:
    """Every stub library build-stubs.sh must produce, in a fixed,
    deterministic order: the two happy-path copies, the ten named
    loader-refusal and process-trap shapes, then one missing-<sym> per
    exported symbol (sorted), omitting chs_abi_version (already covered by
    "no-abi-version", the only symbol whose absence means not_v1). Every
    other missing symbol, chs_build_info and chs_clickhouse_version
    included, is missing_symbol:<name>."""
    out = [
        Variant("ok", (), "accepted"),
        Variant("ok-b", (), "accepted"),  # a second, byte-identical build: proves cross-image handling
        Variant("no-abi-version", ((omit_define(ABI_VERSION_SYMBOL), None),), "not_v1"),
        Variant("abi-version-2", (("CHS_STUB_ABI_VERSION_OVERRIDE", "2"),), "abi_version"),
        Variant("build-info-null", (("CHS_STUB_BUILD_INFO_MODE", "1"),), "build_info_malformed"),
        Variant("build-info-bad-json", (("CHS_STUB_BUILD_INFO_MODE", "2"),), "build_info_malformed"),
        Variant("build-info-dup-key", (("CHS_STUB_BUILD_INFO_MODE", "3"),), "build_info_malformed"),
        Variant("build-info-non-ascii", (("CHS_STUB_BUILD_INFO_MODE", "4"),), "build_info_malformed"),
        Variant("fingerprint-other", (("CHS_STUB_FINGERPRINT_OTHER", "1"),), "fingerprint"),
        Variant("unbound", (("CHS_STUB_UNBOUND", "1"),), "dlopen", link_allow_undefined=True),
        Variant(
            "ctor-marker",
            (("CHS_STUB_CTOR_MARKER", "1"),),
            "glibc_floor",
            predicate_overrides=(("glibc_floor", "99.0"),),
        ),
        # Rule r7: whether a batch's filter reached the library.
        Variant("filter-observable", (("CHS_STUB_FILTER_OBSERVABLE", "1"),), "accepted"),
    ]
    for sym in model.symbols():
        if sym == ABI_VERSION_SYMBOL:
            continue
        out.append(Variant(f"missing-{sym}", ((omit_define(sym), None),), f"missing_symbol:{sym}"))
    return out


def echo_bytes_field(data: bytes) -> dict:
    """The {head_hex, len, sha256} object the stub's echo reports for one
    bytes_in argument, computed the same way emit/cases.py precomputes the
    expected value: hashlib.sha256, so it matches the stub's C sha256
    bit-for-bit only if that C implementation is correct RFC 6234 SHA-256 —
    which is exactly what stubtest.c proves against known test vectors."""
    import hashlib

    return {"head_hex": data[:HEAD_BYTES].hex(), "len": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _main(argv: list[str]) -> int:
    sys.path.insert(0, str(SCRIPTS_ABI_V1))
    import model as abimodel  # noqa: PLC0415

    if argv != ["--list-variants"]:
        print("usage: _stubshared.py --list-variants", file=sys.stderr)
        return 2
    m = abimodel.load(ROOT)
    for v in plan(m):
        defines = ",".join(f"{k}" if val is None else f"{k}={val}" for k, val in v.defines)
        # "|", not a tab: bash's `read` treats a tab as "IFS whitespace" and
        # COLLAPSES a run of them, silently dropping the empty `defines`
        # field "ok"/"ok-b" have (no -D flags) and shifting every field
        # after it by one — measured (build-stubs.sh wrote "reason": "0" for
        # "ok" in stubs.json, the link_allow_undefined flag, one field over).
        # "|" never appears in a name, a defines list or a reason string, and
        # bash's `read` does not collapse runs of a non-whitespace IFS
        # character, so an empty field stays empty.
        print("|".join([v.name, defines, v.reason, "1" if v.link_allow_undefined else "0"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
