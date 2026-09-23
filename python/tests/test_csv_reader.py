"""Issue #119, items 3 and 4 (the revision-5 CSV/TSV reader) and item 1 (the
two revision-5 `src` provenances), executed END TO END against a real
revision-5 artifact rather than against a hand-built document.

A test that hand-sets the value the code computes tests the belief, not the
computation, so every expectation below comes off a loaded library.

⚠️ Nothing here asserts on an error MESSAGE. With revision-5 artifacts a
rejected CSV or TSV row carries ClickHouse's own wording, which changes
whenever ClickHouse rewords an error; the CODE is the stable part and is the
only thing pinned. 15,245 TSV records in the artifact producer's corpus differ
in the message alone — that is the fragility this change exists to warn
consumers about.
"""

from __future__ import annotations

import json

import pytest

import chtypes
from chtypes.results import Format, Outcome, Reason, Source

# The two rejection codes, measured on 24.8, 26.7 and 26.8 (linux-arm64) and on
# 26.3 through 26.8 (darwin-arm64), identical on every line: ClickHouse's own
# INCORRECT_DATA for the CSV reader's trailing-garbage refusal and
# CANNOT_PARSE_INPUT_ASSERTION_FAILED for the TSV reader's.
CSV_REJECTION_CODE = 117
TSV_REJECTION_CODE = 27

DEFAULTS_OFF = {"input_format_defaults_for_omitted_fields": "0"}
DEFAULTS_ON = {"input_format_defaults_for_omitted_fields": "1"}


@pytest.fixture(scope="session")
def rev5_libraries(registry: chtypes.Registry) -> list[chtypes.Library]:
    """Every line of the test registry that loads at ABI revision 5.

    Every block runs against ALL of them — CI fetches three (the newest -lts,
    the newest -stable and 24.8) — rather than one resolved by a helper,
    because none of these behaviors is line-sensitive and a silent retarget
    onto a different ClickHouse would hide that. A line this platform has not
    relinked to revision 5 refuses to load through the loader's own ABI guard:
    that is the guard working, so it is reported and passed over.
    """
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
            f"this file needs one — fetch one with scripts/fetch.sh (docs/guides/fetch.md)"
        )
    return libraries


# ------------------------------------------- item 3: codes, not messages


