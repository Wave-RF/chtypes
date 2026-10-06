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
