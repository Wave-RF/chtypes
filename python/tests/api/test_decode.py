"""The document decoders: stock JSON, one to one, nothing computed."""

from __future__ import annotations

import base64
import json

import pytest

from chtypes import DefaultKind, FilterOutcome, InternalError, Outcome, Verdict
from chtypes._decode import (
    decode_batch,
    decode_discovery,
    decode_error_codes,
    decode_filter_result,
    decode_live_handles,
    decode_row_document,
    decode_schema_description,
    strict_loads,
)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def dumps(doc) -> bytes:
    return json.dumps(doc).encode()


ROW = {
    "outcome": "accepted",
    "code": 0,
    "cols": [
        {"name": "a", "null": False, "src": "input", "stored": "1", "value_b64": b64(b"1")},
        {"name_b64": b64(b"\xff\x00n"), "null": True, "src": "default", "stored": ""},
        {"name": "eph", "null": False, "src": "ephemeral_input", "stored_b64": b64(b"\xfe")},
        {"name": "g", "null": False, "src": "default_generated", "stored": "42"},
    ],
    "transformed": [
        {"column": "a", "input": "256", "stored": "0", "reason": "overflow_wrap", "row": 3},
        {"column_b64": b64(b"\xff"), "input_b64": b64(b"\x80"), "stored": "x", "reason": "zzz"},
    ],
    "unknown_fields": ["extra"],
    "unsupported_settings": ["foo"],
    "computed": [{"name": "c", "kind": "materialized", "stored": "9"}],
    "verdict": "t",
    "verdict_code": 0,
    "partition_id": "p1",
    "input_span": {"off": 0, "len": 5},
    "a_key_from_the_future": [1, 2, 3],
}


def test_a_row_decodes_one_to_one_with_names_and_values_as_bytes() -> None:
    row = decode_row_document(dumps(ROW))
    assert row.outcome is Outcome.ACCEPTED
    assert [c.column for c in row.columns] == [b"a", b"\xff\x00n", b"eph", b"g"]
    assert row.columns[0].text == b"1" and row.columns[0].value == b"1"
    assert row.columns[1].value is None and row.columns[1].null is True
    assert row.columns[2].text == b"\xfe"
    assert row.columns[3].source == "default_generated" and row.columns[3].is_stored is True
    # `values` is the subset whose is_stored fact is true, in order: the
    # ephemeral entry is in `columns` and not in `values`.
    assert [c.column for c in row.values] == [b"a", b"\xff\x00n", b"g"]
    assert row.transformed[0].lossy is True and row.transformed[0].row == 3
    assert row.transformed[1].column == b"\xff" and row.transformed[1].input == b"\x80"
    assert row.transformed[1].reason == "zzz" and row.transformed[1].lossy is True
    assert row.transformed[1].row == 0
    assert row.unknown_fields == (b"extra",) and row.unsupported_settings == ("foo",)
    assert row.computed[0].text == b"9" and row.verdict is Verdict.TRUE
    assert row.partition_id == "p1" and row.input_span.off == 0 and row.input_span.len == 5


def test_absent_is_the_default() -> None:
    row = decode_row_document(dumps({}))
    assert row.outcome is Outcome.UNSUPPORTED  # the vocabulary's fallback
    assert row.columns == () and row.verdict is None and row.input_span is None
    assert row.err_msg == b"" and row.partition_id is None


@pytest.mark.parametrize(
    "doc",
    [
        {"cols": [{"null": False, "src": "input"}]},  # neither name nor name_b64
        {"cols": [{"name": "a", "name_b64": "YQ==", "null": False, "src": "input"}]},
        {"cols": [{"name": "a", "null": False, "src": "no-such-source"}]},
        {"cols": [{"name": "a", "null": False}]},  # no src: no is_stored fact
        {"cols": [{"name": "a", "null": "no", "src": "input"}]},
        {"cols": "x"},
        {"code": "7"},
        {
            "cols": [
                {"name": "a", "stored": "x", "stored_b64": "eA==", "null": False, "src": "input"}
            ]
        },
        {"cols": [{"name_b64": "not base64!", "null": False, "src": "input"}]},
    ],
)
def test_a_document_that_breaks_its_schema_is_an_internal_error(doc) -> None:
    with pytest.raises(InternalError) as info:
        decode_row_document(dumps(doc))
    assert info.value.status == 4 and info.value.ch_code == 0


def test_a_duplicate_key_is_an_internal_error_naming_it() -> None:
    with pytest.raises(InternalError, match="duplicate key 'outcome'"):
        decode_row_document(b'{"outcome":"accepted","outcome":"rejected"}')
    with pytest.raises(InternalError, match="not a valid JSON"):
        strict_loads(b'{"a": NaN}', "doc")
    with pytest.raises(InternalError, match="not a valid JSON"):
        strict_loads(b"\xff", "doc")
    with pytest.raises(InternalError, match="not a valid JSON"):
        strict_loads(b"{", "doc")


def test_big_integers_travel_as_strings_and_decode_to_ints() -> None:
    batch = decode_batch(
        dumps(
            {
                "rows_read": "18446744073709551615",
                "unconsumed": [{"off": "9007199254740993", "len": 1}],
            }
        ),
        None,
    )
    assert batch.rows_read == 2**64 - 1 and batch.unconsumed[0].off == 2**53 + 1


