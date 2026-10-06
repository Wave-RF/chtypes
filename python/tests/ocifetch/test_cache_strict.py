"""Public issue #486: every cache fault, in the default mode (absent, plus a
warning naming the path) and in strict mode (`CacheUnusableError` naming the
path and the reason, and no fall-through to a system dir), and the class of a
failed write. Every fault is made by a real chmod, a real write or a real
install, and each chmod is proved to have taken effect before the case runs.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from chtypes import CacheUnusableError, FetchOptions, Registry
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
from chtypes._ocifetch._errors import CacheUnusableError as FetchCacheUnusable

from ._registry_support import build_tree
from .test_cache_roots import _write_record, _zero_x_registry

ROOT_SKIP = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="running as root, which a mode cannot deny"
)


def _denied(path: Path) -> None:
    """The positive control: the chmod took effect for this process."""
    with pytest.raises(PermissionError):
        if path.is_dir():
            os.listdir(path)
        else:
            path.read_bytes()


def _chmod(request: pytest.FixtureRequest, path: Path, mode: int) -> None:
    restore = 0o755 if path.is_dir() else 0o644
    os.chmod(path, mode)
    request.addfinalizer(lambda: os.chmod(path, restore))


def _root_000(request, cache: Path, _entry: Path) -> Path:
    _chmod(request, cache, 0)
    _denied(cache)
    return cache


def _unpacked_000(request, cache: Path, _entry: Path) -> Path:
    p = cache / C.CACHE_UNPACKED_DIR
    _chmod(request, p, 0)
    _denied(p)
    return p


def _entry_000(request, _cache: Path, entry: Path) -> Path:
    _chmod(request, entry, 0)
    _denied(entry)
    return entry


def _record_000(request, _cache: Path, entry: Path) -> Path:
    p = entry / C.CACHE_VERIFIED_RECORD
    _chmod(request, p, 0)
    _denied(p)
    return p


def _record_garbage(_request, cache: Path, entry: Path) -> Path:
    p = entry / C.CACHE_VERIFIED_RECORD
    p.write_text("not json {")
    assert not (cache / "blobs").exists(), "positive control: no blobs to re-verify from"
    return p


def _root_is_a_file(_request, cache: Path, _entry: Path) -> Path:
    shutil.rmtree(cache)
    cache.write_text("not a cache")
    return cache


def _layout_0x(_request, cache: Path, _entry: Path) -> Path:
    shutil.rmtree(cache)
    _zero_x_registry(cache)
    return cache


FAULTS: list[tuple[str, str, bool, Callable[..., Path]]] = [
    ("root-000", "unreadable_root", True, _root_000),
    ("unpacked-000", "unreadable_root", True, _unpacked_000),
    ("entry-000", "unreadable_entry", True, _entry_000),
    ("record-000", "unreadable_entry", True, _record_000),
    ("record-garbage-noblobs", "unacceptable_record", False, _record_garbage),
    ("root-is-a-file", "not_a_directory", True, _root_is_a_file),
    ("layout-0x", "layout_0x", False, _layout_0x),
]


@ROOT_SKIP
@pytest.mark.parametrize(("name", "reason", "warns", "apply"), FAULTS, ids=[f[0] for f in FAULTS])
@pytest.mark.parametrize("with_system", [False, True], ids=["no-system-dir", "system-dir"])
def test_every_cache_fault_in_both_modes(
    request, tmp_path: Path, name, reason, warns, apply, with_system: bool
) -> None:
    cache, system = tmp_path / "cache", tmp_path / "system"
    entry = _write_record(cache, "26.8.1.1", "20260801.000001")
    sys_entry = _write_record(system, "26.8.1.1", "20260801.000001")
    path = apply(request, cache, entry)
    warning = f"{path} could not be read ("
    systems = (system,) if with_system else ()

    # Default mode: the fault reads as absent, with a warning for K1/K2.
    options = Options(
        cache_dir=cache, system_dirs=systems, platform="linux-arm64", strict_cache=False
    )
    got = resolve_installed(Request("26.8"), "linux-arm64", options)
    if with_system:
        assert got is not None and got.dir == sys_entry
        assert (warning in "\n".join(got.warnings)) == warns
    else:
        assert got is None
    assert (warning in "\n".join(missing_notes(options))) == warns
    list_installed(options)
    verify_installed(options)

    # Strict mode: CacheUnusableError, the same with or without a system dir.
    options.strict_cache = True
    calls = {
        "resolve_installed": lambda: resolve_installed(Request("26.8"), "linux-arm64", options),
        "list_installed": lambda: list_installed(options),
        "verify_installed": lambda: verify_installed(options),
        "ensure(offline)": lambda: ensure(
            Request("26.8"),
            Options(
                cache_dir=cache,
                system_dirs=systems,
                platform="linux-arm64",
                strict_cache=True,
                offline=True,
            ),
        ),
    }
    for label, call in calls.items():
        with pytest.raises(FetchCacheUnusable) as raised:
            call()
        e = raised.value
        assert (e.code, e.reason, e.path) == ("CHTYPES_CACHE_UNUSABLE", reason, str(path)), label
        assert f"{path} is unusable as a cache: {reason}" in str(e), label
        assert C.ERROR_EXIT_CODES[e.code] == 9
        assert (e.os_error is not None) == warns, label


@pytest.mark.parametrize(
    ("env", "option", "strict"),
    [
        ("", None, False),
        ("1", None, True),
        ("0", None, False),
        ("1", False, False),
        ("", True, True),
    ],
)
def test_strict_comes_from_the_option_then_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, option: bool | None, strict: bool
) -> None:
    cache = tmp_path / "cache"
    _zero_x_registry(cache)
    monkeypatch.setenv(C.ENV_CACHE_STRICT_NAME, env)
    options = Options(cache_dir=cache, system_dirs=(), strict_cache=option)
    if strict:
        with pytest.raises(FetchCacheUnusable):
            list_installed(options)
    else:
        assert list_installed(options) == []


@ROOT_SKIP
def test_a_strict_system_dir_fault_is_an_error(request, tmp_path: Path) -> None:
    cache, system = tmp_path / "cache", tmp_path / "system"
    _write_record(cache, "26.8.1.1", "20260801.000001")
    _write_record(system, "26.8.1.1", "20260801.000001")
    options = Options(cache_dir=cache, system_dirs=(tmp_path / "absent", system), strict_cache=True)
    assert resolve_installed(Request("26.8"), "linux-arm64", options) is not None
    _chmod(request, system, 0)
    _denied(system)
    with pytest.raises(FetchCacheUnusable) as raised:
        resolve_installed(Request("26.8"), "linux-arm64", options)
    assert (raised.value.path, raised.value.reason) == (str(system), "unreadable_root")


@ROOT_SKIP
def test_a_failed_write_is_cache_unusable(request, tmp_path: Path) -> None:
    """A write the fetch layer needed that failed is `CacheUnusableError` with
    reason `unwritable` in the default mode too, never a raw `OSError`."""
    tree = build_tree(tmp_path / "registry")
    cache = tmp_path / "cache"
    cache.mkdir()
    _chmod(request, cache, 0o555)
    with pytest.raises(PermissionError):
        (cache / "probe").mkdir()
    options = Options(
        bases=(tree.base_url,),
        cache_dir=cache,
        system_dirs=(),
        platform="linux-arm64",
        trusted_keys=(TrustedKey(keyid=tree.keyid, public_key=tree.public_key),),
    )
    with pytest.raises(FetchCacheUnusable) as raised:
        ensure(Request("26.8"), options)
    e = raised.value
    assert (e.reason, e.os_error) == ("unwritable", "EACCES")
    assert e.path.startswith(str(cache))


def test_the_public_class_carries_the_path_and_the_reason(tmp_path: Path) -> None:
    zero_x = tmp_path / "zero-x"
    _zero_x_registry(zero_x)
    registry = Registry(
        fetch=FetchOptions(cache_dir=zero_x, system_dirs=(), strict_cache=True), autofetch=False
    )
    with pytest.raises(CacheUnusableError) as raised:
        registry.for_version("26.1")
    e = raised.value
    assert (e.code, e.path, e.reason, e.os_error) == (
        "CHTYPES_CACHE_UNUSABLE",
        str(zero_x),
        "layout_0x",
        None,
    )
    with pytest.raises(CacheUnusableError):
        registry.installed()
