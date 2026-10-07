"""Runs every "loader" case in tests/fixtures/abi-v2/cases.json
(scripts/abi-v1/emit/_stubshared.py's variant plan, one per stub library)
through the hand-written loader, chtypes._abi2._loader.open(), plus a few
hand-written unit checks the generated cases cannot express: the two-phase
Api constructor (plan section 2.2) and open_unverified()'s gating
(plan section 3.1).
"""

from __future__ import annotations

import platform

import pytest

from chtypes._abi2 import _decls, _errmap, _errors, _loader

from .conftest import cases_of_kind, stub_path

_CASES, _IDS = cases_of_kind("loader")

_HOST_OS = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_case(case: dict, stubs_dir, stubs_manifest: dict) -> None:
    # cases.schema.json: an "os" field is present only when the case's
    # expected reason differs by platform (loader.ctor-marker.linux/.darwin:
    # D3's glibc floor step is Linux-only). PM ruling: this leg must SKIP,
    # never report a pass for, a case whose os does not match its own (a
    # pass that never ran is the pattern this repository refuses) --
    # conftest.py's pytest_runtest_makereport OMITS the case id entirely
    # from the report for exactly that reason.
    case_os = case.get("os")
    if case_os is not None and case_os != _HOST_OS:
        pytest.skip(f"{case['id']}: os={case_os!r} does not match this host ({_HOST_OS!r})")

    variant = case["variant"]
    entry = stubs_manifest["variants"][variant]
    path = stub_path(stubs_dir, entry)
    reason = case["expect"]["reason"]

    if reason == "accepted":
        result = _loader.open(path, entry["predicate"])
        assert result.api is not None
        assert result.build_info["abi_fingerprint"] == _decls.CHS_ABI_FINGERPRINT
        return

    exc_cls = _errmap.loader_error_class(reason)
    with pytest.raises(exc_cls) as exc_info:
        _loader.open(path, entry["predicate"])
    assert exc_info.value.reason == reason, (
        f"{case['id']}: want reason {reason!r}, got {exc_info.value.reason!r}"
    )


def test_api_two_phase_construction_is_private(stubs_dir, stubs_manifest) -> None:
    """plan section 2.2: "The full Api type is constructible only by the
    loader's step 6 ... in Python it is a private constructor checked in a
    test." Calling Api() directly, bypassing resolve_all(), must refuse."""
    entry = stubs_manifest["variants"]["ok"]
    result = _loader.open(stub_path(stubs_dir, entry), entry["predicate"])
    with pytest.raises(TypeError):
        _decls.Api(result.api._lib, {}, object())


def test_open_unverified_needs_both_the_flag_and_the_env_var(
    monkeypatch, stubs_dir, stubs_manifest
) -> None:
    entry = stubs_manifest["variants"]["ok"]
    path = stub_path(stubs_dir, entry)
    monkeypatch.delenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", raising=False)

    with pytest.raises(_errors.UsageError):
        _loader.open_unverified(path, allow=False)
    with pytest.raises(_errors.UsageError):
        _loader.open_unverified(path, allow=True)  # env var still unset

    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    with pytest.raises(_errors.UsageError):
        _loader.open_unverified(path, allow=False)  # flag still false

    result = _loader.open_unverified(path, allow=True)
    assert result.api is not None


def test_open_unverified_skips_step1_with_no_predicate(
    monkeypatch, stubs_dir, stubs_manifest
) -> None:
    """No predicate given: step 1 (glibc) is skipped outright, per plan
    section 3.1 -- proven by loading the ctor-marker variant (whose ONLY
    defect is an impossible glibc_floor in its predicate) unverified, with no
    predicate at all, and it must succeed."""
    monkeypatch.setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
    entry = stubs_manifest["variants"]["ctor-marker"]
    path = stub_path(stubs_dir, entry)
    result = _loader.open_unverified(path, predicate=None, allow=True)
    assert result.api is not None
