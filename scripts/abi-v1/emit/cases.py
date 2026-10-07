"""tests/fixtures/abi-v1/cases.json: the committed, generated conformance
cases every enrolled binding's `v1-abi-conformance` leg runs against the
stub libraries `scripts/abi-v1/build-stubs.sh` builds from the SAME
`spec/abi-v1/abi.json`.

WHY A CASE READS THE WAY IT DOES. A case never assumes a binding has
variables or named state: every argument is a small, self-contained
expression a runner evaluates bottom-up —

  * `{"int": N}` — a scalar or enum literal, passed by value;
  * `{"bytes_hex": "<hex>"}` — a bytes_in argument's exact bytes (hex, so a
    NUL or invalid-UTF-8 byte is unambiguous in JSON; "" is a zero-length
    buffer);
  * `{"handle": {"fn": "<name>", "args": [...]}}` — mint a fresh handle by
    calling `<name>` with its own (recursively, the same three shapes)
    argument list first, and use the handle it produces;
  * `{"null_handle": true}` — NULL, legal only where the parameter is
    nullable;
  * `{"ref": "<name>"}` — inside a lifecycle case only: the handle an
    earlier `let` step bound to that name.

A case's `expect` mirrors exactly what `emit/stub.py`'s generic template
(`scripts/abi-v1/emit/stub.py`'s `_gen_generic`/`_fill_outputs`) computes, so
this file is the stub's behavior restated as data, not a second guess at it:

  * `status`: the `chs_status` name the call must return;
  * `outputs`: one entry per `out_handle` of type `chs_buf` the function
    has, keyed by its parameter name, each `{"fn", "out", "args"}` — `args`
    one entry per INPUT parameter, in declaration order: an int for a
    scalar/enum, `{"len","sha256","head_hex"}` for a bytes_in (computed here
    with hashlib against the SAME bytes the case's own `args` pass in, so a
    conformance runner's result can be compared field by field without
    needing a second implementation of the stub's echo), or `{"kind"}` for a
    handle (never `id`: a serial number no case can predict);
  * `error`: present on every non-CHS_OK case, the four fields a `!S:`
    injection fixes: `ch_code`, `ch_name`, `message`, `column` (always "",
    since the magic injection format carries no column).

SEVEN CASE KINDS. The first three are per function
(`scripts/abi-v1/emit/stub.classify`):
  * "special" and "free" functions get one small case each (a handshake
    return value, a tombstone value, or nothing testable generically — see
    HANDWRITTEN_CASES) — the generic echo/status shape does not apply to
    them, by construction (that is what "special" means).
  * "generic" functions (about half of the description today: 20 of 38) get:
      - one "echo" case, with one parameter carrying ADVERSARIAL bytes (a
        NUL and an invalid-UTF-8 byte) so the sha256/head_hex round-trip is
        actually exercised, not just a plain ASCII placeholder;
      - one "status" case per `may_return` entry other than CHS_OK, via a
        `!S:` injection on the function's FIRST bytes_in parameter — so a
        function with no bytes_in parameter at all (today:
        chs_discover_query, chs_live_handles, chs_error_codes and two of the
        tooling calls) gets only the echo case, a known, documented
        limitation, not a gap silently dropped.

THE DECISION CASES (`_decision_cases`), for what the ABI's confirmation
added beyond the per-function shapes:
  * "document" cases (`DOCUMENT_CASES`): the byte_strings rule end to end. The
    stub's document mode (_stubshared.DOC_TEMPLATES) returns a real row or
    discovery document built from the call's bytes; the case expects that
    document EXACTLY, in `expect.documents.<output>`. A runner decodes the
    output as STRICT UTF-8 (a replacement character is a failure, never a
    pass) and compares the parsed document for equality, every member and no
    more, so a field present in both its plain and its `_b64` form fails. A
    binary String (FF 00 80) must arrive as `stored_b64`, `input_b64` and
    `value_b64`, each exactly the base64 of those bytes;
  * the one-CREATE rule: a second statement refused with the error intact
    (a "status" case), a trailing semicolon accepted (an "echo" case); the
    stub's stand-in and this case both read `_stubshared.ONE_CREATE`;
  * the per-call zone: echo cases whose settings carry `session_timezone`,
    proving the bytes reach the library unmodified;
  * the image zone's process-once rule (`_stubshared.IMAGE_ZONE`): the
    sentinel bad zone refused with ClickHouse's code, and a different
    spelling refused as CHS_INVALID_ARGUMENT, on the image the runner's own
    loader set up under the empty zone (LOADED_ZONE, which is also what the
    chs_initialize echo case passes);
  * "lifecycle" cases: ordered `steps` (`let` a handle from a call, `free`
    a bound handle, `call` with an `expect`, `live_delta` over
    chs_live_handles against the counts read when the case began), proving
    a filter and a block hold a counted reference to their schema: the
    schema outlives the caller's free of it, the child still answers, and
    the schema goes only with its last child. A runner runs a lifecycle
    case with no other case in flight;
  * "concurrent" cases, one per `shared` function the stub implements
    generically: the `args` are evaluated ONCE (each handle minted once),
    then `threads` threads each make `calls` calls at the same time, every
    call checked exactly as the echo case is checked, every produced handle
    freed; afterwards the live counts must equal those read before the case.
    A runtime that cannot call from two threads at once (Node, one isolate)
    makes the calls in sequence and says `sequential` in the result's
    detail. A runner runs a concurrent case with no other case in flight.

Plus one "loader" case per `scripts/abi-v1/emit/_stubshared.py` plan()
entry: `{"variant": "<name>", "expect": {"reason": "<reason>"}}` — except
"ctor-marker", which is OS-CONDITIONAL and gets two cases instead of one,
each carrying an `"os"` field ("linux" or "darwin") a conformance runner
must check against its own platform before asserting anything, skipping
(never failing) a case whose `os` does not match. D3's loader step 1 (the
glibc floor) is Linux-only ("darwin: skip" — the spec, not a guess), so the
predicate's impossible glibc_floor only forces a refusal on Linux;
on darwin the library loads exactly like "ok" (measured: both RTLD_NOW
and a manual dlopen() succeed on darwin-arm64, and the ctor marker file
appears, proving dlopen genuinely ran rather than being refused first).

Generation 2 adds, from the description's input documents (rule r1), the
"input document" cases (`_input_document_cases`): each input document refuses
an unknown top-level key, naming it, and a document that is not a JSON object,
as _stubshared.INPUT_DOCUMENT says the stub does; and the server profile's
cases, which accept an empty profile, `{}` and every member, and a schema that
holds its server. A NULL server keeps every other case's answer: each other
call to chs_schema_create passes a NULL server.

A handle-typed parameter is resolved through HANDLE_RECIPE (server, schema,
filter, block; chs_server only where the description has it). A fifth handle
kind needs one more entry here, which `--check` cannot catch by itself, so a
schema change that adds a handle kind should grep this file. A recipe is a
call, so a signature change to chs_server_create, chs_schema_create,
chs_filter_create or chs_block_create is also an edit here (generation fails
loudly until it is made: `_check_calls` refuses any call in this file whose
arguments do not match its function's input parameters, kind for kind).
chs_schema_create's arguments are built from its own parameters
(`_schema_args`), so its generation-2 signature needs no second copy here.
"""

