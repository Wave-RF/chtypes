"""Rule r6's fingerprint refusal (spec/abi-v2/docs.md): a 2.0.0-dev SDK pins its
dev fingerprint and refuses a library with any other as
CHTYPES_ARTIFACT_INCOMPATIBLE, with exactly the rule's message. The message is
spelled out here, not taken from the code under test.
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


def _unstable() -> None:
    if _decls.CHS_ABI_STABILITY != "unstable":
        pytest.skip(
            f"SKIPPED: the description is {_decls.CHS_ABI_STABILITY!r}; the dev message "
            "applies only while it is unstable"
        )


def test_dev_fingerprint_message_is_rule_r6_exactly() -> None:
    """The refusal alone, with no library."""
    _unstable()
    want = r6_message(_decls.CHS_ABI_FINGERPRINT, OTHER)
    with pytest.raises(ArtifactIncompatibleError) as info:
        _loader._refuse_fingerprint("/p", OTHER)
    err = info.value
    assert str(err) == want
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
    assert (err.want, err.got, err.path) == (_decls.CHS_ABI_FINGERPRINT, OTHER, "/p")


def test_dev_fingerprint_refusal_through_a_load(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """The same through a real load: the stub built to report another fingerprint
    (scripts/abi-v1/emit/_stubshared.py, "fingerprint-other")."""
    _unstable()
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target = tmp_path / "fingerprint-other.so"
    shutil.copyfile(stub_path(stubs_dir, stubs_manifest["variants"]["fingerprint-other"]), target)
    with pytest.raises(ArtifactIncompatibleError) as info, pytest.warns(UserWarning):
        library.open_unverified(target, allow=True)
    err = info.value
    assert str(err) == r6_message(_decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"


def test_dev_fingerprint_beats_a_missing_symbol(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, clean_process, monkeypatch
) -> None:
    """#537: a library with ANOTHER fingerprint that also lacks a symbol this SDK
    declares gets rule r6's exact message (loader step 4), not
    `missing_symbol:<name>` (step 6's sweep, which runs after the fingerprint
    comparison). The stub variant is shared: every binding's loader conformance
    runs the same `loader.fingerprint-other-missing-symbol` case."""
    _unstable()
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    target = tmp_path / "fingerprint-other-missing-symbol.so"
    entry = stubs_manifest["variants"]["fingerprint-other-missing-symbol"]
    assert entry["reason"] == "fingerprint"
    shutil.copyfile(stub_path(stubs_dir, entry), target)
    with pytest.raises(ArtifactIncompatibleError) as info, pytest.warns(UserWarning):
        library.open_unverified(target, allow=True)
    err = info.value
    assert str(err) == r6_message(_decls.CHS_ABI_FINGERPRINT, OTHER)
    assert err.code == CODE_ARTIFACT_INCOMPATIBLE and err.reason == "fingerprint"
