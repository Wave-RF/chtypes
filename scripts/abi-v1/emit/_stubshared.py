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


# --------------------------------------------------------------------------
# ABI v2's reader rules, r2 and r3 (spec/abi-v2/docs.md), as stub variants.
# They exist from generation 2 on only (plan(), below), so ABI v1's stubs and
# cases are unchanged. Their shape is the measurement public pull request
# #509 took of the released 1.0.4 bindings.
#
# r2, variant "r2-unknown-members" (-DCHS_STUB_R2_MEMBERS): every
# document-returning call answers the document below for its kind, carrying
# members no description names at every object level (a scalar, an object,
# an array, a null, an unknown `_b64` byte member), its build_info carries
# unknown members at the top level and inside capabilities, and its
# live_handles one unknown key.
_UNK = {"x_future": 1}
_UNK_TOP = {
    "x_future": 1,
    "x_future_obj": {"a": [1, {"b": None}], "c": "s"},
    "x_future_arr": [1, "two", {"three": 3}, [4]],
    "x_future_null": None,
    "x_future_b64": "/w==",
}
_R2_ROW = {
    "outcome": "accepted",
    "code": 0,
    "err": "",
    "input_span": {"off": 0, "len": 3, **_UNK},
    "cols": [
        {
            "name": "s",
            "type": "String",
            "src": "input",
            "null": False,
            "input": "abc",
            "stored": "abc",
            "value_b64": "YWJj",
            **_UNK,
            "x_future_obj": {"k": [1]},
        }
    ],
    "computed": [{"name": "m", "kind": "materialized", "stored": "5", **_UNK}],
    "transformed": [{"column": "s", "input": "1", "stored": "2", "reason": "date_clamp", "row": 0, **_UNK}],
    "unknown_fields": [{"name": "u", **_UNK}],
    "unsupported_settings": [{"name": "st", **_UNK}],
    "partition_id": "all",
    "verdict_code": 0,
    **_UNK_TOP,
}
R2_DOCS = {
    "row": _R2_ROW,
    "batch": {
        "outcome": "accepted",
        "code": 0,
        "err": "",
        "rows_read": 1,
        "rows_skipped": 0,
        "rows": [_R2_ROW],
        "transformed": [{"column": "s", "input": "1", "stored": "2", "reason": "date_clamp", "row": 0, **_UNK}],
        "storage_transforms": [{"row": 0, "column": "s", "stored": "2", "reason": "date_clamp", **_UNK}],
        "unsupported_settings": [{"name": "st", **_UNK}],
        "at_merge": [
            {"row": 0, "reason": "ttl_column_reset", "column": "s", "stored": "", "input_rows": [0], **_UNK}
        ],
        "engine_rows": [[{"name": "s", "stored": "abc", "null": False, "value_b64": "YWJj", **_UNK}]],
        "row_spans": [{"off": 0, "len": 3, **_UNK}],
        "export_declined": "",
        "rows_passed": 1,
        "rows_cut": 0,
        "partition_count": 1,
        "unconsumed": [{"off": 3, "len": 0, **_UNK}],
        "framing": {
            "bom_skipped": False,
            "container": "stream",
            "header": {"consumed": True, "lines": 1, "names": [{"name": "s", **_UNK}], **_UNK},
            **_UNK,
        },
        **_UNK_TOP,
    },
    "filter_result": {
        "outcome": "ok",
        "code": 0,
        "err": "",
        "rows_read": 2,
        "verdicts": "tf",
        "errors": [{"row": 1, "code": 53, "err": "x", **_UNK}],
        "unsupported_settings": [{"name": "st", **_UNK}],
        **_UNK_TOP,
    },
    "schema_description": {
        "columns": [{"name": "x", "type": "Int32", "default_kind": "", "default_expression": "", **_UNK}],
        **_UNK_TOP,
    },
    "discovery": {
        "columns": [
            {
                "name": "c",
                "type": "String",
                "default_kind": "",
                "default_expression": "",
                "declaration": "c String",
                **_UNK,
            }
        ],
        "columns_sql": "c String",
        **_UNK_TOP,
    },
    "error_code_table": [
        {"code": 0, "name": "OK", **_UNK},
        {"code": 53, "name": "TYPE_MISMATCH", "x_future": {"a": [1]}},
    ],
}
# The export bytes r2-unknown-members and r3-unknown-values answer when a
# binding passes an out_export pointer.
R2_EXPORT = b'{"s":"abc"}\n'


