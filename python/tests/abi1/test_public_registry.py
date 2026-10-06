"""The registry over the fetch seam, driven by the REAL fetch derivation: the
stub library packaged as an OCI layout signed with the SDK test key, resolved,
verified, unpacked, adapted and loaded."""

from __future__ import annotations

import shutil

import pytest

from chtypes import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactMissingError,
    ArtifactUntrustedError,
    FetchOptions,
    Registry,
    UsageError,
    library,
)
from chtypes._abi1 import _loader

from ._stub_tree import build_stub_tree

PLATFORMS = {"darwin-arm64", "linux-amd64", "linux-arm64"}


@pytest.fixture
def tree(stub_copy, tmp_path):
    path, predicate = stub_copy()
    return build_stub_tree(tmp_path / "route", path, predicate, tag=predicate["clickhouse_minor"])


def fetch_options(tree, tmp_path, **overrides) -> FetchOptions:
    fields = {
        "bases": (tree.base_url,),
        "cache_dir": tmp_path / "cache",
        "trusted_keys": (tree.trusted,),
    }
    fields.update(overrides)
    return FetchOptions(**fields)


def test_for_version_fetches_verifies_adapts_and_loads(tree, tmp_path) -> None:
    registry = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True)
    lib = registry.for_version(tree.predicate["clickhouse_minor"])
    resolved = lib.resolved
    assert resolved is not None and resolved.signed_by == tree.trusted.keyid
    assert lib.version == tree.predicate["clickhouse_version"]
    assert lib.build_info.build == tree.predicate["build"]
    assert resolved.library_path.read_bytes() == tree.library_bytes
    assert resolved.predicate == tree.predicate
    assert registry.for_version(lib.minor) is lib  # memoized for the registry's life
    assert registry.libraries() == (lib,)
    assert [r.version for r in registry.installed()] == [lib.version]
    assert lib.validate_type("UInt8")


def test_the_adapter_passes_the_predicate_verbatim_never_re_encoded(
    tree, tmp_path, monkeypatch
) -> None:
    seen = []
    real = _loader.open

    def spy(path, predicate, **kwargs):
        seen.append((path, predicate))
        return real(path, predicate, **kwargs)

    monkeypatch.setattr(library._loader, "open", spy)
    registry = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True)
    lib = registry.for_version(tree.predicate["clickhouse_minor"])
    (path, predicate), *rest = seen
    assert rest == []
    assert predicate is lib.resolved.predicate  # the very mapping the fetch layer returned
    assert path == str(lib.resolved.library_path)


def test_the_call_order_is_resolve_installed_then_ensure_only_with_autofetch(
    tree, tmp_path, monkeypatch
) -> None:
    from chtypes import registry as registry_module

    trace: list[str] = []
    for name in ("resolve_installed", "ensure"):
        real = getattr(registry_module, name)
        monkeypatch.setattr(
            registry_module,
            name,
            lambda *a, _real=real, _name=name: (trace.append(_name), _real(*a))[1],
        )
    minor = tree.predicate["clickhouse_minor"]
    # Autofetch off: the cache is asked, the network never is.
    with pytest.raises(ArtifactMissingError, match="autofetch is off"):
        Registry(fetch=fetch_options(tree, tmp_path)).for_version(minor)
    assert trace == ["resolve_installed"]
    trace.clear()
    # Autofetch on: resolve_installed misses, then ensure.
    first = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True).for_version(minor)
    assert trace == ["resolve_installed", "ensure"]
    trace.clear()
    # Now installed: a second registry never touches the network, and shares the image.
    second = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True).for_version(minor)
    assert trace == ["resolve_installed"] and second is first


def test_autofetch_defaults_to_the_environment_and_is_off_when_unset(
    tree, tmp_path, monkeypatch
) -> None:
    minor = tree.predicate["clickhouse_minor"]
    monkeypatch.delenv("CHTYPES_AUTOFETCH", raising=False)
    with pytest.raises(ArtifactMissingError):
        Registry(fetch=fetch_options(tree, tmp_path)).for_version(minor)
    monkeypatch.setenv("CHTYPES_AUTOFETCH", "1")
    assert Registry(fetch=fetch_options(tree, tmp_path)).for_version(minor).version