from __future__ import annotations

import base64
import json

import model as abimodel
from model import BUF_HANDLE

from . import Output, banner, stub, _stubshared

# Every major some binding speaks (emit/__init__.py, SHARED): each major's
# conformance runners need their own cases and stubs.
MAJORS = (1, 2)
SHARED = True

PATH = "tests/fixtures/abi-v1/cases.json"


def path(major: int) -> str:
    """Each major's own file: tests/fixtures/abi-v<major>/cases.json."""
    return f"tests/fixtures/abi-v{major}/cases.json"


# ------------------------------------------------------------------ recipes


def _schema_args(model, create_table: bytes, server: dict | None = None) -> list[dict]:
    """chs_schema_create's arguments, from its own input parameters: the
    statement, a NULL server (or `server`) where the description has one, and
    every other input empty. ABI v1's are (create_table, settings), exactly as
    before generation 2."""
    fn = model.function("chs_schema_create")
    out: list[dict] = []
    for p in fn.params:
        if p.is_out:
            continue
        if p.kind == "handle":
            out.append(server if server is not None else {"null_handle": True})
        elif p.name == "create_table":
            out.append({"bytes_hex": create_table.hex()})
        else:
            out.append({"bytes_hex": ""})
    return out


def _schema_recipe(model) -> dict:
    return {"fn": "chs_schema_create", "args": _schema_args(model, b"CREATE TABLE t (x Int32)")}


def _server_recipe(model) -> dict:  # noqa: ARG001 - every recipe takes the model
    """A server with nothing described: an empty profile and empty options."""
    return {"fn": "chs_server_create", "args": [{"bytes_hex": ""}, {"bytes_hex": ""}]}


