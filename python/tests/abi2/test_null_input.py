"""The null_input decode cells, through the public API, over the generated v2
stub (variant "null-input").

The stub answers scripts/abi-v1/emit/_stubshared.py's NULL_INPUT_DOCS: for a
JSON null and a TSV `\\N` into each of six non-Nullable types (UUID,
FixedString(4), Int32, DateTime, LowCardinality(String), Enum8), the entry
carries `null_input` true; two controls carry it false and absent. Each entry's
expected value is read off its name (a NULL_INPUT_CONTROL_PREFIX name expects
False), and the cell count is the generator's NULL_INPUT_CELL_COUNT, so a
document that lost its cells fails. The other three bindings pin that count as
12; `test_null_input_cell_count_is_pinned` fails here first if the generator
changes it. Without `$CHTYPES_ABI2_STUBS` the stub tests skip loudly by name.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from chtypes import Format, Value, library

from .conftest import REPO_ROOT, stub_path

PINNED_CELL_COUNT = 12  # Go, TypeScript and Rust pin the same number


def _shared():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "abi-v1"))
    from emit import _stubshared

    return _stubshared


@pytest.fixture
def null_input_library(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
):
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target = tmp_path / "null-input.so"
    shutil.copyfile(stub_path(stubs_dir, stubs_manifest["variants"]["null-input"]), target)
    target.chmod(0o755)
    with pytest.warns(UserWarning, match="UNVERIFIED"):
        return library.open_unverified(target, allow=True)


def _check(where: str, cols: tuple[Value, ...], prefix: bytes) -> int:
    cells = 0
    for c in cols:
        want = not c.column.startswith(prefix)
        cells += want
        assert c.null_input is want, (
            f"{where}: column {c.column!r}: null_input = {c.null_input}, want {want}"
        )
    return cells


def test_null_input_cell_count_is_pinned() -> None:
    assert _shared().NULL_INPUT_CELL_COUNT == PINNED_CELL_COUNT


def test_stub_null_input(null_input_library) -> None:
    shared = _shared()
    prefix = shared.NULL_INPUT_CONTROL_PREFIX.encode()
    schema = null_input_library.compile_table(b"CREATE TABLE t (x Int32)")

    row = schema.row(Format.JSON_EACH_ROW, b'{"x":null}')
    assert _check("row", row.columns, prefix) == shared.NULL_INPUT_CELL_COUNT

    batch = schema.rows(Format.TSV, b"\\N\n")
    cells = sum(_check(f"batch.rows[{i}]", r.columns, prefix) for i, r in enumerate(batch.rows))
    assert cells == shared.NULL_INPUT_CELL_COUNT
