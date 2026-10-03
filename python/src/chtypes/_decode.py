"""The document decoders (docs/reference/bindings-v1.md section 5): stock JSON,
one to one, nothing computed.

Rules, each from that section:

1. The stock `json` parser, and nothing hand-rolled. A bare `NaN` or `Infinity`
   (which RFC 8259 does not have, and the library never writes) is refused.
2. Absent is the default; an unknown key is skipped.
3. A duplicate key, or a value of the wrong JSON type, is an `InternalError`
   naming the document and the key.
4. Nothing is computed: a vocabulary fact is read from the generated tables.
5. Names are bytes. A name is `<key>` (a JSON string) or `<key>_b64` (the standard
   base64 of the raw bytes), exactly one of the two; the decoder surfaces both as
   `bytes`. Every other data-derived string field accepts the same two spellings
   (a plain string, or a `<field>_b64` sibling), and a scalar String or
   FixedString leaf's raw bytes arrive as `value_b64`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from typing import Any

from ._abi1._vocab import DefaultKind, FilterOutcome, Outcome, Reason, Source, Verdict
from .errors import ArtifactCorruptError, internal
from .results import (
    BatchResult,
    BuildInfo,
    Capabilities,
    Column,
    Computed,
    DiscoveredColumn,
    Discovery,
    ErrorCodeEntry,
    ErrorCodeTable,
    FilterResult,
    FilterRowError,
    Framing,
    Header,
    RowResult,
    SchemaDescription,
    Span,
    Transform,
    Value,
)

__all__ = [
    "build_info",
    "decode_batch",
    "decode_discovery",
    "decode_error_codes",
    "decode_filter_result",
    "decode_live_handles",
    "decode_row",
    "decode_schema_description",
    "strict_loads",
]


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"bare {name} is not JSON")


def strict_loads(raw: bytes, where: str) -> Any:
    """Parse one document: UTF-8, no duplicate key, no bare NaN/Infinity."""
    try:
        text = raw.decode("utf-8")
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except _DuplicateKey as exc:
        raise internal(f"{where}: duplicate key {exc.args[0]!r}") from None
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise internal(f"{where}: not a valid JSON document ({exc})") from None


# ------------------------------------------------------------------ accessors


def _wrong(where: str, key: str, want: str, got: object) -> Exception:
    return internal(f"{where}: key {key!r} must be {want}, got {type(got).__name__}")


def _obj(value: object, where: str, key: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _wrong(where, key or "<document>", "an object", value)
    return value


def _list(obj: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _wrong(where, key, "an array", value)
    return value


def _int(obj: Mapping[str, Any], key: str, where: str, default: int = 0) -> int:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong(where, key, "an integer", value)
    return value


def _u64(obj: Mapping[str, Any], key: str, where: str) -> int:
    """An unsigned 64-bit integer: a JSON integer, or a decimal string where it
    exceeds 2^53 (the document contract)."""
    value = obj.get(key, 0)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _wrong(where, key, "an unsigned integer", value)
    return value


def _opt_u64(obj: Mapping[str, Any], key: str, where: str) -> int | None:
    return _u64(obj, key, where) if obj.get(key) is not None else None


def _bool(obj: Mapping[str, Any], key: str, where: str, default: bool = False) -> bool:
    value = obj.get(key, default)
    if not isinstance(value, bool):
        raise _wrong(where, key, "a boolean", value)
    return value


def _opt_bool(obj: Mapping[str, Any], key: str, where: str) -> bool | None:
    """True, false, or unknown (JSON null or absent): never false for unknown."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _wrong(where, key, "a boolean or null", value)
    return value


def _text(obj: Mapping[str, Any], key: str, where: str, default: str = "") -> str:
    """ASCII text the ABI promises (a vocabulary value, a setting name)."""
    value = obj.get(key, default)
    if not isinstance(value, str):
        raise _wrong(where, key, "a string", value)
    return value


