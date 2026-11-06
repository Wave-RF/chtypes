"""Rule r6's fingerprint refusal (spec/abi-v2/docs.md). A library whose
fingerprint is not this SDK's is refused as CHTYPES_ARTIFACT_INCOMPATIBLE,
naming both. While the description is unstable (a 2.0.0-dev SDK) the message is
exactly the rule's; once it is locked, which this SDK's is, the refusal keeps
the loader's own message, never the dev one. The dev side is selected
explicitly (the generated stability, patched for one test); the locked side is
this SDK as built. The messages are spelled out here, not taken from the code
under test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chtypes import CODE_ARTIFACT_INCOMPATIBLE, ArtifactIncompatibleError, library
from chtypes._abi2 import _decls, _loader

from .conftest import stub_path

OTHER = "sha256:" + "0" * 64


def r6_message(sdk: str, lib: str) -> str:
    return f"this SDK speaks dev fingerprint {sdk}; the library has {lib} — update your dev SDK"


def loader_message(path: str, sdk: str, lib: str) -> str:
    """The loader's own refusal, the locked SDK's: reason, path, got, want."""
    return f"fingerprint: {path} (got {lib!r}, want {sdk!r})"


def _locked() -> None:
    assert _decls.CHS_ABI_STABILITY == "locked", (
        f"the description is {_decls.CHS_ABI_STABILITY!r}; this test pins the locked SDK's refusal"
    )


def _load_fingerprint_other(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, variant: str
) -> tuple[Path, ArtifactIncompatibleError]:
    target = tmp_path / f"{variant}.so"
    entry = stubs_manifest["variants"][variant]
    assert entry["reason"] == "fingerprint"
    shutil.copyfile(stub_path(stubs_dir, entry), target)
    with pytest.raises(ArtifactIncompatibleError) as info, pytest.warns(UserWarning):
        library.open_unverified(target, allow=True)
    return target, info.value


def test_dev_fingerprint_message_is_rule_r6_exactly(monkeypatch) -> None:
    """The refusal alone, with no library, at each stability selected
    explicitly: rule r6's exact text while unstable, the loader's own once
    locked."""
    monkeypatch.setattr(_decls, "CHS_ABI_STABILITY", "unstable")
    with pytest.raises(ArtifactIncompatibleError) as info:
        _loader._refuse_fingerprint("/p", OTHER)
    err = info.value
    assert str(err) == r6_message(_decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
    assert (err.want, err.got, err.path) == (_decls.CHS_ABI_FINGERPRINT, OTHER, "/p")

    monkeypatch.setattr(_decls, "CHS_ABI_STABILITY", "locked")
    with pytest.raises(ArtifactIncompatibleError) as info:
        _loader._refuse_fingerprint("/p", OTHER)
    err = info.value
    assert str(err) == loader_message("/p", _decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
    assert (err.want, err.got, err.path) == (_decls.CHS_ABI_FINGERPRINT, OTHER, "/p")


def test_dev_fingerprint_refusal_after_the_lock() -> None:
    """This SDK as built: its description is locked, so the refusal alone
    carries the loader's own message."""
    _locked()
    with pytest.raises(ArtifactIncompatibleError) as info:
        _loader._refuse_fingerprint("/p", OTHER)
    err = info.value
    assert str(err) == loader_message("/p", _decls.CHS_ABI_FINGERPRINT, OTHER)
    assert "dev fingerprint" not in str(err)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
    assert (err.want, err.got, err.path) == (_decls.CHS_ABI_FINGERPRINT, OTHER, "/p")


def test_dev_fingerprint_refusal_through_a_load(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """The same through a real load of this SDK as built: the stub built to
    report another fingerprint (scripts/abi-v1/emit/_stubshared.py,
    "fingerprint-other")."""
    _locked()
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target, err = _load_fingerprint_other(stubs_dir, stubs_manifest, tmp_path, "fingerprint-other")
    assert str(err) == loader_message(str(target), _decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
    assert (err.want, err.got) == (_decls.CHS_ABI_FINGERPRINT, OTHER)


def test_dev_fingerprint_refusal_through_a_load_on_the_dev_side(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """The dev side through a real load, selected explicitly: with the
    generated stability patched to unstable, the same load carries rule r6's
    exact message."""
    monkeypatch.setattr(_decls, "CHS_ABI_STABILITY", "unstable")
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    _, err = _load_fingerprint_other(stubs_dir, stubs_manifest, tmp_path, "fingerprint-other")
    assert str(err) == r6_message(_decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"


def test_dev_fingerprint_beats_a_missing_symbol(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """#537: a library with ANOTHER fingerprint that also lacks a symbol this SDK
    declares is refused for its fingerprint (loader step 4), not as
    `missing_symbol:<name>` (step 6's sweep, which runs after the fingerprint
    comparison). The stub variant is shared: every binding's loader conformance
    runs the same `loader.fingerprint-other-missing-symbol` case."""
    _locked()
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target, err = _load_fingerprint_other(
        stubs_dir, stubs_manifest, tmp_path, "fingerprint-other-missing-symbol"
    )
    assert str(err) == loader_message(str(target), _decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