def test_csv_and_tsv_rejections_keep_their_codes(
    rev5_libraries: list[chtypes.Library],
) -> None:
    """A CSV row the library rejects returns the same code as the
    pre-revision-5 path, and so does a TSV one; only the wording moved.

    A rejected CSV row also now carries an EMPTY per-column list, where the
    previous reader could report a partial one — a consumer reading per-column
    detail off a rejected row sees this. That detail was measured for CSV
    rejections and is asserted for CSV only; the TSV row's own emptiness is
    reported, not pinned.
    """
    # `abc` in the second field: the first field parses, so a reader that
    # reported per-column detail for what it managed to read would report one
    # column here. That is the shape the empty-cols assertion pins.
    cases = (
        ("CSV", Format.CSV, b"1,abc\n", CSV_REJECTION_CODE, True),
        ("TSV", Format.TSV, b"1\tabc\n", TSV_REJECTION_CODE, False),
    )
    ran = 0
    for library in rev5_libraries:
        with library.compile_ddl("id UInt8, n UInt8") as schema:
            for name, fmt, row, code, empty_cols in cases:
                result = schema.row(fmt, row)
                assert result.outcome is Outcome.REJECTED, (
                    f"{library.version} {name}: the row must be refused, not coerced"
                )
                # The CODE, and only the code. `err_msg` is deliberately never
                # asserted — it is ClickHouse's own text now.
                assert result.err_code == code, (
                    f"{library.version} {name}: err_code {result.err_code}, want {code} "
                    f"(message, not asserted, was {result.err_msg!r})"
                )
                if empty_cols:
                    assert result.values == (), (
                        f"{library.version} {name}: a rejected row reported per-column values "
                        f"{result.values} — the revision-5 reader reports no partial column "
                        f"list for a row it refused"
                    )
                    assert result.transformed == (), (
                        f"{library.version} {name}: a rejected row reported transforms "
                        f"{result.transformed} — there is no stored row to have changed"
                    )
                else:
                    print(
                        f"{library.version} {name} rejected row carries "
                        f"{len(result.values)} value(s) (not pinned: the empty-cols "
                        f"measurement is CSV-scoped)"
                    )
                ran += 1

                # The same refusal through the batch path, where an allowed
                # error budget turns it into a skipped row rather than a
                # rejected batch. Same code, again by code only.
                batch = schema.rows(fmt, row, {"input_format_allow_errors_num": "10"})
                assert len(batch.rows) == 1, f"{library.version} {name}: want one row document"
                assert batch.rows[0].outcome is Outcome.SKIPPED
                assert batch.rows[0].err_code == code, (
                    f"{library.version} {name}: batch row err_code {batch.rows[0].err_code}, "
                    f"want {code} (message, not asserted, was {batch.rows[0].err_msg!r})"
                )
                if empty_cols:
                    assert batch.rows[0].values == ()
                ran += 1

    # The count assertion. A block that exercised a library and then ran
    # nothing must say so by name rather than pass.
    want = 4 * len(rev5_libraries)
    assert ran == want, (
        f"test_csv_and_tsv_rejections_keep_their_codes ran {ran} case(s) over "
        f"{len(rev5_libraries)} line(s), want {want}"
    )
    assert ran > 0, (
        "test_csv_and_tsv_rejections_keep_their_codes ran ZERO cases — "
        "a block that asserts nothing is not a pass"
    )


# -------------------------------- item 4: the empty CSV field under `=0`

# The three column types and the transform reason MEASURED for each when a CSV
# empty field arrives under `input_format_defaults_for_omitted_fields=0`.
#
# ⚠️ These three are WHAT WAS MEASURED, not a closed set. The detector's switch
# would assign `date_clamp`, `ip_mangle` and others for other column types, and
# nothing has measured whether an empty field reaches them. Nothing below
# asserts the set is exactly three, and a fourth column type arriving must not
# make this test red.
#
# The Enum carries a member at value 0: an empty field is not a member
# spelling, the reader's zero is, and the coercion between the two is what
# `enum_coerce` names.
EMPTY_FIELD_CASES = (
    ("Enum8", "id UInt8, c Enum8('a' = 0, 'b' = 1)", Reason.ENUM_COERCE),
    ("FixedString", "id UInt8, c FixedString(4)", Reason.FIXEDSTRING_PAD),
    ("UUID", "id UInt8, c UUID", Reason.UUID_MANGLE),
)


def _reasons_for(result: chtypes.RowResult) -> list[str]:
    return [t.reason for t in result.transformed if t.column == "c"]


def _stored_for(result: chtypes.RowResult) -> tuple[str, bool]:
    for value in result.values:
        if value.column == "c":
            return value.text, True
    return "", False


