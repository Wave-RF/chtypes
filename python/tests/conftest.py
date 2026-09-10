"""Fixtures. Every test that needs an artifact runs against a real one or does
not run at all — it skips, loudly, by name. The fetch, CLI and pure-Python
tests need none and always run; that is what this repository's CI proves on
hosted runners, and the artifact-backed proof is the core repository's
`certify` workflow against this same tree.

The registry comes from the search path (docs/fetch.md §1): `$CHTYPES_REGISTRY`,
else the per-user artifact cache for this host (`chtypes.default_registry_dir()`:
`~/.cache/chtypes/artifacts/<os>-<arch>`, where `chtypes fetch` installs and a
core-repo build lands), else the system locations. Without a single line on it
the suite skips with that message: a green run that never touched a ClickHouse
build would be worse than no run.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

import chtypes

FALLBACK_REGISTRY = Path(chtypes.default_registry_dir())


def _candidate() -> tuple[Path, str]:
    env = os.environ.get(chtypes.ENV_REGISTRY)
    if env:
        return Path(env), f"${chtypes.ENV_REGISTRY}"
    return FALLBACK_REGISTRY, f"the default {FALLBACK_REGISTRY}"


@pytest.fixture(scope="session")
def registry() -> chtypes.Registry:
    path, source = _candidate()
    try:
        registry = chtypes.Registry(path)
    except chtypes.RegistryError as exc:
        pytest.skip(f"chtypes artifact registry at {path} (from {source}) is unusable: {exc}")
    if not registry.versions():
        pytest.skip(
            f"no chtypes artifacts on the search path {[str(p) for p in registry.search_path]} "
            f"(from {source}). Fetch one with `scripts/fetch.sh 25.8` or "
            f"`uv run python -m chtypes fetch 25.8` (docs/fetch.md), build one in the core "
            f"repo, or point ${chtypes.ENV_REGISTRY} at an existing registry."
        )
    return registry


@pytest.fixture
def isolated_search_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A registry search path that holds NOTHING of this machine's: no
    `$CHTYPES_REGISTRY`, the cache under a fresh `XDG_CACHE_HOME`, no system
    roots, and none of the fetch-policy variables. Returns the cache directory
    fetch would write to. For tests about missing artifacts, fetch and the
    search path — never for tests that need a real artifact."""
    from chtypes import fetch as fetch_module

    for name in (
        chtypes.ENV_REGISTRY,
        chtypes.ENV_AUTOFETCH,
        fetch_module.ENV_TRUSTED_KEYS,
        fetch_module.ENV_ALLOW_UNSIGNED,
        fetch_module.ENV_ARTIFACTS_URL,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setattr(fetch_module, "SYSTEM_REGISTRY_ROOTS", ())
    return Path(chtypes.default_registry_dir())


@pytest.fixture(scope="session")
def library(registry: chtypes.Registry) -> Callable[[str], chtypes.Library]:
    """Resolve one version, skipping the test if this registry has no artifact for it."""

    def resolve(version: str) -> chtypes.Library:
        if version not in registry:
            pytest.skip(
                f"{registry.directory} has no ClickHouse {version} artifact — "
                f"`scripts/fetch.sh {version}` installs one"
            )
        return registry.for_version(version)

    return resolve


@pytest.fixture(scope="session")
def newest(registry: chtypes.Registry) -> chtypes.Library:
    """The newest loaded library, for behaviour that is not version-specific."""
    return registry.libraries()[-1]
