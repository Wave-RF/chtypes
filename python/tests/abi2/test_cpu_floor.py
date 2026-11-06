"""The CPU floor, loader step 2b (spec/abi-v2/docs.md, "Loading"): a library below
its floor opens, nothing of it has run, and the loader refuses it as an ordinary
error, carrying the library's three answers verbatim. They are spelled out here,
not taken from the code under test.

The stubs plant it on every leg with no emulation
(scripts/abi-v1/emit/_stubshared.py, CPU_FLOOR). In cpu-floor-refused the setup
call answers CHS_INTERNAL, so a loader that reached it reads as an
`InternalError`, never as this refusal.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chtypes import (
    CODE_ARTIFACT_INCOMPATIBLE,
    CODE_CPU_UNSUPPORTED,
    ArtifactError,
    ArtifactIncompatibleError,
    library,
)
from chtypes._abi2 import _decls, _errmap, _loader

from .conftest import stub_path

WANT = "x86-64-v3+pclmul"
GOT = "avx2,bmi2"
SENTENCE = (
    "this CPU cannot run this library: missing avx2,bmi2 (the 26.8 linux-amd64 build "
    "requires x86-64-v3+pclmul). Use a CPU at or above that floor."
)


def test_the_code_is_the_generated_table() -> None:
    """With no library: the code, and the class that still catches it."""
    assert CODE_CPU_UNSUPPORTED == "CHTYPES_CPU_UNSUPPORTED"
    assert _errmap.loader_error_class("cpu_floor") is ArtifactIncompatibleError
    assert _errmap.loader_error_code("cpu_floor") == CODE_CPU_UNSUPPORTED
    assert _errmap.loader_error_code("dlopen") is None
    assert ArtifactIncompatibleError(reason="dlopen").code == CODE_ARTIFACT_INCOMPATIBLE
    assert ArtifactIncompatibleError.code == CODE_ARTIFACT_INCOMPATIBLE


def _copy(stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, variant: str) -> Path:
    target = tmp_path / f"{variant}.so"
    shutil.copyfile(stub_path(stubs_dir, stubs_manifest["variants"][variant]), target)
    return target


def test_refused_through_a_load(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target = _copy(stubs_dir, stubs_manifest, tmp_path, "cpu-floor-refused")
    with pytest.raises(ArtifactIncompatibleError) as info, pytest.warns(UserWarning):
        library.open_unverified(target, allow=True)
    err = info.value
    assert isinstance(err, ArtifactError)
    assert err.code == "CHTYPES_CPU_UNSUPPORTED"
    assert err.reason == "cpu_floor"
    assert (err.want, err.got) == (WANT, GOT)
    assert err.path == str(target)
    assert str(err) == f"{target}: {SENTENCE}"


def test_refused_under_a_signed_statement(stubs_dir: Path, stubs_manifest: dict) -> None:
    """The verified path reaches step 2b the same way."""
    entry = stubs_manifest["variants"]["cpu-floor-refused"]
    with pytest.raises(ArtifactIncompatibleError) as info:
        _loader.open(stub_path(stubs_dir, entry), entry["predicate"])
    err = info.value
    assert (err.code, err.reason) == (CODE_CPU_UNSUPPORTED, "cpu_floor")
    assert (err.want, err.got) == (WANT, GOT)
    assert SENTENCE in str(err)


def test_absent_is_the_fingerprint_refusal(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """A library from before the check is today's fingerprint refusal: not
    cpu_floor, and not a missing symbol."""
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target = _copy(stubs_dir, stubs_manifest, tmp_path, "cpu-floor-absent")
    with pytest.raises(ArtifactIncompatibleError) as info, pytest.warns(UserWarning):
        library.open_unverified(target, allow=True)
    err = info.value
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE
    assert err.reason == "fingerprint"
    assert err.want == _decls.CHS_ABI_FINGERPRINT
    assert err.got == "sha256:" + "0" * 64


def test_the_ok_stub_opens(stubs_dir: Path, stubs_manifest: dict) -> None:
    entry = stubs_manifest["variants"]["ok"]
    result = _loader.open(stub_path(stubs_dir, entry), entry["predicate"])
    assert result.build_info["abi_fingerprint"] == _decls.CHS_ABI_FINGERPRINT
