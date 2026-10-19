"""Marshaling a call's inputs: bytes in, the settings object, the column list.

Plumbing only. Nothing here knows a ClickHouse rule: a settings value is a
string and is written verbatim (no boolean or integer spelling, no float), the
per-call zone is one settings key written verbatim, and a column name is the
bytes the caller gave, in the document's name encoding (`name` when valid UTF-8,
else `name_b64`).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ._abi2._vocab import Posture
from .errors import _misuse as misuse

__all__ = [
    "BytesIn",
    "ColumnDefault",
    "DeferDefaults",
    "ParamEncoding",
    "ServerProfile",
    "Settings",
    "VerdictBasis",
]

BytesIn = bytes | str
Settings = Mapping[str, str]

_ZONE_KEY = "session_timezone"


def to_bytes(value: BytesIn, what: str) -> bytes:
    """A statement, expression, type, name or literal: `str` is UTF-8 encoded
    (the only way text becomes bytes), bytes-like objects are copied."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise TypeError(f"{what} must be bytes or str, not {type(value).__name__}")


def body_bytes(value: bytes, what: str = "body") -> bytes:
    """A body never accepts the text type, as in v0: it may be any bytes."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise TypeError(f"{what} must be bytes, not {type(value).__name__}")


_NON_UTF8_HINT = (
    "a JSON input cannot carry these bytes here "
    '(only a settings or query-parameter value has the {"b64": ...} object form)'
)


def _is_utf8(text: str) -> bool:
    """False for a `str` that cannot be UTF-8 encoded (a lone surrogate, as
    `surrogateescape` makes of undecodable bytes)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _refuse_non_utf8(what: str, part: str, key: str) -> Exception:
    # The key is shown escaped (ASCII only), never as the raw text.
    return misuse(
        f"{what}: the key {json.dumps(key)} is not valid UTF-8, and {_NON_UTF8_HINT}"
        if part == "key"
        else f"{what}: the value for key {json.dumps(key)} is not valid UTF-8, and {_NON_UTF8_HINT}"
    )


def _byte_value(text: str, what: str, key: str) -> str | dict[str, str]:
    """One value of `json_object_byte_values`: the string itself when it is valid
    UTF-8, else the object form `{"b64": <standard base64>}` of its bytes (a `str`
    holding the lone surrogates `surrogateescape` makes of undecodable bytes
    carries those bytes). A surrogate that is not one of those has no bytes to
    carry and is refused."""
    if _is_utf8(text):
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raise _refuse_non_utf8(what, "value", key) from None
    return {"b64": base64.b64encode(raw).decode("ascii")}


def _string_map(mapping: Mapping[str, str], what: str) -> dict[str, str | dict[str, str]]:
    out: dict[str, str | dict[str, str]] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                f"{what} maps strings to strings only; got {type(key).__name__} -> "
                f"{type(value).__name__} for {key!r} (a value is never rewritten)"
            )
        # json.dumps would write a lone surrogate as a \udXXX escape, a key the
        # library does not read as the bytes the caller meant: refuse.
        if not _is_utf8(key):
            raise _refuse_non_utf8(what, "key", key)
        out[key] = _byte_value(value, what, key)
    return out


def _text_map(mapping: Mapping[str, str], what: str) -> dict[str, str]:
    """A map whose values have no object form (a server's macros): a key or a
    value that is not valid UTF-8 is refused."""
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                f"{what} maps strings to strings only; got {type(key).__name__} -> "
                f"{type(value).__name__} for {key!r} (a value is never rewritten)"
            )
        if not _is_utf8(key):
            raise _refuse_non_utf8(what, "key", key)
        if not _is_utf8(value):
            raise _refuse_non_utf8(what, "value", key)
        out[key] = value
    return out


