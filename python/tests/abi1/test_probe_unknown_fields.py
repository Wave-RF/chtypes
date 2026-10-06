"""PROBE (do not merge): do the RELEASED 1.0.4 decoders tolerate members no 1.0
description names? The "ok-x" stub (scripts/abi-v1/emit/_stubshared.py
PROBE_X_DOCS) answers every document-returning call with a document carrying
unknown members at every object level; its build_info and live_handles carry
unknown members too. Each test decodes one document through the public API and
checks the known fields still decode."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chtypes import Format, library

BODY = b'{"x":1}'


@pytest.fixture
def probe_lib(stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch):
    entry = stubs_manifest["variants"]["ok-x"]
    source = stubs_dir / Path(entry["path"]).name
    target = tmp_path / "ok-x-copy.so"
    shutil.copyfile(source, target)
    target.chmod(0o755)
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.warns(UserWarning, match="UNVERIFIED"):
        return library.open_unverified(str(target), allow=True)


def test_probe_build_info(probe_lib) -> None:
    info = probe_lib.build_info
    assert info.clickhouse_version == "26.8.15.10" and info.channel == "lts" and info.abi == 1
    assert "default_generators" in info.capabilities.features


def test_probe_live_handles(probe_lib) -> None:
    live = probe_lib.live_handles()
    print(f"live handles decoded as {live!r}")
    assert "chs_schema" in live


def test_probe_error_code_table(probe_lib) -> None:
    assert probe_lib.error_codes().name(53) == "TYPE_MISMATCH"


def test_probe_schema_description(probe_lib) -> None:
    d = probe_lib.compile_table(b"CREATE TABLE t (x Int32)").describe()
    assert len(d.columns) == 1 and d.columns[0].name == b"x" and d.columns[0].type == b"Int32"


def test_probe_row(probe_lib) -> None:
    r = probe_lib.compile_table(b"CREATE TABLE t (x Int32)").row(Format.JSON_EACH_ROW, BODY)
    assert r.outcome == "accepted" and len(r.columns) == 1 and r.columns[0].text == b"abc"
    assert r.input_span is not None and r.input_span.len == 3
    assert len(r.computed) == 1 and len(r.transformed) == 1
    assert len(r.unknown_fields) == 1 and len(r.unsupported_settings) == 1


def test_probe_batch(probe_lib) -> None:
    b = probe_lib.compile_table(b"CREATE TABLE t (x Int32)").rows(
        Format.JSON_EACH_ROW, BODY, export=Format.JSON_EACH_ROW
    )
    assert b.outcome == "accepted" and b.rows_read == 1 and len(b.rows) == 1
    assert len(b.rows[0].columns) == 1 and b.engine_rows is not None and len(b.engine_rows) == 1
    assert b.spans is not None and len(b.spans) == 1 and len(b.unconsumed) == 1
    assert b.framing is not None and b.framing.header is not None and len(b.framing.header.names) == 1
    assert b.payload == b'{"s":"abc"}\n'


def test_probe_filter_result(probe_lib) -> None:
    schema = probe_lib.compile_table(b"CREATE TABLE t (x Int32)")
    f = schema.compile_filter("x > 1").rows(Format.JSON_EACH_ROW, BODY)
    assert f.outcome == "ok" and f.rows_read == 2 and len(f.verdicts) == 2
    assert len(f.errors) == 1 and len(f.unsupported_settings) == 1


def test_probe_discovery(probe_lib) -> None:
    d = probe_lib.discover_columns(b"{}")
    assert len(d.columns) == 1 and d.columns[0].declaration == b"c String" and d.columns_sql == b"c String"


# --- the second question: an UNKNOWN ENUM VALUE (stub variants ok-e, ok-e-bi).
# Each probe RECORDS what the released decoder did, as a PROBE-ENUM line; it
# asserts nothing beyond "no crash", because every outcome is a finding.


def _open_variant(stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, variant: str):
    entry = stubs_manifest["variants"][variant]
    source = stubs_dir / Path(entry["path"]).name
    target = tmp_path / f"{variant}-copy.so"
    shutil.copyfile(source, target)
    target.chmod(0o755)
    with pytest.warns(UserWarning, match="UNVERIFIED"):
        return library.open_unverified(str(target), allow=True)


def _report(id_: str, fn) -> None:
    try:
        got = fn()
    except Exception as e:  # noqa: BLE001 - the class is the finding
        print(f"PROBE-ENUM python {id_} => ERROR {type(e).__name__}: {str(e)[:300]}")
        return
    print(f"PROBE-ENUM python {id_} => {got}")


def test_probe_unknown_enum_values(stubs_dir, stubs_manifest, tmp_path, clean_process, monkeypatch) -> None:
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    try:
        bi = _open_variant(stubs_dir, stubs_manifest, tmp_path, "ok-e-bi")
        print(f"PROBE-ENUM python build_info.capabilities => {bi.build_info.capabilities!r}")
    except Exception as e:  # noqa: BLE001
        print(f"PROBE-ENUM python build_info.capabilities => ERROR {type(e).__name__}: {str(e)[:300]}")

    lib = _open_variant(stubs_dir, stubs_manifest, tmp_path, "ok-e")
    _report("status.unknown", lambda: lib.validate_type(b"!U:"))
    _report(
        "describe.default_kind",
        lambda: repr(lib.compile_table(b"!E:describe.default_kind").describe().columns[0].default_kind),
    )
    clean = lib.compile_table(b"CREATE TABLE t (x Int32)")
    _report("describe.control", lambda: repr(clean.describe().columns[0].default_kind))

    def row(id_, pick):
        _report(id_, lambda: pick(clean.row(Format.JSON_EACH_ROW, b"!E:" + id_.encode())))

    row("row.outcome", lambda r: f"outcome={r.outcome!r}")
    row("row.cols.src", lambda r: f"source={r.columns[0].source!r} is_stored={r.columns[0].is_stored!r}")
    row("row.transformed.reason", lambda r: f"reason={r.transformed[0].reason!r} lossy={r.transformed[0].lossy!r}")
    row("row.verdict", lambda r: f"verdict={r.verdict!r}")
    _report(
        "row.control",
        lambda: (lambda r: f"outcome={r.outcome!r} source={r.columns[0].source!r}")(
            clean.row(Format.JSON_EACH_ROW, b"{}")
        ),
    )

    def batch(id_, pick):
        _report(
            id_,
            lambda: pick(clean.rows(Format.JSON_EACH_ROW, b"!E:" + id_.encode(), export=Format.JSON_EACH_ROW)),
        )

    batch("batch.outcome", lambda b: f"outcome={b.outcome!r}")
    batch("batch.rows.outcome", lambda b: f"outcome={b.rows[0].outcome!r}")
    batch(
        "batch.rows.cols.src",
        lambda b: f"source={b.rows[0].columns[0].source!r} is_stored={b.rows[0].columns[0].is_stored!r}",
    )
    batch(
        "batch.transformed.reason",
        lambda b: f"reason={b.transformed[0].reason!r} lossy={b.transformed[0].lossy!r}",
    )
    batch("batch.framing.container", lambda b: f"framing={b.framing!r}")
    batch("batch.control", lambda b: f"outcome={b.outcome!r}")

    flt = clean.compile_filter("x > 1")
    for id_ in ("filter.outcome", "filter.verdicts", "filter.control"):
        body = b"{}" if id_ == "filter.control" else b"!E:" + id_.encode()
        _report(
            id_,
            lambda body=body: (lambda f: f"outcome={f.outcome!r} verdicts={f.verdicts!r}")(
                flt.rows(Format.JSON_EACH_ROW, body)
            ),
        )

    _report("discovery.default_kind", lambda: repr(lib.discover_columns(b"!E:discovery.default_kind").columns))
