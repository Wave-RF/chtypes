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

from chtypes._ocifetch import _constants as C

__all__ = ["retired_message", "retired_text"]

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
    kept = [ch for ch in message if not _removed(ord(ch))]
    if len(kept) > C.RETIRED_MESSAGE_MAX_CODE_POINTS:
        kept = [*kept[: C.RETIRED_MESSAGE_MAX_CODE_POINTS], _ELLIPSIS]
    return "".join(kept) or None


def retired_text(url: str, status: int, body: bytes) -> str:
    """The error text for a retired repository's answer: the URL and status
    that answered, and the registry's message when it sent one."""
    text = f"{url} answered {status} Gone: the repository is retired"
    message = retired_message(body)
    if message is not None:
        text += f"; the registry says: {message}"
    return text
