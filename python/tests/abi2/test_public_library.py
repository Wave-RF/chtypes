"""The public operations over the stub library: marshaling, the error table, the
close guard, and handle lifetimes. The stub echoes its inputs instead of doing
anything ClickHouse, so these prove the binding's plumbing; the meaning of an
answer is the artifact's."""

from __future__ import annotations

import gc
import hashlib
import json
import threading

import pytest

from chtypes import (
    BatchResult,
    CallError,
    DocFlags,
    Format,
    InternalError,
    Outcome,
    SchemaError,
    UnsupportedError,
    UsageError,
)
from chtypes._abi2 import _decls

CREATE = b"CREATE TABLE t (a UInt8) ENGINE = Memory"


def _live(lib, kind: str) -> int:
    """The live count of a handle kind, found by its type's suffix (the library
    names its kinds; a test does not spell them)."""
    (name,) = [k for k in lib.live_handles() if k.endswith(kind)]
    return lib.live_handles()[name]


def echo(raw: bytes) -> dict:
    return json.loads(raw)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_library_facts_come_from_build_info(lib) -> None:
    info = lib.build_info
    assert lib.version == info.clickhouse_version and lib.minor == info.clickhouse_minor
    assert info.raw.startswith(b"{") and info.schema == 1
    assert lib.resolved is None  # an unverified open has no fetch record
    assert lib.path.name.startswith("ok-")


def test_quoting_and_validation_pass_bytes_through_and_return_bytes(lib) -> None:
    doc = echo(lib.validate_type("Nullable(UInt8)"))
    assert doc["fn"] == "chs_type_validate"
    assert doc["args"][0]["sha256"] == sha(b"Nullable(UInt8)")
    # NUL and invalid UTF-8 are counted bytes, never a C string.
    name = b"a\x00b\xff"
    assert echo(lib.quote_identifier(name))["args"][0] == {
        "len": 4,
        "sha256": sha(name),
        "head_hex": name.hex(),
    }
    assert isinstance(lib.quote_identifier_if_needed("x"), bytes)
    assert isinstance(lib.quote_literal(b"it's"), bytes)
    assert isinstance(lib.discover_query(), bytes)


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        ("CHS_REJECTED", SchemaError),
        ("CHS_DECLINED", UnsupportedError),
        ("CHS_INVALID_ARGUMENT", UsageError),
        ("CHS_INTERNAL", InternalError),
    ],
)
def test_each_status_maps_to_its_class_with_the_five_fields_verbatim(lib, status, cls) -> None:
    with pytest.raises(cls) as info:
        lib.validate_type(f"!S:{status}:62:SYNTAX_ERROR:it said no: really".encode())
    err = info.value
    assert type(err) is cls and isinstance(err, CallError)
    assert err.status == _decls.STATUS_BY_NAME[status]
    assert err.ch_code == 62 and err.ch_name == "SYNTAX_ERROR"
    assert err.message == b"it said no: really" and err.column == b""
    assert "it said no: really" in str(err)


def test_the_refusal_and_the_decline_are_peers_never_subtypes() -> None:
    assert not issubclass(SchemaError, UnsupportedError)
    assert not issubclass(UnsupportedError, SchemaError)
    for a in (SchemaError, UnsupportedError, UsageError, InternalError):
        assert issubclass(a, CallError)
        for b in (SchemaError, UnsupportedError, UsageError, InternalError):
            if a is not b:
                assert not issubclass(a, b)


def test_a_status_outside_the_closed_set_is_an_internal_error_naming_its_value() -> None:
    # Rule r3: the status is its unknown(n), and the call still fails.
    with pytest.raises(InternalError, match=r"status unknown\(99\)") as info:
        _decls.Api._check(None, 99, None)  # type: ignore[arg-type]
    assert info.value.status == 99
    _decls.Api._check(None, 0, None)  # type: ignore[arg-type]  # CHS_OK is no error