def _filter_recipe(model) -> dict:
    return {
        "fn": "chs_filter_create",
        "args": [
            {"handle": _schema_recipe(model)},
            {"bytes_hex": b"x = 1".hex()},
            {"bytes_hex": "{}".encode().hex()},
            {"bytes_hex": ""},
        ],
    }


def _block_recipe(model) -> dict:
    return {
        "fn": "chs_block_create",
        "args": [
            {"handle": _schema_recipe(model)},
            {"int": 0},  # CHS_JSON_EACH_ROW
            {"bytes_hex": b"{}".hex()},
            {"bytes_hex": ""},
            {"bytes_hex": ""},
        ],
    }


HANDLE_RECIPE = {
    "chs_server": _server_recipe,
    "chs_schema": _schema_recipe,
    "chs_filter": _filter_recipe,
    "chs_block": _block_recipe,
}

# A placeholder value per bytes_in content vocabulary (model.CONTENTS' keys),
# used for every bytes_in parameter EXCEPT the function's first that is not an
# input document, which always gets ADVERSARIAL_BYTES instead so every echo
# case exercises a NUL and an invalid-UTF-8 byte at least once. An input
# document (`input:<name>`, rule r1) is validated JSON, so it always gets
# INPUT_PLACEHOLDER, and a function whose every bytes_in parameter is one
# (chs_server_create) carries no adversarial bytes.
CONTENT_PLACEHOLDER: dict[str, bytes] = {
    "bytes": b"body",
    "name": b"col",
    "sql": b"1",
    "message": b"msg",
    "ascii": b"abc",
    "json_object_string_values": b"{}",
    "json_array_names": b"[]",
    "timezone": b"UTC",
}
ADVERSARIAL_BYTES = b"a\x00\xffz"
INPUT_PLACEHOLDER = b"{}"


def _literal_value(p) -> tuple[dict, bytes | int | None]:
    """(the case's arg expression, the raw value for computing the matching
    echo expectation: bytes for bytes_in, an int for scalar/enum)."""
    if p.kind in ("scalar", "enum"):
        # CHS_EXPORT_NONE for export_format, 0 (the first enum/uint value)
        # otherwise: always a value the stub accepts unconditionally (it
        # never validates a scalar or enum's range).
        v = -1 if p.name == "export_format" else 0
        return {"int": v}, v
    if p.kind == "bytes_in":
        if abimodel.input_name(p.content) is not None:
            data = INPUT_PLACEHOLDER
        else:
            data = CONTENT_PLACEHOLDER.get(p.content or "", b"x")
        return {"bytes_hex": data.hex()}, data
    raise ValueError(f"_literal_value: unexpected kind {p.kind!r} for parameter {p.name}")


def _build_args(fn, model) -> tuple[list[dict], list[dict | int]]:
    """(the case's "args" list, the matching expected echo-arg shapes), with
    the function's FIRST bytes_in parameter that is not an input document
    forced to ADVERSARIAL_BYTES."""
    args: list[dict] = []
    echo: list[dict | int] = []
    first_bytes_seen = False
    for p in fn.params:
        if p.is_out:
            continue
        if p.kind == "handle":
            if p.nullable:
                args.append({"null_handle": True})
                # A null optional handle carries no echo entry of its own
                # shape beyond "it was accepted"; the generic template never
                # echoes a nullable handle specially, so nothing is asserted
                # about it here beyond the call succeeding.
                echo.append(None)
                continue
            recipe = HANDLE_RECIPE.get(p.type)
            if recipe is None:
                raise ValueError(f"{fn.name}: no HANDLE_RECIPE for handle kind {p.type!r}; add one")
            args.append({"handle": recipe(model)})
            echo.append({"kind": p.type})
        elif p.kind == "bytes_in":
            if not first_bytes_seen and abimodel.input_name(p.content) is None:
                data = ADVERSARIAL_BYTES
                first_bytes_seen = True
                args.append({"bytes_hex": data.hex()})
            else:
                expr, data = _literal_value(p)
                args.append(expr)
            echo.append(_stubshared.echo_bytes_field(data))
        else:
            expr, v = _literal_value(p)
            args.append(expr)
            echo.append(v)
    return args, echo


def _buf_out_params(fn) -> list:
    return [p for p in fn.params if p.kind == "out_handle" and p.type == BUF_HANDLE]


