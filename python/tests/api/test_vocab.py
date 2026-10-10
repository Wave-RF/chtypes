"""The generated vocabularies carry the description's own facts and fallbacks, and
every vocabulary keeps an unlisted value as its unknown(n) member (ABI v2 rule r3,
spec/abi-v2/docs.md)."""

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
from chtypes._abi2 import _decls

ABI = json.loads(
    (Path(__file__).resolve().parents[3] / "spec" / "abi-v2" / "abi.json").read_text(
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
    assert errors._misuse("x").status == Status.INVALID_ARGUMENT
    assert errors._internal("x").status == Status.INTERNAL
    # Construction helpers, never public (public issue #499).
    assert not {"internal", "misuse", "_internal", "_misuse"} & set(errors.__all__)


def test_an_unknown_outcome_is_kept_never_read_as_the_fallback_or_accepted() -> None:
    assert Outcome.of("accepted") is Outcome.ACCEPTED and Outcome.ACCEPTED.known
    future = Outcome.of("a-future-verdict")
    assert future == "a-future-verdict" and isinstance(future, Outcome) and not future.known
    assert future is not Outcome.UNSUPPORTED and future != Outcome.ACCEPTED
    assert future.name == "unknown(a-future-verdict)"
    nope = FilterOutcome.of("nope")
    assert nope == "nope" and not nope.known and nope != FilterOutcome.OK
    # The listed members are exactly the description's: unknown(n) is never one.
    assert {o.value for o in Outcome} == {v["value"] for v in _values("row_outcome")}


def test_verdict_answered_is_the_descriptions_fact_and_unknown_is_never_answered() -> None:
    for v in _values("filter_verdict"):
        assert Verdict(v["value"]).answered is v["answered"]
        assert Verdict(v["value"]).known
    unknown = Verdict.of("x")
    assert unknown == "x" and not unknown.known and unknown.answered is False
    assert unknown is not Verdict.DECLINE
    assert Verdict.of("t") is Verdict.TRUE


def test_reason_lossy_is_the_descriptions_fact_with_the_fallbacks() -> None:
    for v in _values("transform_reason"):
        assert Reason.lossy(v["value"]) is v["lossy"]
    # An unlisted reason keeps its spelling and takes the fallback's fact.
    fallback = ABI["enums"]["transform_reason"]["fallback"]
    expected = next(v["lossy"] for v in _values("transform_reason") if v["value"] == fallback)
    assert Reason.lossy("a-future-reason") is expected is True


def test_reason_known_is_false_for_exactly_an_unlisted_reason() -> None:
    for v in _values("transform_reason"):
        assert Reason.known(v["value"])
    assert not Reason.known("a-future-reason")


def test_source_is_stored_is_the_descriptions_fact_and_default_generated_exists() -> None:
    for v in _values("value_src"):
        assert Source.is_stored(v["value"]) is v["is_stored"]
        assert Source.known(v["value"])
    assert Source.DEFAULT_GENERATED == "default_generated"
    assert Source.is_stored(Source.DEFAULT_GENERATED) is True
    # r3: an unlisted source is its unknown(n), never an error; the description
    # names no is_stored fact for one yet, so it reads as not stored.
    assert not Source.known("a-future-source")
    assert Source.is_stored("a-future-source") is False


def test_default_kind_has_no_fallback_and_keeps_an_unlisted_kind() -> None:
    assert DefaultKind.of("") is DefaultKind.NONE
    assert DefaultKind.of("MATERIALIZED") is DefaultKind.MATERIALIZED
    unknown = DefaultKind.of("SOMETHING")
    assert unknown == "SOMETHING" and not unknown.known and isinstance(unknown, DefaultKind)


def test_int_enums_keep_an_unlisted_value() -> None:
    status = Status(99)
    assert status == 99 and not status.known and status.name == "unknown(99)"
    assert Status.OK.known and Status(0) is Status.OK
    fmt = Format(77)
    assert fmt == 77 and not fmt.known and fmt.ch_name == ""
    assert Format.JSON_EACH_ROW.known and Format.JSON_EACH_ROW.ch_name == "JSONEachRow"
    # Neither builds a member from a value of another type.
    for bad in ("1", 1.5, None):
        try:
            Status(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Status({bad!r}) must not build a member")


def test_doc_flags() -> None:
    assert DocFlags.ALL == DocFlags.VALUES | DocFlags.TRANSFORMS | DocFlags.DEFAULTS
    assert int(DocFlags.ALL) == ABI["constants"]["CHS_DOC_ALL"]["value"]