def test_settings_and_the_zone_reach_the_library_as_one_json_object(lib, monkeypatch) -> None:
    seen: list[bytes | None] = []
    real = lib._api.schema_create

    def spy(create_table, settings):
        seen.append(settings)
        return real(create_table, settings)

    monkeypatch.setattr(lib._api, "schema_create", spy)
    lib.compile_table(CREATE, settings={"a": "b"}, session_timezone="Not/AZone").close()
    lib.compile_table(CREATE).close()
    assert seen[0] == b'{"a":"b","session_timezone":"Not/AZone"}'
    assert seen[1] is None
    with pytest.raises(UsageError):
        lib.compile_table(CREATE, settings={"session_timezone": "UTC"}, session_timezone="UTC")
    assert len(seen) == 2  # refused before any call


def test_rows_marshals_columns_filter_export_and_doc_flags(lib, monkeypatch) -> None:
    calls: list[tuple] = []
    real = lib._api.preview_batch

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(lib._api, "preview_batch", spy)
    with lib.compile_table(CREATE) as schema, schema.compile_filter("a > 1") as flt:
        result = schema.rows(
            Format.JSON_EACH_ROW,
            b'{"a":1}\n',
            columns=["a", b"\xff"],
            row_filter=flt,
            export=Format.CSV,
            doc_flags=DocFlags.VALUES | DocFlags.TRANSFORMS,
        )
        schema.rows(Format.CSV, bytearray(b"1\n"))
    assert isinstance(result, BatchResult)
    # The stub's echo has no `outcome`: the empty spelling, which no vocabulary
    # lists, reads as its unknown(n) member, never as accepted (rule r3).
    assert result.outcome == "" and not result.outcome.known and result.payload is not None
    assert result.outcome is not Outcome.ACCEPTED
    (_, fmt, body, settings, columns, handle, export, flags), plain = calls
    assert fmt == 0 and body == b'{"a":1}\n' and settings is None
    assert json.loads(columns) == [{"name": "a"}, {"name_b64": "/w=="}]
    assert handle is not None and export == int(Format.CSV) and flags == 3
    assert plain[5] is None and plain[6] == -1 and plain[7] == 7  # no export, all groups
    assert plain[2] == b"1\n"  # a bytes-like body is copied to bytes


def test_no_export_means_no_payload(lib) -> None:
    with lib.compile_table(CREATE) as schema:
        assert schema.rows(Format.CSV, b"1\n").payload is None


def test_filter_zone_and_parse_zone_are_distinct_arguments(lib, monkeypatch) -> None:
    created: list[bytes | None] = []
    evaluated: list[bytes | None] = []
    real_create, real_eval = lib._api.filter_create, lib._api.filter_eval_body
    monkeypatch.setattr(
        lib._api,
        "filter_create",
        lambda s, e, p, st: (created.append(st), real_create(s, e, p, st))[1],
    )
    monkeypatch.setattr(
        lib._api,
        "filter_eval_body",
        lambda f, fmt, b, st: (evaluated.append(st), real_eval(f, fmt, b, st))[1],
    )
    with lib.compile_table(CREATE) as schema:
        with schema.compile_filter("a > 1", params={"p": "1"}, session_timezone="Asia/Tokyo") as f:
            f.rows(Format.CSV, b"1", session_timezone="Europe/Paris")
    assert created == [b'{"session_timezone":"Asia/Tokyo"}']
    assert evaluated == [b'{"session_timezone":"Europe/Paris"}']


def test_use_after_close_is_a_usage_error_raised_before_any_c_call(lib, monkeypatch) -> None:
    schema = lib.compile_table(CREATE)
    flt = schema.compile_filter("a > 1")
    block = schema.parse_block(Format.CSV, b"1\n")
    still_open = schema.compile_filter("a > 2")
    for obj in (schema, flt, block):
        obj.close()
        obj.close()  # idempotent
    for api_call in ("schema_describe", "preview_row", "filter_eval_body", "filter_eval_block"):
        monkeypatch.setattr(
            lib._api, api_call, lambda *a, **k: pytest.fail("a closed object reached C")
        )
    with pytest.raises(UsageError, match="Schema is closed"):
        schema.describe()
    with pytest.raises(UsageError, match="Schema is closed"):
        schema.row(Format.CSV, b"1")
    with pytest.raises(UsageError, match="Filter is closed"):
        flt.rows(Format.CSV, b"1")
    with pytest.raises(UsageError, match="Block is closed"):
        still_open.eval(block)
    # A closed filter handed to an open schema's rows() is also refused first.
    with lib.compile_table(CREATE) as other, pytest.raises(UsageError, match="Filter is closed"):
        other.rows(Format.CSV, b"1", row_filter=flt)