# The image zone every runner's call-level cases meet: each runner loads the
# "ok" stub through its own loader under the empty setup before it runs a
# call case, so step 7 has already committed the empty spelling on that image
# (_stubshared.IMAGE_ZONE: after that, the same spelling is accepted and any
# other is refused). The echo case of the function the rule names therefore
# passes this spelling, not ADVERSARIAL_BYTES, which the rule would refuse.
LOADED_ZONE = b""


def _echo_case(fn, model) -> dict:
    args, echo_args = _build_args(fn, model)
    zone = _stubshared.IMAGE_ZONE
    if fn.name == zone["fn"]:
        i = next(k for k, p in enumerate([p for p in fn.params if not p.is_out]) if p.name == zone["param"])
        args[i] = {"bytes_hex": LOADED_ZONE.hex()}
        echo_args[i] = _stubshared.echo_bytes_field(LOADED_ZONE)
    outputs = {}
    for p in _buf_out_params(fn):
        outputs[p.name] = {"fn": fn.name, "out": p.name, "args": echo_args}
    expect: dict = {"status": "CHS_OK"}
    if outputs:
        expect["outputs"] = outputs
    return {"id": f"{fn.name}.echo", "kind": "echo", "fn": fn.name, "args": args, "expect": expect}


def _status_cases(fn, model) -> list[dict]:
    first_bytes = next((p for p in fn.params if p.kind == "bytes_in"), None)
    if first_bytes is None:
        return []
    cases = []
    for status in fn.may_return:
        if status == "CHS_OK":
            continue
        args, _ = _build_args(fn, model)
        msg = f"{fn.name} forced {status}"
        injected = f"!S:{status}:77:TEST_CODE:{msg}".encode()
        # Overwrite the first bytes_in argument (built adversarially above)
        # with the injection string: the same parameter status injection
        # always reads, by construction (emit/stub.py's _status_injection).
        first_index = next(i for i, p in enumerate([p for p in fn.params if not p.is_out]) if p is first_bytes)
        args[first_index] = {"bytes_hex": injected.hex()}
        cases.append(
            {
                "id": f"{fn.name}.status.{status}",
                "kind": "status",
                "fn": fn.name,
                "args": args,
                "expect": {
                    "status": status,
                    "error": {"ch_code": 77, "ch_name": "TEST_CODE", "message": msg, "column": ""},
                },
            }
        )
    return cases


def _overridden_echo_case(model, case_id: str, fn_name: str, overrides: dict[str, bytes]) -> dict:
    """An echo case for `fn_name` whose named bytes_in parameters carry the
    given bytes instead of the generic placeholders (the first bytes_in still
    carries ADVERSARIAL_BYTES unless it is overridden). The expectation is
    computed from the same bytes, by the same helper the generic echo cases
    use, so it cannot drift from what the stub echoes."""
    fn = model.function(fn_name)
    args, echo = _build_args(fn, model)
    inputs = [p for p in fn.params if not p.is_out]
    for name, data in overrides.items():
        i = next((k for k, p in enumerate(inputs) if p.name == name and p.kind == "bytes_in"), None)
        if i is None:
            raise ValueError(f"{fn_name}: no bytes_in parameter {name!r} to override")
        args[i] = {"bytes_hex": data.hex()}
        echo[i] = _stubshared.echo_bytes_field(data)
    outputs = {p.name: {"fn": fn.name, "out": p.name, "args": echo} for p in _buf_out_params(fn)}
    expect: dict = {"status": "CHS_OK"}
    if outputs:
        expect["outputs"] = outputs
    return {"id": case_id, "kind": "echo", "fn": fn.name, "args": args, "expect": expect}


ZONE_SETTINGS = b'{"session_timezone":"America/Los_Angeles"}'
ZONE_BODY = b'{"ts":"2026-01-01 05:00:00"}'


