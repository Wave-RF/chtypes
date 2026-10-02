"""Runs every "loader" case in tests/fixtures/abi-v1/cases.json
(scripts/abi-v1/emit/_stubshared.py's variant plan, one per stub library)
through the hand-written loader, chtypes._abi1._loader.open(), plus a few
hand-written unit checks the generated cases cannot express: the two-phase
Api constructor (plan section 2.2) and open_unverified()'s gating
(plan section 3.1).
"""

from __future__ import annotations

import platform

import pytest

from chtypes._abi1 import _decls, _errmap, _errors, _loader

from .conftest import cases_of_kind

_CASES, _IDS = cases_of_kind("loader")


def _expected_reason(variant: str, declared_reason: str) -> str:
    """scripts/abi-v1/emit/_stubshared.py's "ctor-marker" variant proves
    loader step 1 (glibc) refuses BEFORE dlopen -- but step 1 is Linux-only
    by design (plan section 3.2, "darwin: skip"), so on darwin this exact
    predicate (an impossible glibc_floor) never gets checked at all, and
    loading the library legitimately SUCCEEDS there. cases.json does not
    parametrize this one case by OS (a gap worth raising upstream: every
    binding's "darwin: skip" step 1 hits the identical mismatch), so this
    test applies the platform-aware adjustment the spec itself requires
    rather than either skip the case (parity.py would then read it as
    MISSING) or fail it (the loader would be WRONG to refuse here)."""
    if variant == "ctor-marker" and platform.system() != "Linux":
        return "accepted"
    return declared_reason


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_case(case: dict, stubs_manifest: dict) -> None:
    variant = case["variant"]
    entry = stubs_manifest["variants"][variant]
    reason = _expected_reason(variant, case["expect"]["reason"])

    if reason == "accepted":
        result = _loader.open(entry["path"], entry["predicate"])
        assert result.api is not None
        assert result.build_info["abi_fingerprint"] == _decls.CHS_ABI_FINGERPRINT
        return

    exc_cls = _errmap.loader_error_class(reason)
    with pytest.raises(exc_cls) as exc_info:
        _loader.open(entry["path"], entry["predicate"])
    assert exc_info.value.reason == reason, (
        f"{case['id']}: want reason {reason!r}, got {exc_info.value.reason!r}"
    )


def test_api_two_phase_construction_is_private(stubs_dir, stubs_manifest) -> None:
    """plan section 2.2: "The full Api type is constructible only by the
    loader's step 6 ... in Python it is a private constructor checked in a
    test." Calling Api() directly, bypassing resolve_all(), must refuse."""
    entry = stubs_manifest["variants"]["ok"]
    result = _loader.open(entry["path"], entry["predicate"])
    with pytest.raises(TypeError):
        _decls.Api(result.api._lib, {}, object())


def test_open_unverified_needs_both_the_flag_and_the_env_var(monkeypatch, stubs_manifest) -> None:
    entry = stubs_manifest["variants"]["ok"]
    monkeypatch.delenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", raising=False)

    with pytest.raises(_errors.ArtifactIncompatibleError):
        _loader.open_unverified(entry["path"], allow=False)
    with pytest.raises(_errors.ArtifactIncompatibleError):
        _loader.open_unverified(entry["path"], allow=True)  # env var still unset

    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.raises(_errors.ArtifactIncompatibleError):
        _loader.open_unverified(entry["path"], allow=False)  # flag still false

    result = _loader.open_unverified(entry["path"], allow=True)
    assert result.api is not None


def test_open_unverified_skips_step1_with_no_predicate(monkeypatch, stubs_manifest) -> None:
    """No predicate given: step 1 (glibc) is skipped outright, per plan
    section 3.1 -- proven by loading the ctor-marker variant (whose ONLY
    defect is an impossible glibc_floor in its predicate) unverified, with no
    predicate at all, and it must succeed."""
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    entry = stubs_manifest["variants"]["ctor-marker"]
    result = _loader.open_unverified(entry["path"], predicate=None, allow=True)
    assert result.api is not None