def test_construction_opens_nothing_and_preload_opens_at_construction_without_fetching(
    tree, tmp_path
) -> None:
    minor = tree.predicate["clickhouse_minor"]
    registry = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True)
    assert registry.libraries() == ()  # construction opens nothing
    # preload never fetches, even with autofetch on: nothing is installed yet.
    with pytest.raises(ArtifactMissingError):
        Registry(fetch=fetch_options(tree, tmp_path), autofetch=True, preload=[minor])
    registry.for_version(minor)  # install it
    preloaded = Registry(fetch=fetch_options(tree, tmp_path), preload=[minor])
    assert [lib.minor for lib in preloaded.libraries()] == [minor]


def test_a_refused_spelling_is_a_usage_error_before_any_fetch(tree, tmp_path) -> None:
    registry = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True)
    for bad in ("v26.8", "26.8-lts", "26.8.15.10-stable"):
        with pytest.raises(UsageError):
            registry.for_version(bad)
    assert registry.libraries() == ()


def test_fetch_errors_and_loader_errors_are_one_family(tree, tmp_path) -> None:
    minor = tree.predicate["clickhouse_minor"]
    untrusted = fetch_options(tree, tmp_path, trusted_keys=())
    with pytest.raises(ArtifactUntrustedError) as info:
        Registry(fetch=untrusted, autofetch=True).for_version(minor)
    assert isinstance(info.value, ArtifactError)
    assert info.value.code == "CHTYPES_ARTIFACT_UNTRUSTED"
    assert info.value.__cause__ is not None  # the fetch layer's own error, kept


def test_a_signed_statement_the_library_disagrees_with_is_artifact_corrupt(
    stub_copy, tmp_path
) -> None:
    path, predicate = stub_copy()
    lying = dict(predicate, inputs_sha256="f" * 64)
    tree = build_stub_tree(tmp_path / "route", path, lying, tag=predicate["clickhouse_minor"])
    registry = Registry(fetch=fetch_options(tree, tmp_path), autofetch=True)
    with pytest.raises(ArtifactCorruptError) as info:
        registry.for_version(predicate["clickhouse_minor"])
    assert info.value.reason == "build_info_mismatch:inputs_sha256"
    assert info.value.code == "CHTYPES_ARTIFACT_CORRUPT"


def test_installed_lists_the_verified_installs_cache_only(tree, tmp_path) -> None:
    assert Registry(fetch=fetch_options(tree, tmp_path)).installed() == ()
    Registry(fetch=fetch_options(tree, tmp_path), autofetch=True).for_version(
        tree.predicate["clickhouse_minor"]
    )
    shutil.rmtree(tmp_path / "route")  # the network is gone; the listing never needed it
    assert len(Registry(fetch=fetch_options(tree, tmp_path)).installed()) == 1


def test_a_library_outside_its_request_is_refused() -> None:
    """The load-time assertion (public issue #481): a library whose own
    build_info version is not within the request fails the open as
    ArtifactCorruptError, reason build_info_mismatch:clickhouse_version."""
    from types import SimpleNamespace

    from chtypes import errors
    from chtypes.registry import _check_within_request

    library = SimpleNamespace(version="26.8.5.1", path="/cache/unpacked/sha256/x/libchtypes.so")
    for request in ("26.8", "26.8.5", "26.8.5.1", "a-literal-tag"):
        _check_within_request(library, request)  # type: ignore[arg-type]
    for request in ("26.3", "26.3.4.1", "26.8.5.2", "26.80", "26.8.15"):
        try:
            _check_within_request(library, request)  # type: ignore[arg-type]
        except errors.ArtifactCorruptError as exc:
            assert exc.code == "CHTYPES_ARTIFACT_CORRUPT"
            assert exc.reason == "build_info_mismatch:clickhouse_version"
            assert (exc.want, exc.got) == (request, "26.8.5.1")
        else:
            raise AssertionError(f"request {request!r} answered by 26.8.5.1 must be refused")