def _decision_cases(model) -> list[dict]:
    """The cases for the behavior the ABI's confirmation added, beyond the
    generic per-function ones."""
    rule = _stubshared.ONE_CREATE
    two = b"CREATE TABLE a (x Int32) ENGINE = Memory; CREATE TABLE b (y Int32) ENGINE = Memory"
    one = b"CREATE TABLE a (x Int32) ENGINE = Memory;\n"
    cases = [
        # Exactly one CREATE TABLE: a second statement is refused, the error
        # intact, and a trailing semicolon is not a second statement.
        {
            "id": "one_create.second_statement_refused",
            "kind": "status",
            "fn": rule["fn"],
            "args": _schema_args(model, two),
            "expect": {
                "status": rule["status"],
                "error": {"ch_code": rule["ch_code"], "ch_name": rule["ch_name"], "message": rule["message"], "column": ""},
            },
        },
        {
            "id": "one_create.trailing_semicolon_accepted",
            "kind": "echo",
            "fn": rule["fn"],
            "args": _schema_args(model, one),
            "expect": {"status": "CHS_OK"},
        },
        # The per-call zone is the session_timezone key in the call's own
        # settings: it reaches the library byte for byte, never rewritten.
        _overridden_echo_case(
            model, "session_timezone.preview_row", "chs_preview_row", {"body": ZONE_BODY, "settings": ZONE_SETTINGS}
        ),
        _overridden_echo_case(
            model,
            "session_timezone.filter_eval_body",
            "chs_filter_eval_body",
            {"body": ZONE_BODY, "settings": ZONE_SETTINGS},
        ),
        *_image_zone_cases(model),
    ]
    cases += _input_document_cases(model)
    cases += _document_cases(model)
    cases += _lifecycle_cases(model)
    cases += _concurrent_cases(model)
    return cases


def _input_document_status(param: str, doc: str, key: str | None) -> dict:
    rule = _stubshared.INPUT_DOCUMENT
    return {
        "status": rule["status"],
        "error": {
            "ch_code": 0,
            "ch_name": "",
            "message": _stubshared.input_document_message(param, doc, key),
            "column": "",
        },
    }


# A key no input document will ever define: the r1 cases' unknown key.
UNKNOWN_INPUT_KEY = "x_unknown"
# The server profile the cases accept: every member, nested values included,
# so the stub's reader proves it skips a value whatever it holds.
FULL_PROFILE = (
    b'{"timezone":"Asia/Tokyo","settings":{"date_time_input_format":"best_effort"},'
    b'"macros":{"replica":"r1","shard":"01"}}'
)


def _input_document_cases(model) -> list[dict]:
    """Rule r1 (generation 2 on): for every bytes_in parameter that carries an
    input document, a document with an unknown key is CHS_INVALID_ARGUMENT
    naming the key, after every key the document defines; and a document
    that is not a JSON object is refused too. Then the server profile's own
    cases: an empty profile, `{}` and every member are accepted, and a schema
    created ON a server answers as one created on none. Empty for ABI v1."""
    out: list[dict] = []
    for fn in model.functions:
        inputs = [p for p in fn.params if not p.is_out]
        for p in inputs:
            d = model.input_of(p)
            if d is None:
                continue
            known = {k: ({} if s.get("type") == "object" else "") for k, s in d.schema.get("properties", {}).items()}
            doc = json.dumps({**known, UNKNOWN_INPUT_KEY: "1"}, separators=(",", ":")).encode()
            for case_id, data, key in (
                (f"input_document.{fn.name}.{p.name}.unknown_key_refused", doc, UNKNOWN_INPUT_KEY),
                (f"input_document.{fn.name}.{p.name}.not_an_object_refused", b"[]", None),
            ):
                args, _ = _build_args(fn, model)
                args[inputs.index(p)] = {"bytes_hex": data.hex()}
                out.append(
                    {
                        "id": case_id,
                        "kind": "status",
                        "fn": fn.name,
                        "args": args,
                        "expect": _input_document_status(p.name, d.name, key),
                    }
                )
    if "chs_server" not in model.handles:
        return out
    fn = model.function("chs_server_create")
    for case_id, profile in (
        ("server_profile.empty_accepted", b""),
        ("server_profile.empty_object_accepted", b"{}"),
        ("server_profile.every_member_accepted", FULL_PROFILE),
    ):
        args, _ = _build_args(fn, model)
        args[0] = {"bytes_hex": profile.hex()}
        out.append({"id": case_id, "kind": "echo", "fn": fn.name, "args": args, "expect": {"status": "CHS_OK"}})
    # A schema created on a server answers as one created on none (the stub's
    # chs_schema_create mints it either way); the lifecycle case below proves
    # it holds the server.
    out.append(
        {
            "id": "server_profile.schema_on_server_accepted",
            "kind": "echo",
            "fn": "chs_schema_create",
            "args": _schema_args(
                model,
                b"CREATE TABLE t (x Int32)",
                {"handle": {"fn": "chs_server_create", "args": [{"bytes_hex": FULL_PROFILE.hex()}, {"bytes_hex": ""}]}},
            ),
            "expect": {"status": "CHS_OK"},
        }
    )
    return out