def _dump(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("ascii")


def settings_json(
    settings: Settings | None,
    session_timezone: str | None = None,
) -> bytes | None:
    """The settings object, as a JSON object of byte values (a string, or `{"b64": ...}`
    for a value that is not UTF-8), or None for none.

    `session_timezone` is written into it as that one key, verbatim. Giving it
    alongside a settings key of the same name is a `UsageError`, whether or not
    the two agree: a call never carries two spellings of its zone.
    """
    merged: dict[str, str | dict[str, str]] = (
        {} if settings is None else _string_map(settings, "settings")
    )
    if session_timezone is not None:
        if not isinstance(session_timezone, str):
            raise TypeError(f"session_timezone must be str, not {type(session_timezone).__name__}")
        if _ZONE_KEY in merged:
            raise misuse(
                f"the {_ZONE_KEY} option and a {_ZONE_KEY} settings key were both given; "
                "a call carries one spelling of its zone"
            )
        merged[_ZONE_KEY] = _byte_value(session_timezone, "the zone", _ZONE_KEY)
    return _dump(merged) if merged else None


def string_map_json(mapping: Mapping[str, str] | None, what: str) -> bytes | None:
    """A JSON object of string values (default settings, query parameters)."""
    if not mapping:
        return None
    return _dump(_string_map(mapping, what))


def _name_objects(names: Sequence[BytesIn]) -> list[dict[str, str]]:
    out = []
    for name in names:
        raw = to_bytes(name, "a column name")
        try:
            out.append({"name": raw.decode("utf-8")})
        except UnicodeDecodeError:
            out.append({"name_b64": base64.b64encode(raw).decode("ascii")})
    return out


def columns_json(columns: Sequence[BytesIn] | None) -> bytes | None:
    """The INSERT column list: a JSON array of name objects, `{"name": ...}`
    for a name that is valid UTF-8 and `{"name_b64": ...}` otherwise."""
    if columns is None:
        return None
    if isinstance(columns, str | bytes):
        raise TypeError("columns must be a sequence of names, not a single name")
    if len(columns) == 0:
        return None
    return _dump(_name_objects(columns))


# ---- the options documents (rule r1). Each member is its own keyword, and a key
# the binding has no name for goes through the `*_options_extra` mapping as JSON
# text. The binding builds the document from the members the caller set, sends
# nothing when none was set, and never validates a member: the library refuses a
# key it does not know, or a value that is not one of the member's, with
# CHS_INVALID_ARGUMENT, naming it.


class VerdictBasis(StrEnum):
    """`insert_options.verdict_basis`: what row a filter verdict answers for."""

    INPUT = "input"  # the default: the input row
    EXPORT = "export"  # the row the export would store; a build without it declines


class DeferDefaults(StrEnum):
    """`insert_options.defer_defaults`."""

    NONE = "none"  # the default: no DEFAULT is deferred
    SERVER = "server"  # defer to the server each DEFAULT the build would decline


class ParamEncoding(StrEnum):
    """`filter_options.param_encoding`: how a query parameter's value is read."""

    ESCAPED = "escaped"  # the default: ClickHouse's escaped-text reader
    RAW = "raw"  # the bytes are the value; a build without it declines


@dataclass(frozen=True, slots=True)
class ColumnDefault:
    """One entry of `insert_options.column_defaults`: the value the call uses in
    a cell the server would fill from the column's DEFAULT. Both fields are byte
    strings (`str` is UTF-8 encoded); one that is not valid UTF-8 is sent in its
    `_b64` form."""

    name: BytesIn
    value: BytesIn


def _enum_text(value: object, what: str) -> str:
    if isinstance(value, str):  # a StrEnum member is a str: its value is the spelling
        return value.value if isinstance(value, StrEnum) else value
    raise TypeError(f"{what} must be a str or its enum, not {type(value).__name__}")


def _column_default_objects(defaults: Sequence[ColumnDefault]) -> list[dict[str, str]]:
    if isinstance(defaults, str | bytes):
        raise TypeError("column_defaults must be a sequence of ColumnDefault")
    out = []
    for entry in defaults:
        if not isinstance(entry, ColumnDefault):
            raise TypeError(
                f"column_defaults entries must be ColumnDefault, not {type(entry).__name__}"
            )
        item = _name_objects([entry.name])[0]
        raw = to_bytes(entry.value, "a column default value")
        try:
            item["value"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            item["value_b64"] = base64.b64encode(raw).decode("ascii")
        out.append(item)
    return out


def _options_json(
    what: str, members: dict[str, object], extra: Mapping[str, str] | None
) -> bytes | None:
    """The document from the members set (value already JSON-encodable) and the
    extra members (JSON text, spliced verbatim). A key set twice keeps the last
    value, and an extra member is set after the named ones. Nothing set is None
    (length 0, which the library reads as `{}`)."""
    parts: dict[str, str] = {key: json.dumps(value) for key, value in members.items()}
    if extra is not None:
        if not isinstance(extra, Mapping):
            raise TypeError(f"{what}_extra must map keys to JSON text")
        for key, text in extra.items():
            if not isinstance(key, str) or not isinstance(text, str):
                raise TypeError(f"{what}_extra maps strings to JSON text only")
            if not _is_utf8(key):
                raise _refuse_non_utf8(f"{what} options", "key", key)
            try:
                json.loads(text)
            except ValueError:
                raise misuse(
                    f"{what} options: the value for key {json.dumps(key)} is not valid JSON"
                ) from None
            parts[key] = text
    if not parts:
        return None
    body = ",".join(f"{json.dumps(key)}:{text}" for key, text in parts.items())
    try:
        return ("{" + body + "}").encode("utf-8")
    except UnicodeEncodeError:
        raise misuse(f"{what} options: a value is not valid UTF-8 JSON text") from None


def insert_options_json(
    *,
    insertable_columns: Sequence[BytesIn] | None = None,
    column_defaults: Sequence[ColumnDefault] | None = None,
    verdict_basis: VerdictBasis | str | None = None,
    defer_defaults: DeferDefaults | str | None = None,
    insert_options_extra: Mapping[str, str] | None = None,
) -> bytes | None:
    """`insert_options`, from the members set. None leaves a member out; an empty
    list is sent as `[]`."""
    members: dict[str, object] = {}
    if insertable_columns is not None:
        if isinstance(insertable_columns, str | bytes):
            raise TypeError("insertable_columns must be a sequence of names, not a single name")
        members["insertable_columns"] = _name_objects(insertable_columns)
    if column_defaults is not None:
        members["column_defaults"] = _column_default_objects(column_defaults)
    if verdict_basis is not None:
        members["verdict_basis"] = _enum_text(verdict_basis, "verdict_basis")
    if defer_defaults is not None:
        members["defer_defaults"] = _enum_text(defer_defaults, "defer_defaults")
    return _options_json("insert", members, insert_options_extra)


def filter_options_json(
    *,
    param_encoding: ParamEncoding | str | None = None,
    filter_options_extra: Mapping[str, str] | None = None,
) -> bytes | None:
    """`filter_options`, from the members set."""
    members: dict[str, object] = {}
    if param_encoding is not None:
        members["param_encoding"] = _enum_text(param_encoding, "param_encoding")
    return _options_json("filter", members, filter_options_extra)


def schema_options_json(
    *,
    posture: Posture | str | None = None,
    schema_options_extra: Mapping[str, str] | None = None,
) -> bytes | None:
    """`schema_options`, from the members set."""
    members: dict[str, object] = {}
    if posture is not None:
        members["posture"] = _enum_text(posture, "posture")
    return _options_json("schema", members, schema_options_extra)


@dataclass(frozen=True, slots=True)
class ServerProfile:
    """One ClickHouse server, as the library's `input:server_profile` describes
    it. Every field is optional and passed through as given: the library
    validates the zone with DateLUT, each setting with the server's own SET
    check, and the macros with ClickHouse's own reader. The binding validates
    nothing in it.

    `timezone` is the server's zone, the one its `timezone()` returns; None or
    "" leaves it undescribed (omitted) and the image zone applies. `settings` is
    what the server's profile applies to every query, layered under a schema's
    and a call's own; None omits it, and `{}` is sent as `{}`. `macros` is the
    server's `<macros>`: None omits it, and the server's macros are UNKNOWN (a
    schema whose engine reads one is declined); a mapping, even an empty one, is
    sent (`{}` when empty) and is the server's COMPLETE set."""

    timezone: str | None = None
    settings: Settings | None = None
    macros: Settings | None = None


def server_profile_json(profile: ServerProfile) -> bytes:
    """The profile document: an empty or absent zone and an absent map are
    omitted; a present map is sent as given, `{}` when empty. Absent `macros` is
    not empty `macros`."""
    if not isinstance(profile, ServerProfile):
        raise TypeError(f"profile must be a ServerProfile, not {type(profile).__name__}")
    doc: dict[str, object] = {}
    if profile.timezone is not None:
        if not isinstance(profile.timezone, str):
            raise TypeError(f"timezone must be str, not {type(profile.timezone).__name__}")
        if not _is_utf8(profile.timezone):
            raise misuse(f"server profile: the timezone is not valid UTF-8, and {_NON_UTF8_HINT}")
        if profile.timezone != "":
            doc["timezone"] = profile.timezone
    if profile.settings is not None:
        doc["settings"] = _string_map(profile.settings, "server profile settings")
    if profile.macros is not None:
        doc["macros"] = _text_map(profile.macros, "server profile macros")
    return _dump(doc)
