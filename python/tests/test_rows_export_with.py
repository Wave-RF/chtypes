"""Unit-level coverage for issue #54 (`Schema.rows(..., row_filter=)`) that
needs no loaded artifact: the pure document-decoding path
(`_document.parse_batch_document` against a hand-built chs_rows-with-
attached-filter document, shaped exactly as the C ABI contract §Rows
describes it) and the Python-level cross-library refusal `Schema.rows`
performs before any C call.

What this file deliberately does NOT cover: a real `Filter` compiled and
evaluated through `chs_rows` against a live artifact. No revision-5 artifact
exists yet (issue #54's own blocker), so that path is wired
(`Schema.rows`'s `row_filter=` branch and `NativeLibrary.rows`'s
`filter_handle` argument) but not run here.
"""

from __future__ import annotations

import pytest

from chtypes._document import parse_batch_document
from chtypes.errors import ChtypesError
from chtypes.registry import Filter, Schema
from chtypes.results import Format, Outcome, Verdict


class _FakeNative:
    """Stands in for `NativeLibrary` in tests that must never dlopen: the
    only call `Schema.__init__` makes is `schema_columns`, for the column
    introspection list."""

    def schema_columns(self, handle: int) -> list[tuple[str, str, str, str, bool]]:
        return []


class _FakeLibrary:
    """Stands in for `registry.Library`: just enough surface (`.version`,
    `._native`) for `Schema.__init__` and the cross-library error message,
    with no real dlopen behind it."""

    def __init__(self, version: str) -> None:
        self.version = version
        self._native = _FakeNative()


def test_verdict_of_maps_the_four_characters() -> None:
    """The single source of truth `Verdict.of` implements. Fail-closed lives
    here: 'e' and 'd' — and anything unrecognized — must never map to
    `Verdict.FALSE`."""
    assert Verdict.of("t") is Verdict.TRUE
    assert Verdict.of("f") is Verdict.FALSE
    assert Verdict.of("e") is Verdict.ERROR
    assert Verdict.of("d") is Verdict.DECLINE
    assert Verdict.of("?") is Verdict.DECLINE
    for char in ("e", "d", "?"):
        assert Verdict.of(char) is not Verdict.FALSE, (
            f"Verdict.of({char!r}) collapsed a non-answer into FALSE — fail-open"
        )


def test_parse_batch_document_decodes_filter_verdict() -> None:
    """A hand-built document shaped as chs_rows answers WITH an attached
    filter: four rows exercising all four verdict characters, one of them
    ('d') on a row whose own parse outcome is not accepted, plus
    rows_passed/rows_cut at the batch level. This is the acceptance-bar test
    for property (3) — bytes only for 't', e/d never collapsed into f — at
    the decoding layer."""
    raw = b"""
    {
        "outcome":"accepted","code":0,"err":"","rows_read":4,"rows_skipped":0,
        "rows_passed":1,"rows_cut":3,
        "rows":[
            {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"t"},
            {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"f"},
            {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"e",
             "verdict_code":386,"verdict_err":"no common type"},
            {"outcome":"skipped","code":117,"err":"bad row","cols":[],"verdict":"d",
             "verdict_code":117,"verdict_err":"bad row"}
        ],
        "row_spans":[{"off":0,"len":5},{"off":0,"len":0},{"off":0,"len":0},{"off":0,"len":0}]
    }
    """
    res = parse_batch_document(raw)
    assert res.rows_passed == 1
    assert res.rows_cut == 3
    assert res.rows_passed + res.rows_cut == 4

    want = [Verdict.TRUE, Verdict.FALSE, Verdict.ERROR, Verdict.DECLINE]
    assert [r.verdict for r in res.rows] == want

    # Property (3), directly: neither non-answer decoded as FALSE.
    assert res.rows[2].verdict is not Verdict.FALSE, "'e' row decoded as FALSE — fail-open"
    assert res.rows[3].verdict is not Verdict.FALSE, "'d' row decoded as FALSE — fail-open"

    # Row 3's own outcome is not accepted, and it still carries verdict_code
    # / verdict_err beside verdict, per the contract.
    assert res.rows[3].outcome is Outcome.SKIPPED
    assert res.rows[3].verdict_code == 117
    assert res.rows[3].verdict_err == "bad row"
    assert res.rows[2].verdict_code == 386
    assert res.rows[2].verdict_err == "no common type"


def test_parse_batch_document_no_filter_leaves_verdict_none() -> None:
    """The ordinary Row/Rows/RowsExport document — no "verdict" key at all —
    leaves `verdict` `None` rather than decoding an absent field into
    something that could be mistaken for a real decline."""
    raw = b"""
    {"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_skipped":0,
     "rows":[{"outcome":"accepted","code":0,"err":"","cols":[]}]}
    """
    res = parse_batch_document(raw)
    assert res.rows_passed == 0
    assert res.rows_cut == 0
    assert res.rows[0].verdict is None


def test_rows_cross_library_filter_refused() -> None:
    """Property (8)'s Go-level twin: a filter from a DIFFERENT loaded
    library is refused before any C call — no handle crosses a dlopen'd
    image boundary, the same rule `Filter.eval` enforces for a (filter,
    block) pair. Needs no dlopen'd artifact: the refusal fires on the
    `Library` identity check before `self._mu` or any handle is touched.

    The SAME-library-different-schema half of (8) (code 1002) is the
    server's own answer and cannot be exercised without a loaded artifact;
    it is wired (the cross-schema lock path in `Schema.rows`) but not run
    here.
    """
    s = Schema(_FakeLibrary("24.8.1.1-a"), 1, "x UInt8")
    fs = Schema(_FakeLibrary("24.8.1.1-b"), 2, "x UInt8")
    f = Filter(fs, 99, "1")

    with pytest.raises(ChtypesError):
        s.rows(Format.JSON_EACH_ROW, b"{}", row_filter=f)
