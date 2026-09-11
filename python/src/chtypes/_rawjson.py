"""Raw-preserving JSON, because the result documents carry values Python's own
types cannot hold without changing them.

Three of them, all paid for (docs/reference/c-abi.md "The row result document"):

* ClickHouse integers go to 2**256. Decoding `18446744073709551615` into a
  float yields `18446744073709552000`, and an `Int256` yields
  `-5.78960446186581e+76` — a value difference no scorer can tell from a real
  coercion defect. Every number therefore decodes to `RawNumber`, which keeps
  ClickHouse's own text and never becomes a float.
* A ClickHouse `String` column holds arbitrary bytes. The document is decoded
  with ``surrogateescape``, so bytes that are not valid UTF-8 survive the
  round-trip instead of being replaced by U+FFFD — which would read downstream
  as a silent transformation that never happened.
* **A decoded value can no longer be rendered back.** Python's `dict` drops a
  duplicate object key and its encoder respells an escape, so re-rendering the
  decoded form of `{"1":1,"1":2}` (a `Map` ClickHouse really stores and really
  renders that way) yields `{"1":2}`, and ClickHouse's `"\\u000B"` comes back as
  `"\\u000b"`. Both are silent value differences invented by this binding, and
  both were measured: 52 and 11 cases of 34,619 in the Python-vs-Go differential
  (chtypes-core/tests/conformance/python/README.md, classes B and C).

So nothing is ever rendered back. `decode_document` scans the document once and
keeps, for every member of every container, the **exact source span** the
library wrote; `RawObject.raw(key)` / `RawArray.raw(index)` hand that text back
verbatim. It is the same guarantee the reference implementation gets from
`json.RawMessage` (`go/chtypes/chtypes.go`: `StoredRaw`, `RefRaw`,
`EngineRows`), reached the same way — the bytes are never decoded and re-encoded
at all.

`loads_raw` / `decode_prefix` stay decode-only: they serve the transformation
detector, which *compares* two values rather than reporting either onward, and
which must collapse a duplicate key exactly as the reference's
`json.Unmarshal` into `any` does.
"""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Final

__all__ = [
    "RawArray",
    "RawNumber",
    "RawObject",
    "decode_document",
    "decode_prefix",
    "loads_raw",
    "quote_bare_denormals",
]


class RawNumber:
    """A JSON number, kept as the exact text ClickHouse wrote."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RawNumber({self.text!r})"

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RawNumber) and other.text == self.text

    def __hash__(self) -> int:
        return hash(self.text)


# parse_constant covers the `NaN` / `Infinity` / `-Infinity` spellings Python's
# decoder accepts: they must not become floats either.
_DECODER: Final = json.JSONDecoder(
    parse_int=RawNumber, parse_float=RawNumber, parse_constant=RawNumber
)


def loads_raw(text: str) -> object:
    """Decode one JSON document, keeping every number as its exact text.

    Decode-only: use `decode_document` for anything whose text is reported
    onward, because this drops a duplicate object key.
    """
    return _DECODER.decode(text)


def decode_prefix(text: str, idx: int = 0) -> tuple[object, int]:
    """Decode the one JSON value starting at `idx`; returns it and the end index.

    Raises `json.JSONDecodeError` when the text does not start with a value.
    """
    return _DECODER.raw_decode(text, idx)


class RawObject(dict):
    """A decoded JSON object that also keeps each member's exact source text.

    The Python equivalent of unmarshalling into `map[string]json.RawMessage`:
    the decoded form is there for control flow (`outcome`, `code`, the flags),
    and `raw()` returns the library's own bytes for anything reported onward.
    Duplicate keys follow the reference exactly — the last value wins in the
    decoded mapping — and because `raw()` answers from the source text, a
    duplicate key *inside* a reported value survives where ClickHouse put it.
    """

    __slots__ = ("_source", "_spans")

    def __init__(
        self, source: str, members: dict[str, object], spans: dict[str, tuple[int, int]]
    ) -> None:
        super().__init__(members)
        self._source = source
        self._spans = spans

    def raw(self, key: str) -> str | None:
        """The exact source text of one member, or None when the key is absent."""
        span = self._spans.get(key)
        if span is None:
            return None
        return self._source[span[0] : span[1]]


class RawArray(list):
    """A decoded JSON array that also keeps each element's exact source text."""

    __slots__ = ("_source", "_spans")

    def __init__(self, source: str, items: list[object], spans: list[tuple[int, int]]) -> None:
        super().__init__(items)
        self._source = source
        self._spans = spans

    def raw(self, index: int) -> str:
        """The exact source text of one element."""
        start, end = self._spans[index]
        return self._source[start:end]


def decode_document(doc: bytes) -> object:
    """Repair, decode and scan one result document straight from the library.

    Every container remembers where each member's text starts and ends, so the
    text a caller sees is the text ClickHouse wrote — never this binding's
    respelling of it.
    """
    return _scan_document(quote_bare_denormals(doc).decode("utf-8", "surrogateescape"))


