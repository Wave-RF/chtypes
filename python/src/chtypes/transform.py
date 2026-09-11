"""Silent-transformation detection: the port of `go/chtypes/transform.go`.

Two independent detectors run and their union is reported:

1. *Supplied vs stored* — the row supplied X, ClickHouse stored Y, and Y is not
   X. Both are already in hand (the raw field text and ClickHouse's own
   rendering of what it kept), so this models nothing, it only compares
   exactly. It catches what detector 2 is blind to: changes a widened type makes
   identically (a calendar roll-over, `2024-02-30` -> `2024-03-01`) and types
   with no wider type to compare against (Float64, Int256, String).
2. *Reference type* — the C layer already parsed each field a second time
   through a structurally identical type with widened leaves and reported it as
   `ref` / `ref_type`. Both parses are ClickHouse's. This one names the *reason*
   precisely (an overflow wrap versus a decimal truncation) and still fires
   where the supplied text is not comparable (a CSV field, a base64 blob).

Nothing here reimplements a coercion rule: detector 1 compares two values
already in hand, detector 2 compares two ClickHouse answers. Numeric comparison
is exact (`Fraction`, mirroring the reference's `big.Rat`) and container
comparison recurses — `[{"a": 1}]` stored as `[{"a": "1"}]` is the same data
with a different wire type, which a subscriber reading the preview would see and
the table would not have.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from json import JSONDecodeError
from typing import TYPE_CHECKING, Final

from ._rawjson import RawNumber, decode_prefix
from .results import Reason, Transform

if TYPE_CHECKING:  # avoids an import cycle: _document imports this module
    from ._document import ColDoc

__all__ = ["classify"]

# src values that carry no value to compare, so they emit no transformation.
_SILENT_SOURCES: Final = frozenset(
    {"skipped", "default_expr_unsupported", "default_volatile_unresolved", "default_pending"}
)


def classify(col: ColDoc) -> list[Transform]:
    """Every silent change this column underwent, in the reference's order."""
    if col.src in _SILENT_SOURCES:
        return []

    # The accept-then-poison class: ClickHouse stores a value it cannot read
    # back. Not a changed value but a destroyed one, and silent at insert time,
    # which is exactly what this list exists to surface.
    if col.poison:
        return [
            Transform(
                column=col.name, input=col.input, stored="<unreadable>", reason=Reason.POISONED
            )
        ]

    stored, ref = col.stored, col.ref

    # A duplicate key whose later value ClickHouse discarded. Reported first
    # because the value comparison below sees only the value that survived.
    if col.dup_dropped:
        return [
            Transform(
                column=col.name,
                input=col.input,
                stored=stored,
                reason=Reason.DUPLICATE_KEY_DROPPED,
            )
        ]

    # A column the row did not supply that ClickHouse populated. There is no
    # input to carry, so `input` is empty and the reason says where the value
    # came from; omitting these leaves a tenant unable to see what will be
    # stored for a field they never sent.
    filled = {
        "default": Reason.DEFAULT_FILLED,
        "default_substituted": Reason.DEFAULT_MATERIALIZED,
        "absent": Reason.ZERO_FILLED,
    }.get(col.src)
    if filled is not None:
        return [Transform(column=col.name, input="", stored=stored, reason=filled)]

    # ---- detector 2: reference type. Run first because it names the reason.
    ref_reason = ""
    if col.ref_type and ref and not _equivalent(col.base, stored, ref):
        ref_reason = _reason_for(col.base, stored, ref)

    # ---- detector 1: supplied vs stored.
    supplied_reason = ""
    if col.src == "input":
        supplied, supplied_ok = _parse_json_value(col.input)
        kept, kept_ok = _parse_json_value(stored)
        if supplied_ok and kept_ok:
            if not _same_value(supplied, kept):
                supplied_reason = _severity(supplied, kept)
            elif not _same_kind_deep(supplied, kept):
                # Same value, different JSON type: "1024" -> 1024, or a number
                # inside an Array(Map(String, String)) coming back as a string.
                # Nothing is lost, but the preview and the table disagree on the
                # type, and the check has to recurse: the change is often inside
                # a container, not at the top level.
                supplied_reason = Reason.REFORMAT
        elif col.null_input and not col.nullable:
            # A null the format layer replaced with a default. In CSV/TSV the
            # raw text is `\N`, which is not JSON, so it never reaches the
            # branch above — and this is the single largest lossy class.
            supplied_reason = Reason.NULL_TO_DEFAULT

    if not ref_reason and not supplied_reason:
        return []
    # Prefer the reference detector's reason — it can tell an overflow wrap from
    # a decimal truncation, where supplied-vs-stored can only say "numeric" —
    # but never let it downgrade a lossy finding to `reformat`.
    reason = ref_reason
    if not reason or (reason == Reason.REFORMAT and supplied_reason):
        reason = supplied_reason
    # An unclassified numeric/temporal leaf (no reference-ladder entry in the
    # C layer): the precise detector never ran, so a visible change cannot be
    # vouched non-lossy. Claim lossy rather than hide a possible loss.
    if col.ref_unclassified and reason == Reason.REFORMAT:
        reason = Reason.VALUE_CHANGED
    return [Transform(column=col.name, input=col.input, stored=stored, reason=reason)]


