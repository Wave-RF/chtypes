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

from .errors import misuse

__all__ = ["BytesIn", "ServerProfile", "Settings"]

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


def _string_map(mapping: Mapping[str, str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                f"{what} maps strings to strings only; got {type(key).__name__} -> "
                f"{type(value).__name__} for {key!r} (a value is never rewritten)"
            )
        out[key] = value
    return out


def _dump(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("ascii")


def settings_json(
    settings: Settings | None,
    session_timezone: str | None = None,
) -> bytes | None:
    """The settings object, as a JSON object of string values, or None for none.

    `session_timezone` is written into it as that one key, verbatim. Giving it
    alongside a settings key of the same name is a `UsageError`, whether or not
    the two agree: a call never carries two spellings of its zone.
    """
    merged = {} if settings is None else _string_map(settings, "settings")
    if session_timezone is not None:
        if not isinstance(session_timezone, str):
            raise TypeError(f"session_timezone must be str, not {type(session_timezone).__name__}")
        if _ZONE_KEY in merged:
            raise misuse(
                f"the {_ZONE_KEY} option and a {_ZONE_KEY} settings key were both given; "
                "a call carries one spelling of its zone"
            )
        merged[_ZONE_KEY] = session_timezone
    return _dump(merged) if merged else None


def string_map_json(mapping: Mapping[str, str] | None, what: str) -> bytes | None:
    """A JSON object of string values (default settings, query parameters)."""
    if not mapping:
        return None
    return _dump(_string_map(mapping, what))


def columns_json(columns: Sequence[BytesIn] | None) -> bytes | None:
    """The INSERT column list: a JSON array of name objects, `{"name": ...}`
    for a name that is valid UTF-8 and `{"name_b64": ...}` otherwise."""
    if columns is None:
        return None
    if isinstance(columns, str | bytes):
        raise TypeError("columns must be a sequence of names, not a single name")
    if len(columns) == 0:
        return None
    names = []
    for column in columns:
        raw = to_bytes(column, "a column name")
        try:
            names.append({"name": raw.decode("utf-8")})
        except UnicodeDecodeError:
            names.append({"name_b64": base64.b64encode(raw).decode("ascii")})
    return _dump(names)


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
        if profile.timezone != "":
            doc["timezone"] = profile.timezone
    if profile.settings is not None:
        doc["settings"] = _string_map(profile.settings, "settings")
    if profile.macros is not None:
        doc["macros"] = _string_map(profile.macros, "macros")
    return _dump(doc)
