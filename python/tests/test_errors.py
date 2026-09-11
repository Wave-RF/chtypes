"""The error model's shape: peer types, the frozen rendering, the funnel.

No fixtures — nothing here needs an artifact. The peer-type split
(docs/reference/bindings.md rule 12; completed for Python 2026-08-26) is a contract
about the TYPES, and the types can be asserted offline.
"""

from __future__ import annotations

import chtypes
from chtypes.errors import _error_for
from chtypes.results import Outcome


def test_unsupported_error_is_a_peer_not_a_subtype() -> None:
    # The whole point of the split: `except SchemaError` must never catch a
    # decline, or every handler that forgot the distinction converts declines
    # into rejections silently — a manufactured over-reject, budgeted at zero.
    assert not issubclass(chtypes.UnsupportedError, chtypes.SchemaError)
    assert not issubclass(chtypes.SchemaError, chtypes.UnsupportedError)
    # Both still hang off the shared base: catching ChtypesError is an
    # explicit choice to handle both arms.
    assert issubclass(chtypes.UnsupportedError, chtypes.ChtypesError)
    assert issubclass(chtypes.SchemaError, chtypes.ChtypesError)


def test_no_error_value_carries_the_sentinel() -> None:
    decline = chtypes.UnsupportedError("engine not modelled")
    # No `.code` field at all — there is no ClickHouse code to carry — and no
    # `.unsupported` predicate on the refusal type: the TYPE is the answer.
    assert not hasattr(decline, "code")
    assert not hasattr(chtypes.SchemaError(115, "unknown setting"), "unsupported")
    # The wire sentinel stays exported, because row-level results carry it.
    assert chtypes.CODE_UNSUPPORTED == -2


def test_the_rendered_sentinel_shape_is_frozen() -> None:
    # The decline renders the header's -2 in the same shape the refusal
    # renders its code — the conformance drivers put this exact string on the
    # protocol wire as an `unsupported` scope (docs/reference/bindings.md rule 12).
    assert str(chtypes.UnsupportedError("nope")) == "chtypes: [-2] nope"
    assert str(chtypes.SchemaError(115, "bad name")) == "chtypes: [115] bad name"
    # Column-attributed shapes (used only by callers that KNOW a column —
    # never guessed by the binding).
    assert str(chtypes.UnsupportedError("nope", "e")) == "chtypes: column 'e': [-2] nope"


def test_the_funnel_keys_on_the_sign() -> None:
    # A positive code is the server's own refusal, verbatim; ANY negative code
    # is a decline — -2 "I will not guess", -1 a guarded exception, and any
    # sentinel a later era adds. A negative SchemaError must be unmakeable
    # through the funnel.
    assert isinstance(_error_for(115, "bad name"), chtypes.SchemaError)
    assert isinstance(_error_for(50, "unknown family"), chtypes.SchemaError)
    assert isinstance(_error_for(-2, "declined"), chtypes.UnsupportedError)
    assert isinstance(_error_for(-1, "guarded exception"), chtypes.UnsupportedError)
    assert isinstance(_error_for(-3, "future sentinel"), chtypes.UnsupportedError)


def test_unknown_outcome_strings_degrade_to_unsupported() -> None:
    # A future artifact's new verdict is an answer this binding cannot
    # interpret; UNSUPPORTED is never scored as agreement, while a default of
    # REJECTED would manufacture an over-reject out of vocabulary drift
    # (docs/reference/bindings.md §RowResult).
    assert Outcome.of("accepted") is Outcome.ACCEPTED
    assert Outcome.of("rejected") is Outcome.REJECTED
    assert Outcome.of("accepted_poisoned") is Outcome.ACCEPTED_POISONED
    assert Outcome.of("unsupported") is Outcome.UNSUPPORTED
    assert Outcome.of("some_future_verdict") is Outcome.UNSUPPORTED
    assert Outcome.of("") is Outcome.UNSUPPORTED