def _image_zone_cases(model) -> list[dict]:
    """The image zone's process-once rule at call level (_stubshared.IMAGE_ZONE),
    on the image every runner already set up under the empty zone (LOADED_ZONE):
    the sentinel bad zone is ClickHouse's refusal with every field intact, and a
    different good spelling is CHS_INVALID_ARGUMENT. Neither commits anything,
    so neither depends on the order the cases run in. That a refusal leaves a
    FRESH image free to take a good zone is the public-API setup case's to
    prove (emit/setup_cases.py), since only a fresh image can show it."""
    rule = _stubshared.IMAGE_ZONE
    fn = model.function(rule["fn"])
    inputs = [p for p in fn.params if not p.is_out]
    if [p.name for p in inputs] != [rule["param"]]:
        raise ValueError(f"{fn.name}: _image_zone_cases expects exactly one input, {rule['param']!r}")
    for status in (rule["status"], rule["conflict_status"]):
        if status not in fn.may_return:
            raise ValueError(f"{fn.name}: {status} is not in its may_return")
    return [
        {
            "id": "image_zone.bad_zone_refused",
            "kind": "status",
            "fn": fn.name,
            "args": [{"bytes_hex": rule["bad_zone"].encode().hex()}],
            "expect": {
                "status": rule["status"],
                "error": {"ch_code": rule["ch_code"], "ch_name": rule["ch_name"], "message": rule["message"], "column": ""},
            },
        },
        {
            "id": "image_zone.different_spelling_refused",
            "kind": "status",
            "fn": fn.name,
            "args": [{"bytes_hex": b"Europe/Berlin".hex()}],
            "expect": {
                "status": rule["conflict_status"],
                "error": {"ch_code": 0, "ch_name": "", "message": rule["conflict_message"], "column": ""},
            },
        },
    ]


# The byte_strings cases (document mode, _stubshared.DOC_TEMPLATES): each
# payload is the bytes a document field carries.
DOCUMENT_CASES = [
    # A binary String value: 0xFF and 0x80 are not UTF-8, so the renderings
    # travel as stored_b64 / input_b64, and value_b64 carries the raw bytes.
    ("document.preview_row.binary_string", "chs_preview_row", b"\xff\x00\x80"),
    # Valid UTF-8 with a NUL: plain members (NUL written as an escape), and
    # value_b64 all the same, since a String value always carries it.
    ("document.preview_row.utf8_with_nul", "chs_preview_row", b"a\x00b"),
    # SQL text: a column name that is not UTF-8 makes the name, the
    # declaration and the joined columns_sql all travel as _b64.
    ("document.discover_columns.binary_name", "chs_discover_columns", b"\xffcol"),
]


def _document_cases(model) -> list[dict]:
    """A "document" case: the call's named output must parse as STRICT UTF-8
    JSON and equal the expected document EXACTLY (no extra member, so an
    `F` beside its `F_b64` fails). The expectation is rendered in Python from
    the same template the stub's C is compiled from, and checked against the
    description's own schema for that document before it is written."""
    out = []
    for case_id, fn_name, payload in DOCUMENT_CASES:
        param, template = _stubshared.DOC_FUNCTIONS[fn_name]
        data = _stubshared.DOC_PREFIX + payload
        case = _overridden_echo_case(model, case_id, fn_name, {param: data})
        doc = _stubshared.doc_render(template, data)
        problems = abimodel.validate(doc, model.documents[template].schema)
        if problems:
            raise ValueError(f"{case_id}: the expected document fails documents.{template}.schema: {problems}")
        if template == "row":
            got = base64.b64decode(doc["cols"][0]["value_b64"], validate=True)
            if got != payload:
                raise ValueError(f"{case_id}: value_b64 decodes to {got!r}, not the payload {payload!r}")
        out.append(
            {
                "id": case_id,
                "kind": "document",
                "fn": fn_name,
                "args": case["args"],
                "expect": {"status": "CHS_OK", "documents": {"out": doc}},
            }
        )
    return out


def _live(model, **counts: int) -> dict:
    """A live_delta step over every handle kind the description has: the
    kinds not named must not have moved either."""
    return {"live_delta": {k: counts.get(k, 0) for k in model.handles}}