def test_empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform(
    rev5_libraries: list[chtypes.Library],
) -> None:
    """An empty CSV field under `input_format_defaults_for_omitted_fields=0` is
    reported as an INPUT and its coercion as a TRANSFORM, where the previous
    reader reported no transform: the field's reference value is the empty
    string rather than absent. The STORED VALUE is unchanged — this changes
    what is reported, not what is stored.

    ⚠️ The setting is set explicitly on every call. Under the default (`1`) a
    bare empty field still takes the column's DEFAULT, so a case that forgot
    the setting would pass without ever exercising this.

    ⚠️ TSV is NOT affected by the setting: its reader takes an empty field
    through the typed parse whether the setting is on or off, exactly as the
    previous splitter did. What is pinned for TSV is that the two settings give
    the IDENTICAL answer — that is the asymmetry, and a later change making TSV
    setting-sensitive like CSV turns it red. Note, measured: TSV DOES report
    `fixedstring_pad` for a FixedString empty field under BOTH settings and has
    always done so; "TSV is not affected" means the reader swap did not change
    TSV's answer, never that TSV reports no transform at all. Do not "fix" that
    by asserting TSV reports nothing.
    """
    ran = 0
    for library in rev5_libraries:
        for name, ddl, reason in EMPTY_FIELD_CASES:
            with library.compile_ddl(ddl) as schema:
                off = schema.row(Format.CSV, b"1,\n", DEFAULTS_OFF)
                on = schema.row(Format.CSV, b"1,\n", DEFAULTS_ON)
                off_reasons, on_reasons = _reasons_for(off), _reasons_for(on)
                off_stored, off_found = _stored_for(off)
                on_stored, on_found = _stored_for(on)

                # The empty field is reported as an INPUT: it is in the stored
                # row, with src `input` — not absent, not a default.
                assert off_found, (
                    f"{library.version} {name}: CSV =0 reported no value at all for the empty "
                    f"field, want one with src 'input'"
                )
                assert off.value("c").source == "input", (
                    f"{library.version} {name}: CSV =0 value source "
                    f"{off.value('c').source!r}, want 'input' — the empty field is an input, "
                    f"not an omitted column"
                )

                # ... and its coercion as a TRANSFORM, with the measured reason.
                assert reason in off_reasons, (
                    f"{library.version} {name}: CSV =0 reported {off_reasons}, "
                    f"want {reason!r} among them"
                )
                # Under the default the same field reports no such transform —
                # which is what makes the setting load-bearing rather than
                # decorative. (Not "no transforms at all": that would be a
                # claim about column types nobody measured.)
                assert reason not in on_reasons, (
                    f"{library.version} {name}: CSV =1 reported {reason!r} too — under the "
                    f"default an empty field still takes the column's DEFAULT and this reason "
                    f"must not appear, or the =0 case proves nothing"
                )

                # THE STORED VALUE IS UNCHANGED. This changes what is reported,
                # never what is stored.
                assert (on_found, on_stored) == (off_found, off_stored), (
                    f"{library.version} {name}: stored value differs between =0 "
                    f"({off_stored!r}) and =1 ({on_stored!r}) — this change is about what is "
                    f"REPORTED, never about what is stored"
                )

                # TSV: the setting reaches nothing. The two answers must be the
                # same answer, whatever that answer is.
                tsv_off = schema.row(Format.TSV, b"1\t\n", DEFAULTS_OFF)
                tsv_on = schema.row(Format.TSV, b"1\t\n", DEFAULTS_ON)
                assert (tsv_off.outcome, tsv_off.err_code) == (tsv_on.outcome, tsv_on.err_code), (
                    f"{library.version} {name}: TSV answered =0 as "
                    f"{tsv_off.outcome}/{tsv_off.err_code} and =1 as "
                    f"{tsv_on.outcome}/{tsv_on.err_code} — TSV's reader takes an empty field "
                    f"through the typed parse whether the setting is on or off; a difference "
                    f"here means TSV has started behaving like CSV"
                )
                assert _reasons_for(tsv_off) == _reasons_for(tsv_on), (
                    f"{library.version} {name}: TSV reported {_reasons_for(tsv_off)} under =0 "
                    f"and {_reasons_for(tsv_on)} under =1 — the setting must make NO difference "
                    f"to TSV; the CSV-only transform is the asymmetry this pins"
                )
                assert _stored_for(tsv_off) == _stored_for(tsv_on), (
                    f"{library.version} {name}: TSV stored {_stored_for(tsv_off)} under =0 and "
                    f"{_stored_for(tsv_on)} under =1 — the setting must make no difference to TSV"
                )
                print(
                    f"{library.version} {name}: CSV =0 {off_reasons} / =1 {on_reasons}   "
                    f"TSV =0 {_reasons_for(tsv_off)} / =1 {_reasons_for(tsv_on)}"
                )
                ran += 1

    want = len(EMPTY_FIELD_CASES) * len(rev5_libraries)
    assert ran == want, (
        f"test_empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform ran {ran} "
        f"column type(s) over {len(rev5_libraries)} line(s), want {want}"
    )
    assert ran > 0, (
        "test_empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform ran ZERO "
        "cases — a block that asserts nothing is not a pass"
    )


