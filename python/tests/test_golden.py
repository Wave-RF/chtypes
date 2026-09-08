"""The public golden set — `goldens/cases.json` at the repository root.

A few dozen cases whose expectations were produced by the library itself and
agreed on by every ClickHouse version in the generating registry (chtypes-core:
`tests/conformance/go/cmd/goldens-gen`). Every SDK runs the same file, so the
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


def _goldens() -> dict:
    default = Path(__file__).resolve().parents[2] / "goldens" / "cases.json"
    path = os.environ.get("CHTYPES_GOLDENS") or str(default)
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["schema"] == 1, f"golden set schema {doc['schema']}; this test reads schema 1"
    assert doc["cases"], "golden set holds no cases"
    return doc


GOLDENS = _goldens()


@pytest.mark.parametrize("case", GOLDENS["cases"], ids=[c["id"] for c in GOLDENS["cases"]])
def test_golden(registry: chtypes.Registry, case: dict) -> None:
    fmt = FORMATS[case["format"]]
    body = case["body"].encode("utf-8")
    settings = case.get("settings") or None
    expect = case["expect"]
    checked = 0
    for lib in registry.libraries():
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
    assert checked == len(registry.libraries()) > 0
