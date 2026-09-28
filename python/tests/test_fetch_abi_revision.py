"""docs/guides/fetch.md §2: fetch selects only rows at the binding's own ABI
revision, FIRST, and the per-user cache is keyed by that revision.

Synthetic listings, no network and no fixtures: the revision under test is
always `chtypes.ABI_REVISION` itself (or one past it), never a typed number,
so these hold at whatever revision the binding speaks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import chtypes
from chtypes import fetch as fetch_module
from chtypes.errors import ArtifactUnpublishedError
from chtypes.fetch import Fetcher, Release, ReleaseEntry

OWN = chtypes.ABI_REVISION
OTHER = OWN + 1
PLATFORM = "linux-amd64"


def _row(version: str, build: int, revision: int | None) -> ReleaseEntry:
    return ReleaseEntry(
        file=f"chtypes-{version}-{PLATFORM}-b{build}.tar.gz",
        sha256="a" * 64,
        bytes=1,
        clickhouse_version=version,
        clickhouse_minor=".".join(version.split(".")[:2]),
        library="libchtypes.so",
        library_sha256="b" * 64,
        os="linux",
        arch="amd64",
        build=build,
        abi_revision=revision,
    )


def _release(*entries: ReleaseEntry) -> Release:
    return Release(source="synthetic", tag="t", entries=entries, sums={}, signed_by=None)


@pytest.fixture(autouse=True)
def _at_the_bindings_own_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the binding's OWN revision: no override."""
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", None)


def test_a_higher_build_at_another_revision_never_beats_the_bindings_own() -> None:
    """(a) Another revision's row with a HIGHER build, and a NEWER version in a
    row that declares no revision, both lose to the one row at the binding's
    revision — in every listing order, for a line, an exact patch and --all."""
    mine = _row("25.8.28.1-lts", 10, OWN)
    higher_build_elsewhere = _row("25.8.28.1-lts", 20, OTHER)
    newer_undeclared = _row("25.8.33.6-lts", 30, None)
    for order in (
        (mine, higher_build_elsewhere, newer_undeclared),
        (newer_undeclared, higher_build_elsewhere, mine),
        (higher_build_elsewhere, mine, newer_undeclared),
    ):
        release = _release(*order)
        for spelling in ("25.8", "25.8.28.1-lts", "25.8.28.1"):
            assert release.select(spelling, PLATFORM) is mine, spelling
        assert release.offered(PLATFORM) == [mine]


def test_only_another_revision_served_is_unpublished_naming_both(tmp_path: Path) -> None:
    """(b) Only other-revision rows: CHTYPES_ARTIFACT_UNPUBLISHED (exit 4), never
    a fallback, naming the binding's revision and the one the release serves."""
    release = _release(_row("25.8.28.1-lts", 20, OTHER), _row("26.7.3.19-stable", 20, OTHER))
    fetcher = Fetcher(dest=tmp_path, platform=PLATFORM, url=str(tmp_path))
    fetcher._release = release
    for attempt in (
        lambda: release.select("25.8", PLATFORM),
        lambda: release.select("25.8.28.1-lts", PLATFORM),
        fetcher.ensure_all,
    ):
        with pytest.raises(ArtifactUnpublishedError) as err:
            attempt()
        message = str(err.value)
        assert err.value.code == "CHTYPES_ARTIFACT_UNPUBLISHED"  # exit 4, fetch.md §6
        assert f"ABI revision {OWN} (this SDK's)" in message, message
        assert f"only at ABI revision {OTHER}" in message, message