# ------------------------------------- item 1: the two `src` provenances


def test_ephemeral_input_is_read_never_stored_never_exported(
    rev5_libraries: list[chtypes.Library],
) -> None:
    """A listed EPHEMERAL column is READ — it is in scope for the DEFAULT
    expressions that reference it — and is never stored and never exported.

    The plant that makes this a test rather than a hope: drop
    `Source.EPHEMERAL_INPUT` from `_document.py`'s values exclusion and this
    test goes red on its "values must not contain e" assertion, naming the
    source the artifact really reported.
    """
    ran = 0
    for library in rev5_libraries:
        # e is EPHEMERAL: it has no value at all outside a column list, and d
        # reads it. d == 6 is the proof the value was read.
        with library.compile_ddl("id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1") as schema:
            result = schema.row(Format.JSON_EACH_ROW, b'{"id":3,"e":5}', columns=["id", "e"])
            assert result.outcome is Outcome.ACCEPTED, (
                f"{library.version}: outcome {result.outcome} (code {result.err_code})"
            )
            seen = {v.column: v.source for v in result.values}
            assert result.value("d").text == "6", (
                f"{library.version}: d is {result.value('d').text!r}, want '6' — a listed "
                f"EPHEMERAL column's value must reach the DEFAULT expressions referencing it"
            )
            assert "e" not in seen, (
                f"{library.version}: values contains e with src {seen.get('e')!r} — a listed "
                f"EPHEMERAL column is never stored, so it must never sit where a caller reads "
                f"the stored row"
            )
            assert Source.EPHEMERAL_INPUT not in seen.values(), (
                f"{library.version}: values carries a {Source.EPHEMERAL_INPUT!r} column "
                f"({seen}) — it is excluded from the stored-row view exactly as "
                f"{Source.SKIPPED!r} is"
            )

            # ... and never EXPORTED. The exported tuple is the wire tuple —
            # declared order minus MATERIALIZED/ALIAS/EPHEMERAL — so the row is
            # [id, d] and the ephemeral 5 is nowhere in it. Parsed rather than
            # string-compared: the separator spacing is the vendored writer's,
            # not this repository's, and pinning it would test the wrong thing.
            batch = schema.rows(
                Format.JSON_EACH_ROW,
                b'{"id":3,"e":5}\n',
                export=Format.JSON_COMPACT_EACH_ROW,
                columns=["id", "e"],
            )
            assert batch.outcome is Outcome.ACCEPTED, (
                f"{library.version}: export outcome {batch.outcome}, "
                f"declined {batch.export_declined!r}"
            )
            assert batch.payload is not None
            fields = json.loads(batch.payload.decode())
            assert len(fields) == 2, (
                f"{library.version}: exported row {batch.payload!r} has {len(fields)} field(s), "
                f"want 2 — the wire tuple is declared order minus MATERIALIZED/ALIAS/EPHEMERAL"
            )
            assert fields == [3, 6], (
                f"{library.version}: exported row {batch.payload!r}, want the id and the computed d"
            )
            assert 5 not in fields, (
                f"{library.version}: the EPHEMERAL value 5 rode in the exported bytes "
                f"{batch.payload!r} — a listed EPHEMERAL column is never exported"
            )
            ran += 1

    assert ran == len(rev5_libraries), (
        f"test_ephemeral_input_is_read_never_stored_never_exported ran {ran} line(s), "
        f"want {len(rev5_libraries)}"
    )
    assert ran > 0, (
        "test_ephemeral_input_is_read_never_stored_never_exported ran ZERO cases — "
        "a block that asserts nothing is not a pass"
    )


