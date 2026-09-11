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
    for lib in registry.libraries():
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
                assert expect["outcome"] == "ok", (lib.version, fr)
                assert [VERDICTS[v] for v in fr.verdicts] == expect["verdicts"], (lib.version, fr)
                checked += 1
                continue
            br = schema.rows(fmt, body, settings)
            assert br.outcome == Outcome(expect["outcome"]), (lib.version, br.outcome, br.err_msg)
            assert br.err_code == expect.get("err_code", 0), (lib.version, br.err_code, br.err_msg)
            assert len(br.rows) == len(expect["rows"]), (lib.version, br)
            for i, (row, want) in enumerate(zip(br.rows, expect["rows"], strict=True)):
                got = (lib.version, i, row.outcome, row.err_msg)
                assert row.outcome == Outcome(want["outcome"]), got
                if want["outcome"] in ("rejected", "skipped"):
                    assert row.err_code == want.get("err_code", 0), got
                    continue
                values = {v.column: v.text for v in row.values}
                assert values == want.get("values", {}), (lib.version, i, values)
                nulls = sorted(v.column for v in row.values if v.null)
                assert nulls == want.get("nulls", []), (lib.version, i, nulls)
                tr = sorted(f"{t.column}:{t.reason}" for t in row.transformed)
                wtr = sorted(f"{t['column']}:{t['reason']}" for t in want.get("transformed", []))
                assert tr == wtr, (lib.version, i, tr)
                sub = sorted(s.column for s in row.substituted)
                assert sub == want.get("substituted", []), (lib.version, i, sub)
                comp = {c.column: c.text for c in row.computed}
                assert comp == want.get("computed", {}), (lib.version, i, comp)
            checked += 1
        finally:
            schema.close()
    # Every library either ran the case or was skipped by name, and a run that
    # checked nothing is a skip rather than a silent pass.
    assert checked + len(skipped) == len(registry.libraries()) > 0
    if checked == 0:
        pytest.skip(
            "no artifact matches the golden set's generated versions — " + "; ".join(skipped)
        )
