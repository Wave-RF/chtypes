"""The public golden set — SERVED beside the artifacts as `sdk-goldens.json`.

A few dozen cases whose expectations were produced by the library itself and
agreed on by every ClickHouse version in the generating registry (the
golden-set generator). Every SDK runs the same file, so the
four bindings are held to one answer. It is not the corpus; that lives with
the rigs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import chtypes
from chtypes import FilterOutcome, Format, Outcome, SchemaError, Verdict

FORMATS = {
    "JSONEachRow": Format.JSON_EACH_ROW,
    "CSV": Format.CSV,
    "TSV": Format.TSV,
    "Values": Format.VALUES,
    "JSONCompactEachRow": Format.JSON_COMPACT_EACH_ROW,
}
# The document spells a verdict in one character; the golden spells it out.
VERDICTS = {
    Verdict.TRUE: "true",
    Verdict.FALSE: "false",
    Verdict.ERROR: "error",
    Verdict.DECLINE: "decline",
}


# The served document omits every optional field when it is empty: an absent
# key means empty or zero (chtypes#199). These read a case/row dict exactly
# the way `test_golden` needs to, so `test_golden_reader_*` below can pin the
# same behavior without an artifact.
def _case_outcome(case: dict, expect: dict) -> str:
    """The case-level ``outcome``. ``""`` is not a valid outcome, and
    omitting the key is legal only on a compile-error case (one carrying
    ``compile_error_code``) — every caller here is reached only after that
    branch has already continued away. Anywhere else a missing ``outcome``
    is a malformed golden, so this fails loudly instead of defaulting."""
    assert "outcome" in expect, (
        f"golden case {case['id']} has no outcome and is not a compile-error case"
    )
    return expect["outcome"]


def _case_verdicts(expect: dict) -> list:
    """Case-level ``verdicts``; absent means none. A filter case with no
    verdicts omits the key — reading it unconditionally is the latent break
    chtypes#199 found."""
    return expect.get("verdicts", [])


def _case_rows(expect: dict) -> list:
    """Case-level ``rows``; absent means none — a failed or declined Values
    body answers one verdict for the whole body with no per-row detail."""
    return expect.get("rows", [])


def _case_err_code(expect: dict) -> int:
    return expect.get("err_code", 0)


def _row_outcome(case: dict, row_index: int, want: dict) -> str:
    """The per-row ``outcome``; a row with none is a malformed golden, so
    this fails loudly rather than defaulting."""
    assert "outcome" in want, f"golden case {case['id']} row {row_index} has no outcome"
    return want["outcome"]


def _row_err_code(want: dict) -> int:
    return want.get("err_code", 0)


def _row_values(want: dict) -> dict:
    return want.get("values", {})


def _row_nulls(want: dict) -> list:
    return want.get("nulls", [])


def _row_transformed(want: dict) -> list:
    return want.get("transformed", [])


def _row_substituted(want: dict) -> list:
    return want.get("substituted", [])


def _row_computed(want: dict) -> dict:
    return want.get("computed", {})


def _goldens_path() -> Path:
    """``$CHTYPES_GOLDENS``, else the served set beside the artifacts."""
    override = os.environ.get("CHTYPES_GOLDENS")
    if override:
        return Path(override)
    reg = os.environ.get(chtypes.ENV_REGISTRY) or chtypes.default_registry_dir()
    return Path(reg) / "sdk-goldens.json"


def _goldens() -> dict | None:
    """The served set, or None when this machine has not fetched one.

    Returns rather than raises: the set is SERVED now, so a registry fetched
    before core started publishing it simply has no file — and a missing golden
    set is a loud skip at collection, never an import error that takes the whole
    module down with it."""
    try:
        doc = json.loads(_goldens_path().read_text(encoding="utf-8"))
    except OSError:
        return None
    assert doc["schema"] == 1, f"golden set schema {doc['schema']}; this test reads schema 1"
    assert doc["cases"], "golden set holds no cases"
    return doc


GOLDENS = _goldens()
_CASES = GOLDENS["cases"] if GOLDENS else []


