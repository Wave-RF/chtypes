"""docs/guides/fetch.md, tested offline against `tests/fixtures/fetch/` (§9).

Every fixture release is served over ``file://`` and must produce the spec's
verdict and code — read off `expected.json`, never restated here. The rest is
the contract around the chain: idempotence, force, atomic install with no
debris, the lock file, offline, the search path, lazy fetch, and the tar
extraction refusing traversal. The "libraries" in the fixtures are a few
bytes of text; nothing here dlopens.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import shutil
import socketserver
import tarfile
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

import chtypes
from chtypes import fetch as fetch_module
from chtypes.fetch import (
    Fetcher,
    ensure,
    fetch_lines,
    installed_lines,
    read_lock,
    verify_registry,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"

pytestmark = [
    pytest.mark.skipif(
        not EXPECTED_FILE.is_file(),
        reason=(
            f"no fetch fixtures at {FIXTURES}: tests/fixtures/fetch is generated in the core "
            f"repository (docs/guides/fetch.md §9) and must be present for the fetch suite to run"
        ),
    ),
    # Fetch selects only rows at the binding's own ABI revision; every test here
    # fetches at the revision the fixtures carry, read off their own index.json.
    pytest.mark.usefixtures("at_fixture_revision"),
]


def _expected() -> dict:
    return json.loads(EXPECTED_FILE.read_text())


def _url(fixture: str) -> str:
    return f"file://{FIXTURES / fixture}"


def _test_keys() -> list[str]:
    return list(_expected()["trusted_keys"])


PLATFORM = "linux-arm64"  # published by every fixture, whatever host runs this


@pytest.fixture
def dest(isolated_search_path: Path, tmp_path: Path) -> Path:
    """A fresh registry directory, on an isolated search path."""
    return tmp_path / "registry"


@pytest.fixture
def signed_entry() -> dict:
    """The index row the tests below install: 25.8 for linux-arm64 from signed/."""
    index = json.loads((FIXTURES / "signed" / "index.json").read_text())
    (row,) = [
        a
        for a in index["artifacts"]
        if a["clickhouse_minor"] == "25.8" and a["arch"] == "arm64"
        if a["os"] == "linux"
    ]
    return row


def _assert_installed(directory: Path, row: dict) -> None:
    manifest = chtypes.read_manifest(directory)
    assert manifest is not None
    assert manifest.library == row["library"]
    assert manifest.clickhouse_version == row["clickhouse_version"]
    library = directory / manifest.library
    assert hashlib.sha256(library.read_bytes()).hexdigest() == row["library_sha256"]
    assert (directory / "CH_VERSION").is_file()
    chtypes.verify_library(directory)


# --------------------------------------------------------- §9: every fixture


@pytest.mark.parametrize(
    "case",
    (_expected().get("builds") or {}).get("cases", []) if EXPECTED_FILE.is_file() else [],
    ids=lambda c: f"{c['platform']}-{c['line']}",
)
def test_a_rebuild_installs_the_highest_build(case: dict, dest: Path) -> None:
    """`two-builds/` publishes one ClickHouse version twice, as a rebuild does.

    A release lists every build it publishes per (version, platform), so resolving
    a line is not a question about the ClickHouse version alone: among rows of
    the newest version the highest `build` wins. Before this, nothing in any
    suite covered that — the rule lived only in unit tests of the comparator.

    The proof is the library's own bytes. Both rows carry the same
    `clickhouse_version`, so a manifest check cannot tell them apart; their
    `library_sha256` differs, and `_assert_installed` hashes what landed. An
    implementation that took the first matching row, or the older build, fails
    here rather than passing quietly.
    """
    installed = ensure(
        case["line"],
        platform=case["platform"],
        url=_url("two-builds"),
        dest=dest,
        trusted_keys=_test_keys(),
    )
    assert installed == dest / case["line"]

    index = json.loads((FIXTURES / "two-builds" / "index.json").read_text())
    (want,) = [a for a in index["artifacts"] if a["file"] == case["install"]["file"]]
    (other,) = [a for a in index["artifacts"] if a["file"] == case["superseded"]["file"]]
    assert want["build"] > other["build"], "the fixture's own case is upside down"
    assert want["clickhouse_version"] == other["clickhouse_version"] == case["clickhouse_version"]

    _assert_installed(installed, want)
    # Say the negative outright: the superseded build is not what is on disk.
    library = installed / chtypes.read_manifest(installed).library  # type: ignore[union-attr]
    assert hashlib.sha256(library.read_bytes()).hexdigest() != other["library_sha256"]


# ------------------------------------------- §2: two-revisions/, the filter itself


def _revisions() -> dict:
    return (_expected().get("revisions") or {}) if EXPECTED_FILE.is_file() else {}


def _pick_at(revisions: dict, case: dict, rev: int) -> dict:
    """expected.json's row for ``case`` at revision ``rev``: low or high."""
    if rev == revisions["low_revision"]:
        return case["at_low_revision"]
    if rev == revisions["high_revision"]:
        return case["at_high_revision"]
    raise AssertionError(f"revision {rev} is neither low nor high in expected.json")


