"""A retired repository (docs/guides/fetch-v1.md §2, "A retired repository";
public issue #571).

The registry's operator answers every route of a retired repository with 410
Gone (`RETIRED_STATUSES`) and a short error document. That is permanent: the
transport never retries it and never asks the next base, so the caller sees
`CHTYPES_SOURCE_RETIRED` with the URL that answered and the registry's own
message, made safe to print by `retired_message`, the one function every
binding has (tests/fixtures/retired-message/cases.json is the table all four
answer alike).
"""

from __future__ import annotations

import json

from chtypes._advisory import Advisory
from chtypes._ocifetch import _constants as C

__all__ = [
    "advisory_text",
    "revoked_410",
    "revoked_410_text",
    "retired_message",
    "retired_text",
    "sanitize",
]

_ELLIPSIS = "…"


def _removed(code_point: int) -> bool:
    return (
        code_point <= 0x1F
        or 0x7F <= code_point <= 0x9F
        or code_point in (0x200E, 0x200F)
        or 0x202A <= code_point <= 0x202E
        or 0x2066 <= code_point <= 0x2069
    )


def _not_json(name: str) -> None:
    # `json.loads` accepts NaN, Infinity and -Infinity, which are not JSON;
    # the other three bindings' readers refuse them.
    raise ValueError(f"{name} is not JSON")


def retired_message(body: bytes) -> str | None:
    """The registry's own message in a retired repository's response body,
    made safe to print, or None when it sent none.

    `body` is what was read of it (at most `RETIRED_BODY_MAX_BYTES`). When it is
    UTF-8 JSON whose `errors[0].message` is a string, that string is the
    message: every code point in U+0000-U+001F, U+007F-U+009F, U+200E, U+200F,
    U+202A-U+202E and U+2066-U+2069 is removed, and the rest is cut to
    `RETIRED_MESSAGE_MAX_CODE_POINTS` code points, with U+2026 appended when it
    was cut. Nothing else is changed, and an empty result is no message."""
    try:
        text = body.decode("utf-8")
        doc = json.loads(text, parse_constant=_not_json)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    errors = doc.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    message = errors[0].get("message")
    # A lone surrogate escape is not a Unicode string (and could not be printed).
    if not isinstance(message, str) or any("\ud800" <= ch <= "\udfff" for ch in message):
        return None
    return sanitize(message) or None


def sanitize(message: str) -> str:
    """`message` made safe to print, the one rule every served string follows
    (the retired repository's message, a 410's advisories, a signed revocation's
    fields): the code points `_removed` names are dropped, the rest is cut to
    `RETIRED_MESSAGE_MAX_CODE_POINTS` code points with U+2026 appended when it was
    cut. "" when nothing is left."""
    kept = [ch for ch in message if not _removed(ord(ch))]
    if len(kept) > C.RETIRED_MESSAGE_MAX_CODE_POINTS:
        kept = [*kept[: C.RETIRED_MESSAGE_MAX_CODE_POINTS], _ELLIPSIS]
    return "".join(kept)


def retired_text(url: str, status: int, body: bytes) -> str:
    """The error text for a retired repository's answer: the URL and status
    that answered, and the registry's message when it sent one."""
    text = f"{url} answered {status} Gone: the repository is retired"
    message = retired_message(body)
    if message is not None:
        text += f"; the registry says: {message}"
    return text


def _safe_field(obj: dict, key: str) -> str:
    """`obj[key]` when it is a string with no lone surrogate escape, made safe to
    print; "" otherwise."""
    value = obj.get(key)
    if not isinstance(value, str) or any("\ud800" <= ch <= "\udfff" for ch in value):
        return ""
    return sanitize(value)


def revoked_410(body: bytes) -> tuple[Advisory, ...] | None:
    """A 410 body's advisories when it says the tag is revoked, else None
    (docs/guides/fetch-v1.md, "Build-level revocation", "A 410 on a tag").

    `body` is what was read of it. It is a revoked 410 when it is UTF-8 JSON
    (a byte order mark is not JSON) whose `errors[0].detail.revoked` is an
    object. Its advisories are that object's `advisories` entries, each field a
    string made safe to print: one that is absent, not a string, empty once made
    safe or a lone surrogate escape is "" (absent). An entry that is not an
    object, or leaves every field empty, is skipped. A repeated key reads as its
    last value. tests/fixtures/revoked-410/cases.json is the table all four
    bindings answer alike."""
    try:
        doc = json.loads(body.decode("utf-8"), parse_constant=_not_json)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    errors = doc.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    detail = errors[0].get("detail")
    if not isinstance(detail, dict):
        return None
    revoked = detail.get("revoked")
    if not isinstance(revoked, dict):
        return None
    entries = revoked.get("advisories")
    advisories: list[Advisory] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            advisory = Advisory(
                id=_safe_field(entry, "id"),
                url=_safe_field(entry, "url"),
                class_=_safe_field(entry, "class"),
                summary=_safe_field(entry, "summary"),
                build=_safe_field(entry, "build"),
                superseded_by=_safe_field(entry, "superseded_by"),
            )
            if advisory == Advisory():
                continue
            advisories.append(advisory)
    return tuple(advisories)


def advisory_text(a: Advisory) -> str:
    """One 410 advisory: `<id> <url> (<class>) build <build>, superseded by build
    <superseded_by>: <summary>`, a part whose field is empty left out with the
    separator before it, and the first part present taking none."""
    out = ""

    def join(sep: str, part: str) -> None:
        nonlocal out
        out = f"{out}{sep}{part}" if out else part

    for part in (a.id, a.url):
        if part:
            join(" ", part)
    if a.class_:
        join(" ", f"({a.class_})")
    if a.build:
        join(" ", f"build {a.build}")
    if a.superseded_by:
        join(", ", f"superseded by build {a.superseded_by}")
    if a.summary:
        join(": ", a.summary)
    return out


def revoked_410_text(advisories: tuple[Advisory, ...]) -> str:
    """What follows `<url> answered 410 Gone: ` for a revoked 410."""
    parts = [t for t in (advisory_text(a) for a in advisories) if t]
    if not parts:
        return "revoked, and the registry names no advisory"
    return "revoked: " + "; ".join(parts)
