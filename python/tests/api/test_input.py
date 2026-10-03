"""Settings, query parameters, column lists and byte inputs: plumbing only."""

from __future__ import annotations

import base64
import json

import pytest

from chtypes import UsageError
from chtypes._input import body_bytes, columns_json, settings_json, string_map_json, to_bytes


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