# ------------------------------------------------------- supplied vs stored


def _parse_json_value(text: str) -> tuple[object, bool]:
    """Decode exactly one JSON value, keeping numbers exact.

    docs/reference/bindings.md §detectors: the text is a JSON value only if a strict
    parse consumes ALL of it, with whitespace being exactly JSON's four.
    This port originally reproduced the Go reference's two defects (fixed in
    lockstep 2026-08-17): the trailing check decoded a SECOND value and only
    refused when that succeeded, so "1.2.3.4" passed as 1.2; and str.strip()
    admits NBSP and friends, which JSON does not.
    """
    text = text.strip(" \t\n\r")
    if not text:
        return None, False
    try:
        value, end = decode_prefix(text)
    except (JSONDecodeError, ValueError):
        return None, False
    if _skip_space(text, end) != len(text):
        return None, False
    return value, True


def _skip_space(text: str, idx: int) -> int:
    while idx < len(text) and text[idx] in " \t\n\r":
        idx += 1
    return idx


def _same_value(a: object, b: object) -> bool:
    """Equal after canonicalisation: a number and its decimal spelling are the
    same value (5 vs "5"), but a float that has thrown away 60 digits of an
    Int256 is not. Mirrors the arbiter's `_same_value`."""
    if isinstance(a, dict):
        if not isinstance(b, dict) or len(a) != len(b):
            return False
        return all(k in b and _same_value(v, b[k]) for k, v in a.items())
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return False
        return all(_same_value(x, y) for x, y in zip(a, b, strict=True))
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    a_text, a_is_str = _scalar_text(a)
    b_text, b_is_str = _scalar_text(b)
    if a_text == b_text and a_is_str == b_is_str:
        return True
    if _denormal(a_text) or _denormal(b_text):
        return _denormal(a_text) == _denormal(b_text)
    ra, ok_a = _rational(a_text)
    rb, ok_b = _rational(b_text)
    return ok_a and ok_b and ra == rb


def _scalar_text(value: object) -> tuple[str, bool]:
    if isinstance(value, RawNumber):
        return value.text, False
    if isinstance(value, str):
        return value, True
    return "", False


_NOT_A_NUMBER: Final[tuple[Fraction, bool]] = (Fraction(0), False)

# A ceiling on the digits `_rational` will materialize for one comparison. The
# reference's `big.Rat` has no limit, and neither does the corpus (its longest
# numeric field is 5,000 digits), but `Fraction(Decimal("1e999999999"))` would
# try to build a billion-digit integer to answer "are these two equal": at that
# size the honest answer is "not comparable", not a hung request.
_MAX_DIGITS: Final = 1_000_000