def test_materialized_input_is_stored_and_stays_in_values(
    rev5_libraries: list[chtypes.Library],
) -> None:
    """The other half: the two provenances get OPPOSITE treatment, and folding
    them together is the mistake this guards.

    ⚠️ The supplied value must be the stored one: `m Int64 MATERIALIZED id +
    10` with id = 1 would compute 11, and 99 is what was sent. Reading 11 here
    would mean the supplied value never replaced the expression.

    ⚠️ The export channel DECLINES this row, loudly, and that is the C ABI
    contract's own rule rather than a defect: the exported tuple is the wire
    tuple (declared order minus MATERIALIZED/ALIAS/EPHEMERAL), which has no
    position for a MATERIALIZED column, so bytes that carried the supplied
    value could not be re-INSERTed. Fail-closed with the reason in
    `export_declined` is what the header specifies, and it is asserted here so
    a later silent emission would show up.
    """
    allow = {"insert_allow_materialized_columns": "1"}
    ran = 0
    for library in rev5_libraries:
        ddl = "id UInt32, p UInt8 DEFAULT 3, m Int64 MATERIALIZED id + 10"
        with library.compile_ddl(ddl) as schema:
            result = schema.row(
                Format.JSON_EACH_ROW, b'{"id":1,"m":99}', allow, columns=["id", "m"]
            )
            assert result.outcome is Outcome.ACCEPTED, (
                f"{library.version}: outcome {result.outcome} "
                f"(code {result.err_code}, {result.err_msg!r})"
            )
            try:
                stored = result.value("m")
            except KeyError:  # pragma: no cover - the failure message is the point
                pytest.fail(
                    f"{library.version}: values has no m — a listed MATERIALIZED column's "
                    f"supplied value IS stored and stays IN the stored-row view, unlike "
                    f"{Source.EPHEMERAL_INPUT!r}"
                )
            assert stored.source == Source.MATERIALIZED_INPUT, (
                f"{library.version}: m src {stored.source!r}, want {Source.MATERIALIZED_INPUT!r}"
            )
            # ⚠️ 24.8 renders a stored Int64 as a JSON string and 25.8/26.8 as
            # a number — ClickHouse's own 64-bit quoting changing between
            # lines, which belongs to the artifact and is never normalized
            # here. Both spellings are the same stored value; neither is 11.
            assert stored.text in ("99", '"99"'), (
                f"{library.version}: m is {stored.text!r}, want the SUPPLIED 99 (as a number "
                f"or, on a line that quotes 64-bit integers, as '\"99\"') — not the "
                f"expression's 11"
            )
            computed = {c.column: c.text for c in result.computed}
            assert computed.get("m") == stored.text, (
                f"{library.version}: computed m {computed.get('m')!r}, values m "
                f"{stored.text!r} — the same stored value, reported twice"
            )

            # The export channel's documented refusal.
            batch = schema.rows(
                Format.JSON_EACH_ROW,
                b'{"id":1,"m":99}\n',
                allow,
                export=Format.JSON_COMPACT_EACH_ROW,
                columns=["id", "m"],
            )
            assert not batch.payload, (
                f"{library.version}: export emitted {batch.payload!r} — the wire tuple has no "
                f"position for a MATERIALIZED column, so bytes carrying the supplied value "
                f"could not be re-INSERTed; the contract is fail-closed"
            )
            assert batch.export_declined, (
                f"{library.version}: export withheld its bytes without saying why — "
                f"a decline carries its reason"
            )
            ran += 1

    assert ran == len(rev5_libraries), (
        f"test_materialized_input_is_stored_and_stays_in_values ran {ran} line(s), "
        f"want {len(rev5_libraries)}"
    )
    assert ran > 0, (
        "test_materialized_input_is_stored_and_stays_in_values ran ZERO cases — "
        "a block that asserts nothing is not a pass"
    )
