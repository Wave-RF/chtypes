"""Revision 6's partition key: the two result fields, the setter's sign rule,
and — with a revision-6 artifact — the key end to end.

The unit half parses documents through the SAME parsers every `row`/`rows`
call uses, and drives `Schema.set_partition_by` through a stand-in native
layer, so the rc -> exception mapping that ships is the one tested. The
artifact half skips LOUDLY, by name, without a revision-6 registry.
"""

from __future__ import annotations

import threading
import weakref

import pytest

import chtypes
from chtypes._document import parse_batch_document, parse_row_document


def test_partition_fields_parse() -> None:
    batch = parse_batch_document(
        b'{"outcome":"accepted","rows_read":3,"partition_count":2,"rows":['
        b'{"outcome":"accepted","cols":[],"partition_id":"202601"},'
        b'{"outcome":"accepted","cols":[],"partition_id":"202602"},'
        b'{"outcome":"rejected","code":27,"err":"x","cols":[]}]}'
    )
    assert batch.partition_count == 2
    assert [r.partition_id for r in batch.rows] == ["202601", "202602", None]


def test_partition_fields_absent_are_none() -> None:
    batch = parse_batch_document(
        b'{"outcome":"accepted","rows":[{"outcome":"accepted","cols":[]}]}'
    )
    assert batch.partition_count is None
    assert batch.rows[0].partition_id is None
    row = parse_row_document(b'{"outcome":"accepted","cols":[],"partition_id":"all"}')
    assert row.partition_id == "all"
    # A present zero is a real answer (a key, and no stored row), not absence.
    assert parse_batch_document(b'{"outcome":"accepted","partition_count":0}').partition_count == 0


def test_too_many_parts_is_an_ordinary_rejection() -> None:
    batch = parse_batch_document(
        b'{"outcome":"rejected","code":252,"err":"Too many partitions for single INSERT block",'
        b'"rows_read":2,"rows":[{"outcome":"accepted","cols":[],"partition_id":"1"},'
        b'{"outcome":"accepted","cols":[],"partition_id":"2"}]}'
    )
    assert batch.outcome is chtypes.Outcome.REJECTED
    assert batch.err_code == 252
    assert len(batch.rows) == 2


class _StubNative:
    """Just enough of NativeLibrary for Schema.set_partition_by to reach."""

    def __init__(self, rc: int, err: str) -> None:
        self.rc, self.err = rc, err
        self.calls: list[str] = []

    def schema_partition_by(self, handle: int, expr: str) -> tuple[int, str]:
        self.calls.append(expr)
        return self.rc, self.err

    def schema_free(self, handle: int) -> None:
        pass


class _StubLibrary:
    def __init__(self, native: _StubNative) -> None:
        self._native = native


def _schema_over(native: _StubNative) -> chtypes.Schema:
    """A Schema whose native layer is the stub: the real method, a fake C side."""
    schema = object.__new__(chtypes.Schema)
    schema._library = _StubLibrary(native)
    schema._handle = 1
    schema._mu = threading.Lock()
    schema._filters = weakref.WeakSet()
    schema._blocks = weakref.WeakSet()
    return schema


def test_set_partition_by_follows_the_engine_sign_rule() -> None:
    ok = _StubNative(0, "")
    _schema_over(ok).set_partition_by("toYYYYMM(ts)")
    assert ok.calls == ["toYYYYMM(ts)"]

    # Positive codes are the server's own refusal: 36 BAD_ARGUMENTS is what a
    # non-deterministic key gets on every served line, 549 a key over a type
    # the line will not key on.
    for rc, expr in ((36, "rand()"), (549, "m")):
        with pytest.raises(chtypes.SchemaError) as refused:
            _schema_over(_StubNative(rc, "the server's own message")).set_partition_by(expr)
        assert refused.value.code == rc
        assert "the server's own message" in str(refused.value)

    # Negative codes are declines: -2 a key the server accepts but this build
    # will not evaluate, -1 a guarded exception.
    for rc in (-1, -2):
        with pytest.raises(chtypes.UnsupportedError) as declined:
            _schema_over(_StubNative(rc, "why")).set_partition_by("k")
        assert not isinstance(declined.value, chtypes.SchemaError)


def test_set_partition_by_empty_string_is_passed_through() -> None:
    native = _StubNative(0, "")
    _schema_over(native).set_partition_by("")
    assert native.calls == [""]


# ---------------------------------------------------------------- with artifacts


def _rev6_libraries(registry: chtypes.Registry) -> list[chtypes.Library]:
    libs: list[chtypes.Library] = []
    for version in registry.versions():
        try:
            lib = registry.for_version(version)
        except chtypes.ChtypesError:
            continue
        if lib.abi_revision >= 6:
            libs.append(lib)
    if not libs:
        pytest.skip(
            f"{registry.directory} holds no ABI revision-6 artifact: every case here needs one — "
            f"fetch one with `scripts/fetch.sh` (docs/guides/fetch.md)"
        )
    return libs


BODY = (
    b'{"ts":"2026-01-15 10:00:00","tenant":"a"}\n'
    b'{"ts":"2026-01-20 10:00:00","tenant":"b"}\n'
    b'{"ts":"2026-02-01 10:00:00","tenant":"a"}\n'
)


def test_partition_key_end_to_end(registry: chtypes.Registry) -> None:
    for lib in _rev6_libraries(registry):
        with lib.compile_ddl("ts DateTime, tenant String") as schema:
            plain = schema.rows(chtypes.Format.JSON_EACH_ROW, BODY)
            assert plain.partition_count is None and plain.rows[0].partition_id is None

            schema.set_partition_by("toYYYYMM(ts)")
            keyed = schema.rows(chtypes.Format.JSON_EACH_ROW, BODY)
            assert keyed.outcome is chtypes.Outcome.ACCEPTED, lib.minor
            assert keyed.partition_count == 2, lib.minor
            p0, p1, p2 = (r.partition_id for r in keyed.rows)
            assert p0 and p0 == p1 and p0 != p2, (lib.minor, p0, p1, p2)

            over = schema.rows(
                chtypes.Format.JSON_EACH_ROW, BODY, {"max_partitions_per_insert_block": "1"}
            )
            assert over.outcome is chtypes.Outcome.REJECTED, lib.minor
            assert over.err_code == 252, lib.minor
            assert len(over.rows) == 3, lib.minor

            schema.set_partition_by("")
            cleared = schema.rows(chtypes.Format.JSON_EACH_ROW, BODY)
            assert cleared.partition_count is None and cleared.rows[0].partition_id is None

            # A non-deterministic key is the server's own rejection — 36
            # BAD_ARGUMENTS on every served line — not a decline.
            with pytest.raises(chtypes.SchemaError) as refused:
                schema.set_partition_by("rand()")
            assert refused.value.code == 36, lib.minor
