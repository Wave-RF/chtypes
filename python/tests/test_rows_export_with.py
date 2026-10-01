"""Unit-level coverage for issue #54 (`Schema.rows(..., row_filter=)`): the
pure document-decoding path (`_document.parse_batch_document` against a
hand-built chs_rows-with-attached-filter document, shaped exactly as the C
ABI contract §Rows describes it) and the Python-level cross-library refusal
`Schema.rows` performs before any C call.

The chtypes#298 regression tests at the end of this file are the exception:
each needs a revision-5 artifact (a real `Filter` compiled and evaluated
through `chs_rows`) and skips loudly without one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import chtypes
from chtypes._document import parse_batch_document
from chtypes.errors import ChtypesError
from chtypes.registry import Filter, Schema
from chtypes.results import Format, Outcome, Verdict

# Measured on darwin-arm64 26.8.15.10-lts, same as go/chtypes/csv_reader_test.go's
# csvRejectionCode: ClickHouse's own INCORRECT_DATA for the CSV reader's
# trailing-garbage refusal.
_CSV_REJECTION_CODE = 117


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


# ----------------------------------------------------- chtypes#298 (G1-G5)
#
# The artifact producer's relink served at chtypes_build 1790845279 changed
# four document fields by design, measured against that build and against
# the immediately preceding build (1790783214) of the SAME ClickHouse patch
# on darwin-arm64 — same ABI revision, only the library rebuilt:
#
#   - G2: verdict_code/verdict_err are now populated on a non-answered
#     verdict whose row is not itself accepted (measured: 0/"" before, the
#     row's own code/message after).
#   - G3: rows_passed/rows_cut are now 0 for a batch whose own outcome is
#     not ACCEPTED, even when an individual row before the one that aborted
#     it was itself accepted with a TRUE verdict (measured: 1/0 before, 0/0
#     after).
#   - G5: export_declined is now populated for every non-accepted batch
#     that requested an export, including a batch with ZERO rows (measured
#     on a CSV_WITH_NAMES body naming an unknown header column under
#     input_format_skip_unknown_fields=0: "" before, a reason after).
#
# G4 (a skipped row counts in neither rows_passed nor rows_cut) has no
# library change — it is a docs-only clarification — so it is not measured
# here; see docs/guides/filters.md.
#
# The fix reached only the SUPPORTED lines' artifacts (docs/support.md):
# 26.3, 26.7, 26.8 and 26.9, at chtypes_build 1790845279 or later. A served,
# unsupported (retired) line gets no new build or ABI revision, ever, so its
# existing artifact keeps the pre-relink behavior permanently — measured:
# 26.6's newest served build, 1790767905, predates this relink and was never
# republished. _rev5_libraries also opens such a line (anything ABI revision
# 5+), so the three tests below gate on the artifact's OWN chtypes_build
# (_relinked_libraries), rather than a hand-typed line list — a hand-typed
# list goes stale the moment a new supported line ships, and silently stops
# exercising it.


@pytest.fixture(scope="session")
def _rev5_libraries(registry: chtypes.Registry) -> list[chtypes.Library]:
    """Every line of the test registry that loads at ABI revision 5 — the
    same selection as test_csv_reader.py's rev5_libraries fixture, defined
    again here because pytest fixtures do not cross test-file boundaries."""
    libraries: list[chtypes.Library] = []
    for version in registry.versions():
        try:
            library = registry.for_version(version)
        except chtypes.ChtypesError as exc:
            print(f"line {version} not exercised: {exc}")
            continue
        if library.abi_revision < 5:
            print(f"line {version} not exercised: ABI revision {library.abi_revision}, needs 5")
            continue
        libraries.append(library)
    if not libraries:
        pytest.skip(
            f"registry {registry.directory} holds no ABI revision-5 artifact: every case in "
            f"this block needs one — fetch one with scripts/fetch.sh (docs/guides/fetch.md)"
        )
    return libraries


# _RELINK_BUILD_298 is the chtypes_build the chtypes#298 relink was served
# at. It is the one constant _relinked_libraries is built from: the relink
# is a property of the BUILD, not of which lines happened to be supported
# the day this file was written.
_RELINK_BUILD_298 = 1790845279


def _is_relinked_build(build: int) -> bool:
    """`_relinked_libraries`' predicate, factored out so it can be pinned
    against fabricated build numbers without a loaded artifact (see
    test_is_relinked_build_threshold). A missing or zero chtypes_build — 0 is
    what a manifest that predates the field reads as — must never be treated
    as relinked."""
    return build >= _RELINK_BUILD_298


def _chtypes_build_of(library: chtypes.Library) -> int:
    """Read library's own manifest.json for its chtypes_build field, the
    same file and the same place scripts/lib/provenance.py reads it from:
    next to the loaded library. There is no public accessor for this field —
    chtypes.Manifest does not carry it — so this reads the manifest directly
    rather than guessing. A manifest that cannot be read or parsed fails the
    fixture loudly: this gate must never default a line it could not
    actually measure to "relinked"."""
    manifest_path = Path(library.path).parent / "manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    return int(doc.get("chtypes_build") or 0)


@pytest.fixture(scope="session")
def _relinked_libraries(_rev5_libraries: list[chtypes.Library]) -> list[chtypes.Library]:
    """`_rev5_libraries` narrowed to the lines whose own artifact's
    chtypes_build is at least _RELINK_BUILD_298 — the chtypes#298 relink. A
    loaded line below that build — a served, unsupported (retired) line that
    will never receive it (docs/support.md), or simply a supported line
    fetched before the relink shipped — is logged and passed over, the same
    verdict every artifact-backed test here gives for a line it cannot
    exercise."""
    libraries = []
    for library in _rev5_libraries:
        build = _chtypes_build_of(library)
        if not _is_relinked_build(build):
            print(
                f"skipping {library.minor} at build {build}: predates the relink "
                f"{_RELINK_BUILD_298}"
            )
            continue
        libraries.append(library)
    if not libraries:
        pytest.skip(
            f"registry holds no artifact at chtypes_build {_RELINK_BUILD_298} or later (the "
            "chtypes#298 relink) — fetch one with scripts/fetch.sh (docs/guides/fetch.md)"
        )
    return libraries


def test_is_relinked_build_threshold() -> None:
    """Pins `_is_relinked_build`'s own threshold against fabricated build
    numbers, independent of any loaded artifact: the relink's own build is
    kept, one build short of it is not, and a manifest predating
    chtypes_build (0) is not — it must never read as relinked by default."""
    assert _is_relinked_build(_RELINK_BUILD_298) is True
    assert _is_relinked_build(_RELINK_BUILD_298 - 1) is False
    assert _is_relinked_build(0) is False


def test_non_accepted_batch_zeroes_counts_and_carries_verdict_code(
    _relinked_libraries: list[chtypes.Library],
) -> None:
    """G2 and G3 together: the same strict (no input_format_allow_errors_*)
    body exercises both at once. Row 0 parses and is individually ACCEPTED
    with verdict TRUE; row 1 is the CSV reader's own trailing-garbage
    refusal (code 117), which aborts the batch — so the batch's own outcome
    is REJECTED even though row 0, in isolation, was accepted and passed
    the filter."""
    ran = 0
    for library in _relinked_libraries:
        with library.compile_ddl("id UInt8, n UInt8") as schema:
            with schema.compile_filter("id >= 0") as f:
                body = b"1,2\n1,abc\n"  # row 1: "abc" in a UInt8 column
                batch = schema.rows(Format.CSV, body, row_filter=f)
                assert batch.outcome is Outcome.REJECTED, (
                    f"{library.version}: outcome {batch.outcome} "
                    f"(code {batch.err_code}, {batch.err_msg!r}), want REJECTED — the aborting "
                    f"row must still reject the whole batch"
                )
                # G3: the batch's own outcome is not ACCEPTED, so BOTH
                # counts must be zero, regardless of row 0's own
                # accepted-and-TRUE verdict.
                assert (batch.rows_passed, batch.rows_cut) == (0, 0), (
                    f"{library.version}: rows_passed/rows_cut = "
                    f"{batch.rows_passed}/{batch.rows_cut}, want 0/0 for a batch whose own "
                    f"outcome ({batch.outcome}) is not ACCEPTED"
                )
                assert len(batch.rows) == 2, (
                    f"{library.version}: got {len(batch.rows)} row document(s), want 2"
                )
                # G2: row 1's own parse outcome is not ACCEPTED (REJECTED,
                # code 117), so its verdict is DECLINE and must carry that
                # same code/message beside it — never the 0/"" the
                # pre-relink library left there.
                row1 = batch.rows[1]
                assert row1.verdict is Verdict.DECLINE, (
                    f"{library.version}: row 1 verdict {row1.verdict}, want DECLINE — its own "
                    f"outcome is not ACCEPTED"
                )
                assert row1.verdict_code == _CSV_REJECTION_CODE and row1.verdict_err, (
                    f"{library.version}: row 1 verdict_code/verdict_err = "
                    f"{row1.verdict_code}/{row1.verdict_err!r}, want "
                    f"{_CSV_REJECTION_CODE}/<non-empty> — a non-accepted row's own error, "
                    f"beside its decline verdict"
                )
                assert row1.verdict_code == row1.err_code, (
                    f"{library.version}: verdict_code {row1.verdict_code} != its own err_code "
                    f"{row1.err_code} — the decline must carry the SAME error the row's own "
                    f"outcome already reports"
                )
                ran += 1

    assert ran == len(_relinked_libraries), (
        f"test_non_accepted_batch_zeroes_counts_and_carries_verdict_code ran {ran} line(s), "
        f"want {len(_relinked_libraries)}"
    )
    assert ran > 0, (
        "test_non_accepted_batch_zeroes_counts_and_carries_verdict_code ran ZERO cases — a "
        "block that asserts nothing is not a pass"
    )


def test_rejected_zero_row_batch_names_the_export_decline(
    _relinked_libraries: list[chtypes.Library],
) -> None:
    """G5: a CSV_WITH_NAMES body naming a header column no schema column
    matches, read under input_format_skip_unknown_fields=0, is refused
    before a single row is admitted — REJECTED, rows_read 0, zero row
    documents — and an export was requested. The pre-relink library left
    export_declined empty here even though bytes were withheld; the
    relinked one names the batch's own outcome as the reason."""
    ran = 0
    for library in _relinked_libraries:
        with library.compile_ddl("id UInt8, p String") as schema:
            body = b"id,unknown_col\n1,a\n"
            batch = schema.rows(
                Format.CSV_WITH_NAMES,
                body,
                {"input_format_skip_unknown_fields": "0"},
                export=Format.JSON_COMPACT_EACH_ROW,
            )
            assert batch.outcome is Outcome.REJECTED, (
                f"{library.version}: outcome {batch.outcome} (code {batch.err_code}, "
                f"{batch.err_msg!r}), want REJECTED"
            )
            assert batch.rows_read == 0 and len(batch.rows) == 0, (
                f"{library.version}: rows_read={batch.rows_read}, {len(batch.rows)} row "
                f"document(s) — want a call-level refusal with none"
            )
            assert not batch.payload, (
                f"{library.version}: payload {batch.payload!r}, want none — a rejected batch "
                f"exports nothing"
            )
            assert batch.export_declined, (
                f"{library.version}: export_declined is empty on a rejected zero-row batch "
                f"that requested an export — a decline must always carry its reason "
                f"(chtypes#298, G5)"
            )
            ran += 1

    assert ran == len(_relinked_libraries), (
        f"test_rejected_zero_row_batch_names_the_export_decline ran {ran} line(s), "
        f"want {len(_relinked_libraries)}"
    )
    assert ran > 0, (
        "test_rejected_zero_row_batch_names_the_export_decline ran ZERO cases — a block that "
        "asserts nothing is not a pass"
    )


