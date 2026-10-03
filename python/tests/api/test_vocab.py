"""The generated vocabularies carry the description's own facts and fallbacks."""

from __future__ import annotations

import json
from pathlib import Path

from chtypes import (
    DefaultKind,
    DocFlags,
    FilterOutcome,
    Format,
    Outcome,
    Reason,
    Source,
    Status,
    Verdict,
    errors,
)
from chtypes._abi1 import _decls

ABI = json.loads(
    (Path(__file__).resolve().parents[3] / "spec" / "abi-v1" / "abi.json").read_text(
        encoding="utf-8"
    )
)


def _values(name: str) -> list[dict]:
    return ABI["enums"][name]["values"]


def test_format_numbers_and_ch_names_are_the_descriptions() -> None:
    for v in _values("chs_format"):
        member = Format[v["name"].removeprefix("CHS_")]
        assert member.value == v["value"]
        assert member.ch_name == v["ch_name"]
    assert len(Format) == len(_values("chs_format"))


def test_status_matches_the_decl_table_and_the_binding_misuse_shape() -> None:
    assert {s.value: f"CHS_{s.name}" for s in Status} == _decls.STATUS_BY_VALUE
    assert errors.misuse("x").status == Status.INVALID_ARGUMENT
    assert errors.internal("x").status == Status.INTERNAL


def test_outcome_unknown_reads_as_the_fallback() -> None:
    assert Outcome.of("accepted") is Outcome.ACCEPTED
    assert Outcome.of("a-future-verdict") is Outcome.UNSUPPORTED
    assert FilterOutcome.of("nope") is FilterOutcome.UNSUPPORTED
    assert {o.value for o in Outcome} == {v["value"] for v in _values("row_outcome")}


def test_verdict_answered_is_the_descriptions_fact_and_unknown_declines() -> None:
    for v in _values("filter_verdict"):
        assert Verdict(v["value"]).answered is v["answered"]
    assert Verdict.of("x") is Verdict.DECLINE
    assert Verdict.of("t") is Verdict.TRUE


def test_reason_lossy_is_the_descriptions_fact_with_the_fallbacks() -> None:
    for v in _values("transform_reason"):
        assert Reason.lossy(v["value"]) is v["lossy"]
    # An unlisted reason keeps its spelling and takes the fallback's fact.
    fallback = ABI["enums"]["transform_reason"]["fallback"]
    expected = next(v["lossy"] for v in _values("transform_reason") if v["value"] == fallback)
    assert Reason.lossy("a-future-reason") is expected is True


def test_source_is_stored_is_the_descriptions_fact_and_default_generated_exists() -> None:
    for v in _values("value_src"):
        assert Source.is_stored(v["value"]) is v["is_stored"]
    assert Source.DEFAULT_GENERATED == "default_generated"
    assert Source.is_stored(Source.DEFAULT_GENERATED) is True


def test_default_kind_has_no_fallback() -> None:
    assert DefaultKind.of("") is DefaultKind.NONE
    assert DefaultKind.of("MATERIALIZED") is DefaultKind.MATERIALIZED
    try:
        DefaultKind.of("SOMETHING")
    except ValueError:
        pass
    else:
        raise AssertionError("an unlisted default_kind must raise")


def test_doc_flags() -> None:
    assert DocFlags.ALL == DocFlags.VALUES | DocFlags.TRANSFORMS | DocFlags.DEFAULTS
    assert int(DocFlags.ALL) == ABI["constants"]["CHS_DOC_ALL"]["value"]
