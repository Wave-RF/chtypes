"""Lock schema 3 (`_lock.py`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._errors import ArtifactCorruptError, ArtifactPinnedError
from chtypes._ocifetch._lock import LockPin, load_lock, new_lock, save_lock


def _pin(**overrides) -> LockPin:
    base = dict(
        version="26.8.15.10",
        build="20261001.183455",
        manifest="sha256:" + "a" * 64,
        layer="sha256:" + "b" * 64,
        bundle="sha256:" + "c" * 64,
    )
    base.update(overrides)
    return LockPin(**base)


def test_new_lock_has_v1_schema_and_abi() -> None:
    lock = new_lock()
    assert lock.schema == C.LOCK_SCHEMA == 3
    assert lock.abi == C.ABI_GENERATION == 1


def test_with_pin_and_pin_for_round_trip() -> None:
    lock = new_lock().with_pin("26.8", "linux-arm64", _pin())
    assert lock.pin_for("26.8", "linux-arm64") == _pin()
    assert lock.pin_for("26.8", "darwin-arm64") is None
    assert "linux-arm64" in lock.platforms


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    lock = new_lock().with_pin("26.8", "linux-arm64", _pin())
    path = tmp_path / "chtypes.lock"
    save_lock(path, lock)
    loaded = load_lock(path)
    assert loaded == lock


def test_save_lock_is_valid_json_matching_the_schema_shape(tmp_path: Path) -> None:
    lock = new_lock().with_pin("26.8", "linux-arm64", _pin(index="sha256:" + "d" * 64))
    path = tmp_path / "chtypes.lock"
    save_lock(path, lock)
    doc = json.loads(path.read_text())
    assert doc["schema"] == 3
    assert doc["abi"] == 1
    assert doc["requests"]["26.8"]["linux-arm64"]["index"] == "sha256:" + "d" * 64


def test_load_lock_refuses_v0_schema(tmp_path: Path) -> None:
    path = tmp_path / "chtypes.lock"
    path.write_text(json.dumps({"schema": 2, "artifacts": {}}))
    with pytest.raises(ArtifactPinnedError, match="Re-lock"):
        load_lock(path)


def test_load_lock_refuses_wrong_abi(tmp_path: Path) -> None:
    path = tmp_path / "chtypes.lock"
    path.write_text(
        json.dumps({"schema": 3, "abi": 2, "platforms": ["linux-arm64"], "requests": {}})
    )
    with pytest.raises(ArtifactPinnedError, match="ABI"):
        load_lock(path)


def test_load_lock_refuses_platform_not_in_platforms_list(tmp_path: Path) -> None:
    path = tmp_path / "chtypes.lock"
    doc = {
        "schema": 3,
        "abi": 1,
        "platforms": ["linux-arm64"],
        "requests": {"26.8": {"darwin-arm64": _pin().to_json()}},
    }
    path.write_text(json.dumps(doc))
    with pytest.raises(ArtifactCorruptError, match="not in its own platforms"):
        load_lock(path)


def test_lock_pin_index_is_optional() -> None:
    pin = _pin()
    assert pin.index is None
    doc = pin.to_json()
    assert "index" not in doc
    assert LockPin.from_json(doc) == pin


def test_with_pin_preserves_other_requests() -> None:
    lock = new_lock().with_pin("26.8", "linux-arm64", _pin())
    lock2 = lock.with_pin("26.9", "linux-amd64", _pin(version="26.9.1.1"))
    assert lock2.pin_for("26.8", "linux-arm64") is not None
    assert lock2.pin_for("26.9", "linux-amd64") is not None


def test_save_lock_atomic_leaves_no_temp_file_on_success(tmp_path: Path) -> None:
    lock = new_lock().with_pin("26.8", "linux-arm64", _pin())
    path = tmp_path / "chtypes.lock"
    save_lock(path, lock)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "chtypes.lock"]
    assert leftovers == []