def test_a_row_without_an_abi_revision_is_never_selected() -> None:
    """(c) A row that declares no revision — absent, or not an integer — is
    never selected, even when it is the only row there is."""
    release = _release(_row("25.8.28.1-lts", 0, None))
    with pytest.raises(ArtifactUnpublishedError) as err:
        release.select("25.8", PLATFORM)
    assert "declare no ABI revision" in str(err.value)
    assert f"ABI revision {OWN}" in str(err.value)
    assert release.offered(PLATFORM) == []

    # What index.json can actually carry: the revision as a JSON number is a
    # revision; as a string, a float, null or a bool it is not — and none of
    # them fails the rest of the listing.
    for raw, want in (
        (OWN, OWN),
        (str(OWN), None),
        (float(OWN), None),
        (None, None),
        (True, None),
    ):
        row = {
            "file": "x.tar.gz",
            "sha256": "a" * 64,
            "bytes": 1,
            "clickhouse_version": "25.8.28.1-lts",
            "library": "libchtypes.so",
            "library_sha256": "b" * 64,
            "os": "linux",
            "arch": "amd64",
            "abi_revision": raw,
        }
        doc = json.dumps({"schema": 1, "artifacts": [row]}).encode()
        (entry,), *_ = fetch_module._parse_index(doc, "synthetic")
        assert entry.abi_revision == want, raw
    row.pop("abi_revision")
    doc = json.dumps({"schema": 1, "artifacts": [row]}).encode()
    (entry,), *_ = fetch_module._parse_index(doc, "synthetic")
    assert entry.abi_revision is None


def test_the_per_user_cache_is_keyed_by_the_abi_revision(
    isolated_search_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """abi<R>/ with R read off the binding's own constant, never typed — and the
    fetch test override does not move it."""
    abi = f"abi{chtypes.ABI_REVISION}"
    want = tmp_path / "xdg-cache" / "chtypes" / "artifacts" / abi / chtypes.host_platform()
    assert isolated_search_path == want
    assert Path(chtypes.default_registry_dir()) == want
    assert chtypes.fetch_destination() == want
    assert chtypes.registry_search_path()[0] == want
    assert Path(chtypes.fetch.cache_registry_dir(PLATFORM)) == want.parent / PLATFORM
    assert Fetcher(platform=chtypes.host_platform()).dest == want
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", OTHER)
    assert Path(chtypes.default_registry_dir()) == want
    assert fetch_module._fetch_abi_revision() == OTHER


def test_the_fixture_revision_is_derived_never_typed(
    tmp_path: Path, derive_fixture_abi_revision: Callable[[Path], int]
) -> None:
    """The fetch-fixture suites' override is read off the fixtures, so the
    derivation itself is pinned: one agreed revision is the answer;
    disagreement, a row without one, or no release at all is an error."""

    def write(root: Path, release: str, *revisions: int | None) -> None:
        rows = [{"file": "x"} | ({} if r is None else {"abi_revision": r}) for r in revisions]
        (root / release).mkdir(parents=True, exist_ok=True)
        (root / release / "index.json").write_text(json.dumps({"schema": 1, "artifacts": rows}))

    with pytest.raises(AssertionError):
        derive_fixture_abi_revision(tmp_path)
    write(tmp_path, "one", 7, 7)
    write(tmp_path, "two", 7)
    assert derive_fixture_abi_revision(tmp_path) == 7
    write(tmp_path, "three", 8)
    with pytest.raises(AssertionError, match="disagree"):
        derive_fixture_abi_revision(tmp_path)
    other = tmp_path / "other"
    write(other, "one", 7, None)
    with pytest.raises(AssertionError, match="no integer abi_revision"):
        derive_fixture_abi_revision(other)


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fetch"


@pytest.mark.skipif(
    not (FIXTURES / "expected.json").is_file(), reason=f"no fetch fixtures at {FIXTURES}"
)
def test_the_fixtures_are_fetched_at_their_own_revision_and_no_other(
    isolated_search_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_fixture_revision: int,
) -> None:
    """The override is wired and derived: the fixtures install at their own
    revision, and one past it the same release refuses UNPUBLISHED, naming
    both — they have nothing at any other revision."""
    keys = json.loads((FIXTURES / "expected.json").read_text())["trusted_keys"]
    monkeypatch.setenv("CHTYPES_TRUSTED_KEYS", ",".join(keys))
    url = f"file://{FIXTURES / 'signed'}"
    assert fetch_module._fetch_abi_revision() == at_fixture_revision
    assert Fetcher(dest=tmp_path / "ok", platform="linux-arm64", url=url).ensure("25.8")
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", at_fixture_revision + 1)
    with pytest.raises(ArtifactUnpublishedError) as err:
        Fetcher(dest=tmp_path / "no", platform="linux-arm64", url=url).ensure("25.8")
    assert f"ABI revision {at_fixture_revision + 1}" in str(err.value)
    assert f"only at ABI revision {at_fixture_revision}" in str(err.value)
    assert not (tmp_path / "no" / "25.8").exists()
