"""Issue #122: five more results-group rules the four bindings agree on by
construction, declared nowhere in `tests/parity/manifest.json` until this
change.

Every document below is parsed through the same production path
`parse_row_document`/`parse_filter_document` already use, rather than
constructing a result dataclass by hand.
"""

from __future__ import annotations

import pytest

from chtypes._document import parse_filter_document, parse_row_document
from chtypes.results import FilterOutcome, Outcome, Source, Verdict

# --------------------------------------------------------------------------
# rule 1: results.unsupported-settings-forces-unsupported


@pytest.mark.parametrize(
    ("wire_outcome", "want"),
    [
        ("accepted", Outcome.UNSUPPORTED),
        ("rejected", Outcome.REJECTED),
    ],
)
def test_unsupported_settings_forces_unsupported_unless_rejected(
    wire_outcome: str, want: Outcome
) -> None:
    doc = (
        '{"outcome": "' + wire_outcome + '", "unsupported_settings": ["some_setting"], "cols": []}'
    ).encode()
    result = parse_row_document(doc)
    assert result.outcome is want, (
        f"outcome is {result.outcome!r}, want {want!r} "
        f"(unsupported_settings {result.unsupported_settings!r}, wire outcome {wire_outcome!r})"
    )


# --------------------------------------------------------------------------
# rule 2: results.unknown-outcome-degrades-to-unsupported /
# results.unknown-verdict-degrades-to-decline


def test_unknown_outcome_degrades_to_unsupported() -> None:
    row_doc = b'{"outcome": "totally-unknown-future-outcome", "cols": []}'
    row = parse_row_document(row_doc)
    assert row.outcome is Outcome.UNSUPPORTED, (
        f"RowResult.outcome is {row.outcome!r}, want UNSUPPORTED (never REJECTED) "
        "for an unrecognized outcome string"
    )

    filter_doc = b'{"outcome": "totally-unknown-future-outcome", "verdicts": "", "errors": []}'
    filt = parse_filter_document(filter_doc)
    assert filt.outcome is FilterOutcome.UNSUPPORTED, (
        f"FilterResult.outcome is {filt.outcome!r}, want UNSUPPORTED (never REJECTED) "
        "for an unrecognized outcome string"
    )


def test_unknown_verdict_degrades_to_decline() -> None:
    row_doc = b'{"outcome": "accepted", "cols": [], "verdict": "z"}'
    row = parse_row_document(row_doc)
    assert row.verdict is Verdict.DECLINE, (
        f"RowResult.verdict is {row.verdict!r}, want DECLINE for an unrecognized character 'z'"
    )

    filter_doc = b'{"outcome": "ok", "verdicts": "z", "errors": []}'
    filt = parse_filter_document(filter_doc)
    assert filt.verdicts == (Verdict.DECLINE,), (
        f"FilterResult.verdicts is {filt.verdicts!r}, want (DECLINE,) for an unrecognized "
        "character 'z'"
    )


# --------------------------------------------------------------------------
# rule 3: results.null-false-when-poisoned


@pytest.mark.parametrize(
    ("poison", "want"),
    [
        ("false", True),
        ("true", False),
    ],
)
def test_value_null_false_when_poisoned(poison: str, want: bool) -> None:
    doc = (
        '{"outcome": "accepted", "cols": [{"name": "c", "type": "UInt8", "base": "UInt8", '
        '"src": "input", "input": "", "stored": null, "poison": ' + poison + ', '
        '"nullable": true}]}'
    ).encode()
    result = parse_row_document(doc)
    assert len(result.values) == 1, f"values has {len(result.values)} entries, want 1"
    assert result.values[0].null is want, (
        f"values[0].null is {result.values[0].null!r}, want {want!r} (poison={poison})"
    )


# --------------------------------------------------------------------------
# rule 4: results.default-substituted-populates-substituted


def test_default_substituted_populates_substituted() -> None:
    assert Source.DEFAULT_SUBSTITUTED == "default_substituted"
    doc = b"""{
        "outcome": "accepted",
        "cols": [
            {
                "name": "ts", "type": "DateTime", "base": "DateTime",
                "src": "default_substituted", "input": "now()",
                "stored": "2026-09-21 00:00:00", "nullable": false
            },
            {
                "name": "in_col", "type": "UInt8", "base": "UInt8",
                "src": "input", "input": "5", "stored": 5, "nullable": false
            }
        ]
    }"""
    result = parse_row_document(doc)
    assert len(result.substituted) == 1, (
        f"substituted has {len(result.substituted)} entries, want exactly 1 "
        f"(only the default_substituted column): {result.substituted!r}"
    )
    sub = result.substituted[0]
    assert sub.column == "ts"
    assert sub.expr == "now()"
    assert sub.text == '"2026-09-21 00:00:00"'


# --------------------------------------------------------------------------
# rule 5: results.non-ok-filter-result-forces-empty


def test_non_ok_filter_result_forces_empty_verdicts_and_errors() -> None:
    doc = (
        b'{"outcome": "rejected", "code": 115, "err": "unknown setting", '
        b'"verdicts": "tfed", "errors": [{"row": 0, "code": 27, "err": "boom"}]}'
    )
    result = parse_filter_document(doc)
    assert result.outcome is FilterOutcome.REJECTED
    assert result.verdicts == (), (
        f"verdicts is {result.verdicts!r}, want empty: a non-OK FilterResult must not leak "
        "partial answers"
    )
    assert result.errors == (), (
        f"errors is {result.errors!r}, want empty: a non-OK FilterResult must not leak "
        "partial answers"
    )