def _lifecycle_cases(model) -> list[dict]:
    """D2 plus decision 2, as steps: a filter (and a block) holds a counted
    reference to its schema, so the schema outlives the caller's free of it,
    the child still works, and the schema is released only with the last
    child. Expectations come from the same helpers the echo cases use."""
    for k in ("chs_buf", "chs_error", "chs_schema", "chs_filter", "chs_block"):
        if k not in model.handles:
            raise ValueError(f"_lifecycle_cases: the description has no handle {k!r}")
    schema = _schema_recipe(model)
    filt = {
        "fn": "chs_filter_create",
        "args": [{"ref": "s"}, {"bytes_hex": b"x = 1".hex()}, {"bytes_hex": b"{}".hex()}, {"bytes_hex": ""}],
    }
    block = {
        "fn": "chs_block_create",
        "args": [{"ref": "s"}, {"int": 0}, {"bytes_hex": b"{}".hex()}, {"bytes_hex": ""}, {"bytes_hex": ""}],
    }
    body = b'{"x":1}'
    eval_body = {
        "fn": "chs_filter_eval_body",
        "args": [{"ref": "f"}, {"int": 0}, {"bytes_hex": body.hex()}, {"bytes_hex": ""}],
    }
    eval_body_expect = {
        "status": "CHS_OK",
        "outputs": {
            "out": {
                "fn": "chs_filter_eval_body",
                "out": "out",
                "args": [{"kind": "chs_filter"}, 0, _stubshared.echo_bytes_field(body), _stubshared.echo_bytes_field(b"")],
            }
        },
    }
    eval_block = {"fn": "chs_filter_eval_block", "args": [{"ref": "f"}, {"ref": "b"}]}
    eval_block_expect = {
        "status": "CHS_OK",
        "outputs": {
            "out": {"fn": "chs_filter_eval_block", "out": "out", "args": [{"kind": "chs_filter"}, {"kind": "chs_block"}]}
        },
    }
    return [
        {
            "id": "lifecycle.filter_holds_schema",
            "kind": "lifecycle",
            "steps": [
                {"let": "s", "call": schema},
                {"let": "f", "call": filt},
                _live(model, chs_schema=1, chs_filter=1),
                {"free": "s"},
                _live(model, chs_schema=1, chs_filter=1),
                {"call": eval_body, "expect": eval_body_expect},
                {"free": "f"},
                _live(model),
            ],
        },
        {
            "id": "lifecycle.block_and_filter_hold_schema",
            "kind": "lifecycle",
            "steps": [
                {"let": "s", "call": schema},
                {"let": "f", "call": filt},
                {"let": "b", "call": block},
                {"free": "s"},
                _live(model, chs_schema=1, chs_filter=1, chs_block=1),
                {"call": eval_block, "expect": eval_block_expect},
                {"free": "f"},
                _live(model, chs_schema=1, chs_block=1),
                {"free": "b"},
                _live(model),
            ],
        },
        *_server_lifecycle_cases(model),
    ]


def _server_lifecycle_cases(model) -> list[dict]:
    """Generation 2: a schema holds a counted reference to the server it was
    created on, so the server outlives the caller's free of it, the schema
    still answers, and the server goes only with the schema. Empty for ABI v1."""
    if "chs_server" not in model.handles:
        return []
    server = {"fn": "chs_server_create", "args": [{"bytes_hex": FULL_PROFILE.hex()}, {"bytes_hex": ""}]}
    schema = {"fn": "chs_schema_create", "args": _schema_args(model, b"CREATE TABLE t (x Int32)", {"ref": "v"})}
    describe = {"fn": "chs_schema_describe", "args": [{"ref": "s"}]}
    describe_expect = {
        "status": "CHS_OK",
        "outputs": {"out": {"fn": "chs_schema_describe", "out": "out", "args": [{"kind": "chs_schema"}]}},
    }
    return [
        {
            "id": "lifecycle.schema_holds_server",
            "kind": "lifecycle",
            "steps": [
                {"let": "v", "call": server},
                {"let": "s", "call": schema},
                _live(model, chs_server=1, chs_schema=1),
                {"free": "v"},
                _live(model, chs_server=1, chs_schema=1),
                {"call": describe, "expect": describe_expect},
                {"free": "s"},
                _live(model),
            ],
        }
    ]


CONCURRENT_THREADS = 8
CONCURRENT_CALLS = 16


def _concurrent_cases(model) -> list[dict]:
    """Decision 1: every `shared` call the stub implements generically, made
    from several threads at once on the SAME handles (each argument handle
    minted once), every call answering exactly as its echo case does."""
    kinds = stub.classify(model)
    out = []
    for fn in model.functions:
        if fn.thread != "shared" or kinds.get(fn.name) != "generic":
            continue
        echo = _echo_case(fn, model)
        out.append(
            {
                "id": f"{fn.name}.concurrent",
                "kind": "concurrent",
                "fn": fn.name,
                "args": echo["args"],
                "threads": CONCURRENT_THREADS,
                "calls": CONCURRENT_CALLS,
                "expect": echo["expect"],
            }
        )
    return out