def _strip_unknown(v):
    if isinstance(v, dict):
        return {k: _strip_unknown(x) for k, x in v.items() if not k.startswith("x_future")}
    if isinstance(v, list):
        return [_strip_unknown(x) for x in v]
    return v


# Rule r7, variant "filter-observable" (-DCHS_STUB_FILTER_OBSERVABLE):
# chs_preview_batch answers one of these two batch documents, by whether its
# filter reached it. rows_passed is the only difference.
FILTER_OBSERVABLE_DOCS = {
    "filter": '{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_passed":1,"rows_cut":0}',
    "none": '{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_passed":0,"rows_cut":0}',
}

# The filter-declined settings, variant "declined-settings"
# (-DCHS_STUB_DECLINED_SETTINGS): a schema compiled under settings this build's
# filters do not honor in a WHERE, from any layer known at schema compile
# (spec/abi-v2/docs.md: schema_description's top-level filter_declined_settings,
# its declined_layer vocabulary, and chs_filter_create). A chs_schema_create
# whose statement is exactly DECLINED["prefix"] + <id> selects
# DECLINED_LISTS[<id>]; chs_schema_describe then answers declined_document(<id>),
# and chs_filter_create, after its input checks and the status injection,
# answers DECLINED["status"] with ch_code 0 and declined_message(<id>) when that
# list is not empty, and its usual filter otherwise. The statement
# DECLINED["prefix"] + DEEP["id"] selects the deep input instead:
# chs_schema_describe answers DEEP's refusal, the server's stack check (ch_code
# 306), and chs_filter_create its usual filter. Any other statement selects
# nothing, and every call answers as the ok build does. Like the r3 variant's
# describe, the selection is the image's LAST chs_schema_create, never a field
# of the handle: a test double, enough for the one schema each step describes.
# The message is the specification's text, rendered here once for the stub's C
# and for the cases that expect it.
DECLINED = {
    "prefix": "!F:",
    "status": "CHS_DECLINED",
    "ch_code": 0,
    "message": (
        "chs_filter_create: the settings this schema was compiled under set {names}, which this build's filters "
        "do not honor in a WHERE (listed in chs_schema_describe's filter_declined_settings); declined rather "
        "than answered"
    ),
    # How the message names one entry: its name's bytes, then its layer.
    "entry": "{name} ({layer})",
}
# The layers, lowest first: the order of the list (spec/abi-v2/docs.md).
DECLINED_LAYERS = ("defaults", "server", "schema")
# id -> the selection: whether the case compiles on a server (the document then
# carries a `server` member, whose `settings` give back the profile's names in
# `profile`), and the top-level filter_declined_settings, as the library would
# write it. Each entry is {name | name_b64, tier, layer}:
#   "layers" spans all three layers in the list's order: by layer, lowest
#     first, then by name as raw bytes, so a reader that sorted by name alone,
#     or kept any other order, would fail;
#   "two_layers" is one name set by the profile AND the schema's own settings:
#     one entry, at the higher layer (the profile still gives it back);
#   "name_b64" carries a name that is not valid UTF-8 (the byte strings rule;
#     a JSON settings object cannot spell one today, and every reader decodes it
#     all the same);
#   "unknown_tier" and "unknown_layer" each carry a value its vocabulary does
#     not list (rule r3);
#   "schema_only" is one schema-layer setting, on a schema with no server.
DECLINED_LISTS: dict[str, dict] = {
    "none": {"server": True, "profile": [], "entries": []},
    "layers": {
        "server": True,
        "profile": ["apply_deleted_mask", "final"],
        "entries": [
            {"name": "aggregate_functions_null_for_empty", "tier": "predicate", "layer": "defaults"},
            {"name": "apply_deleted_mask", "tier": "result-content", "layer": "server"},
            {"name": "final", "tier": "result-content", "layer": "server"},
            {"name": "additional_result_filter", "tier": "result-content", "layer": "schema"},
        ],
    },
    "two_layers": {
        "server": True,
        "profile": ["final"],
        "entries": [{"name": "final", "tier": "result-content", "layer": "schema"}],
    },
    "name_b64": {
        "server": True,
        "profile": [],
        "entries": [{"name_b64": "eP95", "tier": "predicate-unflipped", "layer": "schema"}],
    },
    "unknown_tier": {
        "server": True,
        "profile": ["x_future_setting"],
        "entries": [{"name": "x_future_setting", "tier": "x_future_tier", "layer": "server"}],
    },
    "unknown_layer": {
        "server": True,
        "profile": [],
        "entries": [{"name": "final", "tier": "result-content", "layer": "x_future_layer"}],
    },
    "schema_only": {
        "server": False,
        "profile": [],
        "entries": [{"name": "final", "tier": "result-content", "layer": "schema"}],
    },
}
# The deep input: chs_schema_describe's refusal by the server's stack check
# (spec/abi-v2/docs.md, chs_status and chs_schema_describe). The code is the
# one the specification gives, and the name is ClickHouse's own name for it;
# the message is the stub's stand-in, not ClickHouse's text.
DEEP = {
    "id": "deep",
    "fn": "chs_schema_describe",
    "status": "CHS_REJECTED",
    "ch_code": 306,
    "ch_name": "TOO_DEEP_RECURSION",
    "message": "Stack size too large (the stub's stand-in for the server's stack check on a deep input)",
}


