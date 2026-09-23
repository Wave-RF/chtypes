"""Issue #119 items 1-5, for `chs_format` 10 (`CSVWithNames`) and 11
(`TSVWithNames`): the round trip against a real artifact, the 26.4 / 26.5
header-matching boundary from BOTH sides, the INSERT-column-list interplay, and
the loud export decline. Until revision 5 was published these formats were
declared and executed by nothing; this file executes them.

Three rules shape it:

* The PROBE, never the enum and never the ABI revision, decides whether an
  artifact knows these two values (``docs/reference/bindings.md`` §Values a
  binding must accept and reject). They joined ``enum chs_format`` inside
  revision 5 after the number was set, so an artifact built from an earlier
  revision-5 header reports 5, passes the handshake, and does not know them.
  Every line here is asked before it is used, with a header spelled exactly as
  the column is declared — a probe whose answer depended on case-folding would
  not ask the same question on both sides of the boundary this file measures.
* Every block COUNTS what it ran and fails by name on zero. A boundary case
  that skips forever reads as a pass otherwise, which is the one outcome worse
  than a red.
* Codes and verdicts are asserted; ClickHouse's message text never is.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import chtypes
from chtypes.results import BatchResult, Format, Outcome, RowResult, Value

# The probe schema and the two probe payloads. The header spells `id` and `p`
# exactly as the DDL declares them, so the question is the same on every line.
WITHNAMES_DDL = "id UInt8, p String"
CSV_PROBE = b"id,p\n1,a\n"
TSV_PROBE = b"id\tp\n1\ta\n"

# The column-list schema: two DEFAULTs, so "took its DEFAULT" and "took the
# reader's zero" are different observable values rather than both being 0.
LIST_DDL = "id UInt8, p UInt8 DEFAULT 7, q UInt8 DEFAULT 9"

DEFAULTS_FOR_OMITTED = "input_format_defaults_for_omitted_fields"


@dataclass(frozen=True)
class Line:
    """One line of the registry: loaded, and asked whether it knows 10 and 11."""

    minor: str
    library: chtypes.Library
    supported: bool


def _probe(minor: str, library: chtypes.Library) -> bool:
    """Ask ONE artifact about format 10 and format 11, separately, exactly as
    `docs/reference/bindings.md` requires: one payload each through `chs_rows`,
    and only `accepted` counts. These two need no era refinement — both formats
    exist on every ClickHouse line measured, so unlike `Buffers` there is no 73
    era to allow for."""
    schema = library.compile_ddl(WITHNAMES_DDL)
    try:
        for fmt, body in ((Format.CSV_WITH_NAMES, CSV_PROBE), (Format.TSV_WITH_NAMES, TSV_PROBE)):
            if schema.rows(fmt, body).outcome is not Outcome.ACCEPTED:
                return False
        return True
    finally:
        schema.close()


@pytest.fixture(scope="session")
def withnames_lines(registry: chtypes.Registry) -> list[Line]:
    """Every line this build can open, each asked about formats 10 and 11.

    Three outcomes, deliberately different:

    * nothing on the registry this build can open: a loud SKIP by name, the
      same verdict every other artifact test here reaches. A refused ABI
      revision is `test_abi_revision.py`'s subject, not this file's.
    * lines opened, none of which knows format 10 or 11: a FAILURE. A registry
      of pre-value artifacts is a real finding, and a suite that skipped past
      it would look exactly like one that proved these formats work.
    * at least one line knows them: the cases below run.
    """
    lines: list[Line] = []
    opened = 0
    for minor in registry.versions():
        try:
            library = registry.for_version(minor)
        except chtypes.RegistryError as exc:
            # A mixed registry is an ordinary developer situation: an older
            # line sitting at a refused ABI revision beside current ones.
            print(f"line {minor} did not open, so it is not measured here: {exc}")
            continue
        opened += 1
        supported = _probe(minor, library)
        if not supported:
            print(
                f"line {minor} (ClickHouse {library.version}) does not answer the "
                f"CSVWithNames/TSVWithNames probe: a pre-value artifact, not measured "
                f"for the format cases"
            )
        lines.append(Line(minor=minor, library=library, supported=supported))
    if opened == 0:
        pytest.skip(
            f"no line on the registry {registry.directory} opened in this build — "
            f"fetch a current one with `scripts/fetch.sh 26.8`"
        )
    if not any(line.supported for line in lines):
        pytest.fail(
            f"no artifact in {registry.directory} knows chs_format 10 or 11: {opened} line(s) "
            f"opened and every one of them refused the CSVWithNames/TSVWithNames probe. These "
            f"formats joined enum chs_format inside ABI revision 5 without bumping it, so an "
            f"artifact built from an earlier revision-5 header reports 5 and still does not know "
            f"them — fetch a current line (scripts/fetch.sh 26.8)"
        )
    return lines


@pytest.fixture(scope="session")
def supported_lines(withnames_lines: list[Line]) -> list[Line]:
    """The subset the format cases run against."""
    return [line for line in withnames_lines if line.supported]


def header_matching_is_exact(minor: str) -> bool:
    """True when this line matches header names EXACTLY — through 26.4 — and
    False from 26.5, where matching is case-insensitive, exactly as those
    servers do."""
    major, feature = minor.split(".")[:2]
    return (int(major), int(feature)) <= (26, 4)


def value_of(row: RowResult, column: str) -> Value:
    for value in row.values:
        if value.column == column:
            return value
    raise AssertionError(
        f"row has no value for {column!r} (it has: {[v.column for v in row.values]})"
    )


def row_shape(row: RowResult) -> str:
    """The parts of a row this file compares — verdict, values and unknown
    fields, never ClickHouse's message text, which the CSV reader change warns
    consumers not to pin."""
    values = " ".join(f"{v.column}={v.text}/{v.source}/null={v.null}" for v in row.values)
    transformed = " ".join(f"{t.column}:{t.reason}" for t in row.transformed)
    return (
        f"outcome={row.outcome} code={row.err_code} values=[{values}] "
        f"unknown=[{' '.join(row.unknown_fields)}] transformed=[{transformed}]"
    )


def batch_shape(batch: BatchResult) -> str:
    rows = "\n    ".join(row_shape(row) for row in batch.rows)
    return (
        f"outcome={batch.outcome} code={batch.err_code} rows_read={batch.rows_read} "
        f"rows_skipped={batch.rows_skipped}\n    {rows}"
    )


# ------------------------------------------------------------------- item 1


def test_withnames_round_trip_matches_headerless(supported_lines: list[Line]) -> None:
    """A body in format 10 or 11 is accepted, and its verdicts match the
    headerless equivalent for the same rows. The reordered-header cases are the
    point of the formats: the data is addressed by NAME, so a header naming the
    columns in the other order still produces the declared-order stored row."""
    cases = 0
    for line in supported_lines:
        schema = line.library.compile_ddl(WITHNAMES_DDL)
        try:
            for name, named_format, plain_format, body, plain in (
                (
                    "CSVWithNames",
                    Format.CSV_WITH_NAMES,
                    Format.CSV,
                    b"id,p\n1,a\n2,b\n",
                    b"1,a\n2,b\n",
                ),
                (
                    "CSVWithNames/reordered header",
                    Format.CSV_WITH_NAMES,
                    Format.CSV,
                    b"p,id\na,1\nb,2\n",
                    b"1,a\n2,b\n",
                ),
                (
                    "TSVWithNames",
                    Format.TSV_WITH_NAMES,
                    Format.TSV,
                    b"id\tp\n1\ta\n2\tb\n",
                    b"1\ta\n2\tb\n",
                ),
                (
                    "TSVWithNames/reordered header",
                    Format.TSV_WITH_NAMES,
                    Format.TSV,
                    b"p\tid\na\t1\nb\t2\n",
                    b"1\ta\n2\tb\n",
                ),
            ):
                where = f"{line.minor} ({line.library.version}) {name}"
                named = schema.rows(named_format, body)
                control = schema.rows(plain_format, plain)
                # Not vacuously equal: two identical failures would satisfy the
                # comparison below and prove nothing.
                assert control.outcome is Outcome.ACCEPTED and len(control.rows) == 2, (
                    f"{where}: the headerless control did not accept two rows:"
                    f"\n{batch_shape(control)}"
                )
                assert named.outcome is Outcome.ACCEPTED and len(named.rows) == 2, (
                    f"{where}: format {int(named_format)} did not accept two rows:"
                    f"\n{batch_shape(named)}"
                )
                assert batch_shape(named) == batch_shape(control), (
                    f"{where}: format {int(named_format)} answered differently from format "
                    f"{int(plain_format)} for the same rows"
                )
                cases += 1
        finally:
            schema.close()
    assert cases, (
        "test_withnames_round_trip_matches_headerless ran ZERO cases: no line in the registry "
        "answered the format 10/11 probe, so nothing was compared against a headerless body"
    )
    print(f"{cases} round-trip case(s) over {len(supported_lines)} line(s)")


# ------------------------------------------------------------------- item 2


def test_withnames_header_case_boundary(supported_lines: list[Line]) -> None:
    """Header-name matching is EXACT through 26.4 and case-insensitive from
    26.5, so the same case-differing header binds differently on either side.
    Both sides are required — a test on one side alone does not show a
    boundary, it shows one answer.

    The header is ``ID,p`` (and ``ID<TAB>p``) against ``id UInt8, p String``,
    with the row ``1,a``:

    through 26.4
        ``ID`` names no column: it is an UNKNOWN FIELD and ``id`` takes the
        reader's zero, absent from the input.
    from 26.5
        ``ID`` names ``id`` case-insensitively: ``id`` is 1, from input, and
        there is no unknown field.
    """
    below = above = 0
    exact_lines = folding_lines = 0
    for line in supported_lines:
        schema = line.library.compile_ddl(WITHNAMES_DDL)
        exact = header_matching_is_exact(line.minor)
        if exact:
            exact_lines += 1
        else:
            folding_lines += 1
        try:
            for name, fmt, body in (
                ("CSVWithNames", Format.CSV_WITH_NAMES, b"ID,p\n1,a\n"),
                ("TSVWithNames", Format.TSV_WITH_NAMES, b"ID\tp\n1\ta\n"),
            ):
                where = f"ClickHouse {line.library.version} {name}"
                res = schema.rows(fmt, body)
                assert res.outcome is Outcome.ACCEPTED and len(res.rows) == 1, (
                    f"{where}: want one accepted row:\n{batch_shape(res)}"
                )
                row = res.rows[0]
                identifier = value_of(row, "id")
                if exact:
                    assert identifier.source != "input", (
                        f"{where} matches header names exactly (through 26.4), so `ID` must not "
                        f"bind to `id`; got {row_shape(row)}"
                    )
                    assert identifier.text == "0", (
                        f"{where}: `id` was named by no header column, so it takes the reader's "
                        f"zero; got {row_shape(row)}"
                    )
                    assert row.unknown_fields == ("ID",), (
                        f"{where}: `ID` names no column through 26.4 and must be reported as an "
                        f"unknown field; got {row_shape(row)}"
                    )
                    below += 1
                else:
                    assert identifier.source == "input" and identifier.text == "1", (
                        f"{where} matches header names case-insensitively (from 26.5), so `ID` "
                        f"must bind to `id`; got {row_shape(row)}"
                    )
                    assert row.unknown_fields == (), (
                        f"{where}: `ID` binds to `id` from 26.5, so there is no unknown field; "
                        f"got {row_shape(row)}"
                    )
                    above += 1
                print(f"{where}: {row_shape(row)}")
        finally:
            schema.close()
    # Both sides, or this proved nothing. A registry holding only lines above
    # the boundary answers every case the same way and says nothing about where
    # the behavior changes.
    assert below, (
        f"test_withnames_header_case_boundary ran ZERO cases BELOW the 26.5 boundary: the "
        f"registry holds no line at or under 26.4 that knows formats 10 and 11 (it offered "
        f"{folding_lines} line(s) above it). Header matching is exact through 26.4 and "
        f"case-insensitive from 26.5, and one side alone cannot show that — install an older "
        f"line (scripts/fetch.sh 24.8, or 26.4)"
    )
    assert above, (
        f"test_withnames_header_case_boundary ran ZERO cases AT OR ABOVE the 26.5 boundary: the "
        f"registry holds no line from 26.5 on that knows formats 10 and 11 (it offered "
        f"{exact_lines} line(s) below it). Install a current line (scripts/fetch.sh 26.8)"
    )
    print(
        f"{below} case(s) below the boundary over {exact_lines} line(s), "
        f"{above} at or above it over {folding_lines} line(s)"
    )


# ------------------------------------------------------------------- item 3


def test_withnames_column_list_interplay(supported_lines: list[Line]) -> None:
    """With an INSERT column list as well as a header, the LIST decides the
    block and the HEADER decides the layout:

    * a listed column the header omits takes its `DEFAULT` under
      ``input_format_defaults_for_omitted_fields=1`` and the reader's zero
      under ``0``;
    * an unlisted column the header names is an unknown field;
    * an unlisted column takes its `DEFAULT` whatever that setting says.

    The last rule is NOT a `WithNames` rule — the 0.3.0 CHANGELOG states it
    applies to every format — so it is executed on `JSONEachRow` and on
    headerless CSV and TSV as well, which a test on 10 and 11 alone would not
    show.
    """
    omitted = unknown = unlisted = 0
    for line in supported_lines:
        schema = line.library.compile_ddl(LIST_DDL)
        try:
            # A LISTED column the header omits, under each setting.
            for name, fmt, defaults, want_text, want_source in (
                ("CSVWithNames", Format.CSV_WITH_NAMES, "1", "7", "default"),
                ("CSVWithNames", Format.CSV_WITH_NAMES, "0", "0", "absent"),
                ("TSVWithNames", Format.TSV_WITH_NAMES, "1", "7", "default"),
                ("TSVWithNames", Format.TSV_WITH_NAMES, "0", "0", "absent"),
            ):
                where = f"ClickHouse {line.library.version} {name} omitted-defaults={defaults}"
                res = schema.rows(
                    fmt, b"id\n1\n", {DEFAULTS_FOR_OMITTED: defaults}, columns=["id", "p"]
                )
                assert res.outcome is Outcome.ACCEPTED and len(res.rows) == 1, (
                    f"{where}: want one accepted row:\n{batch_shape(res)}"
                )
                p = value_of(res.rows[0], "p")
                assert (p.text, p.source) == (want_text, want_source), (
                    f"{where}: `p` is listed and the header omits it, so it must be "
                    f"{want_text}/{want_source}; got {row_shape(res.rows[0])}"
                )
                omitted += 1

            # An UNLISTED column the header names is an unknown field — and
            # still takes its own DEFAULT.
            for name, fmt, body in (
                ("CSVWithNames", Format.CSV_WITH_NAMES, b"id,p\n1,3\n"),
                ("TSVWithNames", Format.TSV_WITH_NAMES, b"id\tp\n1\t3\n"),
            ):
                where = f"ClickHouse {line.library.version} {name}"
                res = schema.rows(fmt, body, columns=["id"])
                assert res.outcome is Outcome.ACCEPTED and len(res.rows) == 1, (
                    f"{where}: want one accepted row:\n{batch_shape(res)}"
                )
                row = res.rows[0]
                assert row.unknown_fields == ("p",), (
                    f"{where}: `p` is not in the column list, so the header naming it is an "
                    f"unknown field; got {row_shape(row)}"
                )
                p = value_of(row, "p")
                assert (p.text, p.source) == ("7", "default"), (
                    f"{where}: `p` is unlisted, so it takes its DEFAULT 7; got {row_shape(row)}"
                )
                unknown += 1

            # The unlisted-column DEFAULT rule, on formats that are NOT
            # WithNames. The 0.3.0 CHANGELOG says it applies to every format; a
            # case on 10 and 11 alone would not show that.
            for name, fmt, body in (
                ("JSONEachRow", Format.JSON_EACH_ROW, b'{"id":1}\n'),
                ("CSV", Format.CSV, b"1\n"),
                ("TSV", Format.TSV, b"1\n"),
            ):
                for defaults in ("0", "1"):
                    where = f"ClickHouse {line.library.version} {name} omitted-defaults={defaults}"
                    res = schema.rows(fmt, body, {DEFAULTS_FOR_OMITTED: defaults}, columns=["id"])
                    assert res.outcome is Outcome.ACCEPTED and len(res.rows) == 1, (
                        f"{where}: want one accepted row:\n{batch_shape(res)}"
                    )
                    row = res.rows[0]
                    for column, text in (("p", "7"), ("q", "9")):
                        value = value_of(row, column)
                        assert (value.text, value.source) == (text, "default"), (
                            f"{where}: `{column}` is not in the column list, so it takes its "
                            f"DEFAULT {text} whatever {DEFAULTS_FOR_OMITTED} says; got "
                            f"{row_shape(row)}"
                        )
                    unlisted += 1
        finally:
            schema.close()
    assert omitted and unknown and unlisted, (
        f"test_withnames_column_list_interplay ran ZERO cases in at least one block: "
        f"listed-column-the-header-omits={omitted}, unlisted-column-the-header-names={unknown}, "
        f"unlisted-column-takes-its-DEFAULT={unlisted} — each must run at least once"
    )
    print(
        f"{omitted} omitted-listed-column case(s), {unknown} unknown-field case(s), "
        f"{unlisted} unlisted-DEFAULT case(s) over {len(supported_lines)} line(s)"
    )


# ------------------------------------------------------------------- item 4


def test_withnames_export_format_declines(withnames_lines: list[Line]) -> None:
    """As an `export_format`, 10 and 11 are the existing loud decline, like
    every format other than `JSONCompactEachRow`. This one needs no probe — an
    artifact that cannot SERIALIZE a format declines it whether or not it can
    READ it — so it runs against every line that opened."""
    cases = 0
    for line in withnames_lines:
        schema = line.library.compile_ddl(WITHNAMES_DDL)
        try:
            for fmt in (Format.CSV_WITH_NAMES, Format.TSV_WITH_NAMES):
                where = f"ClickHouse {line.library.version} export_format {int(fmt)}"
                res = schema.rows(Format.JSON_EACH_ROW, b'{"id":1,"p":"a"}\n', export=fmt)
                assert res.outcome is Outcome.UNSUPPORTED, (
                    f"{where} must be declined, loudly:\n{batch_shape(res)}"
                )
                assert res.err_code == chtypes.CODE_UNSUPPORTED, (
                    f"{where} declined with code {res.err_code}; the ABI's sentinel is "
                    f"{chtypes.CODE_UNSUPPORTED}"
                )
                assert res.payload is None, (
                    f"{where} was declined and still produced "
                    f"{len(res.payload or b'')} payload byte(s)"
                )
                assert not res.rows and res.rows_read == 0, (
                    f"{where}: a declined export processes nothing:\n{batch_shape(res)}"
                )
                cases += 1
        finally:
            schema.close()
    assert cases, (
        "test_withnames_export_format_declines ran ZERO cases: no line in the registry opened, so "
        "the export decline for formats 10 and 11 was never asked for"
    )
    print(f"{cases} export-decline case(s) over {len(withnames_lines)} line(s)")
