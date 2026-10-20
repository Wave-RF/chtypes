"""Settings, query parameters, column lists and byte inputs: plumbing only."""

from __future__ import annotations

import base64
import json

import pytest

from chtypes import ColumnDefault, ParamEncoding, Posture, ServerProfile, UsageError, VerdictBasis
from chtypes._input import (
    body_bytes,
    columns_json,
    filter_options_json,
    insert_options_json,
    schema_options_json,
    server_profile_json,
    settings_json,
    string_map_json,
    to_bytes,
)


def test_settings_are_a_json_object_of_string_values_written_verbatim() -> None:
    raw = settings_json({"max_threads": "4", "x": "true"})
    assert raw is not None
    assert json.loads(raw) == {"max_threads": "4", "x": "true"}
    assert settings_json(None) is None
    assert settings_json({}) is None


@pytest.mark.parametrize("bad", [{"a": 1}, {"a": True}, {"a": 1.5}, {1: "x"}])
def test_a_non_string_setting_is_a_type_error_never_rewritten(bad) -> None:
    with pytest.raises(TypeError):
        settings_json(bad)


def test_the_per_call_zone_is_one_settings_key_written_verbatim() -> None:
    assert json.loads(settings_json(None, "Not/AZone") or b"") == {"session_timezone": "Not/AZone"}
    merged = json.loads(settings_json({"a": "b"}, "UTC") or b"")
    assert merged == {"a": "b", "session_timezone": "UTC"}


@pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo"])
def test_the_zone_option_beside_a_zone_key_is_a_usage_error_even_when_they_agree(zone) -> None:
    with pytest.raises(UsageError) as info:
        settings_json({"session_timezone": "UTC"}, zone)
    assert info.value.status == 3 and info.value.ch_code == 0 and info.value.ch_name == ""
    assert info.value.column == b""


def test_a_name_that_is_valid_utf8_travels_as_name_and_otherwise_as_name_b64() -> None:
    raw = columns_json(["a", b"b\x00c", b"\xff\xfe"])
    names = json.loads(raw or b"")
    assert names[0] == {"name": "a"}
    assert names[1] == {"name": "b\x00c"}
    assert base64.b64decode(names[2]["name_b64"]) == b"\xff\xfe"
    assert columns_json(None) is None and columns_json([]) is None
    with pytest.raises(TypeError):
        columns_json("abc")


def test_query_parameters_are_string_valued() -> None:
    assert json.loads(string_map_json({"p": "1"}, "params") or b"") == {"p": "1"}
    with pytest.raises(TypeError):
        string_map_json({"p": 1}, "params")  # type: ignore[dict-item]


def test_text_inputs_are_utf8_and_bodies_never_accept_text() -> None:
    assert to_bytes("é", "x") == b"\xc3\xa9"
    assert to_bytes(bytearray(b"ab"), "x") == b"ab"
    with pytest.raises(TypeError):
        to_bytes(5, "x")  # type: ignore[arg-type]
    assert body_bytes(memoryview(b"ab")) == b"ab"
    with pytest.raises(TypeError):
        body_bytes("text")  # type: ignore[arg-type]


_LONE = "\udcff"  # what surrogateescape makes of the byte 0xff


def test_a_non_utf8_value_travels_as_the_b64_object_form() -> None:
    # ABI v2 json_object_byte_values: the bytes are exact; a UTF-8 value stays a string.
    got = json.loads(settings_json({"k": _LONE, "ok": "é"}) or b"")
    assert got == {"k": {"b64": "/w=="}, "ok": "é"}
    assert json.loads(settings_json({"k": _LONE}, "UTC") or b"") == {
        "k": {"b64": "/w=="},
        "session_timezone": "UTC",
    }
    # the zone is a settings value too
    assert json.loads(settings_json(None, _LONE) or b"") == {"session_timezone": {"b64": "/w=="}}
    assert json.loads(string_map_json({"p": _LONE + "a"}, "query parameters") or b"") == {
        "p": {"b64": base64.b64encode(b"\xffa").decode()}
    }


@pytest.mark.parametrize(
    ("call", "words"),
    [
        (lambda: settings_json({"k" + _LONE: "v"}), ("settings", 'the key "k\\udcff"')),
        (
            lambda: string_map_json({_LONE: "v"}, "query parameters"),
            ("query parameters", 'the key "\\udcff"'),
        ),
        # a surrogate that is not a surrogateescape byte has no bytes to carry
        (lambda: settings_json({"k": "\ud800"}), ("settings", 'the value for key "k"')),
        # a macro (name or value) and a server timezone have no object form
        (
            lambda: server_profile_json(ServerProfile(macros={"m": _LONE})),
            ("macros", 'the value for key "m"'),
        ),
        (
            lambda: server_profile_json(ServerProfile(macros={_LONE: "v"})),
            ("macros", 'the key "\\udcff"'),
        ),
        (lambda: server_profile_json(ServerProfile(timezone=_LONE)), ("timezone",)),
    ],
)
def test_a_key_or_value_with_no_byte_form_is_a_usage_error_naming_the_input_and_the_key(
    call, words
) -> None:
    with pytest.raises(UsageError) as info:
        call()
    message = info.value.message.decode("utf-8")
    for word in (*words, "not valid UTF-8", "cannot carry these bytes"):
        assert word in message
    assert info.value.status == 3 and info.value.ch_code == 0
    assert message.isascii()  # the key is escaped, never raw