@pytest.mark.skipif(
    GOLDENS is None,
    reason=(
        f"golden set not found at {_goldens_path()} — it is served beside the artifacts now; "
        f"`scripts/fetch.sh 25.8` installs it, and $CHTYPES_GOLDENS overrides the path"
    ),
)
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_golden(registry: chtypes.Registry, case: dict) -> None:
    fmt = FORMATS[case["format"]]
    body = case["body"].encode("utf-8")
    settings = case.get("settings") or None
    expect = case["expect"]
    assert GOLDENS is not None  # the skipif above guarantees it
    exact = GOLDENS["generated"].get("exact") or {}
    checked = 0
    skipped: list[str] = []
    # Construction opens nothing, so the goldens ASK for every line they are
    # about to score: `libraries()` lists what is open, and an empty list here
    # would score nothing and report itself green — which the tail assertion
    # exists to refuse.
    for line in registry.versions():
        registry.for_version(line)
    libraries = registry.libraries()
    for lib in libraries:
        # A case is only a golden for the EXACT build it was generated against.
        # The rolling index keeps older patch rows, so a machine can hold a patch
        # the generator never saw; that is a loud skip, never a failure.
        want = exact.get(lib.minor)
        if want is None:
            skipped.append(f"{lib.minor}: the set was not generated on that line")
            continue
        if lib.version != want:
            skipped.append(
                f"{lib.minor}: generated on ClickHouse {want}, this registry holds "
                f"{lib.version} — fetch {lib.minor} to run these cases"
            )
            continue
        if expect.get("compile_error_code"):
            with pytest.raises(SchemaError) as ei:
                lib.compile_ddl(case["ddl"])
            assert ei.value.code == expect["compile_error_code"], f"{lib.version}: {ei.value}"
            checked += 1
            continue
        schema = lib.compile_ddl(case["ddl"])
        try:
            if case.get("filter"):
                flt = schema.compile_filter(case["filter"])
                try:
                    fr = flt.rows(fmt, body, settings)
                finally:
                    flt.close()
                assert fr.outcome is FilterOutcome.OK, (lib.version, fr)
                want_filter_outcome = _case_outcome(case, expect)
                assert want_filter_outcome == "ok", (lib.version, fr)
                got_verdicts = [VERDICTS[v] for v in fr.verdicts]
                assert got_verdicts == _case_verdicts(expect), (lib.version, fr)
                checked += 1
                continue
            br = schema.rows(fmt, body, settings)
            want_batch_outcome = _case_outcome(case, expect)
            assert br.outcome == Outcome(want_batch_outcome), (lib.version, br.outcome, br.err_msg)
            assert br.err_code == _case_err_code(expect), (lib.version, br.err_code, br.err_msg)
            # A golden may carry no `rows` at all: a failed or declined Values body answers one
            # verdict for the whole body with no per-row detail. Go, TypeScript and Rust already
            # read a missing `rows` as empty; this reads it the same way, so the four agree.
            want_rows = _case_rows(expect)
            assert len(br.rows) == len(want_rows), (lib.version, br)
            for i, (row, want) in enumerate(zip(br.rows, want_rows, strict=True)):
                got = (lib.version, i, row.outcome, row.err_msg)
                want_outcome = _row_outcome(case, i, want)
                assert row.outcome == Outcome(want_outcome), got
                if want_outcome in ("rejected", "skipped"):
                    assert row.err_code == _row_err_code(want), got
                    continue
                values = {v.column: v.text for v in row.values}
                assert values == _row_values(want), (lib.version, i, values)
                nulls = sorted(v.column for v in row.values if v.null)
                assert nulls == _row_nulls(want), (lib.version, i, nulls)
                tr = sorted(f"{t.column}:{t.reason}" for t in row.transformed)
                wtr = sorted(f"{t['column']}:{t['reason']}" for t in _row_transformed(want))
                assert tr == wtr, (lib.version, i, tr)
                sub = sorted(s.column for s in row.substituted)
                assert sub == _row_substituted(want), (lib.version, i, sub)
                comp = {c.column: c.text for c in row.computed}
                assert comp == _row_computed(want), (lib.version, i, comp)
            checked += 1
        finally:
            schema.close()
    # Every library either ran the case or was skipped by name, and a run that
    # checked nothing is a skip rather than a silent pass.
    assert checked + len(skipped) == len(libraries) > 0
    if checked == 0:
        pytest.skip(
            "no artifact matches the golden set's generated versions — " + "; ".join(skipped)
        )


def test_golden_reader_treats_omitted_optional_fields_as_empty() -> None:
    """Pins the served document's omit-when-empty shape rule (chtypes#199): a
    key absent from a case or a row means empty or zero, not a KeyError.
    Needs no artifact or registry, so it runs in the no-artifact CI job — an
    unconditional ``expect["x"]`` creeping back into `_case_*`/`_row_*` fails
    here immediately, rather than waiting for a golden case that happens to
    omit that particular field.
    """
    filter_case = {"id": "synthetic-filter", "expect": {"outcome": "ok"}}
    assert _case_outcome(filter_case, filter_case["expect"]) == "ok"
    assert _case_verdicts(filter_case["expect"]) == []

    batch_case = {"id": "synthetic-batch", "expect": {"outcome": "ok"}}
    assert _case_rows(batch_case["expect"]) == []
    assert _case_err_code(batch_case["expect"]) == 0

    bare_row = {"outcome": "accepted"}
    assert _row_outcome(batch_case, 0, bare_row) == "accepted"
    assert _row_err_code(bare_row) == 0
    assert _row_values(bare_row) == {}
    assert _row_nulls(bare_row) == []
    assert _row_transformed(bare_row) == []
    assert _row_substituted(bare_row) == []
    assert _row_computed(bare_row) == {}


def test_golden_reader_requires_outcome_unless_compile_error() -> None:
    """``""`` is not a valid outcome. Omitting the key is legal only on a
    compile-error case; anywhere else it is a malformed golden and must fail
    loudly rather than default."""
    case = {"id": "synthetic-no-outcome"}
    with pytest.raises(AssertionError, match="synthetic-no-outcome.*has no outcome"):
        _case_outcome(case, {})
    with pytest.raises(AssertionError, match="synthetic-no-outcome row 0 has no outcome"):
        _row_outcome(case, 0, {})
