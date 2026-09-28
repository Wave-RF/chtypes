"""Fixtures. Every test that needs an artifact runs against a real one or does
not run at all — it skips, loudly, by name. The fetch, CLI and pure-Python
tests need none and always run; that is what this repository's CI proves on
hosted runners, and the artifact-backed proof is the core repository's
server-truth suites against this same tree.

The registry comes from the search path (docs/guides/fetch.md §1): `$CHTYPES_REGISTRY`,
else the per-user artifact cache for this host (`chtypes.default_registry_dir()`:
`~/.cache/chtypes/artifacts/abi<R>/<os>-<arch>`, R the binding's ABI revision,
where `chtypes fetch` installs), else the system locations. Without a single line on it
the suite skips with that message: a green run that never touched a ClickHouse
build would be worse than no run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import chtypes

FALLBACK_REGISTRY = Path(chtypes.default_registry_dir())
FETCH_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fetch"


def _fixture_abi_revision(fixtures: Path) -> int:
    """The one ``abi_revision`` every row of every fixture release under
    ``fixtures`` carries (one release per subdirectory with an ``index.json``).

    DERIVED from the fixtures' own bytes, never typed into a test: a suite that
    hand-set it would be testing its author's belief about the fixtures, not
    the fetch. Anything short of one unambiguous answer — no release, a row
    without an integer ``abi_revision``, rows that disagree — raises."""
    indexes = sorted(fixtures.glob("*/index.json"))
    if not indexes:
        raise AssertionError(f"no <release>/index.json under {fixtures}")
    seen: dict[int, list[str]] = {}
    for index in indexes:
        rows = json.loads(index.read_text()).get("artifacts") or []
        if not rows:
            raise AssertionError(f"{index} lists no artifacts")
        for i, row in enumerate(rows):
            rev = row.get("abi_revision")
            if not isinstance(rev, int) or isinstance(rev, bool):
                raise AssertionError(f"{index}: row {i} carries no integer abi_revision ({rev!r})")
            releases = seen.setdefault(rev, [])
            if index.parent.name not in releases:
                releases.append(index.parent.name)
    if len(seen) != 1:
        parts = "; ".join(f"{rev} in {', '.join(names)}" for rev, names in sorted(seen.items()))
        raise AssertionError(
            f"the fixture releases under {fixtures} disagree on abi_revision ({parts}); "
            f"there is no one revision to fetch them at"
        )
    return next(iter(seen))


@pytest.fixture
def derive_fixture_abi_revision() -> Callable[[Path], int]:
    """The derivation itself, for the test that pins it."""
    return _fixture_abi_revision


@pytest.fixture
def at_fixture_revision(monkeypatch: pytest.MonkeyPatch) -> int:
    """Fetch selects only rows at the binding's own ABI revision
    (docs/guides/fetch.md §2); the fixtures carry whatever revision they were
    generated at. Point the test-only override at THAT revision — read off
    the fixtures' own index.json rows — for this test, and return it."""
    from chtypes import fetch as fetch_module

    rev = _fixture_abi_revision(FETCH_FIXTURES)
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", rev)
    return rev


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
            f"`uv run python -m chtypes fetch 25.8` (docs/guides/fetch.md), build one in the core "
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
    """The newest library this registry can answer for, OPENED.

    Off `versions()`, not off `libraries()`: construction opens nothing, so
    `libraries()` holds only what something has already asked for — which on a
    fresh session is nothing, and `[-1]` of nothing is an IndexError rather
    than a loud skip.
    """
    return registry.for_version(registry.versions()[-1])
