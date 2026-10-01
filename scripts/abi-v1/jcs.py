#!/usr/bin/env python3
"""jcs.py: RFC 8785 (JSON Canonicalization Scheme) for the ABI description.

The ABI fingerprint is `sha256:` plus the lowercase hex sha256 of the JCS form
of `spec/abi-v1/abi.json`. This module computes that form with the standard
library only, and it is EXACT rather than approximate because it refuses every
input outside the subset on which Python's own serializer and RFC 8785 agree
byte for byte:

  * every string, object key included, is ASCII. Key order is then the same
    whether it is taken over UTF-16 code units (RFC 8785 section 3.2.3) or
    over code points (Python's sort), and nothing needs a non-ASCII escape;
  * there are no floating-point numbers, and every integer n satisfies
    |n| < 2**53. RFC 8785 serializes numbers through ECMAScript's
    Number.prototype.toString, which for such integers is the plain decimal
    spelling that Python's int.__str__ produces;
  * no object repeats a key. RFC 8785 requires a parser to refuse duplicates,
    and Python's json module silently keeps the last one, so the parse here
    refuses them itself.

Inside that subset `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` is RFC 8785: Python escapes `"` and `\\` with a
backslash, writes \\b \\f \\n \\r \\t as two-character escapes and every other
control character as a lowercase \\u00xx escape, and leaves everything else,
`/` and DEL included, unescaped. That is RFC 8785 section 3.2.2.2 exactly.

The generator's self-test runs RFC 8785's rules on planted inputs, and CI
recomputes the fingerprint with an independent RFC 8785 implementation and
requires the same bytes. Nothing else in this repository computes the
fingerprint: the header, the bindings and the artifact producer copy it.

    python3 scripts/abi-v1/jcs.py FILE   print FILE's canonical form (no newline)
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

MAX_SAFE = 2**53


class JCSError(ValueError):
    """An input outside the subset this canonicalizer handles exactly."""


def _refuse_float(text: str) -> Any:
    raise JCSError(f"floating-point number {text!r}: the description carries integers only")


def _refuse_constant(text: str) -> Any:
    raise JCSError(f"non-JSON constant {text!r}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise JCSError(f"duplicate object key {key!r}")
        out[key] = value
    return out


def loads(data: bytes | str) -> Any:
    """Parse JSON, refusing duplicate keys, floats and NaN/Infinity, then
    check the result against the subset (ASCII strings, safe integers)."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise JCSError(f"not UTF-8: {e}") from None
    else:
        text = data
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
        )
    except json.JSONDecodeError as e:
        raise JCSError(f"not JSON: {e}") from None
    check(obj)
    return obj


def check(obj: Any, where: str = "$") -> None:
    """Refuse anything outside the subset, naming where it is."""
    if obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, int):
        if not -MAX_SAFE < obj < MAX_SAFE:
            raise JCSError(f"{where}: integer {obj} is outside +/-2**53")
        return
    if isinstance(obj, float):
        raise JCSError(f"{where}: floating-point number {obj!r}")
    if isinstance(obj, str):
        if not obj.isascii():
            raise JCSError(f"{where}: non-ASCII string {obj!r}")
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            check(item, f"{where}[{i}]")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise JCSError(f"{where}: non-string key {key!r}")
            if not key.isascii():
                raise JCSError(f"{where}: non-ASCII key {key!r}")
            check(value, f"{where}.{key}")
        return
    raise JCSError(f"{where}: {type(obj).__name__} is not a JSON value")


def canonicalize(obj: Any) -> bytes:
    """The RFC 8785 form of `obj`, which must lie inside the subset."""
    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fingerprint(data: bytes) -> str:
    """`sha256:<64 lowercase hex>` over the JCS form of the JSON text `data`."""
    return "sha256:" + hashlib.sha256(canonicalize(loads(data))).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: jcs.py FILE", file=sys.stderr)
        return 2
    with open(argv[0], "rb") as f:
        data = f.read()
    try:
        sys.stdout.buffer.write(canonicalize(loads(data)))
    except JCSError as e:
        print(f"jcs: {argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
