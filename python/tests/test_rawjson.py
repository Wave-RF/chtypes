"""The denormal repair and the raw-JSON layer, tested without an artifact.

These are the two places where a plausible-looking shortcut silently corrupts an
answer, so they are asserted directly rather than only through the library.
"""

from __future__ import annotations

import json

import pytest

from chtypes._rawjson import (
    RawArray,
    RawNumber,
    RawObject,
    decode_document,
    loads_raw,
    quote_bare_denormals,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Bare tokens outside a string, exactly as serializeTextJSON writes them.
        (b'{"stored":inf}', b'{"stored":"inf"}'),
        (b'{"stored":-inf}', b'{"stored":"-inf"}'),
        (b'{"stored":nan}', b'{"stored":"nan"}'),
        (b'{"stored":[inf,-inf,nan]}', b'{"stored":["inf","-inf","nan"]}'),
        (b'{"a":inf,"b":1}', b'{"a":"inf","b":1}'),
        (b'{"stored": inf}', b'{"stored": "inf"}'),
        # ... and never inside one.
        (b'{"stored":"inf"}', b'{"stored":"inf"}'),
        (b'{"info":1}', b'{"info":1}'),
        (b'{"s":"nan and inf"}', b'{"s":"nan and inf"}'),
        (b'{"s":"esc\\"inf"}', b'{"s":"esc\\"inf"}'),
        # A token that is not one of the three is left alone rather than guessed at.
        (b'{"a":infinity}', b'{"a":infinity}'),
        (b'{"a":1}', b'{"a":1}'),
    ],
)
def test_quote_bare_denormals(raw: bytes, expected: bytes) -> None:
    assert quote_bare_denormals(raw) == expected


def test_the_repair_produces_parseable_json() -> None:
    doc = decode_document(b'{"cols":[{"stored":[inf,nan],"name":"q"}]}')
    assert doc == {"cols": [{"stored": ["inf", "nan"], "name": "q"}]}


def test_the_fast_path_is_a_no_op() -> None:
    doc = b'{"stored":0,"name":"x"}'
    assert quote_bare_denormals(doc) is doc


def test_numbers_keep_clickhouse_own_text() -> None:
    doc = decode_document(
        b'{"u":18446744073709551615,'
        b'"i":-57896044618658097711785492504343953926634992332820282019728792003956564819968,'
        b'"d":2.50,"e":1e400}'
    )
    assert isinstance(doc, RawObject)
    assert all(isinstance(v, RawNumber) for v in doc.values())
    # A float round-trip would render 18446744073709552000 and -5.78960446186581e+76.
    assert doc.raw("u") == "18446744073709551615"
    assert (
        doc.raw("i")
        == "-57896044618658097711785492504343953926634992332820282019728792003956564819968"
    )
    assert doc.raw("d") == "2.50"  # trailing zero preserved: it is Decimal text
    assert doc.raw("e") == "1e400"


@pytest.mark.parametrize(
    "text",
    [
        b"0",
        b"-1",
        b'"hi"',
        b"null",
        b"true",
        b"[1,0,3]",  # the rendering measured from the artifact: no spaces
        b'{"k":1}',
        b'[7,"z"]',
        b'{"k":[1,{"n":null}]}',
        b'"2023-11-14 22:13:20"',
    ],
)
def test_a_value_comes_back_as_the_library_wrote_it(text: bytes) -> None:
    doc = decode_document(b'{"stored":' + text + b"}")
    assert isinstance(doc, RawObject)
    assert doc.raw("stored") == text.decode()


def test_a_duplicate_object_key_survives() -> None:
    """A `Map` ClickHouse stores twice under one key, rendered as it renders it.

    Measured document from the 25.8 artifact for `m Map(String, UInt8)` fed
    `{"m":{"1":1,"1":2}}`. Rendering the decoded form gave `{"1":2}` — a value
    difference this binding invented, and 52 of 34,619 differential cases
    (the conformance suite, class B). The decoded mapping still
    keeps only the last value, exactly as the reference's `json.Unmarshal`
    does; it is the reported TEXT that has to be ClickHouse's.
    """
    doc = decode_document(b'{"cols":[{"name":"m","stored":{"1":1,"1":2},"ref":null}]}')
    assert isinstance(doc, RawObject)
    cols = doc["cols"]
    assert isinstance(cols, RawArray)
    assert cols[0].raw("stored") == '{"1":1,"1":2}'
    assert cols[0]["stored"] == {"1": RawNumber("2")}
    # An absent key is None — "the library said nothing" — not an empty value.
    assert cols[0].raw("dup_dropped") is None


def test_clickhouse_escape_spelling_survives() -> None:
    """ClickHouse writes `\\u000B`; `json.encoder` writes `\\u000b`.

    Measured document from the 25.8 artifact for `s String` fed
    `{"s":"a\\u000Bb\\u001Fc"}`. The two spellings decode to the same string, so
    a scorer comparing re-parsed JSON cannot see the difference — but a
    non-UTF-8 `AggregateFunction` state has no comparable `value` and is scored
    on raw bytes, where 6 cases scored `silently_different` for exactly this
    (the conformance suite, class C).
    """
    doc = decode_document(b'{"stored":"a\\u000Bb\\u001Fc"}')
    assert isinstance(doc, RawObject)
    assert doc.raw("stored") == '"a\\u000Bb\\u001Fc"'
    assert doc["stored"] == "a\x0bb\x1fc"


def test_invalid_utf8_survives_the_round_trip() -> None:
    doc = decode_document(b'{"stored":"\xff\xfe"}')
    assert isinstance(doc, RawObject)
    assert doc.raw("stored").encode("utf-8", "surrogateescape") == b'"\xff\xfe"'


@pytest.mark.parametrize(
    "doc",
    [
        b"",
        b"{",
        b'{"a"',
        b'{"a":}',
        b'{"a":1,}',
        b'{"a" 1}',
        b"{1:2}",
        b"[1,]",
        b'{"a":1}trailing',
        b'{"a":[1',
    ],
)
def test_a_malformed_document_is_a_decode_error(doc: bytes) -> None:
    # The scanner replaces the stdlib's decode, so it must fail where the stdlib
    # fails rather than inventing a partial document.
    with pytest.raises(json.JSONDecodeError):
        decode_document(doc)


def test_whitespace_and_nesting_scan_the_same_as_the_stdlib() -> None:
    text = b'{ "a" : [ 1 , { "b" : "c" } ] , "d" : null }'
    doc = decode_document(text)
    assert doc == loads_raw(text.decode())
    assert isinstance(doc, RawObject)
    # The span is the value's own text: surrounding whitespace is not part of it.
    assert doc.raw("a") == '[ 1 , { "b" : "c" } ]'
    assert doc["a"].raw(1) == '{ "b" : "c" }'
    assert doc.raw("d") == "null"
