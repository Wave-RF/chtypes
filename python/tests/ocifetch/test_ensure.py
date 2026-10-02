"""End-to-end `ensure()`/`resolve_installed()`/`list_installed()`/
`verify_installed()` against a hand-written `file://` tree (`_registry_support.py`).

Not a substitute for the real conformance suite (`tests/fixtures/fetch-v1/`,
lane 0B) — this exercises the wiring across every module in this package
with no network and no dependency on that tree, which does not exist yet in
this worktree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chtypes._ocifetch._dsse import TrustedKey
from chtypes._ocifetch._ensure import (
    Options,
    Request,
    ensure,
    list_installed,
    resolve_installed,
    verify_installed,
)
from chtypes._ocifetch._errors import (
    ArtifactCorruptError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
)
from chtypes._ocifetch._lock import load_lock

from ._registry_support import build_tree


def _options(tree, tmp_path: Path, **overrides) -> Options:
    defaults = dict(
        bases=(tree.base_url,),
        cache_dir=tmp_path / "cache",
        trusted_keys=(TrustedKey(keyid=tree.keyid, public_key=tree.public_key),),
    )
    defaults.update(overrides)
    return Options(**defaults)


def test_ensure_happy_path_installs_and_verifies(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    resolved = ensure(Request("26.8"), options)

    assert resolved.version == "26.8.15.10"
    assert resolved.build == "20261001.183455"
    assert resolved.channel == "lts"
    assert resolved.platform == "linux-arm64"
    assert resolved.signed_by == tree.keyid
    assert resolved.already_installed is False
    assert resolved.digests["manifest"] == tree.manifest_digest
    assert resolved.digests["layer"] == tree.layer_digest
    assert resolved.digests["bundle"] == tree.bundle_digest
    assert resolved.library_path.read_bytes() == tree.library_bytes
    assert resolved.warnings == ()


def test_ensure_exact_version_request(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry", tag="26.8.15.10")
    options = _options(tree, tmp_path, platform="linux-arm64")
    resolved = ensure(Request("26.8.15.10"), options)
    assert resolved.version == "26.8.15.10"


def test_ensure_second_call_makes_no_referrer_or_layer_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN §3.2 'existing-install-noop': once the resolved manifest digest
    is already a verified install, ensure() must not re-discover referrers
    or re-download the layer — it trusts its own prior verification."""
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    first = ensure(Request("26.8"), options)
    assert first.already_installed is False

    import chtypes._ocifetch._ensure as ensure_module

    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "ensure() re-fetched a referrer/layer for an already-installed manifest"
        )

    monkeypatch.setattr(ensure_module, "discover_referrers", _forbidden)
    monkeypatch.setattr(ensure_module, "fetch_blob_to_path", _forbidden)

    second = ensure(Request("26.8"), options)
    assert second.already_installed is True
    assert second.digests["manifest"] == first.digests["manifest"]
    assert second.library_path.read_bytes() == tree.library_bytes