HANDWRITTEN_CASES = [
    {"id": "chs_abi_version.handshake", "kind": "handshake", "fn": "chs_abi_version", "expect": {"int": 1}},
    {"id": "chs_abi_revision.tombstone", "kind": "handshake", "fn": "chs_abi_revision", "expect": {"int": 1001}},
    {
        "id": "chs_build_info.handshake",
        "kind": "handshake",
        "fn": "chs_build_info",
        "expect": {"contains": "\"schema\":1"},
    },
    {
        "id": "chs_clickhouse_version.handshake",
        "kind": "handshake",
        "fn": "chs_clickhouse_version",
        "expect": {"non_empty": True},
    },
]


CTOR_MARKER_VARIANT = "ctor-marker"


def _loader_cases(model) -> list[dict]:
    cases = []
    for v in _stubshared.plan(model):
        if v.name == CTOR_MARKER_VARIANT:
            # Linux: the predicate's glibc_floor "99.0" refuses before dlopen
            # ever runs (v.reason, from _stubshared.plan(), is "glibc_floor").
            # Darwin: step 1 is skipped entirely by spec, so the library
            # loads like "ok" — "accepted", not a refusal.
            cases.append(
                {
                    "id": f"loader.{v.name}.linux",
                    "kind": "loader",
                    "variant": v.name,
                    "os": "linux",
                    "expect": {"reason": v.reason},
                }
            )
            cases.append(
                {
                    "id": f"loader.{v.name}.darwin",
                    "kind": "loader",
                    "variant": v.name,
                    "os": "darwin",
                    "expect": {"reason": "accepted"},
                }
            )
            continue
        cases.append({"id": f"loader.{v.name}", "kind": "loader", "variant": v.name, "expect": {"reason": v.reason}})
    return cases


_ARG_FOR_KIND = {
    "scalar": ("int",),
    "enum": ("int",),
    "bytes_in": ("bytes_hex",),
    "handle": ("handle", "null_handle", "ref"),
}


def _check_call(model, fn_name: str, args: list, where: str) -> None:
    fn = model.function(fn_name)
    inputs = [p for p in fn.params if not p.is_out]
    if len(args) != len(inputs):
        raise ValueError(f"{where}: {fn_name} takes {len(inputs)} inputs, the case passes {len(args)}")
    for p, a in zip(inputs, args, strict=True):
        shape = next(iter(a))
        if shape not in _ARG_FOR_KIND[p.kind]:
            raise ValueError(f"{where}: {fn_name} parameter {p.name} is {p.kind}, the case passes {shape}")
        if shape == "null_handle" and not p.nullable:
            raise ValueError(f"{where}: {fn_name} parameter {p.name} is not nullable")
        if shape == "handle":
            _check_call(model, a["handle"]["fn"], a["handle"]["args"], where)


def _check_calls(model, cases: list[dict]) -> None:
    """Every call this file describes, recipes and lifecycle steps included,
    matches its function's input parameters in number and kind, so a
    signature change in the description fails generation here instead of in
    four runners."""
    for c in cases:
        where = f"case {c['id']}"
        if "fn" in c and "args" in c:
            _check_call(model, c["fn"], c["args"], where)
        for s in c.get("steps", ()):
            if "call" in s:
                _check_call(model, s["call"]["fn"], s["call"]["args"], where)


def build_cases(model) -> list[dict]:
    kinds = stub.classify(model)
    # The handshake answers this description's own generation (1, or 2 under
    # gen.py --major 2): the stub returns CHS_ABI_VERSION from its own header.
    cases = [
        {**c, "expect": {"int": model.abi}} if c["id"] == "chs_abi_version.handshake" else c
        for c in HANDWRITTEN_CASES
    ]
    for fn in model.functions:
        if kinds.get(fn.name) != "generic":
            continue
        cases.append(_echo_case(fn, model))
        cases += _status_cases(fn, model)
    cases += _decision_cases(model)
    cases += _loader_cases(model)
    _check_calls(model, cases)
    return cases


def render(model) -> str:
    doc = {
        "_generated": banner(model),
        "schema": 1,
        "cases": build_cases(model),
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def outputs(model) -> list[Output]:
    return [Output(path(model.major), content=render(model))]
