"""Fixtures. Every test here runs against real artifacts or does not run at all.

The registry comes from `$CHTYPES_REGISTRY`, else the per-user artifact cache
for this host (`chtypes.default_registry_dir()`: `~/.cache/chtypes/artifacts/
<os>-<arch>`, where `scripts/fetch.sh` installs and a core-repo build lands).
Without one the suite skips with that message: a green run that never touched a
ClickHouse build would be worse than no run.
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
        return chtypes.Registry(path)
    except chtypes.RegistryError as exc:
        pytest.skip(
            f"no chtypes artifact registry at {path} (from {source}): {exc}. "
            f"Fetch one with scripts/fetch.sh (docs/artifacts.md) or build one in the core repo, "
            f"or point ${chtypes.ENV_REGISTRY} at an existing registry."
        )


@pytest.fixture(scope="session")
def library(registry: chtypes.Registry) -> Callable[[str], chtypes.Library]:
    """Resolve one version, skipping the test if this registry has no artifact for it."""

    def resolve(version: str) -> chtypes.Library:
        if version not in registry:
            pytest.skip(f"{registry.directory} has no ClickHouse {version} artifact")
        return registry.for_version(version)

    return resolve


@pytest.fixture(scope="session")
def newest(registry: chtypes.Registry) -> chtypes.Library:
    """The newest loaded library, for behaviour that is not version-specific."""
    return registry.libraries()[-1]