def _rational(text: str) -> tuple[Fraction, bool]:
    """`text` as an exact rational, mirroring the reference's `big.Rat.SetString`.

    The fallback is not defensive: CPython's `int_max_str_digits` guard (4,300
    digits by default) makes `Fraction("9" * 5000)` raise `ValueError`, which
    read as "not a number" and cost a whole class of findings — `_severity`
    answered `reformat`, which is in `LOSSLESS_REASONS`, so a genuinely lossy
    5,000-digit coercion left `Transform.lossy` False and vanished from
    `lossy_transforms` (42 cases of 34,619; chtypes-core/tests/conformance/python/README.md
    class D). `big.Rat` parses any length, so this must too.

    `Decimal`'s string parse does not go through `int()`, so it is not subject to
    that guard, and `Fraction(Decimal(...))` is exact — the two paths produce
    equal `Fraction`s for equal values, which matters because both sides of a
    comparison may not take the same one. Raising the interpreter's limit was
    the alternative and was rejected: `sys.set_int_max_str_digits` is
    process-wide, so a library that moved it would be changing its host's
    denial-of-service posture to spell one reason string correctly.
    """
    try:
        return Fraction(text), True
    except ValueError:
        pass  # possibly the digit-string guard; Decimal answers without int()
    except (ZeroDivisionError, ArithmeticError):
        return _NOT_A_NUMBER
    try:
        number = Decimal(text)
    except (ArithmeticError, ValueError):
        return _NOT_A_NUMBER
    _, digits, exponent = number.as_tuple()
    # `nan` / `inf` carry a string exponent ("n", "N", "F"); they are numeric to
    # `_denormal`, which every caller consults first, and not to `Fraction`.
    if not isinstance(exponent, int) or len(digits) + abs(exponent) > _MAX_DIGITS:
        return _NOT_A_NUMBER
    try:
        return Fraction(number), True
    except (ValueError, ArithmeticError):
        return _NOT_A_NUMBER


_DENORMALS: Final[dict[str, str]] = {
    "nan": "nan",
    "__nan__": "nan",
    "-nan": "nan",
    "inf": "inf",
    "infinity": "inf",
    "__inf__": "inf",
    "+inf": "inf",
    "-inf": "-inf",
    "-infinity": "-inf",
    "__-inf__": "-inf",
}


def _denormal(text: str) -> str:
    return _DENORMALS.get(text.strip().lower(), "")


def _is_numeric(value: object) -> bool:
    text, _ = _scalar_text(value)
    if not text:
        return False
    if _denormal(text):
        return True
    return _rational(text)[1]


def _severity(a: object, b: object) -> str:
    """Mirrors the arbiter's `_severity`."""
    if _is_numeric(a) and _is_numeric(b):
        return Reason.LOSSY_NUMERIC
    if a is None and b is not None:
        return Reason.NULL_TO_DEFAULT  # the arbiter's `null_filled`
    if a is not None and b is None:
        return Reason.NULL_LOSS
    a_text, a_is_str = _scalar_text(a)
    b_text, b_is_str = _scalar_text(b)
    if a_is_str and b_is_str:
        if _dateish(a_text) and _dateish(b_text):
            if a_text[:10] != b_text[:10]:
                return Reason.DATE_SHIFT
            return Reason.REFORMAT
        if a_text and not b_text:
            return Reason.EMPTIED
        return Reason.REFORMAT
    if b_is_str and _denormal(b_text):
        return Reason.LOSSY_NUMERIC
    return Reason.REFORMAT


def _same_kind_deep(a: object, b: object) -> bool:
    """Compare JSON shape recursively.

    Two values can be equal by `_same_value` and still differ in type somewhere
    inside. Two strings that are "equal" only after denormal folding are also a
    visible change: the row said "NaN" and the table holds "nan".
    """
    if _json_kind(a) != _json_kind(b):
        return False
    if isinstance(a, str):
        return a == b
    if isinstance(a, dict):
        assert isinstance(b, dict)
        if len(a) != len(b):
            return False
        return all(k in b and _same_kind_deep(v, b[k]) for k, v in a.items())
    if isinstance(a, list):
        assert isinstance(b, list)
        if len(a) != len(b):
            return False
        return all(_same_kind_deep(x, y) for x, y in zip(a, b, strict=True))
    return True


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, RawNumber):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "?"


def _dateish(text: str) -> bool:
    if len(text) < 10:
        return False
    for i in range(10):
        c = text[i]
        if i in (4, 7):
            if c != "-":
                return False
        elif not ("0" <= c <= "9"):
            return False
    return True


# ---------------------------------------------------------- reference type


def _reason_for(base: str, stored: str, ref: str) -> str:
    if base.startswith("Enum"):
        return Reason.ENUM_COERCE
    if base.startswith(("Array", "Tuple", "Map")):
        return Reason.ELEMENT_CHANGED
    if base.startswith("Decimal"):
        if _trim_num(ref).startswith(_trim_num(stored)):
            return Reason.DECIMAL_TRUNCATE
        return Reason.OVERFLOW_WRAP
    if base.startswith("DateTime"):
        return Reason.DATETIME_WRAP
    if base.startswith("Date"):
        return Reason.DATE_CLAMP
    # Time / Time64 (25.8+): the same truncation mechanism as DateTime64 —
    # sub-second ticks at the declared scale, whole seconds in Time.
    if base.startswith("Time"):
        return Reason.DATETIME_WRAP
    if base == "UUID":
        return Reason.UUID_MANGLE
    if base in ("IPv4", "IPv6"):
        return Reason.IP_MANGLE
    if base in ("Float32", "Float64", "BFloat16"):
        return Reason.FLOAT_PRECISION
    if base.startswith("FixedString"):
        return Reason.FIXEDSTRING_PAD
    if _is_int_family(base):
        return Reason.OVERFLOW_WRAP
    return Reason.VALUE_CHANGED