def declined_name(entry: dict[str, str]) -> bytes:
    """An entry's name as bytes, from either of its two forms."""
    import base64

    return entry["name"].encode("utf-8") if "name" in entry else base64.b64decode(entry["name_b64"], validate=True)


def declined_in_order(entries: list[dict[str, str]]) -> bool:
    """Whether `entries` keep the specification's rule: one entry per name, in
    layer order (DECLINED_LAYERS, lowest first), then by name as raw bytes. An
    entry whose layer the description does not list has no place in that
    order, so a list carrying one is judged on its names alone."""
    names = [declined_name(e) for e in entries]
    if len(set(names)) != len(names):
        return False
    if any(e["layer"] not in DECLINED_LAYERS for e in entries):
        return True
    keys = [(DECLINED_LAYERS.index(e["layer"]), declined_name(e)) for e in entries]
    return keys == sorted(keys)


def declined_document(sel: str) -> dict:
    """The schema_description chs_schema_describe answers for selection `sel`:
    one column, the top-level filter_declined_settings, and, on a server, a
    `server` member whose profile set each name in `profile`."""
    chosen = DECLINED_LISTS[sel]
    doc: dict = {
        "columns": [{"name": "k", "type": "UInt8", "default_kind": "", "default_expression": ""}],
        "filter_declined_settings": chosen["entries"],
    }
    if chosen["server"]:
        doc["server"] = {"timezone": "UTC", "settings": {n: "1" for n in chosen["profile"]}}
    return doc


def declined_message(sel: str) -> bytes | None:
    """The message chs_filter_create declines with for selection `sel`, naming
    each entry and its layer in the list's order; None when the list is empty
    (no decline)."""
    entries = DECLINED_LISTS[sel]["entries"]
    if not entries:
        return None
    one_head, one_mid = DECLINED["entry"].split("{name}")
    names = b", ".join(
        one_head.encode("utf-8") + declined_name(e) + one_mid.format(layer=e["layer"]).encode("utf-8")
        for e in entries
    )
    head, tail = DECLINED["message"].split("{names}")
    return head.encode("utf-8") + names + tail.encode("utf-8")