def test_ensure_untrusted_signature_raises(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    other_key = TrustedKey(keyid="someone-else", public_key=b"\x00" * 32)
    options = Options(
        bases=(tree.base_url,),
        cache_dir=tmp_path / "cache",
        trusted_keys=(other_key,),
        platform="linux-arm64",
    )
    with pytest.raises(ArtifactUntrustedError):
        ensure(Request("26.8"), options)


def test_ensure_allow_unsigned_falls_back_to_config_blob(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    other_key = TrustedKey(keyid="someone-else", public_key=b"\x00" * 32)
    options = Options(
        bases=(tree.base_url,),
        cache_dir=tmp_path / "cache",
        trusted_keys=(other_key,),
        allow_unsigned=True,
        platform="linux-arm64",
    )
    # The config blob in this test tree is the OCI empty-config shape
    # ("{}"), so allow-unsigned here has nothing to recover a library name
    # from and must fail loudly rather than silently installing nothing.
    with pytest.raises(ArtifactCorruptError, match="config blob"):
        ensure(Request("26.8"), options)


def test_ensure_missing_platform_is_unpublished(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry", os_name="linux", arch="arm64")
    options = _options(tree, tmp_path, platform="darwin-arm64")
    with pytest.raises(ArtifactUnpublishedError, match="linux-arm64"):
        ensure(Request("26.8"), options)


def test_ensure_wrong_version_predicate_is_corrupt(tmp_path: Path) -> None:
    """A manifest under tag 26.9 whose SIGNED predicate still claims 26.8
    must be refused — the predicate is the source of truth the fetcher
    checks the request against, not the tag."""
    tree = build_tree(tmp_path / "registry", tag="26.9", version="26.8.15.10")
    options = _options(tree, tmp_path, platform="linux-arm64")
    with pytest.raises(ArtifactCorruptError, match="version"):
        ensure(Request("26.9"), options)


def test_ensure_offline_miss_raises_missing(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64", offline=True)
    with pytest.raises(ArtifactMissingError):
        ensure(Request("26.8"), options)


def test_ensure_offline_hit_after_online_install(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    ensure(Request("26.8"), options)

    offline_options = _options(tree, tmp_path, platform="linux-arm64", offline=True)
    resolved = ensure(Request("26.8"), offline_options)
    assert resolved.already_installed is True
    assert resolved.source == "cache"


def test_resolve_installed_miss_returns_none(tmp_path: Path) -> None:
    options = Options(cache_dir=tmp_path / "cache")
    assert resolve_installed(Request("26.8"), "linux-arm64", options) is None


def test_resolve_installed_hit_after_ensure(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    ensure(Request("26.8"), options)
    resolved = resolve_installed(Request("26.8"), "linux-arm64", options)
    assert resolved is not None
    assert resolved.version == "26.8.15.10"


def test_list_installed_and_verify_installed(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    ensure(Request("26.8"), options)

    installed = list_installed(options)
    assert len(installed) == 1
    assert installed[0].version == "26.8.15.10"

    results = verify_installed(options)
    assert len(results) == 1
    assert results[0].ok is True


def test_verify_installed_detects_tampered_library(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    options = _options(tree, tmp_path, platform="linux-arm64")
    resolved = ensure(Request("26.8"), options)
    resolved.library_path.write_bytes(b"tampered bytes, not the verified library")

    results = verify_installed(options)
    assert len(results) == 1
    assert results[0].ok is False


def test_ensure_resolves_floating_request_across_platforms(tmp_path: Path) -> None:
    tree_linux = build_tree(tmp_path / "registry", os_name="linux", arch="arm64")
    options_linux = _options(tree_linux, tmp_path, platform="linux-arm64")
    resolved_linux = ensure(Request("26.8"), options_linux)
    assert resolved_linux.platform == "linux-arm64"


# ---------------------------------------------------------------------------
# Lock precedence (decided-here; see ensure()'s own docstring).
# ---------------------------------------------------------------------------


def test_ensure_with_lock_write_then_frozen_reads_the_pin(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    lock_path = tmp_path / "chtypes.lock"
    options = _options(tree, tmp_path, platform="linux-arm64", lock_path=lock_path, lock_write=True)
    ensure(Request("26.8"), options)
    lock = load_lock(lock_path)
    pin = lock.pin_for("26.8", "linux-arm64")
    assert pin is not None
    assert pin.manifest == tree.manifest_digest

    frozen_options = _options(
        tree,
        tmp_path,
        platform="linux-arm64",
        lock_path=lock_path,
        frozen=True,
    )
    resolved = ensure(Request("26.8"), frozen_options)
    assert resolved.version == "26.8.15.10"


def test_ensure_default_with_a_pin_present_uses_the_pin_not_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """decided-here: a plain ensure() with a lock present and pinning this
    (request, platform) follows the pin (by digest) rather than resolving
    the floating tag — so a tag that moved to a DIFFERENT, unsigned-for-us
    build in the meantime is never even looked at."""
    tree = build_tree(tmp_path / "registry")
    lock_path = tmp_path / "chtypes.lock"
    options = _options(tree, tmp_path, platform="linux-arm64", lock_path=lock_path, lock_write=True)
    ensure(Request("26.8"), options)

    import chtypes._ocifetch._ensure as ensure_module

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("ensure() resolved the floating tag despite a matching pin")

    monkeypatch.setattr(ensure_module, "fetch_manifest_by_tag", _forbidden)

    plain_options = _options(tree, tmp_path, platform="linux-arm64", lock_path=lock_path)
    resolved = ensure(Request("26.8"), plain_options)
    assert resolved.version == "26.8.15.10"


def test_ensure_update_ignores_the_pin_and_re_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = build_tree(tmp_path / "registry")
    lock_path = tmp_path / "chtypes.lock"
    options = _options(tree, tmp_path, platform="linux-arm64", lock_path=lock_path, lock_write=True)
    ensure(Request("26.8"), options)

    import chtypes._ocifetch._ensure as ensure_module

    called = {"tag": False}
    real_fetch = ensure_module.fetch_manifest_by_tag

    def _spy(*args, **kwargs):
        called["tag"] = True
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(ensure_module, "fetch_manifest_by_tag", _spy)

    update_options = _options(
        tree,
        tmp_path,
        platform="linux-arm64",
        lock_path=lock_path,
        update=True,
    )
    ensure(Request("26.8"), update_options)
    assert called["tag"] is True


def test_ensure_frozen_without_a_pin_raises_pinned_error(tmp_path: Path) -> None:
    tree = build_tree(tmp_path / "registry")
    lock_path = tmp_path / "chtypes.lock"
    options = _options(tree, tmp_path, platform="linux-arm64", lock_path=lock_path, frozen=True)
    with pytest.raises(ArtifactPinnedError):
        ensure(Request("26.8"), options)