def test_server_profile_settings_take_the_b64_form_too() -> None:
    doc = json.loads(server_profile_json(ServerProfile(settings={"k": _LONE, "j": "v"})))
    assert doc == {"settings": {"k": {"b64": "/w=="}, "j": "v"}}


def test_no_option_set_sends_no_document() -> None:
    assert insert_options_json() is None
    assert filter_options_json() is None
    assert schema_options_json() is None


def test_insert_options_are_the_members_set_and_nothing_else() -> None:
    raw = insert_options_json(
        insertable_columns=["a", b"\xff"],
        column_defaults=[ColumnDefault("a", "1"), ColumnDefault(b"\xfe", b"\xfd")],
        verdict_basis=VerdictBasis.EXPORT,
        defer_defaults="server",
    )
    assert json.loads(raw or b"") == {
        "insertable_columns": [{"name": "a"}, {"name_b64": "/w=="}],
        "column_defaults": [
            {"name": "a", "value": "1"},
            {"name_b64": "/g==", "value_b64": "/Q=="},
        ],
        "verdict_basis": "export",
        "defer_defaults": "server",
    }


def test_an_absent_list_is_left_out_and_a_present_empty_one_is_sent() -> None:
    assert json.loads(insert_options_json(insertable_columns=[]) or b"") == {
        "insertable_columns": []
    }
    assert json.loads(insert_options_json(column_defaults=[]) or b"") == {"column_defaults": []}
    assert insert_options_json(insertable_columns=None, column_defaults=None) is None


def test_an_unnamed_member_is_passed_through_as_json_text_verbatim() -> None:
    raw = insert_options_json(
        verdict_basis="input", insert_options_extra={"future": '{"a": [1, 2.50]}', "n": "7"}
    )
    # the JSON text is spliced as given (no re-serialization), after the named members
    assert raw == b'{"verdict_basis":"input","future":{"a": [1, 2.50]},"n":7}'
    # a key set twice keeps the last value: the extra member wins
    assert json.loads(
        insert_options_json(
            verdict_basis="input", insert_options_extra={"verdict_basis": '"export"'}
        )
        or b""
    ) == {"verdict_basis": "export"}
    with pytest.raises(UsageError) as info:
        insert_options_json(insert_options_extra={"k": "{not json"})
    assert b"not valid JSON" in info.value.message
    with pytest.raises(TypeError):
        insert_options_json(insert_options_extra={"k": 1})  # type: ignore[dict-item]


def test_filter_and_schema_options() -> None:
    assert json.loads(filter_options_json(param_encoding=ParamEncoding.RAW) or b"") == {
        "param_encoding": "raw"
    }
    assert json.loads(
        schema_options_json(posture=Posture.LOAD, schema_options_extra={"x": "true"}) or b""
    ) == {"posture": "load", "x": True}


@pytest.mark.parametrize("bad", [5, b"input", None.__class__])
def test_an_enum_option_is_a_str_or_its_enum(bad) -> None:
    with pytest.raises(TypeError):
        insert_options_json(verdict_basis=bad)


def test_the_options_never_validate_a_member() -> None:
    # The library refuses an unknown value, naming it; the binding sends it.
    assert json.loads(insert_options_json(verdict_basis="nonsense") or b"") == {
        "verdict_basis": "nonsense"
    }


def test_valid_non_ascii_serializes_byte_for_byte_as_before() -> None:
    # Measured before the change on the same inputs: json.dumps, ASCII escapes.
    assert string_map_json({"p": "é日本"}, "query parameters") == b'{"p":"\\u00e9\\u65e5\\u672c"}'
    assert settings_json({"é": "\ufffd"}, "Asia/Tokyo") == (
        b'{"\\u00e9":"\\ufffd","session_timezone":"Asia/Tokyo"}'
    )


def test_column_defaults_wire_form_utf8_b64_empty_and_order() -> None:
    def doc(*entries: ColumnDefault) -> dict[str, object]:
        return json.loads(insert_options_json(column_defaults=list(entries)) or b"")

    assert doc(ColumnDefault("a", "h\u00e9llo")) == {
        "column_defaults": [{"name": "a", "value": "h\u00e9llo"}]
    }
    assert doc(ColumnDefault("a", b"\xff\x00")) == {
        "column_defaults": [{"name": "a", "value_b64": "/wA="}]
    }
    assert doc(ColumnDefault(b"\xfe", "v")) == {
        "column_defaults": [{"name_b64": "/g==", "value": "v"}]
    }
    # An empty value is a supplied value: sent as "", never omitted.
    assert doc(ColumnDefault("a", "")) == {"column_defaults": [{"name": "a", "value": ""}]}
    assert doc(ColumnDefault("a", b"")) == {"column_defaults": [{"name": "a", "value": ""}]}
    # The list keeps its order (a JSON list compares in order).
    raw = insert_options_json(column_defaults=[ColumnDefault("z", "1"), ColumnDefault("a", "2")])
    assert json.loads(raw or b"") == {
        "column_defaults": [{"name": "z", "value": "1"}, {"name": "a", "value": "2"}]
    }
