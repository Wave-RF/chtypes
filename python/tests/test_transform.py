"""The transformation detector, driven directly on `cols[]` entries.

`classify` is the one part of the product that is *derived* rather than executed
by ClickHouse, so it is asserted on its own inputs and not only through a
library: a wrong reason string here is a silent downgrade, which is the failure
mode this repository exists to kill.

Every document shape below was measured from the `darwin-arm64/25.8` artifact and
cross-checked against `chtypes-core/lib/build/chtypes-oracle`.
"""

from __future__ import annotations

from chtypes._document import ColDoc
from chtypes.results import Reason
from chtypes.transform import classify

# Longer than CPython's `int_max_str_digits` guard (4,300 digits), which is the
# whole point: the corpus really contains this row.
NINES = "9" * 5000


def _col(**overrides: object) -> ColDoc:
    fields: dict[str, object] = {
        "name": "x",
        "type": "UInt8",
        "base": "UInt8",
        "src": "input",
        "input": "",
        "ref_type": "",
        "stored_raw": None,
        "ref_raw": None,
        "nullable": False,
        "poison": False,
        "null_input": False,
        "dup_dropped": False,
    }
    fields.update(overrides)
    return ColDoc(**fields)  # type: ignore[arg-type]


def test_a_five_thousand_digit_value_is_lossy_not_a_reformat() -> None:
    """`x IntervalQuarter` fed 5,000 nines stores -1, and that is a LOSS.

    The measured document (arbiter case `fz-f270a0507ce1`, all six versions) and
    the reference oracle both call this `lossy_numeric`. This binding called it
    `reformat`, because `Fraction("9" * 5000)` raises under CPython's
    integer-string guard and `_rational` read the exception as "not a number".
    `reformat` is in `LOSSLESS_REASONS`, so the finding did not merely get a
    wrong label — `Transform.lossy` was False and it disappeared from
    `lossy_transforms` altogether (42 of 34,619 differential cases,
    chtypes-core/tests/conformance/python/README.md class D).
    """
    (transform,) = classify(
        _col(
            type="IntervalQuarter",
            base="IntervalQuarter",
            input=f'"{NINES}"',
            stored_raw="-1",
            ref_raw="null",
        )
    )
    assert transform.reason == Reason.LOSSY_NUMERIC
    assert transform.lossy
    assert (transform.column, transform.stored) == ("x", "-1")


def test_one_value_spelled_two_ways_is_not_reported_at_any_length() -> None:
    """The same defect in the other direction: an invented finding.

    With `_rational` failing on the long side, `10**4999` and `1e4999` compared
    unequal and the detector reported a `reformat` that nothing justified. Exact
    comparison is a contract (spec/bindings.md: "Numeric comparison MUST be
    exact"), and it does not get to stop being exact past 4,300 digits.
    """
    assert classify(_col(base="Decimal(76, 0)", input="1e4999", stored_raw="1" + "0" * 4999)) == []


def test_a_long_digit_string_that_really_changed_is_still_lossy() -> None:
    assert [
        t.reason for t in classify(_col(base="Int256", input=NINES, stored_raw="9" * 4999))
    ] == [Reason.LOSSY_NUMERIC]