def _fetch_at_revision(
    revisions: dict, case: dict, rev: int, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch one line of two-revisions/ with fetch selecting at ``rev``, and check
    that what was chosen and what landed is expected.json's row for it: the
    file, its sha256, the row's own build and abi_revision, the library bytes."""
    want = _pick_at(revisions, case, rev)
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", rev)
    fetcher = Fetcher(
        dest=dest,
        platform=case["platform"],
        url=_url(revisions["fixture"]),
        trusted_keys=_test_keys(),
    )
    entry = fetcher.release().select(case["line"], case["platform"])
    assert (entry.file, entry.sha256, entry.build_number, entry.abi_revision) == (
        want["file"],
        want["sha256"],
        want["build"],
        want["abi_revision"],
    ), (case["platform"], case["line"], rev)
    assert want["abi_revision"] == rev
    installed = fetcher.ensure(case["line"])
    index = json.loads((FIXTURES / revisions["fixture"] / "index.json").read_text())
    (row,) = [a for a in index["artifacts"] if a["file"] == want["file"]]
    _assert_installed(installed, row)


@pytest.mark.parametrize(
    ("case", "which"),
    [(c, w) for c in _revisions().get("cases", []) for w in ("low_revision", "high_revision")],
    ids=lambda v: (
        v if isinstance(v, str) else f"{v['platform']}-{v['line']}"  # (case, which) halves
    ),
)
def test_two_revisions_pick_the_row_at_each_revision(
    case: dict, which: str, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every case of expected.json's revisions, at the low and at the high
    revision: the pick is at_low_revision / at_high_revision — every expected
    value read from expected.json."""
    revisions = _revisions()
    _fetch_at_revision(revisions, case, revisions[which], dest, monkeypatch)


@pytest.mark.parametrize(
    "discriminating",
    _revisions().get("discriminating_lines", []),
    ids=lambda d: f"abi{d['revision']}-{d['line']}",
)
def test_two_revisions_filter_changes_the_answer(
    discriminating: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the discriminating lines expected.json names, the pick at that
    revision DIFFERS from the unfiltered one: the filter decided it, not two
    rules that happened to agree."""
    revisions = _revisions()
    cases = [c for c in revisions["cases"] if c["line"] == discriminating["line"]]
    assert cases, f"no case for discriminating line {discriminating['line']}"
    for i, case in enumerate(cases):
        want = _pick_at(revisions, case, discriminating["revision"])
        assert want["file"] != case["unfiltered"]["file"], (
            f"{case['platform']} {case['line']}: expected.json's pick is the unfiltered one"
        )
        _fetch_at_revision(
            revisions, case, discriminating["revision"], tmp_path / str(i), monkeypatch
        )


def test_two_revisions_one_past_the_high_revision_is_unpublished(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: nothing is served at high_revision + 1."""
    revisions = _revisions()
    assert revisions.get("cases"), "expected.json carries no revisions cases"
    case = revisions["cases"][0]
    monkeypatch.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", revisions["high_revision"] + 1)
    with pytest.raises(chtypes.ArtifactUnpublishedError) as err:
        ensure(
            case["line"],
            platform=case["platform"],
            url=_url(revisions["fixture"]),
            dest=dest,
            trusted_keys=_test_keys(),
        )
    assert f"ABI revision {revisions['high_revision'] + 1} (this SDK's)" in str(err.value)
    assert not (dest / case["line"]).exists()


@pytest.mark.parametrize(
    "verdict",
    _expected()["verdicts"] if EXPECTED_FILE.is_file() else [],
    ids=lambda v: (
        f"{v['fixture']}-{v['trusted_keys']}-{'unsigned-ok' if v['allow_unsigned'] else 'strict'}"
    ),
)
def test_every_fixture_gives_the_spec_verdict(verdict: dict, dest: Path) -> None:
    keys = _test_keys() if verdict["trusted_keys"] == "test" else None  # None = the embedded key
    kwargs = dict(
        platform=PLATFORM,
        url=_url(verdict["fixture"]),
        dest=dest,
        trusted_keys=keys,
        allow_unsigned=verdict["allow_unsigned"],
    )
    if verdict["code"] is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", chtypes.UnsignedArtifactWarning)
            installed = ensure("25.8", **kwargs)
        assert installed == dest / "25.8"
        index = json.loads((FIXTURES / verdict["fixture"] / "index.json").read_text())
        (row,) = [
            a for a in index["artifacts"] if a["file"] == f"chtypes-25.8.28.1-lts-{PLATFORM}.tar.gz"
        ]
        _assert_installed(installed, row)
        assert installed_lines(dest).keys() == {"25.8"}
        return
    with pytest.raises(chtypes.ArtifactError) as caught:
        ensure("25.8", **kwargs)
    assert caught.value.code == verdict["code"], verdict["why"]
    assert isinstance(caught.value, chtypes.RegistryError)
    # A refused release installs NOTHING and leaves no debris behind.
    assert not dest.exists() or list(dest.iterdir()) == []


def test_the_signed_release_names_its_key_and_license(dest: Path) -> None:
    lines: list[str] = []
    fetcher = Fetcher(
        platform=PLATFORM,
        url=_url("signed"),
        dest=dest,
        trusted_keys=_test_keys(),
        progress=lines.append,
    )
    release = fetcher.release()
    assert release.signed_by == _expected()["key_id"]
    assert release.tag == _expected()["release_tag"]
    assert release.license == "Elastic-2.0"
    assert [e.minor for e in release.offered(PLATFORM)] == list(_expected()["lines"])
    assert any("signature verified" in ln and release.signed_by in ln for ln in lines)
    assert any("Elastic-2.0" in ln for ln in lines)
    assert fetcher.release() is release  # read once per Fetcher


def test_allow_unsigned_warns_exactly_once_naming_the_source(dest: Path) -> None:
    lines: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        installed = ensure(
            "25.8",
            platform=PLATFORM,
            url=_url("unsigned"),
            dest=dest,
            trusted_keys=_test_keys(),
            allow_unsigned=True,
            progress=lines.append,
        )
    assert installed.is_dir()
    warned = [w for w in caught if issubclass(w.category, chtypes.UnsignedArtifactWarning)]
    assert len(warned) == 1
    assert str(FIXTURES / "unsigned") in str(warned[0].message)
    assert "CHTYPES_ALLOW_UNSIGNED" in str(warned[0].message)
    assert sum("WARNING" in ln and str(FIXTURES / "unsigned") in ln for ln in lines) == 1


def test_allow_unsigned_comes_from_the_environment(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(fetch_module.ENV_ALLOW_UNSIGNED, "1")
    monkeypatch.setenv(fetch_module.ENV_TRUSTED_KEYS, ",".join(_test_keys()))
    with pytest.warns(chtypes.UnsignedArtifactWarning):
        assert ensure("25.8", platform=PLATFORM, url=_url("unsigned"), dest=dest).is_dir()
    monkeypatch.setenv(fetch_module.ENV_ALLOW_UNSIGNED, "0")
    with pytest.raises(chtypes.ArtifactUntrustedError):
        ensure("26.7", platform=PLATFORM, url=_url("unsigned"), dest=dest)


def test_unpublished_platform_line_and_patch(dest: Path) -> None:
    un = _expected()["unpublished"]
    for spelling, platform in (
        (un["line"], PLATFORM),
        (un["patch"], PLATFORM),
        ("25.8", un["platform"]),
    ):
        with pytest.raises(chtypes.ArtifactUnpublishedError) as caught:
            ensure(
                spelling,
                platform=platform,
                url=_url("signed"),
                dest=dest,
                trusted_keys=_test_keys(),
            )
        assert caught.value.code == un["code"] == "CHTYPES_ARTIFACT_UNPUBLISHED"
    assert not dest.exists()


def test_a_line_resolves_to_its_patch_and_an_exact_patch_is_exact(dest: Path) -> None:
    kw = dict(platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys())
    patch = _expected()["lines"]["25.8"]
    first = ensure("25.8", **kw)
    assert ensure(patch, **kw) == first
    assert ensure(f"v{patch}", **kw) == first
    assert ensure(patch.split("-")[0], **kw) == first  # channel suffix omitted
    with pytest.raises(chtypes.ArtifactUnpublishedError, match="exactly"):
        ensure("25.8.99.1-lts", **kw)
    with pytest.raises(ValueError):
        ensure("not-a-version", **kw)
    with pytest.raises(ValueError):
        ensure("25", **kw)


# ---------------------------------------------------------- idempotence, force


def test_ensure_is_idempotent_repairs_and_force_redownloads(dest: Path, signed_entry: dict) -> None:
    lines: list[str] = []
    kw = dict(
        platform=PLATFORM,
        url=_url("signed"),
        dest=dest,
        trusted_keys=_test_keys(),
        progress=lines.append,
    )
    installed = ensure("25.8", **kw)
    assert any(ln.startswith("downloading") for ln in lines)

    lines.clear()
    assert ensure("25.8", **kw) == installed
    assert any(ln.startswith("already installed and verified") for ln in lines)
    assert not any(ln.startswith("downloading") for ln in lines)

    # A corrupted install is not "installed": it is replaced and re-verified.
    library = installed / signed_entry["library"]
    library.write_bytes(b"truncated")
    lines.clear()
    assert ensure("25.8", **kw) == installed
    assert any("replacing" in ln for ln in lines) and any(
        ln.startswith("downloading") for ln in lines
    )
    _assert_installed(installed, signed_entry)

    lines.clear()
    assert ensure("25.8", force=True, **kw) == installed
    assert any(ln.startswith("downloading") for ln in lines)
    _assert_installed(installed, signed_entry)


def test_install_is_atomic_and_leaves_no_debris(dest: Path, signed_entry: dict) -> None:
    kw = dict(platform=PLATFORM, dest=dest, trusted_keys=_test_keys())
    installed = ensure("25.8", url=_url("signed"), **kw)
    assert sorted(p.name for p in dest.iterdir()) == ["25.8"]
    before = (installed / signed_entry["library"]).read_bytes()

    # A tampered release must not disturb the good install: the failure
    # happens in the temporary sibling, which is removed.
    with pytest.raises(chtypes.ArtifactCorruptError):
        ensure("25.8", url=_url("tampered-tarball"), force=True, **kw)
    assert sorted(p.name for p in dest.iterdir()) == ["25.8"]
    assert (installed / signed_entry["library"]).read_bytes() == before
    _assert_installed(installed, signed_entry)

    # And a sibling line lands beside it without touching it.
    ensure("26.7", url=_url("signed"), **kw)
    assert sorted(p.name for p in dest.iterdir()) == ["25.8", "26.7"]
    assert [m for m, _, err in verify_registry(dest) if err is None] == ["25.8", "26.7"]


def test_fetch_all_installs_every_published_line(dest: Path) -> None:
    got = fetch_lines(
        all_lines=True, platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys()
    )
    assert [p.name for p in got] == list(_expected()["lines"])
    assert installed_lines(dest).keys() == set(_expected()["lines"])
    with pytest.raises(ValueError):
        fetch_lines(["25.8"], all_lines=True, url=_url("signed"), dest=dest)
    with pytest.raises(ValueError):
        fetch_lines([], url=_url("signed"), dest=dest)


# -------------------------------------------------------------------- §5 lock


def test_lock_records_then_enforces(
    dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK#284 §6: schema 2, keyed by the EXACT patch. The committed fixture
    lock (``tests/fixtures/fetch/chtypes.lock``) is still schema 1, keyed by
    line — `read_lock` converts it to schema-2 keys transparently, and the
    first WRITE through a schema-1 file rewrites it as schema 2 wholesale."""
    kw = dict(platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys())
    patch_258 = _expected()["lines"]["25.8"]
    raw_key_258 = f"{PLATFORM}/25.8"  # the schema-1 (line-keyed) spelling
    key = f"{PLATFORM}/{patch_258}"  # the schema-2 (exact-patch-keyed) spelling
    lock = tmp_path / "chtypes.lock"
    ensure("25.8", lock=lock, **kw)
    pins = read_lock(lock)
    fixture_pins = read_lock(FIXTURES / "chtypes.lock")
    # The committed fixture lock predates abi_revision (docs/guides/fetch.md
    # §5); a fresh fetch pins the same file and sha256, plus this binding's
    # own ABI revision — the one new thing it now records.
    assert "abi_revision" not in fixture_pins[key]
    assert pins[key]["file"] == fixture_pins[key]["file"]
    assert pins[key]["sha256"] == fixture_pins[key]["sha256"]
    assert pins[key]["abi_revision"] == chtypes.ABI_REVISION
    # The SDK always WRITES schema 2, whatever schema it read (§6).
    assert json.loads(lock.read_text())["schema"] == 2

    # --frozen with the fixture lock installs signed/, every row it pins —
    # the fixture lock is schema 1 and is read (converted), never written.
    frozen_dest = tmp_path / "frozen"
    got = fetch_lines(
        all_lines=True, lock=FIXTURES / "chtypes.lock", frozen=True, **{**kw, "dest": frozen_dest}
    )
    assert [p.name for p in got] == list(_expected()["lines"])
    assert read_lock(FIXTURES / "chtypes.lock") == fixture_pins  # never written under --frozen
    assert json.loads((FIXTURES / "chtypes.lock").read_text())["schema"] == 1  # untouched on disk

    # A lock whose sha256 differs refuses with CHTYPES_ARTIFACT_PINNED — before
    # anything is downloaded, and whether or not the line is already installed.
    drifted = tmp_path / "drifted.lock"
    doc = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc["artifacts"][raw_key_258]["sha256"] = "00" * 32
    drifted.write_text(json.dumps(doc))
    with pytest.raises(chtypes.ArtifactPinnedError) as caught:
        ensure("25.8", lock=drifted, frozen=True, **{**kw, "dest": tmp_path / "never"})
    assert caught.value.code == "CHTYPES_ARTIFACT_PINNED"
    with pytest.raises(chtypes.ArtifactPinnedError):
        ensure("25.8", lock=drifted, frozen=True, **kw)
    assert not (tmp_path / "never").exists()

    # Without --frozen, --lock re-pins: the drifted entry is replaced by what
    # this fetch installed, which is the remedy the PINNED message names —
    # and the schema-1 lock converts to schema 2 in the same write.
    ensure("25.8", lock=drifted, **kw)
    repinned = read_lock(drifted)[key]
    assert repinned["file"] == fixture_pins[key]["file"]
    assert repinned["sha256"] == fixture_pins[key]["sha256"]
    assert repinned["abi_revision"] == chtypes.ABI_REVISION
    assert json.loads(drifted.read_text())["schema"] == 2
    ensure("25.8", lock=drifted, frozen=True, **kw)  # and --frozen now accepts it

    # A lock naming another ABI revision re-pins the same way without --frozen.
    other_rev = tmp_path / "other-rev.lock"
    doc = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc["artifacts"][raw_key_258]["abi_revision"] = chtypes.ABI_REVISION + 1
    other_rev.write_text(json.dumps(doc))
    with pytest.raises(chtypes.ArtifactPinnedError, match="re-lock with"):
        ensure("25.8", lock=other_rev, frozen=True, **kw)
    ensure("25.8", lock=other_rev, **kw)
    assert read_lock(other_rev)[key]["abi_revision"] == chtypes.ABI_REVISION

    # --frozen refuses a line the lock does not pin. Without a lock path it
    # reads ./chtypes.lock, and none there pins nothing — refused, PINNED.
    with pytest.raises(chtypes.ArtifactPinnedError, match="pins nothing"):
        ensure("26.7", lock=lock, frozen=True, **kw)
    nolock = tmp_path / "nolock"
    nolock.mkdir()
    monkeypatch.chdir(nolock)
    with pytest.raises(chtypes.ArtifactPinnedError, match="no lock file at chtypes.lock"):
        ensure("26.7", frozen=True, **kw)
    (nolock / "chtypes.lock").write_bytes((FIXTURES / "chtypes.lock").read_bytes())
    assert ensure("25.8", frozen=True, **kw) == dest / "25.8"  # ./chtypes.lock pins signed/
    # A malformed lock is a usage error, loudly, not a silent "no pins" — and
    # schema 2 is no longer one of the malformed numbers (SDK#284 reads it).
    bad = tmp_path / "bad.lock"
    bad.write_text('{"schema": 3, "artifacts": {}}')
    with pytest.raises(ValueError, match="schema"):
        ensure("25.8", lock=bad, **kw)


def test_lock_abi_revision_mismatch_is_named_and_needs_no_source(
    dest: Path, tmp_path: Path, at_fixture_revision: int
) -> None:
    """Issue #253: a lock written by an SDK at one ABI revision must not be
    silently accepted, or surface as a bare drifted-pin or unpublished-line
    error, once the SDK speaks another."""
    kw = dict(platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys())
    key = f"{PLATFORM}/25.8"

    # (b) A lock entry at a different revision: PINNED, naming both numbers,
    # and — proven by pointing at a source that cannot be read — refused
    # WITHOUT ever reading a release.
    other = at_fixture_revision + 1
    mismatched = tmp_path / "mismatched.lock"
    doc = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc["artifacts"][key]["abi_revision"] = other
    mismatched.write_text(json.dumps(doc))
    with pytest.raises(chtypes.ArtifactPinnedError) as caught:
        ensure(
            "25.8",
            lock=mismatched,
            frozen=True,
            platform=PLATFORM,
            url="file://" + str(tmp_path / "does-not-exist"),
            dest=tmp_path / "reg2",
            trusted_keys=_test_keys(),
        )
    msg = str(caught.value)
    assert f"ABI revision {other}" in msg
    assert f"ABI revision {at_fixture_revision}" in msg
    assert "re-lock with:" in msg
    assert caught.value.code == "CHTYPES_ARTIFACT_PINNED"

    # (c) A lock entry with no abi_revision at all (an older SDK's lock): the
    # old path, plus one appended sentence — for a real drift (PINNED) and
    # for a line the release does not offer at this revision (UNPUBLISHED).
    no_rev_drift = tmp_path / "no-rev-drift.lock"
    doc2 = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc2["artifacts"][key]["sha256"] = "00" * 32
    no_rev_drift.write_text(json.dumps(doc2))
    with pytest.raises(chtypes.ArtifactPinnedError) as caught:
        ensure("25.8", lock=no_rev_drift, frozen=True, **{**kw, "dest": tmp_path / "reg3"})
    msg = str(caught.value)
    assert "records no ABI revision" in msg
    assert "older SDK" in msg
    assert f"ABI revision {at_fixture_revision}" in msg
    # The appended sentence starts its own sentence — never glued onto the
    # one before it with no punctuation between them.
    assert f"deliberately. {no_rev_drift} records no ABI revision" in msg

    # 24.8 is not among signed/'s published lines (expected.json's "lines"
    # names only 25.8 and 26.7), so this is a genuine UNPUBLISHED. The file
    # name must still parse under the asset grammar — SDK#284's schema-1 ->
    # schema-2 key conversion needs it to recover the exact patch the old
    # entry pinned — so it names a plausible, still-unpublished patch.
    no_rev_unpublished = tmp_path / "no-rev-unpublished.lock"
    no_rev_unpublished.write_text(
        json.dumps(
            {
                "schema": 1,
                "artifacts": {
                    f"{PLATFORM}/24.8": {
                        "file": f"chtypes-24.8.1.1-{PLATFORM}.tar.gz",
                        "sha256": "00" * 32,
                    }
                },
            }
        )
    )
    with pytest.raises(chtypes.ArtifactUnpublishedError) as caught:
        ensure("24.8", lock=no_rev_unpublished, frozen=True, **{**kw, "dest": tmp_path / "reg4"})
    msg = str(caught.value)
    assert "records no ABI revision" in msg
    assert "older SDK" in msg
    assert f". {no_rev_unpublished} records no ABI revision" in msg

    # (d) A lock entry at the SDK's own (matching) revision installs exactly
    # as before.
    matching = tmp_path / "matching.lock"
    doc3 = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc3["artifacts"][key]["abi_revision"] = at_fixture_revision
    matching.write_text(json.dumps(doc3))
    got = ensure("25.8", lock=matching, frozen=True, **{**kw, "dest": tmp_path / "reg5"})
    assert got == tmp_path / "reg5" / "25.8"

    # (e) --offline --frozen stays unaffected: no source is ever read, so a
    # mismatched-revision lock is simply not consulted against one.
    offline_dest = tmp_path / "reg6"
    ensure("25.8", lock=tmp_path / "plain.lock", **{**kw, "dest": offline_dest})
    got = ensure(
        "25.8",
        dest=offline_dest,
        platform=PLATFORM,
        lock=mismatched,
        frozen=True,
        offline=True,
        trusted_keys=_test_keys(),
    )
    assert got == offline_dest / "25.8"


# ---------------------------------------------------------------- offline


def test_offline_never_touches_the_source(dest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kw = dict(platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys())
    touched: list[str] = []

    def spy(self: object, name: str) -> bytes | None:
        touched.append(name)
        raise AssertionError(f"offline must not read {name}")

    ensure("25.8", **kw)  # online first
    monkeypatch.setattr(fetch_module._Source, "read", spy)
    monkeypatch.setattr(fetch_module._Source, "download", spy)
    assert ensure("25.8", offline=True, **kw) == dest / "25.8"
    with pytest.raises(chtypes.SourceUnreachableError) as caught:
        ensure("26.7", offline=True, **kw)
    assert caught.value.code == "CHTYPES_SOURCE_UNREACHABLE"
    with pytest.raises(chtypes.SourceUnreachableError):
        ensure("25.8.99.1-lts", offline=True, **kw)  # installed, but not that patch
    assert touched == []
    # A corrupted install is not "installed" offline either.
    (dest / "25.8" / "libchtypes.so").write_bytes(b"x")
    with pytest.raises(chtypes.ArtifactCorruptError):
        ensure("25.8", offline=True, **kw)


# ------------------------------------------------------------- http sources


@pytest.fixture
def http_release() -> Iterator[str]:
    """The signed fixture, served over real HTTP on a loopback port."""
    handler = type(
        "Quiet",
        (http.server.SimpleHTTPRequestHandler,),
        {"log_message": lambda *a, **k: None},
    )
    root = str(FIXTURES / "signed")

    def factory(*args: object, **kwargs: object) -> http.server.SimpleHTTPRequestHandler:
        return handler(*args, directory=root, **kwargs)  # type: ignore[arg-type]

    with socketserver.TCPServer(("127.0.0.1", 0), factory) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()


def test_http_source_goes_through_the_same_chain(
    dest: Path, http_release: str, signed_entry: dict
) -> None:
    lines: list[str] = []
    installed = ensure(
        "25.8",
        platform=PLATFORM,
        url=http_release,
        dest=dest,
        trusted_keys=_test_keys(),
        progress=lines.append,
    )
    _assert_installed(installed, signed_entry)
    assert any("signature verified" in ln for ln in lines)
    # A wrong path under a live host is "no release there", not a crash.
    with pytest.raises(chtypes.SourceUnreachableError, match="SHA256SUMS"):
        ensure(
            "25.8",
            platform=PLATFORM,
            url=http_release + "/nope",
            dest=dest,
            trusted_keys=_test_keys(),
        )


class _CountingHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a directory like ``SimpleHTTPRequestHandler``, counting each
    path's requests and — once a name has been served for the FIRST time —
    running a registered hook synchronously, right after those response bytes
    are written. That is what lets a test heal a file the instant the bad
    version has actually been read, instead of guessing at wall-clock timing
    (the same technique the Go suite's ``afterFirstServe`` uses)."""

    def log_message(self, *a: object, **k: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib's naming)
        name = self.path.lstrip("/")
        hits: dict[str, int] = self.server.hits  # type: ignore[attr-defined]
        hits[name] = hits.get(name, 0) + 1
        first = hits[name] == 1
        super().do_GET()
        if first:
            hook = self.server.hooks.get(name)  # type: ignore[attr-defined]
            if hook is not None:
                hook()


@contextlib.contextmanager
def _hooked_release_server(root: Path) -> Iterator[tuple[str, dict[str, int], dict]]:
    """A release directory served over real HTTP, with per-path hit counts and
    an after-first-serve hook: the mid-publish window, made real."""

    def factory(*args: object, **kwargs: object) -> _CountingHandler:
        return _CountingHandler(*args, directory=str(root), **kwargs)  # type: ignore[arg-type]

    with socketserver.TCPServer(("127.0.0.1", 0), factory) as server:
        server.hits = {}  # type: ignore[attr-defined]
        server.hooks = {}  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield (
                f"http://127.0.0.1:{server.server_address[1]}",
                server.hits,  # type: ignore[attr-defined]
                server.hooks,  # type: ignore[attr-defined]
            )
        finally:
            server.shutdown()


# ---------------------------------------------- the golden-set publish window
#
# A fixed, offline-generated ed25519 keypair and signature — not the shared
# fixtures' test key (its private half is not in this repository) and never
# the release key. Generated once with
# `openssl genpkey -algorithm ed25519` + `openssl pkeyutl -sign -rawin`
# over the exact SHA256SUMS bytes below;
# test_fetch_golden_window_fixture_is_internally_consistent pins that it still
# verifies under `chtypes._ed25519.verify`.
_GOLDEN_WINDOW_PUBKEY = "49102573bd3fe2d0a90e05bf91f79fc7a27c5bebc529ef6f57546f543f2a8bf4"
_GOLDEN_WINDOW_KEYID = "f5886bc874cb68e4"
_GOLDEN_WINDOW_GOOD = b'{"generated":{"at":"2026-09-26T00:00:00Z"},"cases":[]}\n'
_GOLDEN_WINDOW_STALE = b'{"generated":{"at":"2026-09-01T00:00:00Z"},"cases":[]}\n'
# SHA256SUMS lists only the golden set's hash; nothing in these tests installs
# an artifact, so index.json publishes none.
_GOLDEN_WINDOW_SUMS = (
    hashlib.sha256(_GOLDEN_WINDOW_GOOD).hexdigest() + "  sdk-goldens.json\n"
).encode()
_GOLDEN_WINDOW_SIG = (
    f"untrusted comment: chtypes artifacts, ed25519 key {_GOLDEN_WINDOW_KEYID}\n"
    "eZhX7upf/FMnvDeDN8p1kCKf4wP2U8dDupfjCMQVM6xitL/UYgwDY2yyFtbbBk9UBeIAymGRyMaiE86L0WIMDg==\n"
).encode()


def test_fetch_golden_window_fixture_is_internally_consistent() -> None:
    """Pins the offline-generated fixture above: the signature really does
    verify over SHA256SUMS under the embedded public key. A failure here means
    the fixture was hand-edited inconsistently — regenerate it, don't patch it."""
    from chtypes._ed25519 import verify

    _, sig = fetch_module.parse_signature_file(_GOLDEN_WINDOW_SIG)
    assert verify(bytes.fromhex(_GOLDEN_WINDOW_PUBKEY), _GOLDEN_WINDOW_SUMS, sig)


def _write_golden_window_release(root: Path, goldens: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_bytes(b'{"schema": 1, "artifacts": []}')
    (root / "SHA256SUMS").write_bytes(_GOLDEN_WINDOW_SUMS)
    (root / "SHA256SUMS.sig").write_bytes(_GOLDEN_WINDOW_SIG)
    (root / "sdk-goldens.json").write_bytes(goldens)


def test_fetch_goldens_retries_a_stale_edge_cache_pairing_then_installs(
    dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new window symptom (docs/guides/fetch.md §3a): SHA256SUMS, its
    signature and index.json all agree throughout, but the bytes actually
    SERVED for the golden set are stale on the first read — exactly what an
    edge cache does to a release-level file just after a republish.
    `install_goldens` must re-read the whole set (not just re-fetch the
    goldens file against the first read's now-stale SHA256SUMS) and heal once
    the source catches up."""
    monkeypatch.setattr(fetch_module, "RELEASE_RETRY_DELAY", 0.01)
    root = tmp_path / "release"
    _write_golden_window_release(root, _GOLDEN_WINDOW_STALE)

    with _hooked_release_server(root) as (url, hits, hooks):
        hooks["sdk-goldens.json"] = lambda: (root / "sdk-goldens.json").write_bytes(
            _GOLDEN_WINDOW_GOOD
        )
        lines: list[str] = []
        f = Fetcher(
            platform=PLATFORM,
            url=url,
            dest=dest,
            trusted_keys=[_GOLDEN_WINDOW_PUBKEY],
            progress=lines.append,
        )
        f.release()  # the base metadata only; nothing to install here
        path = f.install_goldens()

    assert path == dest / "sdk-goldens.json"
    assert path is not None and path.read_bytes() == _GOLDEN_WINDOW_GOOD
    assert hits["sdk-goldens.json"] >= 2, "the retry did not happen"
    assert any("attempt 1" in ln for ln in lines)


def test_fetch_goldens_window_that_never_heals_leaves_golden_tests_skipping(
    dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window, but it never closes: `sdk-goldens.json` keeps hashing
    to something SHA256SUMS does not say, on every attempt. `install_goldens`
    must still never raise — a release-level file is best-effort — must not
    install anything, and must actually have retried through every attempt
    rather than refusing once and giving up silently."""
    monkeypatch.setattr(fetch_module, "RELEASE_RETRY_DELAY", 0.01)
    root = tmp_path / "release"
    _write_golden_window_release(root, _GOLDEN_WINDOW_STALE)  # never becomes GOOD

    with _hooked_release_server(root) as (url, hits, _hooks):
        lines: list[str] = []
        f = Fetcher(
            platform=PLATFORM,
            url=url,
            dest=dest,
            trusted_keys=[_GOLDEN_WINDOW_PUBKEY],
            progress=lines.append,
        )
        f.release()
        path = f.install_goldens()

    assert path is None
    assert not (dest / "sdk-goldens.json").exists()
    assert hits["sdk-goldens.json"] == fetch_module.RELEASE_LOAD_ATTEMPTS
    assert any("hashes to" in ln and "SHA256SUMS says" in ln for ln in lines)


def test_unreachable_host_is_source_unreachable(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "_HTTP_ATTEMPTS", 1)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda s: None)
    with pytest.raises(chtypes.SourceUnreachableError) as caught:
        ensure("25.8", platform=PLATFORM, url="http://127.0.0.1:1/artifacts", dest=dest)
    assert caught.value.code == "CHTYPES_SOURCE_UNREACHABLE"
    assert not dest.exists()


def test_the_default_source_is_the_artifacts_host_plus_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fetch_module.ENV_ARTIFACTS_URL, raising=False)
    assert (
        Fetcher().source.base == f"{fetch_module.DEFAULT_ARTIFACTS_URL}/{fetch_module.DEFAULT_TAG}"
    )
    assert Fetcher(tag="v1.2.0").source.base == f"{fetch_module.DEFAULT_ARTIFACTS_URL}/v1.2.0"
    monkeypatch.setenv(fetch_module.ENV_ARTIFACTS_URL, "https://mirror.example/chtypes/")
    assert Fetcher().source.base == "https://mirror.example/chtypes/artifacts"
    assert Fetcher(url="/some/dir").source.kind == "file"  # a plain directory
    with pytest.raises(ValueError, match="pass one"):
        Fetcher(url="https://x", tag="v1")
    with pytest.raises(ValueError, match="platform"):
        Fetcher(platform="windows-x64")
    with pytest.raises(ValueError, match="scheme"):
        Fetcher(url="ftp://x/y")


# ------------------------------------------------------------ §4 key policy


def test_trusted_keys_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fetch_module.ENV_TRUSTED_KEYS, raising=False)
    assert fetch_module.trusted_keys() == (bytes.fromhex(chtypes.RELEASE_PUBLIC_KEY),)
    test_key = _test_keys()[0]
    monkeypatch.setenv(fetch_module.ENV_TRUSTED_KEYS, f"{test_key}, {chtypes.RELEASE_PUBLIC_KEY}")
    assert fetch_module.trusted_keys() == (
        bytes.fromhex(test_key),
        bytes.fromhex(chtypes.RELEASE_PUBLIC_KEY),
    )  # REPLACES the embedded list — the release key is there only because it was named
    assert fetch_module.trusted_keys([test_key]) == (bytes.fromhex(test_key),)
    for bad in (["zz"], ["abcd"], [""]):
        with pytest.raises(ValueError):
            fetch_module.trusted_keys(bad)


def test_signature_file_shape() -> None:
    sig = (FIXTURES / "signed" / "SHA256SUMS.sig").read_bytes()
    comment, raw = fetch_module.parse_signature_file(sig)
    assert comment == f"chtypes artifacts, ed25519 key {_expected()['key_id']}"
    assert len(raw) == 64
    sums = (FIXTURES / "signed" / "SHA256SUMS").read_bytes()
    assert (
        fetch_module.verify_sums_signature(sums, sig, fetch_module.trusted_keys(_test_keys()), "x")
        == (_expected()["key_id"])
    )
    with pytest.raises(chtypes.ArtifactUntrustedError):
        fetch_module.verify_sums_signature(
            sums + b"\n", sig, fetch_module.trusted_keys(_test_keys()), "x"
        )
    with pytest.raises(chtypes.ArtifactUntrustedError, match="base64"):
        fetch_module.parse_signature_file(b"untrusted comment: x\n!!!\n")
    with pytest.raises(chtypes.ArtifactUntrustedError, match="64"):
        fetch_module.parse_signature_file(b"untrusted comment: x\nAAAA\n")


# ------------------------------------------------------------- tar safety


def _tarball(members: list[tuple[str, bytes | None, str]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                assert data is not None
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    return buf.getvalue()


@pytest.mark.parametrize(
    "members",
    [
        [("../escape", b"x", "file")],
        [("/abs/escape", b"x", "file")],
        [("sub/../../escape", b"x", "file")],
        [("link", None, "symlink")],
        [("ok\\evil", b"x", "file")],
    ],
    ids=["dotdot", "absolute", "nested-dotdot", "symlink", "backslash"],
)
def test_tar_extraction_refuses_traversal_and_links(tmp_path: Path, members: list) -> None:
    tarball = tmp_path / "evil.tar.gz"
    tarball.write_bytes(_tarball([("manifest.json", b"{}", "file"), *members]))
    into = tmp_path / "into"
    into.mkdir()
    with pytest.raises(chtypes.ArtifactCorruptError, match="not unpacking"):
        fetch_module._extract(tarball, into, "evil.tar.gz")
    assert list(into.iterdir()) == []  # nothing extracted, nothing escaped
    assert not (tmp_path / "escape").exists()


def test_tar_extraction_accepts_plain_files_and_directories(tmp_path: Path) -> None:
    tarball = tmp_path / "ok.tar.gz"
    tarball.write_bytes(
        _tarball([("d", None, "dir"), ("d/f", b"hi", "file"), ("./g", b"yo", "file")])
    )
    into = tmp_path / "into"
    into.mkdir()
    fetch_module._extract(tarball, into, "ok.tar.gz")
    assert (into / "d" / "f").read_bytes() == b"hi"
    assert (into / "g").read_bytes() == b"yo"
    (tmp_path / "garbage.tar.gz").write_bytes(b"not a tarball")
    with pytest.raises(chtypes.ArtifactCorruptError, match="readable"):
        fetch_module._extract(tmp_path / "garbage.tar.gz", into, "garbage.tar.gz")


# -------------------------------------------------- the registry, lazily


def test_registry_serves_a_fetched_line_from_the_first_directory_that_has_it(
    dest: Path, isolated_search_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1: the first directory holding the line wins; the fixtures' fake
    libraries cannot dlopen, so what is asserted is WHICH directory the
    registry resolves each line to."""
    kw = dict(platform=PLATFORM, url=_url("signed"), trusted_keys=_test_keys())
    ensure("25.8", dest=dest, **kw)
    ensure("26.7", dest=isolated_search_path, **kw)
    ensure("26.7", dest=dest, **kw)
    reg = chtypes.Registry(dest)
    assert reg.versions() == ("25.8", "26.7")
    assert reg.search_path == (dest, isolated_search_path)
    assert reg._index == {"25.8": dest / "25.8", "26.7": dest / "26.7"}
    # Both present: the explicit directory shadows the cache for 26.7 …
    shutil.rmtree(dest / "26.7")
    reg = chtypes.Registry(dest)
    assert reg._index == {"25.8": dest / "25.8", "26.7": isolated_search_path / "26.7"}
    # … and a line installed AFTER construction is found on the next open.
    reg = chtypes.Registry(isolated_search_path)
    assert "25.8" not in reg
    ensure("25.8", dest=isolated_search_path, **kw)
    with pytest.raises(chtypes.RegistryError) as caught:
        reg.for_version("25.8")  # found, then refused by dlopen: a fake library
    assert not isinstance(caught.value, chtypes.ArtifactMissingError)
    assert "25.8" in reg


def test_autofetch_runs_ensure_once_per_line_under_one_lock(
    dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: chtypes.Registry
) -> None:
    """§6: opening a missing line runs `ensure` first, once per process per
    line, however many threads open it. The fetch is faked to stage a REAL
    artifact (a symlink to one this suite already loads), so the open then
    succeeds and the same Library comes back to every thread."""
    real = Path(registry.for_version(registry.versions()[-1]).path).parent
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class FakeFetcher:
        def __init__(self, *, dest: Path) -> None:
            self.dest = dest

        def ensure(self, spelling: str) -> Path:
            calls.append(spelling)
            entered.set()
            assert release.wait(timeout=10)
            self.dest.mkdir(parents=True, exist_ok=True)
            (self.dest / real.name).symlink_to(real)
            return self.dest / real.name

    monkeypatch.setattr("chtypes.registry.Fetcher", FakeFetcher)
    monkeypatch.setattr("chtypes.registry._AUTOFETCHED", set())
    reg = chtypes.Registry(dest, autofetch=True)
    assert real.name not in reg
    results: list[object] = []

    def opener() -> None:
        try:
            results.append(reg.for_version(real.name))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            results.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(3)]
    for t in threads:
        t.start()
    # One opener is inside the (held-open) fetch; the other two are queued on
    # the lock, not fetching: exactly one call, no result yet.
    assert entered.wait(timeout=10)
    for t in threads:
        t.join(timeout=0.2)
    assert calls == [real.name]
    assert results == []
    release.set()
    for t in threads:
        t.join(timeout=30)
    assert calls == [real.name]
    assert all(r is results[0] for r in results), results
    assert isinstance(results[0], chtypes.Library)
    # Once per process: a second registry over the same destination never
    # re-fetches (the line is there), and a broken fetch would not loop.
    assert real.name in chtypes.Registry(dest, autofetch=True)
    assert calls == [real.name]
    # Off by default, and CHTYPES_AUTOFETCH=1 turns it on. With autofetch off
    # an empty registry fails at CONSTRUCTION, not at the open: #50 moved that
    # error earlier so a mistyped directory is named before any request, and
    # all four bindings answer the same way (Rust has always had EmptyRegistry).
    monkeypatch.setattr("chtypes.registry._AUTOFETCHED", set())
    other = tmp_path / "other"
    with pytest.raises(chtypes.RegistryError):
        chtypes.Registry(other).for_version(real.name)
    monkeypatch.setenv(chtypes.ENV_AUTOFETCH, "1")
    assert isinstance(chtypes.Registry(other).for_version(real.name), chtypes.Library)
    assert calls == [real.name, real.name]
    with pytest.raises(chtypes.RegistryError):
        chtypes.Registry(tmp_path / "third", autofetch=False).for_version(real.name)