def _opt_text(obj: Mapping[str, Any], key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _wrong(where, key, "a string or null", value)
    return value


def _decode_b64(value: object, where: str, key: str) -> bytes:
    if not isinstance(value, str):
        raise _wrong(where, key, "a base64 string", value)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise internal(f"{where}: key {key!r} is not standard base64") from None


def _utf8(value: str, where: str, key: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise internal(f"{where}: key {key!r} holds a lone surrogate") from None


def _bytes_or_none(obj: Mapping[str, Any], key: str, where: str) -> bytes | None:
    """`<key>` or `<key>_b64`, surfaced as bytes; None when neither is present.
    Both present is a document that breaks its own schema."""
    plain = obj.get(key)
    b64 = obj.get(f"{key}_b64")
    if plain is not None and b64 is not None:
        raise internal(f"{where}: both {key!r} and {key + '_b64'!r} are present")
    if plain is not None:
        if not isinstance(plain, str):
            raise _wrong(where, key, "a string", plain)
        return _utf8(plain, where, key)
    if b64 is not None:
        return _decode_b64(b64, where, f"{key}_b64")
    return None


def _bytes(obj: Mapping[str, Any], key: str, where: str) -> bytes:
    """Optional data-derived bytes: absent is empty."""
    value = _bytes_or_none(obj, key, where)
    return b"" if value is None else value


def _name(obj: Mapping[str, Any], key: str, where: str) -> bytes:
    """A name: exactly one of `<key>` and `<key>_b64`."""
    value = _bytes_or_none(obj, key, where)
    if value is None:
        raise internal(f"{where}: neither {key!r} nor {key + '_b64'!r} is present")
    return value


def _strings_as_bytes(obj: Mapping[str, Any], key: str, where: str) -> tuple[bytes, ...]:
    out = []
    for item in _list(obj, key, where):
        if not isinstance(item, str):
            raise _wrong(where, key, "an array of strings", item)
        out.append(_utf8(item, where, key))
    return tuple(out)


def _ascii_list(obj: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    out = []
    for item in _list(obj, key, where):
        if not isinstance(item, str):
            raise _wrong(where, key, "an array of strings", item)
        out.append(item)
    return tuple(out)


def _objects(
    obj: Mapping[str, Any], key: str, where: str, decode: Callable[[dict[str, Any], str], Any]
) -> tuple[Any, ...]:
    return tuple(
        decode(_obj(item, where, key), f"{where}.{key}[{i}]")
        for i, item in enumerate(_list(obj, key, where))
    )


# -------------------------------------------------------------------- pieces


def _span(obj: Mapping[str, Any], where: str) -> Span:
    return Span(off=_u64(obj, "off", where), len=_u64(obj, "len", where))


def _opt_span(obj: Mapping[str, Any], key: str, where: str) -> Span | None:
    value = obj.get(key)
    if value is None:
        return None
    return _span(_obj(value, where, key), f"{where}.{key}")


def _spans(obj: Mapping[str, Any], key: str, where: str) -> tuple[Span, ...]:
    return _objects(obj, key, where, _span)


def _value(obj: dict[str, Any], where: str) -> Value:
    source = obj.get("src")
    if not isinstance(source, str):
        raise _wrong(where, "src", "a string", source)
    try:
        stored = Source.is_stored(source)
    except KeyError:
        # The vocabulary has no fallback, so no is_stored fact can be read.
        raise internal(f"{where}: unknown value source {source!r}") from None
    return Value(
        column=_name(obj, "name", where),
        text=_bytes(obj, "stored", where),
        null=_bool(obj, "null", where),
        source=source,
        is_stored=stored,
        value=_bytes_or_none(obj, "value", where),
    )


def _transform(obj: dict[str, Any], where: str) -> Transform:
    reason = _text(obj, "reason", where)
    return Transform(
        column=_name(obj, "column", where),
        input=_bytes(obj, "input", where),
        stored=_bytes(obj, "stored", where),
        reason=reason,
        lossy=Reason.lossy(reason),
        row=_int(obj, "row", where),
    )


def _computed(obj: dict[str, Any], where: str) -> Computed:
    return Computed(
        column=_name(obj, "name", where),
        kind=_text(obj, "kind", where),
        text=_bytes(obj, "stored", where),
    )


# ----------------------------------------------------------------- documents


def decode_row(obj: Mapping[str, Any], where: str = "row") -> RowResult:
    columns = _objects(obj, "cols", where, _value)
    verdict = _opt_text(obj, "verdict", where)
    return RowResult(
        outcome=Outcome.of(_text(obj, "outcome", where)),
        err_code=_int(obj, "code", where),
        err_msg=_bytes(obj, "err", where),
        columns=columns,
        values=tuple(v for v in columns if v.is_stored),
        transformed=_objects(obj, "transformed", where, _transform),
        unknown_fields=_strings_as_bytes(obj, "unknown_fields", where),
        unsupported_settings=_ascii_list(obj, "unsupported_settings", where),
        computed=_objects(obj, "computed", where, _computed),
        verdict=None if verdict is None else Verdict.of(verdict),
        verdict_code=_int(obj, "verdict_code", where),
        verdict_err=_bytes(obj, "verdict_err", where),
        partition_id=_opt_text(obj, "partition_id", where),
        input_span=_opt_span(obj, "input_span", where),
    )


def _header(obj: dict[str, Any], where: str) -> Header:
    return Header(
        consumed=_bool(obj, "consumed", where),
        lines=_int(obj, "lines", where),
        names=tuple(
            _name(_obj(n, where, "names"), "name", f"{where}.names[{i}]")
            for i, n in enumerate(_list(obj, "names", where))
        ),
    )


def _framing(obj: dict[str, Any], where: str) -> Framing:
    header = obj.get("header")
    return Framing(
        bom_skipped=_opt_bool(obj, "bom_skipped", where),
        container=_opt_text(obj, "container", where),
        header=None
        if header is None
        else _header(_obj(header, where, "header"), f"{where}.header"),
    )


def decode_batch(raw: bytes, export: bytes | None) -> BatchResult:
    where = "batch"
    obj = _obj(strict_loads(raw, where), where)
    framing = obj.get("framing")
    engine_rows = (
        None if obj.get("engine_rows") is None else _strings_as_bytes(obj, "engine_rows", where)
    )
    spans = None if obj.get("row_spans") is None else _spans(obj, "row_spans", where)
    return BatchResult(
        outcome=Outcome.of(_text(obj, "outcome", where)),
        err_code=_int(obj, "code", where),
        err_msg=_bytes(obj, "err", where),
        rows=_objects(obj, "rows", where, decode_row),
        rows_read=_u64(obj, "rows_read", where),
        rows_skipped=_u64(obj, "rows_skipped", where),
        transformed=_objects(obj, "transformed", where, _transform),
        engine_rows=engine_rows,
        payload=export,
        spans=spans,
        export_declined=_text(obj, "export_declined", where),
        rows_passed=_u64(obj, "rows_passed", where),
        rows_cut=_u64(obj, "rows_cut", where),
        partition_count=_opt_u64(obj, "partition_count", where),
        unconsumed=_spans(obj, "unconsumed", where),
        framing=None
        if framing is None
        else _framing(_obj(framing, where, "framing"), f"{where}.framing"),
    )


def decode_row_document(raw: bytes) -> RowResult:
    return decode_row(_obj(strict_loads(raw, "row"), "row"), "row")


def _filter_row_error(obj: dict[str, Any], where: str) -> FilterRowError:
    return FilterRowError(
        row=_int(obj, "row", where),
        code=_int(obj, "code", where),
        msg=_bytes(obj, "err", where),
    )


def decode_filter_result(raw: bytes) -> FilterResult:
    where = "filter_result"
    obj = _obj(strict_loads(raw, where), where)
    verdicts = _text(obj, "verdicts", where)
    return FilterResult(
        outcome=FilterOutcome.of(_text(obj, "outcome", where)),
        err_code=_int(obj, "code", where),
        err_msg=_bytes(obj, "err", where),
        rows_read=_u64(obj, "rows_read", where),
        unsupported_settings=_ascii_list(obj, "unsupported_settings", where),
        verdicts=tuple(Verdict.of(c) for c in verdicts),
        errors=_objects(obj, "errors", where, _filter_row_error),
    )


def _column(obj: dict[str, Any], where: str) -> Column:
    kind = _text(obj, "default_kind", where)
    try:
        default_kind = DefaultKind.of(kind)
    except ValueError:
        raise internal(f"{where}: unknown default_kind {kind!r}") from None
    return Column(
        name=_name(obj, "name", where),
        type=_bytes(obj, "type", where),
        default_kind=default_kind,
        default_expr=_bytes(obj, "default_expr", where),
    )


def decode_schema_description(raw: bytes) -> SchemaDescription:
    where = "schema_description"
    obj = _obj(strict_loads(raw, where), where)
    return SchemaDescription(columns=_objects(obj, "columns", where, _column))


def _discovered(obj: dict[str, Any], where: str) -> DiscoveredColumn:
    return DiscoveredColumn(
        name=_name(obj, "name", where),
        declaration=_bytes(obj, "declaration", where),
    )


def decode_discovery(raw: bytes) -> Discovery:
    where = "discovery"
    obj = _obj(strict_loads(raw, where), where)
    return Discovery(columns=_objects(obj, "columns", where, _discovered))


def decode_error_codes(raw: bytes) -> ErrorCodeTable:
    where = "error_code_table"
    doc = strict_loads(raw, where)
    if not isinstance(doc, list):
        raise _wrong(where, "<document>", "an array", doc)
    entries = []
    for i, item in enumerate(doc):
        item_where = f"{where}[{i}]"
        entry = _obj(item, item_where)
        entries.append(
            ErrorCodeEntry(
                code=_int(entry, "code", item_where),
                name=_text(entry, "name", item_where),
            )
        )
    return ErrorCodeTable(tuple(entries))


def decode_live_handles(raw: bytes) -> dict[str, int]:
    where = "live_handles"
    obj = _obj(strict_loads(raw, where), where)
    out: dict[str, int] = {}
    for kind in obj:
        out[kind] = _int(obj, kind, where)
    return out


# ----------------------------------------------------------------- build info


def build_info(raw: bytes, info: Mapping[str, Any], path: str) -> BuildInfo:
    """`chs_build_info`'s already-parsed document as a `BuildInfo`. A document
    the loader accepted but whose fields have the wrong types is the artifact's
    own inconsistency: `ArtifactCorruptError` (loader step 4)."""

    def corrupt(message: str) -> ArtifactCorruptError:
        return ArtifactCorruptError(reason="build_info_malformed", path=path, got=message)

    def text(key: str) -> str:
        value = info.get(key, "")
        if not isinstance(value, str):
            raise corrupt(f"{key} must be a string")
        return value

    def integer(key: str) -> int:
        value = info.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise corrupt(f"{key} must be an integer")
        return value

    def strings(source: Mapping[str, Any], key: str) -> tuple[str, ...]:
        value = source.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise corrupt(f"capabilities.{key} must be an array of strings")
        return tuple(value)

    caps = info.get("capabilities", {})
    if not isinstance(caps, dict):
        raise corrupt("capabilities must be an object")
    toolchain = info.get("toolchain", {})
    if not isinstance(toolchain, dict):
        raise corrupt("toolchain must be an object")
    return BuildInfo(
        schema=integer("schema"),
        abi=integer("abi"),
        abi_fingerprint=text("abi_fingerprint"),
        clickhouse_version=text("clickhouse_version"),
        channel=text("channel"),
        clickhouse_minor=text("clickhouse_minor"),
        clickhouse_commit=text("clickhouse_commit"),
        core_commit=text("core_commit"),
        build=text("build"),
        inputs_sha256=text("inputs_sha256"),
        os=text("os"),
        arch=text("arch"),
        toolchain=toolchain,
        capabilities=Capabilities(
            input_formats=strings(caps, "input_formats"),
            export_formats=strings(caps, "export_formats"),
            doc_flags=strings(caps, "doc_flags"),
            features=strings(caps, "features"),
        ),
        raw=raw,
    )