def test_session_timezone_setting_is_declined_not_ignored(
    _relinked_libraries: list[chtypes.Library],
) -> None:
    """G1: ClickHouse's session_timezone is a real, known setting name —
    never the server's own code 115 — but this library resolves
    bare-DateTime timezone once, process-wide, at chs_init, and never
    re-reads it per call. Before the relink, a per-call session_timezone
    was silently accepted and had no effect, indistinguishable from
    agreement. The relinked library declines it the same way an unmodeled
    MergeTree setting is declined (docs/guides/settings.md): it comes back
    in unsupported_settings, which promotes the row's own outcome to
    UNSUPPORTED."""
    ran = 0
    for library in _relinked_libraries:
        with library.compile_ddl("id UInt8, t DateTime") as schema:
            result = schema.row(
                Format.JSON_EACH_ROW,
                b'{"id":1,"t":"2024-01-01 00:00:00"}',
                {"session_timezone": "Europe/Berlin"},
            )
            assert "session_timezone" in result.unsupported_settings, (
                f"{library.version}: unsupported_settings {result.unsupported_settings!r}, "
                f"want it to name session_timezone — sent on a per-call map, this is a "
                f"decline, never a silent admission"
            )
            assert result.outcome is Outcome.UNSUPPORTED, (
                f"{library.version}: outcome {result.outcome}, want UNSUPPORTED — a non-empty "
                f"unsupported_settings must promote the row (docs/guides/settings.md)"
            )
            ran += 1

    assert ran == len(_relinked_libraries), (
        f"test_session_timezone_setting_is_declined_not_ignored ran {ran} line(s), "
        f"want {len(_relinked_libraries)}"
    )
    assert ran > 0, (
        "test_session_timezone_setting_is_declined_not_ignored ran ZERO cases — a block that "
        "asserts nothing is not a pass"
    )