def _is_int_family(base: str) -> bool:
    return base.startswith(("UInt", "Int")) or base == "Bool"


def _equivalent(base: str, stored: str, ref: str) -> bool:
    """Whether two ClickHouse renderings mean the same value.

    Only formatting differences are collapsed — anything else is a real change.
    """
    if stored == ref:
        return True
    # Bool renders as true/false; its Int256 reference renders as 1/0.
    if base == "Bool":
        return _bool_norm(stored) == _bool_norm(ref)
    # DateTime64 references widen the scale: ".123" vs ".123000000" — and so
    # do Time64's (Time/Time64 arrived in 25.8; same rendering shape).
    if base.startswith(("DateTime", "Time")):
        return _trim_frac(stored) == _trim_frac(ref)
    # Decimal references widen the scale: "2.50" vs "2.5000000".
    if (
        base.startswith("Decimal")
        or _is_int_family(base)
        or base in ("Float32", "Float64", "BFloat16")
    ):
        return _num_equal(stored, ref)
    if base.startswith(("Array", "Tuple", "Map")):
        return _compact_json(stored) == _compact_json(ref) or _num_list_equal(stored, ref)
    return False


def _bool_norm(text: str) -> str:
    stripped = text.strip('"')
    if stripped in ("true", "1"):
        return "1"
    if stripped in ("false", "0"):
        return "0"
    return text


def _trim_frac(text: str) -> str:
    """Drop trailing zeros (and a bare dot) from a datetime's fractional part, so
    DateTime64(3) and its DateTime64(9) reference compare equal."""
    dot = text.rfind(".")
    if dot < 0:
        return text
    end = len(text)
    quoted = end > 0 and text[end - 1] == '"'
    if quoted:
        end -= 1
    frac = text[dot + 1 : end].rstrip("0")
    out = text[:dot]
    if frac:
        out += "." + frac
    if quoted:
        out += '"'
    return out


def _trim_num(text: str) -> str:
    return text.strip('"')


def _num_equal(a: str, b: str) -> bool:
    """Compare two numeric renderings exactly, so Decimal(76,24)'s extra digits
    and 64-bit-integer quoting never matter."""
    a, b = _trim_num(a), _trim_num(b)
    if a == b:
        return True
    if _denormal(a) or _denormal(b):
        return _denormal(a) == _denormal(b)
    ra, ok_a = _rational(a)
    rb, ok_b = _rational(b)
    return ok_a and ok_b and ra == rb


def _num_list_equal(a: str, b: str) -> bool:
    """Compare two rendered containers element-wise as numbers, so [1,2] and
    [1.000,2.000] agree while [1,0,3] and [1,256,3] do not."""
    ta, tb = _tokenize_nums(a), _tokenize_nums(b)
    if len(ta) != len(tb) or not ta:
        return False
    if any(not _num_equal(x, y) for x, y in zip(ta, tb, strict=True)):
        return False
    return _skeleton(a) == _skeleton(b)


_NUM_BODY: Final = frozenset("0123456789.eE-+")


def _tokenize_nums(text: str) -> list[str]:
    """Pull the numeric literals out of a rendered container."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':  # skip strings wholesale
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c in "-+" or c.isdigit():
            j = i + 1
            while j < n and text[j] in _NUM_BODY:
                j += 1
            out.append(text[i:j])
            i = j
            continue
        i += 1
    return out


def _skeleton(text: str) -> str:
    """Keep only structural punctuation, so two renderings that agree
    numerically but differ in shape are still reported as different."""
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
                out.append('"')
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append('"')
        elif c in "[]{}(),:":
            out.append(c)
        i += 1
    return "".join(out)


def _compact_json(text: str) -> str:
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                i += 1
                out.append(text[i])
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
        elif c not in " \t\n\r":
            out.append(c)
        i += 1
    return "".join(out)