# r3, variant "r3-unknown-values" (-DCHS_STUB_R3_VALUES): each document call
# answers the CLEAN document of its kind (R3_BASE: no unknown member anywhere),
# or, when the call's first bytes_in parameter is exactly b"!E:" + <id>, that
# document after one field is set to a value its vocabulary does not list
# (R3_MUTATIONS: every vocabulary a document carries, and the two schema
# enums r3 says constrain only the writer). chs_schema_describe has no
# bytes_in: it answers the mutation the image's last chs_schema_create
# statement named, when that began "!E:". chs_type_validate("!U:") answers
# R3_UNKNOWN_STATUS, a status outside the closed set. Variant
# "r3-unknown-capabilities" (-DCHS_STUB_R3_CAPABILITIES) is the ok build with
# an unlisted value in every capabilities list of its build_info.
R3_BASE = {k: _strip_unknown(v) for k, v in R2_DOCS.items() if k != "error_code_table"}
# id -> (document kind, path, value)
R3_MUTATIONS = {
    "row.outcome": ("row", ("outcome",), "x_future_outcome"),
    "row.cols.src": ("row", ("cols", 0, "src"), "x_future_src"),
    "row.transformed.reason": ("row", ("transformed", 0, "reason"), "x_future_reason"),
    "row.verdict": ("row", ("verdict",), "x"),
    "batch.outcome": ("batch", ("outcome",), "x_future_outcome"),
    "batch.rows.outcome": ("batch", ("rows", 0, "outcome"), "x_future_outcome"),
    "batch.rows.cols.src": ("batch", ("rows", 0, "cols", 0, "src"), "x_future_src"),
    "batch.transformed.reason": ("batch", ("transformed", 0, "reason"), "x_future_reason"),
    "batch.storage_transforms.reason": ("batch", ("storage_transforms", 0, "reason"), "x_future_reason"),
    "batch.at_merge.reason": ("batch", ("at_merge", 0, "reason"), "x_future_reason"),
    "batch.framing.container": ("batch", ("framing", "container"), "x_future_container"),
    "filter.outcome": ("filter_result", ("outcome",), "x_future_outcome"),
    "filter.verdicts": ("filter_result", ("verdicts",), "tx"),
    "describe.default_kind": ("schema_description", ("columns", 0, "default_kind"), "X_FUTURE"),
    "discovery.default_kind": ("discovery", ("columns", 0, "default_kind"), "X_FUTURE"),
}
R3_UNKNOWN_STATUS = 99
R3_CAPABILITIES = {
    "input_formats": ["JSONEachRow", "XFutureFormat"],
    "export_formats": ["JSONEachRow", "XFutureFormat"],
    "doc_flags": ["values", "x_future_flag"],
    "features": ["default_generators", "x_future_feature"],
}


def r3_doc(mutation_id: str):
    """The document R3_MUTATIONS[mutation_id] names, as a Python value."""
    import copy

    kind, path, value = R3_MUTATIONS[mutation_id]
    doc = copy.deepcopy(R3_BASE[kind])
    cur = doc
    for step in path[:-1]:
        cur = cur[step]
    cur[path[-1]] = value
    return doc



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

# Generation 2, rule r1: a closed input document (a bytes_in parameter whose
# content is `input:<name>`), as the stub models it and as the cases that probe
# it expect it. emit/stub.py generates the C check from this dict and from each
# input document's schema in the description, and emit/cases.py the cases, so
# the two can never disagree. The stub is a test double: it reads only the
# document's top-level keys (no escape inside a key is decoded, and a value is
# only skipped, tracking strings and nesting), and refuses one the document's
# schema does not name in its `properties`, or a document that is not a JSON
# object. Length 0 is `{}`. What a real library answers is the same status
# naming the same key; its message is the library's own, not this one.
INPUT_DOCUMENT = {
    "status": "CHS_INVALID_ARGUMENT",
    "unknown_key": '{param}: unknown key "{key}" in the input document {doc}',
    "not_object": "{param}: the input document {doc} is not a JSON object",
}


