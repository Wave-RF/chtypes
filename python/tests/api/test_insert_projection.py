"""The projection gate for `Schema.insert`: every case of the shared fixture
(tests/fixtures/abi-v2/insert-projection.json), read through this binding's REAL
decoders, answers as scripts/abi-v2/check-insert-projection.py answers it.

`first_violation` mirrors that checker's `evaluate`, rule by rule and in order:
e_row_rejects, export_decline_is_call_decline, post_where_cut, post_where_refusal,
shared_members, counts, err_prefix, err_suffix, output_presence, non_t_row, line_output,
native_output; and the one exception, generator_draw. A case's batch_of_t_rows, the batch
over the rows the insert keeps, goes through the same real batch decoder; whether it is
accepted decides which of the two post-WHERE rules the case is held to. It is a test
helper, not public API.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from chtypes import BatchResult, InsertResult, Outcome, RefusalSite, Verdict
from chtypes._decode import decode_batch, decode_insert_result

FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "abi-v2" / "insert-projection.json"
)
# A bare copy of the python/ directory (the packaged tree) has no shared fixture:
# the tests skip loudly, by name, below. A present fixture that is unreadable or
# malformed fails at collection.
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if FIXTURE.exists() else []
needs_fixture = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason=f"insert-projection fixture absent ({FIXTURE}): the shared fixture is not in this tree",
)

RULES = (
    "e_row_rejects",
    "export_decline_is_call_decline",
    "post_where_cut",
    "post_where_refusal",
    "shared_members",
    "counts",
    "err_prefix",
    "err_suffix",
    "output_presence",
    "non_t_row",
    "line_output",
    "native_output",
)
SUFFIX = re.compile(rb": \(at row [0-9]+\)\n")
INSERT_NEVER = ("export_declined", "export_declined_b64", "export_declined_reason")
POST_WHERE_SITES = (RefusalSite.ROW, RefusalSite.BODY_STEP)
PROVENANCE_MARKS = ("unrecorded", "planted")


class TRows(NamedTuple):
    """A case's batch_of_t_rows, decoded: `rows` index the body's records,
    `batch` is the batch over those records alone, `records` is the case's
    body_records, each (off, len), and `failing` is the case's failing_t_record,
    the first failing kept record's body index (None where the t rows' batch is
    accepted, which has none)."""

    rows: list[int]
    batch: BatchResult
    records: list[tuple[int, int]]
    failing: int | None = None


def _b64(case: dict, key: str) -> bytes | None:
    value = case.get(key)
    return None if value is None else base64.b64decode(value)


def _raw_cols(raw: dict) -> tuple[bytes, ...] | None:
    cols = raw.get("export_columns")
    if cols is None:
        return None
    return tuple(base64.b64decode(c["b64"]) if "b64" in c else c["name"].encode() for c in cols)


def same_records(pwc: TRows) -> bool:
    """The t rows' batch reads exactly the listed records, in order: one row each,
    each input_span as long as its body record, none skipped. A batch refused at a
    row stops reading there, so then its rows end at the refused row."""
    idx, sub = pwc.rows, pwc.batch
    n = len(idx)
    if (
        sub.outcome == Outcome.REJECTED
        and sub.refused_at == RefusalSite.ROW
        and sub.refused_row is not None
        and 0 <= sub.refused_row < n
    ):
        n = sub.refused_row + 1
    if len(sub.rows) != n or sub.rows_read != n or sub.rows_skipped != 0:
        return False
    for r, i in zip(sub.rows, idx[:n], strict=True):
        if r.input_span is None or r.input_span.len != pwc.records[i][1]:
            return False
    return True


def post_where_refusal(
    batch: BatchResult,
    ins: InsertResult,
    raw_insert: dict,
    out: bytes | None,
    pwc: TRows,
) -> bool:
    """The checker's `_post_where_refusal`: the insert is `rejected` with the code
    of both batches and the t rows' batch's err, and its rows_read is exactly the
    failing kept record's body index + 1."""
    idx, sub = pwc.rows, pwc.batch
    if batch.outcome != Outcome.REJECTED or batch.refused_at not in POST_WHERE_SITES:
        return False
    if not same_records(pwc):
        return False
    if sub.outcome != Outcome.REJECTED or sub.refused_at not in POST_WHERE_SITES:
        return False
    if not idx or (
        sub.refused_at == RefusalSite.ROW
        and not (sub.refused_row is not None and 0 <= sub.refused_row < len(idx))
    ):
        return False
    if ins.outcome != Outcome.REJECTED or any(k in raw_insert for k in INSERT_NEVER):
        return False
    # the outcome class and code stay equal, to the whole batch and to the t rows' batch
    if not ins.err_code == batch.err_code == sub.err_code:
        return False
    # err: the server's for the first failing kept row, numbered over the kept rows
    if ins.err_msg != sub.err_msg:
        return False
    shared = (
        (batch.reason, ins.reason),
        (batch.refused_at, ins.refused_at),
        (batch.rows_skipped, ins.rows_skipped),
        (batch.unsupported_settings, ins.unsupported_settings),
        (batch.framing, ins.framing),
        (batch.partition_count, ins.partition_count),
    )
    if any(a != b for a, b in shared):
        return False
    # rows_read: exact. A refused insert, like the batch, stops at its refusing record, so
    # it has read through the first failing kept record, whose body index the case carries
    if pwc.failing is None or ins.rows_read != pwc.failing + 1:
        return False
    return (
        (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == (0, 0, 0)
        and "export_columns" not in raw_insert
        and out is None
    )


def post_where_cut(
    batch: BatchResult,
    ins: InsertResult,
    raw_insert: dict,
    out: bytes | None,
    pwc: TRows | None,
) -> bool | tuple[str, str | None]:
    """The checker's `_post_where_cut`: the verdict when the pair holds, False when not."""
    if pwc is None:  # an accepted insert over a refused body, with no t rows' batch
        return False
    idx, sub = pwc.rows, pwc.batch
    rows = batch.rows
    if batch.outcome != Outcome.REJECTED or batch.refused_at not in POST_WHERE_SITES:
        return False
    if batch.refused_at == RefusalSite.ROW and batch.refused_row in idx:
        return False
    # the rows the batch read: it stops at its refusal, so a kept row past it is not among them
    if any(rows[i].outcome != Outcome.ACCEPTED for i in idx if i < len(rows)):
        return False
    if not same_records(pwc):
        return False
    if sub.outcome != Outcome.ACCEPTED or any(r.verdict != Verdict.TRUE for r in sub.rows):
        return False
    if sub.export_declined != b"" or sub.export_declined_reason is not None:
        return False
    if any(k in raw_insert for k in INSERT_NEVER):
        return False
    from_t_rows = (
        (sub.outcome, ins.outcome),
        (sub.err_code, ins.err_code),
        (sub.reason, ins.reason),
        (sub.refused_at, ins.refused_at),
        (sub.export_columns, _raw_cols(raw_insert)),
        (sub.partition_count, ins.partition_count),
    )
    from_body = (
        (batch.rows_skipped, ins.rows_skipped),
        (batch.unsupported_settings, ins.unsupported_settings),
        (batch.framing, ins.framing),
    )
    if any(a != b for a, b in from_t_rows + from_body):
        return False
    # the insert read the whole body, which the batch, stopped at its refusal, did not
    if ins.rows_read != len(pwc.records):
        return False
    cut = ins.rows_read - ins.rows_skipped - len(idx)
    if not (
        sub.rows_passed == len(idx) == ins.rows_passed
        and ins.rows_cut == cut
        and ins.rows_undecided is not None
        and 0 <= ins.rows_undecided <= cut
    ):
        return False
    if ins.err_msg != sub.err_msg:
        return False
    if (sub.payload is None) != (out is None):
        return False
    if out is None:
        return "equal", None
    if any(c.source == "default_generated" for r in sub.rows for c in r.columns):
        return "exception", "generator_draw"
    # every row of the t rows' batch is t, so its whole export is the t rows' output
    return ("equal", None) if out == sub.payload else False


def first_violation(
    batch: BatchResult,
    ins: InsertResult,
    out_format: str,
    raw_insert: dict,
    out: bytes | None,
    pwc: TRows | None = None,
):
    """("equal", None), ("exception", name) or ("violates", rule).

    `raw_insert` is the insert document as the fixture holds it and `out` the raw
    output bytes: the public `InsertResult` has no `export_declined*` member and
    keeps `output` only for an accepted call, so a red that plants either is seen
    only through these two. `pwc` is the case's batch_of_t_rows, decoded: the
    kept rows' indexes and their batch."""
    rows = batch.rows
    verdicts = [r.verdict for r in rows]
    exp = batch.payload
    # The batch decodes its counts as 0 when absent, so a filter is read from the
    # rows' verdicts (present exactly with a filter) and from a nonzero count.
    filtered = any(v is not None for v in verdicts) or batch.rows_passed > 0 or batch.rows_cut > 0

    if batch.outcome == Outcome.ACCEPTED and Verdict.ERROR in verdicts:
        first = rows[verdicts.index(Verdict.ERROR)]
        if not (
            ins.outcome == Outcome.REJECTED
            and ins.err_code == first.verdict_code
            and ins.err_msg == first.verdict_err
            and ins.refused_at == RefusalSite.ROW
            and ins.rows_passed == 0
            and ins.rows_cut == 0
            and ins.rows_undecided == 0
            and ins.rows_read == batch.rows_read
            and out is None
        ):
            return "violates", "e_row_rejects"
        return "equal", None

    declined = batch.outcome == Outcome.ACCEPTED and (
        batch.export_declined != b"" or batch.export_declined_reason is not None
    )
    if declined and not (
        ins.outcome == Outcome.UNSUPPORTED
        and ins.reason == batch.export_declined_reason
        and ins.err_code == -2
        and ins.refused_at == RefusalSite.BODY_STEP
        and "export_columns" not in raw_insert
        and out is None
        and ins.err_msg == batch.export_declined
        and (not filtered or (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == (0, 0, 0))
    ):
        return "violates", "export_decline_is_call_decline"

    # post_where_cut and post_where_refusal split on the t rows' batch, accepted or refused.
    if pwc is not None and pwc.batch.outcome != Outcome.ACCEPTED:
        ok = post_where_refusal(batch, ins, raw_insert, out, pwc)
        return ("equal", None) if ok else ("violates", "post_where_refusal")
    if pwc is not None or (
        filtered and batch.outcome == Outcome.REJECTED and ins.outcome == Outcome.ACCEPTED
    ):
        got = post_where_cut(batch, ins, raw_insert, out, pwc)
        return ("violates", "post_where_cut") if got is False else got

    if any(k in raw_insert for k in INSERT_NEVER):
        return "violates", "shared_members"
    ins_cols = _raw_cols(raw_insert)

    # A declined output replaces these members with the insert's own values.
    replaced = (
        ()
        if declined
        else (
            (batch.outcome, ins.outcome),
            (batch.err_code, ins.err_code),
            (batch.reason, ins.reason),
            (batch.refused_at, ins.refused_at),
            (batch.export_columns, ins_cols),
            (batch.rows_passed if filtered else None, ins.rows_passed),
            (batch.rows_cut if filtered else None, ins.rows_cut),
        )
    )
    shared = replaced + (
        (batch.rows_read, ins.rows_read),
        (batch.rows_skipped, ins.rows_skipped),
        (batch.unsupported_settings, ins.unsupported_settings),
        (batch.framing, ins.framing),
        (batch.partition_count, ins.partition_count),
    )
    if any(a != b for a, b in shared):
        return "violates", "shared_members"

    # Over the accepted rows alone: a record skipped under the error allowance carries the verdict d
    # too, and is counted only in rows_skipped.
    kept = [r.verdict for r in rows if r.outcome == Outcome.ACCEPTED]
    t = kept.count(Verdict.TRUE)
    f = kept.count(Verdict.FALSE)
    d = kept.count(Verdict.DECLINE)
    if filtered:
        if batch.outcome == Outcome.ACCEPTED:
            good = batch.rows_passed == t and batch.rows_cut == f + d
        else:
            good = batch.rows_passed == 0 and batch.rows_cut == 0
        want = (t, f + d, d) if ins.outcome == Outcome.ACCEPTED else (0, 0, 0)
        if not (good and (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == want):
            return "violates", "counts"
    elif ins.rows_passed is not None or ins.rows_cut is not None or ins.rows_undecided is not None:
        return "violates", "counts"

    # A declined output's err is the decline text, checked above.
    if not declined:
        if not ins.err_msg.startswith(batch.err_msg):
            return "violates", "err_prefix"
        rest = ins.err_msg[len(batch.err_msg) :]
        if rest and not SUFFIX.fullmatch(rest):
            return "violates", "err_suffix"

    if (exp is None) != (out is None):
        return "violates", "output_presence"
    if out is None:
        return "equal", None
    if any(c.source == "default_generated" for r in rows for c in r.columns):
        return "exception", "generator_draw"
    if out_format == "Native":
        return ("equal", None) if out == exp else ("violates", "native_output")
    t_lines = [
        exp[s.off : s.off + s.len]
        for s, v in zip(batch.spans, verdicts, strict=True)
        if v is None or v == Verdict.TRUE
    ]
    if len(out.splitlines(keepends=True)) > len(t_lines):
        return "violates", "non_t_row"
    return ("equal", None) if out == b"".join(t_lines) else ("violates", "line_output")


def expected(case: dict):
    e = case["expect"]
    if e == "equal":
        return ("equal", None)
    if "exception" in e:
        return ("exception", e["exception"])
    return ("violates", e["violates"])


def decode_case(case: dict):
    batch = decode_batch(json.dumps(case["batch"]).encode(), _b64(case, "batch_export_b64"))
    ins = decode_insert_result(json.dumps(case["insert"]).encode(), _b64(case, "insert_rows_b64"))
    return batch, ins


def check_provenance(case: dict) -> None:
    """provenance names exactly the case's documents, each recorded ({build, line})
    or marked; a malformed one is a fixture error."""
    prov = case.get("provenance")
    if prov is None:
        assert "batch_of_t_rows" not in case, f"{case['id']}: batch_of_t_rows without provenance"
        return
    want = {"batch", "insert"} | ({"batch_of_t_rows"} if "batch_of_t_rows" in case else set())
    assert isinstance(prov, dict) and set(prov) == want, case["id"]
    for v in prov.values():
        recorded = (
            isinstance(v, dict)
            and set(v) == {"build", "line"}
            and all(isinstance(x, str) and x for x in v.values())
        )
        assert recorded or v in PROVENANCE_MARKS, f"{case['id']}: provenance mark {v!r}"


def decode_body_records(case: dict, batch: BatchResult) -> list[tuple[int, int]]:
    """body_records, each (off, len), in body order and not overlapping; every row
    the whole batch read carries its record's span. A malformed one is a fixture
    error."""
    records: list[tuple[int, int]] = []
    end = 0
    for r in case["body_records"]:
        assert isinstance(r, dict) and set(r) == {"off", "len"}, case["id"]
        off, n = r["off"], r["len"]
        assert type(off) is int and type(n) is int and n >= 1 and off >= end, case["id"]
        records.append((off, n))
        end = off + n
    assert len(batch.rows) <= len(records) and batch.rows_read <= len(records), case["id"]
    for row, (off, n) in zip(batch.rows, records, strict=False):
        assert row.input_span is not None, case["id"]
        assert (row.input_span.off, row.input_span.len) == (off, n), case["id"]
    return records


def check_failing(case: dict, pwc: TRows | None) -> int | None:
    """failing_t_record: required where the t rows' batch is refused (the
    post_where_refusal rule handles the case), absent otherwise; an integer, not a
    boolean, that is one of the t rows and, where that batch is refused at one of
    its rows, the body record that row is. A violation is a fixture error."""
    cid = case["id"]
    refused = pwc is not None and pwc.batch.outcome != Outcome.ACCEPTED
    if "failing_t_record" not in case:
        assert not refused, (
            f"{cid}: batch_of_t_rows is refused and the case carries no failing_t_record"
        )
        return None
    assert pwc is not None and refused, (
        f"{cid}: failing_t_record where post_where_refusal does not apply"
    )
    n = case["failing_t_record"]
    assert type(n) is int, f"{cid}: failing_t_record is not an integer"
    assert n in pwc.rows, f"{cid}: failing_t_record {n} is not one of batch_of_t_rows.rows"
    sub = pwc.batch
    if sub.refused_at == RefusalSite.ROW and sub.refused_row is not None:
        if 0 <= sub.refused_row < len(pwc.rows):
            assert pwc.rows[sub.refused_row] == n, (
                f"{cid}: failing_t_record {n} is not the record the t rows' batch refused at"
            )
    return n


def decode_pwc(case: dict, batch: BatchResult) -> TRows | None:
    """The case's batch_of_t_rows through the real batch decoder; a malformed one
    is a fixture error (the checker's FixtureError), never a verdict."""
    check_provenance(case)
    records = decode_body_records(case, batch) if "body_records" in case else None
    ev = case.get("batch_of_t_rows")
    if ev is None:
        check_failing(case, None)
        return None
    assert records is not None, f"{case['id']}: batch_of_t_rows without body_records"
    assert set(ev) == {"rows", "batch", "export_b64"}, case["id"]
    idx = ev["rows"]
    assert idx == sorted(set(idx)), case["id"]
    assert all(0 <= i < len(records) for i in idx), case["id"]
    sub = decode_batch(json.dumps(ev["batch"]).encode(), _b64(ev, "export_b64"))
    pwc = TRows(idx, sub, records)
    return pwc._replace(failing=check_failing(case, pwc))


def verdict(case: dict):
    batch, ins = decode_case(case)
    return first_violation(
        batch,
        ins,
        case["output_format"],
        case["insert"],
        _b64(case, "insert_rows_b64"),
        decode_pwc(case, batch),
    )


@needs_fixture
def test_the_fixture_is_the_shape_this_gate_expects() -> None:
    assert sum(c["expect"] == "equal" for c in CASES) == 24
    assert sum(isinstance(c["expect"], dict) and "exception" in c["expect"] for c in CASES) == 1
    for rule in RULES:
        assert any(c["expect"] == {"violates": rule} for c in CASES), rule
    assert any(c["expect"] == {"exception": "generator_draw"} for c in CASES)
    assert sum(isinstance(c["expect"], dict) and "violates" in c["expect"] for c in CASES) == 38


@needs_fixture
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_answers_as_the_reference_checker_does(case: dict) -> None:
    batch, ins = decode_case(case)
    assert verdict(case) == expected(case)


@needs_fixture
def test_a_planted_red_is_not_masked_by_an_earlier_equal_path() -> None:
    """Every rule's planted red fires THAT rule, not another: the order is the gate."""
    fired = set()
    for case in CASES:
        kind, name = verdict(case)
        if kind == "violates":
            fired.add(name)
    assert fired == set(RULES)


@needs_fixture
def test_decoded_insert_members_one_to_one() -> None:
    case = next(c for c in CASES if c["id"] == "equal-jsoncompacteachrow-filter-mixed")
    _, ins = decode_case(case)
    assert ins.outcome == Outcome.ACCEPTED and ins.err_code == 0 and ins.err_msg == b""
    assert (ins.rows_read, ins.rows_skipped) == (4, 0)
    assert (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == (2, 2, 1)
    assert ins.export_columns == (b"n", b"s") and ins.output is not None
    case = next(c for c in CASES if c["id"] == "equal-jsoncompacteachrow-no-filter-all-accepted")
    _, ins = decode_case(case)
    assert (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == (None, None, None)


@needs_fixture
def test_insert_output_absent_unless_accepted() -> None:
    """A declined output is the call's decline: `unsupported`, no public output,
    even when the library handed bytes back. An accepted call keeps its bytes."""
    case = next(c for c in CASES if c["id"] == "equal-declined-output-is-the-call-decline")
    ins = decode_insert_result(json.dumps(case["insert"]).encode(), None)
    assert ins.outcome == Outcome.UNSUPPORTED and ins.output is None
    assert ins.err_code == -2 and ins.refused_at == RefusalSite.BODY_STEP
    stray = decode_insert_result(json.dumps(case["insert"]).encode(), b"stray")
    assert stray.output is None and stray.export_columns is None
    case = next(c for c in CASES if c["id"] == "equal-jsoncompacteachrow-filter-mixed")
    _, ins = decode_case(case)
    assert ins.outcome == Outcome.ACCEPTED
    assert ins.output is not None and ins.output == _b64(case, "insert_rows_b64")


def test_the_fixture_gate_ran_cases() -> None:
    """A census that ran zero cases fails; an absent fixture skips by name, unless
    CHTYPES_REQUIRE_FIXTURES=1 (the repository's main CI), where it fails."""
    if not FIXTURE.exists():
        if os.environ.get("CHTYPES_REQUIRE_FIXTURES") == "1":
            pytest.fail(
                f"CHTYPES_REQUIRE_FIXTURES=1 and the insert-projection fixture is absent: {FIXTURE}"
            )
        pytest.skip(f"insert-projection fixture absent ({FIXTURE}): not a full-repository tree")
    assert len(CASES) == 63
