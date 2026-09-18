"""The transformation detector, driven directly on `cols[]` entries.

`classify` is the one part of the product that is *derived* rather than executed
by ClickHouse, so it is asserted on its own inputs and not only through a
library: a wrong reason string here is a silent downgrade, which is the failure
mode this repository exists to kill.

Every document shape below was measured from the `darwin-arm64/25.8` artifact and
cross-checked against the reference oracle.
"""

from __future__ import annotations

from chtypes._document import ColDoc, _col_doc, parse_row_document
from chtypes._rawjson import decode_document
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
    the conformance suite, class D).
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
    comparison is a contract (docs/reference/bindings.md: "Numeric comparison MUST be
    exact"), and it does not get to stop being exact past 4,300 digits.
    """
    assert classify(_col(base="Decimal(76, 0)", input="1e4999", stored_raw="1" + "0" * 4999)) == []


def test_a_long_digit_string_that_really_changed_is_still_lossy() -> None:
    assert [
        t.reason for t in classify(_col(base="Int256", input=NINES, stored_raw="9" * 4999))
    ] == [Reason.LOSSY_NUMERIC]


# ------------------------------------------------------------------- #97 ---
#
# A listed EPHEMERAL column's value is read (it is in scope for other
# columns' DEFAULT expressions) but never stored. `classify`'s early-return
# list carried "skipped", "default_expr_unsupported",
# "default_volatile_unresolved" and "default_pending" but not
# `Source.EPHEMERAL_INPUT`.
#
# Both documents below are parsed with this binding's own parser
# (`decode_document` + `_col_doc` / `parse_row_document`), not hand-built
# `ColDoc(...)` calls like `_col()` above, per the issue: "Build them from a
# result document parsed by the binding's own parser wherever that is
# possible, rather than hand-constructing the result object."
#
# eph_col and mat_col share the identical UInt8-overflow shape (input 256,
# reference-widened 256, `ref_type` Int256) so that the reference-type
# detector (not gated on `src`) would flag both if `classify` did not stop
# the ephemeral one first. eph_col has no "stored" key at all — an EPHEMERAL
# column is never written — while mat_col's "stored": 0 is the value that
# genuinely landed under insert_allow_materialized_columns=1.
_EPHEMERAL_AND_MATERIALIZED_DOC = b"""{
    "outcome": "accepted",
    "cols": [
        {
            "name": "eph_col", "type": "UInt8", "base": "UInt8",
            "src": "ephemeral_input", "input": "256",
            "ref_type": "Int256", "ref": 256, "nullable": false
        },
        {
            "name": "mat_col", "type": "UInt8", "base": "UInt8",
            "src": "materialized_input", "input": "256", "stored": 0,
            "ref_type": "Int256", "ref": 256, "nullable": false
        }
    ]
}"""


def _parsed_cols() -> dict[str, ColDoc]:
    """`cols[]`, parsed by `_col_doc` off the same decoded document
    `parse_row_document` would use — not hand-built."""
    doc = decode_document(_EPHEMERAL_AND_MATERIALIZED_DOC)
    assert isinstance(doc, dict)
    return {(col := _col_doc(entry)).name: col for entry in doc["cols"]}


def test_values_excludes_ephemeral_input_keeps_materialized_input() -> None:
    """The `RowResult.values` half of #97.

    `_row_result`'s own column loop already special-cases
    `Source.EPHEMERAL_INPUT` the same way it special-cases "skipped" (and
    that loop `continue`s before ever reaching its `classify(col)` call, so
    it cannot observe the `classify` bug below). This was already correct
    before #97's fix; it simply had no test. Expect PASS both before and
    after the `classify` fix — this is not what that fix changes.
    """
    result = parse_row_document(_EPHEMERAL_AND_MATERIALIZED_DOC)
    columns = {v.column for v in result.values}
    assert "eph_col" not in columns, (
        "values contains eph_col (src ephemeral_input): a column that is "
        "never stored must not appear in the stored-row view"
    )
    assert "mat_col" in columns, (
        "values is missing mat_col (src materialized_input): it IS stored "
        "under insert_allow_materialized_columns=1 and must stay in values"
    )


def test_classify_excludes_ephemeral_input_still_classifies_materialized_input() -> None:
    """The `classify` half of #97 — `classify` ITSELF, called directly.

    `_row_result`'s loop already `continue`s past `Source.EPHEMERAL_INPUT`
    before it ever calls `classify`, so driving this through
    `parse_row_document` (as the test above does) would pass before the fix
    for the wrong reason: it would never run the code under test. `classify`
    must be correct standing alone — it is the piece #53 deliberately left
    as "a behavior judgement" per the issue, and nothing stops a future
    caller (or a refactor of that loop) from invoking it on an
    ephemeral_input column without the same guard.

    Before the fix, eph_col's early-return list does not include
    "ephemeral_input", so `classify` falls through to the reference-type
    detector, sees stored ("") disagree with the reference-widened value
    ("256"), and reports a phantom overflow_wrap transform for a column that
    was never stored. This assertion FAILS before the fix, PASSES after.

    mat_col carries the identical overflow shape but src
    "materialized_input", which must keep classifying either way — the guard
    against folding the two early-return lists together (the issue's
    explicit warning).
    """
    cols = _parsed_cols()

    eph_transforms = classify(cols["eph_col"])
    assert eph_transforms == [], (
        f"classify() reported a transform for eph_col (src ephemeral_input), "
        f"want none — a column that is never stored has no stored value to "
        f"have silently changed: {eph_transforms!r}"
    )

    (mat_transform,) = classify(cols["mat_col"])
    assert mat_transform.reason == Reason.OVERFLOW_WRAP
    assert mat_transform.column == "mat_col"