BATCH = {
    "outcome": "accepted_poisoned",
    "rows": [ROW, {"outcome": "skipped", "code": 27, "err": "boom"}],
    "rows_read": 2,
    "rows_skipped": 1,
    "transformed": [{"column": "a", "input": "1", "stored": "2", "reason": "reformat", "row": 1}],
    "engine_rows": ["x", "y"],
    "row_spans": [{"off": 0, "len": 4}],
    "export_declined": "",
    "rows_passed": 1,
    "rows_cut": 0,
    "partition_count": 2,
    "unconsumed": [{"off": 5, "len": 1}],
    "framing": {
        "bom_skipped": None,
        "container": "array",
        "header": {"consumed": True, "lines": 1, "names": [{"name": "a"}, {"name_b64": "/w=="}]},
    },
}


def test_a_batch_decodes_rows_payload_framing_and_spans() -> None:
    batch = decode_batch(dumps(BATCH), b"export bytes")
    assert batch.outcome is Outcome.ACCEPTED_POISONED
    assert len(batch.rows) == 2 and batch.rows[1].outcome is Outcome.SKIPPED
    assert batch.rows[1].err_msg == b"boom" and batch.rows[1].err_code == 27
    assert batch.transformed[0].row == 1
    assert batch.engine_rows == (b"x", b"y")
    assert batch.payload == b"export bytes"
    assert batch.spans is not None and batch.spans[0].len == 4
    assert batch.partition_count == 2 and batch.rows_passed == 1
    assert batch.framing.container == "array"
    assert batch.framing.header.names == (b"a", b"\xff")


def test_unknown_is_never_false_and_never_empty() -> None:
    batch = decode_batch(
        dumps({"framing": {"bom_skipped": None, "container": None, "header": None}}), None
    )
    assert batch.framing.bom_skipped is None  # not False
    assert batch.framing.header is None  # not an empty Header
    assert batch.framing.container is None
    known = decode_batch(
        dumps({"framing": {"bom_skipped": False, "container": "stream", "header": None}}), None
    )
    assert known.framing.bom_skipped is False
    assert decode_batch(dumps({}), None).framing is None
    assert decode_batch(dumps({}), None).payload is None
    assert decode_batch(dumps({}), None).engine_rows is None


def test_a_filter_result_maps_verdicts_through_the_generated_vocabulary() -> None:
    res = decode_filter_result(
        dumps(
            {
                "outcome": "ok",
                "rows_read": 4,
                "verdicts": "tfed?",
                "errors": [{"row": 2, "code": 386, "err": "no common type"}],
            }
        )
    )
    assert res.outcome is FilterOutcome.OK
    assert res.verdicts == (
        Verdict.TRUE,
        Verdict.FALSE,
        Verdict.ERROR,
        Verdict.DECLINE,
        Verdict.DECLINE,
    )
    assert [v.answered for v in res.verdicts] == [True, True, False, False, False]
    assert res.errors[0].msg == b"no common type" and res.errors[0].row == 2
    assert decode_filter_result(dumps({"outcome": "weird"})).outcome is FilterOutcome.UNSUPPORTED


def test_schema_description_and_discovery_carry_names_as_bytes() -> None:
    desc = decode_schema_description(
        dumps(
            {
                "columns": [
                    {"name": "a", "type": "UInt8", "default_kind": "", "default_expr": ""},
                    {
                        "name_b64": b64(b"\xff"),
                        "type_b64": b64(b"Nullable(\xfe)"),
                        "default_kind": "MATERIALIZED",
                        "default_expr": "a + 1",
                    },
                ]
            }
        )
    )
    assert desc.columns[0].default_kind is DefaultKind.NONE
    assert desc.columns[1].name == b"\xff" and desc.columns[1].type == b"Nullable(\xfe)"
    assert desc.columns[1].default_kind is DefaultKind.MATERIALIZED
    with pytest.raises(InternalError, match="default_kind"):
        decode_schema_description(dumps({"columns": [{"name": "a", "default_kind": "WHATEVER"}]}))
    disc = decode_discovery(
        dumps(
            {
                "columns": [
                    {"name": "a", "declaration": "`a` UInt8"},
                    {"name_b64": "/w==", "declaration": "x"},
                ]
            }
        )
    )
    assert disc.columns[0].declaration == b"`a` UInt8" and disc.columns[1].name == b"\xff"


def test_error_code_table_and_live_handles() -> None:
    table = decode_error_codes(
        dumps([{"code": 0, "name": "OK"}, {"code": 62, "name": "SYNTAX_ERROR"}])
    )
    assert table.name(62) == "SYNTAX_ERROR" and table.code("OK") == 0
    assert table.name(1) is None and table.code("syntax_error") is None  # exact, never synthesized
    assert [e.code for e in table.all()] == [0, 62] and len(table) == 2
    with pytest.raises(InternalError):
        decode_error_codes(b'{"a": 1}')
    assert decode_live_handles(b'{"chs_schema": 2, "chs_buf": 0}') == {
        "chs_schema": 2,
        "chs_buf": 0,
    }