def input_document_message(param: str, doc: str, key: str | None) -> str:
    """The message the stub's INPUT_DOCUMENT check gives: naming `key`, or
    saying the document is not a JSON object when `key` is None."""
    if key is None:
        return INPUT_DOCUMENT["not_object"].format(param=param, doc=doc)
    return INPUT_DOCUMENT["unknown_key"].format(param=param, doc=doc, key=key)


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


# The symbols loader steps 3 and 4 resolve before the full sweep.
HANDSHAKE_SYMBOLS = frozenset({"chs_abi_version", "chs_build_info"})


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


def plan(model) -> list[Variant]:
    """Every stub library build-stubs.sh must produce, in a fixed,
    deterministic order: the two happy-path copies, the ten named
    loader-refusal and process-trap shapes, then one missing-<sym> per
    exported symbol (sorted), omitting chs_abi_version (already covered by
    "no-abi-version", the only symbol whose absence means not_v1). Every
    other missing symbol, chs_build_info and chs_clickhouse_version
    included, is missing_symbol:<name>."""
    other = 2 if model.abi == 1 else 1
    out = [
        Variant("ok", (), "accepted"),
        Variant("ok-b", (), "accepted"),  # a second, byte-identical build: proves cross-image handling
        Variant("no-abi-version", ((omit_define(ABI_VERSION_SYMBOL), None),), "not_v1"),
        # A library answering ANOTHER generation: 2 for ABI v1's stubs (the
        # variant's name and define unchanged since ABI v1), 1 for ABI v2's.
        Variant(f"abi-version-{other}", (("CHS_STUB_ABI_VERSION_OVERRIDE", str(other)),), "abi_version"),
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
    ]
    if model.abi >= 2:
        # The reader rules (r2, r3): see R2_DOCS and R3_MUTATIONS above.
        out += [
            Variant("r2-unknown-members", (("CHS_STUB_R2_MEMBERS", "1"),), "accepted"),
            Variant("r3-unknown-values", (("CHS_STUB_R3_VALUES", "1"),), "accepted"),
            Variant("r3-unknown-capabilities", (("CHS_STUB_R3_CAPABILITIES", "1"),), "accepted"),
            # Rule r7: whether a batch's filter reached the library.
            Variant("filter-observable", (("CHS_STUB_FILTER_OBSERVABLE", "1"),), "accepted"),
            # The filter-declined settings, and describe's deep-input
            # refusal: see DECLINED and DEEP above.
            Variant("declined-settings", (("CHS_STUB_DECLINED_SETTINGS", "1"),), "accepted"),
        ]
        # Rule r6 beats the symbol sweep (public issue #537): ANOTHER
        # fingerprint AND one declared symbol absent. Loader step 4's
        # fingerprint comparison runs before step 6's resolve-every-symbol
        # sweep, so every binding refuses this as "fingerprint" (r6's exact
        # message), never as missing_symbol:<name>. A fingerprint move that
        # adds an export meets exactly this library until staging catches up.
        # The omitted symbol is the last one in sorted order, never a
        # handshake symbol steps 3 and 4 resolve first.
        absent = sorted(s for s in model.symbols() if s not in HANDSHAKE_SYMBOLS)[-1]
        out.append(
            Variant(
                "fingerprint-other-missing-symbol",
                (("CHS_STUB_FINGERPRINT_OTHER", "1"), (omit_define(absent), None)),
                "fingerprint",
            )
        )
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

    if argv[:1] != ["--list-variants"] or argv[1:] not in ([], ["--major", "1"], ["--major", "2"]):
        print("usage: _stubshared.py --list-variants [--major N]", file=sys.stderr)
        return 2
    m = abimodel.load(ROOT, int(argv[2]) if len(argv) == 3 else 1)
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
