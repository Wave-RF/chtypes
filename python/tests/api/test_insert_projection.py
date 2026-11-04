"""The projection gate for `Schema.insert`: every case of the shared fixture
(tests/fixtures/abi-v2/insert-projection.json), read through this binding's REAL
decoders, answers as scripts/abi-v2/check-insert-projection.py answers it.

`first_violation` mirrors that checker's `evaluate`, rule by rule and in order:
e_row_rejects, export_decline_is_call_decline, shared_members, counts, err_prefix,
err_suffix, output_presence, non_t_row, line_output, native_output; and the one exception,
generator_draw. It is a test helper, not public API.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

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


def first_violation(
    batch: BatchResult,
    ins: InsertResult,
    out_format: str,
    raw_insert: dict,
    out: bytes | None,
):
    """("equal", None), ("exception", name) or ("violates", rule).

    `raw_insert` is the insert document as the fixture holds it and `out` the raw
    output bytes: the public `InsertResult` has no `export_declined*` member and
    keeps `output` only for an accepted call, so a red that plants either is seen
    only through these two."""
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
    ):
        return "violates", "export_decline_is_call_decline"

    if any(
        k in raw_insert
        for k in ("export_declined", "export_declined_b64", "export_declined_reason")
    ):
        return "violates", "shared_members"
    raw_cols = raw_insert.get("export_columns")
    ins_cols = (
        None
        if raw_cols is None
        else tuple(
            base64.b64decode(c["b64"]) if "b64" in c else c["name"].encode() for c in raw_cols
        )
    )

    replaced = (
        ()
        if declined
        else (
            (batch.outcome, ins.outcome),
            (batch.err_code, ins.err_code),
            (batch.reason, ins.reason),
            (batch.refused_at, ins.refused_at),
            (batch.export_columns, ins_cols),
        )
    )
    shared = replaced + (
        (batch.rows_read, ins.rows_read),
        (batch.rows_skipped, ins.rows_skipped),
        (batch.unsupported_settings, ins.unsupported_settings),
        (batch.framing, ins.framing),
        (batch.partition_count, ins.partition_count),
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


def verdict(case: dict):
    batch, ins = decode_case(case)
    return first_violation(
        batch, ins, case["output_format"], case["insert"], _b64(case, "insert_rows_b64")
    )


@needs_fixture
def test_the_fixture_is_the_shape_this_gate_expects() -> None:
    assert sum(c["expect"] == "equal" for c in CASES) == 12
    assert sum(isinstance(c["expect"], dict) and "exception" in c["expect"] for c in CASES) == 1
    for rule in RULES:
        assert any(c["expect"] == {"violates": rule} for c in CASES), rule
    assert any(c["expect"] == {"exception": "generator_draw"} for c in CASES)
    assert sum(isinstance(c["expect"], dict) and "violates" in c["expect"] for c in CASES) == 24


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
    assert len(CASES) == 37
