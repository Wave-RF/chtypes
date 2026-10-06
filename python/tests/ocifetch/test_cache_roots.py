"""Public issue #486, the behavior fixes: modes that follow the umask, one root
order, read-only lookups that create nothing, and the 0.x hint.

Every fault here is made by a real write or a real install, never by a stubbed
reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from chtypes import FetchOptions, Registry
from chtypes import errors as public_errors
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import TrustedKey
from chtypes._ocifetch._ensure import (
    Options,
    Request,
    ensure,
    list_installed,
    missing_notes,
    resolve_installed,
    verify_installed,
)
from chtypes._ocifetch._errors import ArtifactMissingError
from chtypes._ocifetch._layout import VerifiedRecord

from ._registry_support import build_tree

HINT_TAIL = (
    "holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at "
    "${XDG_CACHE_HOME:-~/.cache}/chtypes/v1"
)


@pytest.mark.parametrize("umask", [0o022, 0o077], ids=["umask-022", "umask-077"])
def test_install_modes_follow_the_umask(tmp_path: Path, umask: int) -> None:
    """Every directory and file an install creates is 0777 and 0666 less the
    umask, so at umask 022 another uid can read the cache. The 022 case is the
    one that discriminates: `mkstemp` and `mkdtemp` give 0600 and 0700."""
    tree = build_tree(tmp_path / "registry")
    cache = tmp_path / "cache"
    options = Options(
        bases=(tree.base_url,),
        cache_dir=cache,
        system_dirs=(),
        platform="linux-arm64",
        trusted_keys=(TrustedKey(keyid=tree.keyid, public_key=tree.public_key),),
    )
    old = os.umask(umask)
    try:
        resolved = ensure(Request("26.8"), options)
    finally:
        os.umask(old)
    seen = []
    for dirpath, dirnames, filenames in os.walk(cache):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            mode = stat.S_IMODE(path.lstat().st_mode)
            want = (0o777 if path.is_dir() else 0o666) & ~umask
            assert mode == want, f"{path} is {mode:03o}, want {want:03o}"
            seen.append(path)
    record = resolved.dir / C.CACHE_VERIFIED_RECORD
    assert {resolved.dir, record, resolved.library_path} <= set(seen)


def _write_record(root: Path, version: str, build: str) -> Path:
    """One record installed by hand under `root`, as any binding leaves it."""
    library = f"library {version} {build}".encode()
    manifest = hashlib.sha256(f"{root}{version}{build}".encode()).hexdigest()
    record = VerifiedRecord(
        manifest=f"sha256:{manifest}",
        layer="sha256:" + "c" * 64,
        platform="linux-arm64",
        version=version,
        build=build,
        channel=None,
        predicate={"clickhouse_version": version, "build": build},
        signed_by=None,
        library="lib.so",
        library_sha256=hashlib.sha256(library).hexdigest(),
        library_bytes=len(library),
    )
    entry = root / C.CACHE_UNPACKED_DIR / manifest
    entry.mkdir(parents=True)
    (entry / "lib.so").write_bytes(library)
    (entry / C.CACHE_VERIFIED_RECORD).write_text(json.dumps(record.to_json()))
    return entry


@pytest.mark.parametrize(
    ("cache_version", "cache_build", "sys_version", "sys_build", "winner"),
    [
        ("26.8.1.1", "20260801.000001", "26.8.2.1", "20260802.000001", "system"),
        ("26.8.1.1", "20260801.000001", "26.8.1.1", "20260801.000002", "system"),
        ("26.8.2.1", "20260802.000001", "26.8.1.1", "20260801.000001", "cache"),
        ("26.8.1.1", "20260801.000001", "26.8.1.1", "20260801.000001", "cache"),
    ],
)
def test_resolve_installed_is_the_newest_across_roots(
    tmp_path: Path, cache_version, cache_build, sys_version, sys_build, winner
) -> None:
    """The cache and the system dirs are one search: the newest (version,
    build) wins, and a tie goes to the earlier root."""
    cache, system = tmp_path / "cache", tmp_path / "system"
    cache_entry = _write_record(cache, cache_version, cache_build)
    sys_entry = _write_record(system, sys_version, sys_build)
    options = Options(cache_dir=cache, system_dirs=(system,), platform="linux-arm64")
    got = resolve_installed(Request("26.8"), "linux-arm64", options)
    assert got is not None
    want_dir, want_source = (
        (sys_entry, f"system:{system}") if winner == "system" else (cache_entry, "cache")
    )
    assert (got.dir, got.source) == (want_dir, want_source)
    options.offline = True
    assert ensure(Request("26.8"), options).dir == want_dir


def _tree(root: Path) -> dict[str, int] | None:
    if not root.exists():
        return None
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            out[str(path)] = path.lstat().st_size
    return out


def _zero_x_registry(root: Path) -> None:
    """What a 0.x install left behind: `<minor>/manifest.json`, no `oci-layout`."""
    for rel, body in {
        "26.1/manifest.json": "{}",
        "26.1/libchtypes.so": "0.x library",
        "patches/26.1.3.4/manifest.json": "{}",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def test_read_only_lookups_create_nothing(tmp_path: Path) -> None:
    """resolve_installed, list_installed, verify_installed and an offline ensure
    create nothing, whether the cache is missing or holds a 0.x registry."""
    zero_x = tmp_path / "zero-x"
    _zero_x_registry(zero_x)
    no_system = tmp_path / "no-such-system-dir"
    for cache in (tmp_path / "no-such-cache", zero_x):
        before = _tree(cache)
        options = Options(cache_dir=cache, system_dirs=(no_system,), platform="linux-arm64")
        assert resolve_installed(Request("26.1"), "linux-arm64", options) is None
        assert list_installed(options) == []
        assert verify_installed(options) == []
        options.offline = True
        with pytest.raises(ArtifactMissingError):
            ensure(Request("26.1"), options)
        assert _tree(cache) == before
        assert not no_system.exists()


def test_missing_carries_the_zero_x_hint(tmp_path: Path) -> None:
    """A MISSING answer from a cache that holds a 0.x registry says so and names
    the v1 root; a 1.x cache without an oci-layout and an empty one carry none."""
    zero_x = tmp_path / "zero-x"
    _zero_x_registry(zero_x)
    options = Options(cache_dir=zero_x, system_dirs=(), platform="linux-arm64", offline=True)
    with pytest.raises(ArtifactMissingError) as raised:
        ensure(Request("26.1"), options)
    assert f"{zero_x} {HINT_TAIL}" in str(raised.value)
    notes = missing_notes(options)
    assert len(notes) == 1 and notes[0].startswith(f"{zero_x} {HINT_TAIL}")

    one_x = tmp_path / "one-x"
    _write_record(one_x, "26.8.1.1", "20260801.000001")
    empty = tmp_path / "empty"
    empty.mkdir()
    for cache in (one_x, empty, tmp_path / "absent"):
        assert missing_notes(Options(cache_dir=cache)) == []

    registry = Registry(fetch=FetchOptions(cache_dir=zero_x, system_dirs=()), autofetch=False)
    with pytest.raises(public_errors.ArtifactMissingError) as raised:
        registry.for_version("26.1")
    assert f"{zero_x} {HINT_TAIL}" in str(raised.value)
