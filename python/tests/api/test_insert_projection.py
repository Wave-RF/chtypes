"""The projection gate for `Schema.insert`: every case of the shared fixture
(tests/fixtures/abi-v2/insert-projection.json), read through this binding's REAL
decoders, answers as scripts/abi-v2/check-insert-projection.py answers it.

`first_violation` mirrors that checker's `evaluate`, rule by rule and in order:
e_row_rejects, shared_members, counts, err_prefix, err_suffix, output_presence,
non_t_row, line_output, native_output; and the one exception, generator_draw. It
is a test helper, not public API.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from chtypes import BatchResult, InsertResult, Outcome, RefusalSite, Verdict
from chtypes._decode import decode_batch, decode_insert_result

FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "abi-v2" / "insert-projection.json"
)
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

RULES = (
    "e_row_rejects",
    "shared_members",
    "counts",
    "err_prefix",
    "err_suffix",
    "output_presence",
    "non_t_row",
    "line_output",
    "native_output",
)
SUFFIX = re.compile(rb"^ \(at row [0-9]+\)$")


def _b64(case: dict, key: str) -> bytes | None:
    value = case.get(key)
    return None if value is None else base64.b64decode(value)


def first_violation(batch: BatchResult, ins: InsertResult, out_format: str):
    """("equal", None), ("exception", name) or ("violates", rule)."""
    rows = batch.rows
    verdicts = [r.verdict for r in rows]
    exp, out = batch.payload, ins.output
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

    shared = (
        (batch.outcome, ins.outcome),
        (batch.err_code, ins.err_code),
        (batch.reason, ins.reason),
        (batch.refused_at, ins.refused_at),
        (batch.rows_read, ins.rows_read),
        (batch.rows_skipped, ins.rows_skipped),
        (batch.unsupported_settings, ins.unsupported_settings),
        (batch.framing, ins.framing),
        (batch.partition_count, ins.partition_count),
        (batch.export_columns, ins.export_columns),
        (batch.export_declined, ins.export_declined),
        (batch.export_declined_reason, ins.export_declined_reason),
        (batch.rows_passed if filtered else None, ins.rows_passed),
        (batch.rows_cut if filtered else None, ins.rows_cut),
    )
    if any(a != b for a, b in shared):
        return "violates", "shared_members"

    t = verdicts.count(Verdict.TRUE)
    f = verdicts.count(Verdict.FALSE)
    d = verdicts.count(Verdict.DECLINE)
    if filtered:
        if batch.outcome == Outcome.ACCEPTED:
            good = batch.rows_passed == t and batch.rows_cut == f + d
        else:
            good = batch.rows_passed == 0 and batch.rows_cut == 0
        if not (good and ins.rows_undecided == d):
            return "violates", "counts"
    elif ins.rows_passed is not None or ins.rows_cut is not None or ins.rows_undecided is not None:
        return "violates", "counts"

    if not ins.err_msg.startswith(batch.err_msg):
        return "violates", "err_prefix"
    rest = ins.err_msg[len(batch.err_msg) :]
    if rest and not SUFFIX.match(rest):
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


def test_the_fixture_is_the_shape_this_gate_expects() -> None:
    assert sum(c["expect"] == "equal" for c in CASES) == 11
    assert sum(isinstance(c["expect"], dict) and "exception" in c["expect"] for c in CASES) == 1
    for rule in RULES:
        assert any(c["expect"] == {"violates": rule} for c in CASES), rule
    assert any(c["expect"] == {"exception": "generator_draw"} for c in CASES)
    assert sum(isinstance(c["expect"], dict) and "violates" in c["expect"] for c in CASES) == 21


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_answers_as_the_reference_checker_does(case: dict) -> None:
    batch, ins = decode_case(case)
    assert first_violation(batch, ins, case["output_format"]) == expected(case)


def test_a_planted_red_is_not_masked_by_an_earlier_equal_path() -> None:
    """Every rule's planted red fires THAT rule, not another: the order is the gate."""
    fired = set()
    for case in CASES:
        batch, ins = decode_case(case)
        kind, name = first_violation(batch, ins, case["output_format"])
        if kind == "violates":
            fired.add(name)
    assert fired == set(RULES)


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


def test_the_output_buffer_is_surfaced_as_the_call_returned_it() -> None:
    """Nothing is hidden: a declined document with output bytes is a contract
    break the projection gate must be able to see (red-e-row-output-present)."""
    case = next(c for c in CASES if c["id"] == "equal-refused-err-suffix")
    ins = decode_insert_result(json.dumps(case["insert"]).encode(), b"stray")
    assert ins.output == b"stray" and ins.export_declined
    assert ins.export_declined_reason == "export-batch-not-accepted"
    assert decode_insert_result(json.dumps(case["insert"]).encode(), None).output is None