def test_closing_the_schema_first_is_legal_and_filters_keep_working(lib) -> None:
    schema = lib.compile_table(CREATE)
    flt = schema.compile_filter("a > 1")
    block = schema.parse_block(Format.CSV, b"1\n")
    schema.close()
    assert flt.rows(Format.CSV, b"1").rows_read == 0
    assert flt.eval(block).rows_read == 0
    flt.close()
    block.close()


def test_close_waits_for_calls_already_inside_and_refuses_later_ones(lib, monkeypatch) -> None:
    entered, release = threading.Event(), threading.Event()
    real = lib._api.schema_describe

    def slow(handle):
        entered.set()
        assert release.wait(5)
        return real(handle)

    monkeypatch.setattr(lib._api, "schema_describe", slow)
    schema = lib.compile_table(CREATE)
    worker = threading.Thread(target=schema.describe)
    worker.start()
    assert entered.wait(5)
    closer = threading.Thread(target=schema.close)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive(), "close must wait for the call already inside the object"
    with pytest.raises(UsageError):  # a later call is refused at once
        schema.describe()
    release.set()
    worker.join(5)
    closer.join(5)
    assert not closer.is_alive() and not worker.is_alive()


def test_calls_on_one_handle_run_in_parallel(lib, monkeypatch) -> None:
    """No lock around a call: two threads are inside one schema at once."""
    barrier = threading.Barrier(2, timeout=5)
    real = lib._api.schema_describe

    def meet(handle):
        barrier.wait()
        return real(handle)

    monkeypatch.setattr(lib._api, "schema_describe", meet)
    schema = lib.compile_table(CREATE)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            schema.describe()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    schema.close()


def test_a_handle_from_another_image_is_the_librarys_to_refuse(stub_copy, monkeypatch) -> None:
    from chtypes import open_unverified

    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.warns(UserWarning):
        a = open_unverified(stub_copy()[0], allow=True)
        b = open_unverified(stub_copy()[0], allow=True)
    assert a is not b
    with a.compile_table(CREATE) as sa, sa.compile_filter("x") as fa, b.compile_table(CREATE) as sb:
        with pytest.raises(UsageError):
            sb.rows(Format.CSV, b"1", row_filter=fa)


def test_abandoned_handles_are_freed_and_every_live_count_returns_to_zero(lib) -> None:
    def churn() -> None:
        schema = lib.compile_table(CREATE)
        flt = schema.compile_filter("a > 1")
        block = schema.parse_block(Format.CSV, b"1\n")
        flt.eval(block)
        flt.rows(Format.CSV, b"1")
        schema.rows(Format.CSV, b"1\n", row_filter=flt)
        # abandoned, never closed

    assert _live(lib, "schema") == 0
    churn()
    gc.collect()
    assert lib.live_handles() == {k: 0 for k in lib.live_handles()}
    assert lib.live_handles()  # the stub reports every kind


def test_closed_handles_count_zero_too(lib) -> None:
    with lib.compile_table(CREATE) as schema:
        assert _live(lib, "schema") == 1
        with schema.compile_filter("a") as flt:
            assert _live(lib, "filter") == 1
            del flt
    assert _live(lib, "schema") == 0


def test_error_codes_are_decoded_once_and_kept_on_success_only(lib, monkeypatch) -> None:
    calls = []

    def fake():
        calls.append(1)
        if len(calls) == 1:
            return b"[not json"
        return b'[{"code": 62, "name": "SYNTAX_ERROR"}]'

    monkeypatch.setattr(lib._api, "error_codes", fake)
    with pytest.raises(InternalError):
        lib.error_codes()
    table = lib.error_codes()
    assert table.name(62) == "SYNTAX_ERROR"
    assert lib.error_codes() is table and len(calls) == 2
