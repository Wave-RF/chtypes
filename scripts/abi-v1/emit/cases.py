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
    nullable.

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

THREE CASE KINDS, per function (`scripts/abi-v1/emit/stub.classify`):
  * "special" and "free" functions get one small case each (a handshake
    return value, a tombstone value, or nothing testable generically — see
    HANDWRITTEN_CASES) — the generic echo/status shape does not apply to
    them, by construction (that is what "special" means).
  * "generic" functions (about three quarters of the description) get:
      - one "echo" case, with one parameter carrying ADVERSARIAL bytes (a
        NUL and an invalid-UTF-8 byte) so the sha256/head_hex round-trip is
        actually exercised, not just a plain ASCII placeholder;
      - one "status" case per `may_return` entry other than CHS_OK, via a
        `!S:` injection on the function's FIRST bytes_in parameter — so a
        function with no bytes_in parameter at all (today: chs_initialize,
        chs_discover_query, and the three tooling/table calls) gets only the
        echo case, a known, documented limitation (MERGE NOTES), not a gap
        silently dropped.

Plus one "loader" case per `scripts/abi-v1/emit/_stubshared.py` plan()
entry: `{"variant": "<name>", "expect": {"reason": "<reason>"}}`.

A handle-typed parameter is resolved through HANDLE_RECIPE (schema, filter,
block — the only three the description has today; a fourth handle kind
needs one more entry here, which `--check` cannot catch by itself, so a
schema change that adds a handle kind should grep this file).
"""

from __future__ import annotations

import json

from model import BUF_HANDLE

from . import Output, banner, stub, _stubshared

PATH = "tests/fixtures/abi-v1/cases.json"


# ------------------------------------------------------------------ recipes


def _schema_recipe() -> dict:
    return {
        "fn": "chs_schema_create",
        "args": [{"bytes_hex": b"CREATE TABLE t (x Int32)".hex()}, {"bytes_hex": ""}],
    }


def _filter_recipe() -> dict:
    return {
        "fn": "chs_filter_create",
        "args": [{"handle": _schema_recipe()}, {"bytes_hex": b"x = 1".hex()}, {"bytes_hex": "{}".encode().hex()}],
    }


def _block_recipe() -> dict:
    return {
        "fn": "chs_block_create",
        "args": [
            {"handle": _schema_recipe()},
            {"int": 0},  # CHS_JSON_EACH_ROW
            {"bytes_hex": b"{}".hex()},
            {"bytes_hex": ""},
            {"bytes_hex": ""},
        ],
    }


HANDLE_RECIPE = {"chs_schema": _schema_recipe, "chs_filter": _filter_recipe, "chs_block": _block_recipe}

# A placeholder value per bytes_in content vocabulary (model.CONTENTS' keys),
# used for every bytes_in parameter EXCEPT the function's first, which always
# gets ADVERSARIAL_BYTES instead so every echo case exercises a NUL and an
# invalid-UTF-8 byte at least once.
CONTENT_PLACEHOLDER: dict[str, bytes] = {
    "bytes": b"body",
    "name": b"col",
    "sql": b"1",
    "message": b"msg",
    "ascii": b"abc",
    "json_object_string_values": b"{}",
    "json_array_names": b"[]",
}
ADVERSARIAL_BYTES = b"a\x00\xffz"


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
        data = CONTENT_PLACEHOLDER.get(p.content or "", b"x")
        return {"bytes_hex": data.hex()}, data
    raise ValueError(f"_literal_value: unexpected kind {p.kind!r} for parameter {p.name}")


def _build_args(fn) -> tuple[list[dict], list[dict | int]]:
    """(the case's "args" list, the matching expected echo-arg shapes), with
    the function's FIRST bytes_in parameter forced to ADVERSARIAL_BYTES."""
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
            args.append({"handle": recipe()})
            echo.append({"kind": p.type})
        elif p.kind == "bytes_in":
            if not first_bytes_seen:
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


def _echo_case(fn) -> dict:
    args, echo_args = _build_args(fn)
    outputs = {}
    for p in _buf_out_params(fn):
        outputs[p.name] = {"fn": fn.name, "out": p.name, "args": echo_args}
    expect: dict = {"status": "CHS_OK"}
    if outputs:
        expect["outputs"] = outputs
    return {"id": f"{fn.name}.echo", "kind": "echo", "fn": fn.name, "args": args, "expect": expect}


def _status_cases(fn) -> list[dict]:
    first_bytes = next((p for p in fn.params if p.kind == "bytes_in"), None)
    if first_bytes is None:
        return []
    cases = []
    for status in fn.may_return:
        if status == "CHS_OK":
            continue
        args, _ = _build_args(fn)
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


def _loader_cases(model) -> list[dict]:
    return [
        {"id": f"loader.{v.name}", "kind": "loader", "variant": v.name, "expect": {"reason": v.reason}}
        for v in _stubshared.plan(model)
    ]


def build_cases(model) -> list[dict]:
    kinds = stub.classify(model)
    cases = list(HANDWRITTEN_CASES)
    for fn in model.functions:
        if kinds.get(fn.name) != "generic":
            continue
        cases.append(_echo_case(fn))
        cases += _status_cases(fn)
    cases += _loader_cases(model)
    return cases


def render(model) -> str:
    doc = {
        "_generated": banner(model),
        "schema": 1,
        "cases": build_cases(model),
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def outputs(model) -> list[Output]:
    return [Output(PATH, content=render(model))]