_WHITESPACE: Final = " \t\n\r"


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _scan_document(text: str) -> object:
    try:
        value, end = _scan_value(text, _skip_ws(text, 0))
        if _skip_ws(text, end) != len(text):
            raise JSONDecodeError("Extra data", text, end)
    except IndexError:
        raise JSONDecodeError("Unterminated value", text, max(len(text) - 1, 0)) from None
    return value


def _scan_value(text: str, at: int) -> tuple[object, int]:
    """One JSON value and the index just past it, spans kept for containers."""
    char = text[at]
    if char == "{":
        return _scan_object(text, at)
    if char == "[":
        return _scan_array(text, at)
    # Strings, numbers and the three literals: the stdlib scanner decodes them
    # and the caller keeps the span, so no value is ever re-rendered.
    return _DECODER.raw_decode(text, at)


def _scan_object(text: str, at: int) -> tuple[RawObject, int]:
    members: dict[str, object] = {}
    spans: dict[str, tuple[int, int]] = {}
    index = _skip_ws(text, at + 1)
    if text[index] == "}":
        return RawObject(text, members, spans), index + 1
    while True:
        key, index = _DECODER.raw_decode(text, index)
        if not isinstance(key, str):
            raise JSONDecodeError("Expecting property name enclosed in double quotes", text, index)
        index = _skip_ws(text, index)
        if text[index] != ":":
            raise JSONDecodeError("Expecting ':' delimiter", text, index)
        start = _skip_ws(text, index + 1)
        value, index = _scan_value(text, start)
        members[key] = value
        spans[key] = (start, index)
        index = _skip_ws(text, index)
        char = text[index]
        if char == ",":
            index = _skip_ws(text, index + 1)
            continue
        if char == "}":
            return RawObject(text, members, spans), index + 1
        raise JSONDecodeError("Expecting ',' delimiter", text, index)


def _scan_array(text: str, at: int) -> tuple[RawArray, int]:
    items: list[object] = []
    spans: list[tuple[int, int]] = []
    index = _skip_ws(text, at + 1)
    if text[index] == "]":
        return RawArray(text, items, spans), index + 1
    while True:
        start = index
        value, index = _scan_value(text, start)
        items.append(value)
        spans.append((start, index))
        index = _skip_ws(text, index)
        char = text[index]
        if char == ",":
            index = _skip_ws(text, index + 1)
            continue
        if char == "]":
            return RawArray(text, items, spans), index + 1
        raise JSONDecodeError("Expecting ',' delimiter", text, index)


_VALUE_MAY_START_AFTER: Final = frozenset(b":,[ \t\n")
_DENORMAL_ENDS: Final = frozenset(b",}]")


def quote_bare_denormals(doc: bytes) -> bytes:
    """Quote the bare `inf` / `-inf` / `nan` tokens ClickHouse writes.

    This is a REQUIRED repair, not a nicety. ClickHouse's own
    `serializeTextJSON` writes IEEE denormals as bare tokens unless
    `output_format_json_quote_denormals` is set; that is faithful ClickHouse
    output and it is not valid JSON. One `QBit` column full of infinities took
    an entire result document down upstream and cost 18 arbiter cases at once
    (measured again here: `QBit(Float64, 8)` on the 26.7 artifact renders
    `"stored":[inf,inf,...]`).

    The stored *text* is preserved exactly — only quoting is added, and never
    inside a JSON string — so a denormal arrives as the JSON string `"inf"` /
    `"-inf"` / `"nan"`, which is what a caller must pass onward. It MUST NOT be
    converted to a float: that loses the distinction between `inf` and a large
    finite value and cannot survive a JSON round-trip at all.
    """
    if b"inf" not in doc and b"nan" not in doc:
        return doc

    out = bytearray()
    in_string = False
    i = 0
    n = len(doc)
    while i < n:
        byte = doc[i]
        if in_string:
            out.append(byte)
            if byte == 0x5C and i + 1 < n:  # backslash: the next byte is escaped
                i += 1
                out.append(doc[i])
            elif byte == 0x22:  # closing quote
                in_string = False
            i += 1
            continue
        if byte == 0x22:
            in_string = True
            out.append(byte)
            i += 1
            continue
        # A value can only start at the document start or after one of these.
        if out and out[-1] not in _VALUE_MAY_START_AFTER:
            out.append(byte)
            i += 1
            continue
        width = 0
        if doc.startswith(b"-inf", i):
            width = 4
        elif doc.startswith(b"inf", i) or doc.startswith(b"nan", i):
            width = 3
        if width and (i + width == n or doc[i + width] in _DENORMAL_ENDS):
            out += b'"' + doc[i : i + width] + b'"'
            i += width
            continue
        out.append(byte)
        i += 1
    return bytes(out)
