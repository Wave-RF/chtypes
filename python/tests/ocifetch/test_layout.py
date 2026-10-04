"""The cache: OCI layout paths, `verified.json`, the index.json race-reapply
loop (`_layout.py`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._layout import (
    VerifiedRecord,
    list_verified_records,
    read_verified_record,
    resolve_cache_root,
    search_roots,
    unpacked_dir_for,
    update_index_json,
    write_verified_install,
)


def test_resolve_cache_root_uses_explicit_override() -> None:
    assert resolve_cache_root("/explicit/path") == Path("/explicit/path")


def test_resolve_cache_root_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(C.ENV_CACHE_NAME, "/from/env")
    assert resolve_cache_root() == Path("/from/env")


def test_resolve_cache_root_default_uses_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(C.ENV_CACHE_NAME, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert resolve_cache_root() == tmp_path / "xdg" / "chtypes" / "v1"


def test_search_roots_cache_then_system_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(C.ENV_CACHE_NAME, "/my/cache")
    roots = search_roots()
    assert roots[0] == Path("/my/cache")
    assert roots[1:] == tuple(Path(d) for d in C.SYSTEM_CACHE_DIRS)


def test_unpacked_dir_for_rejects_non_hex() -> None:
    with pytest.raises(ValueError, match="64-hex"):
        unpacked_dir_for(Path("/cache"), "not-hex")


def _record(**overrides) -> VerifiedRecord:
    base = dict(
        manifest="sha256:" + "a" * 64,
        layer="sha256:" + "b" * 64,
        bundle="sha256:" + "c" * 64,
        platform="linux-arm64",
        version="26.8.15.10",
        build="20261001.183455",
        channel="lts",
        predicate={"library": "libchtypes.so", "library_sha256": "d" * 64, "library_bytes": 10},
        signed_by="deb275922dbff76e",
        library="libchtypes.so",
        library_sha256="d" * 64,
        library_bytes=10,
    )
    base.update(overrides)
    return VerifiedRecord(**base)


def test_write_verified_install_and_read_back(tmp_path: Path) -> None:
    record = _record()
    unpacked_tmp = tmp_path / "scratch" / "unpacked"
    unpacked_tmp.mkdir(parents=True)
    (unpacked_tmp / "libchtypes.so").write_bytes(b"fake lib")
    dest = write_verified_install(
        tmp_path, record.manifest, record, unpacked_tmp_dir=str(unpacked_tmp)
    )
    assert dest == tmp_path / C.CACHE_UNPACKED_DIR / ("a" * 64)
    assert (dest / "libchtypes.so").read_bytes() == b"fake lib"
    loaded = read_verified_record(dest)
    assert loaded == record


def test_read_verified_record_missing_returns_none(tmp_path: Path) -> None:
    assert read_verified_record(tmp_path / "nothing-here") is None


def test_list_verified_records_across_roots(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    system = tmp_path / "system"
    for root, hexdigit in ((cache, "a"), (system, "b")):
        unpacked_tmp = tmp_path / f"scratch-{hexdigit}"
        unpacked_tmp.mkdir()
        (unpacked_tmp / "libchtypes.so").write_bytes(b"x")
        record = _record(manifest=f"sha256:{hexdigit * 64}")
        write_verified_install(root, record.manifest, record, unpacked_tmp_dir=str(unpacked_tmp))
    found = list_verified_records([cache, system])
    assert len(found) == 2
    assert {r.manifest for _d, r in found} == {"sha256:" + "a" * 64, "sha256:" + "b" * 64}


def test_write_verified_install_is_idempotent_for_same_digest(tmp_path: Path) -> None:
    record = _record()
    for _ in range(2):
        unpacked_tmp = tmp_path / "scratch" / f"try-{_}"
        unpacked_tmp.mkdir(parents=True)
        (unpacked_tmp / "libchtypes.so").write_bytes(b"fake lib")
        dest = write_verified_install(
            tmp_path, record.manifest, record, unpacked_tmp_dir=str(unpacked_tmp)
        )
    assert (dest / "libchtypes.so").read_bytes() == b"fake lib"


def _append_digest(digest: str):  # noqa: ANN202
    return lambda doc: {**doc, "manifests": [*doc["manifests"], {"digest": digest}]}


def test_update_index_json_creates_and_merges(tmp_path: Path) -> None:
    update_index_json(tmp_path, _append_digest("a"))
    update_index_json(tmp_path, _append_digest("b"))
    doc = json.loads((tmp_path / "index.json").read_text())
    digests = {m["digest"] for m in doc["manifests"]}
    assert digests == {"a", "b"}


def test_update_index_json_race_reapply(tmp_path: Path) -> None:
    """PLAN §3.2 'index-race-reapply': a competing writer renames its own
    index.json between our temp-write and our rename. Our own entry must
    still be present afterwards (we notice it is missing on re-read and
    re-apply it on top of the latest state)."""
    index_path = tmp_path / "index.json"

    def competing_writer() -> None:
        # Simulates another process's rename landing in the gap between our
        # temp-write and our own rename.
        racer_doc = {"schemaVersion": 2, "manifests": [{"digest": "from-racer"}]}
        index_path.write_text(json.dumps(racer_doc))

    update_index_json(
        tmp_path,
        _append_digest("mine"),
        before_rename=competing_writer,
    )
    doc = json.loads(index_path.read_text())
    digests = {m["digest"] for m in doc["manifests"]}
    assert "mine" in digests


def test_record_is_the_canonical_object() -> None:
    doc = _record(channel=None, signed_by=None, bundle=None).to_json()
    assert set(doc) == {
        "schema",
        "platform",
        "version",
        "channel",
        "build",
        "library",
        "library_sha256",
        "library_bytes",
        "digests",
        "signed_by",
        "predicate",
    }
    assert set(doc["digests"]) == {"index", "manifest", "layer", "bundle", "bundle_manifest"}
    assert doc["channel"] is None and doc["signed_by"] is None and doc["digests"]["bundle"] is None
    assert VerifiedRecord.from_json(doc) == _record(channel=None, signed_by=None, bundle=None)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema=2),
        lambda d: d.pop("signed_by"),
        lambda d: d["digests"].pop("bundle_manifest"),
        lambda d: d.update(library="/etc/passwd"),
        lambda d: d.update(library="../lib.so"),
        lambda d: d.update(library_sha256="abc"),
        lambda d: d.update(library_bytes=True),
        lambda d: d.update(platform="plan9-mips"),
        lambda d: d.update(predicate=[]),
    ],
)
def test_from_json_refuses_what_the_schema_refuses(mutate) -> None:  # noqa: ANN001
    doc = _record().to_json()
    mutate(doc)
    with pytest.raises(ValueError):
        VerifiedRecord.from_json(doc)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "",
        '{"platform":"linux-arm64","version":"26.8.15.10","build":"1","manifest_digest":"x"}',
        '{"schema":1,"schema":1}',
    ],
)
def test_unreadable_record_reads_as_absent(tmp_path: Path, content: str) -> None:
    (tmp_path / C.CACHE_VERIFIED_RECORD).write_text(content)
    assert read_verified_record(tmp_path) is None


def test_write_verified_install_replaces_an_unreadable_record(tmp_path: Path) -> None:
    record = _record()
    stale = unpacked_dir_for(tmp_path, "a" * 64)
    stale.mkdir(parents=True)
    (stale / "manifest.json").write_text("{}")
    (stale / C.CACHE_VERIFIED_RECORD).write_text('{"platform":"x"}')
    fresh = tmp_path / "scratch" / "unpacked"
    fresh.mkdir(parents=True)
    (fresh / "libchtypes.so").write_bytes(b"fresh")
    dest = write_verified_install(tmp_path, record.manifest, record, unpacked_tmp_dir=str(fresh))
    assert read_verified_record(dest) == record
    assert (dest / "libchtypes.so").read_bytes() == b"fresh"
    assert not (dest / "manifest.json").exists()
