"""docs/fetch.md, tested offline against `spec/fixtures/fetch/` (§9).

Every fixture release is served over ``file://`` and must produce the spec's
verdict and code — read off `expected.json`, never restated here. The rest is
the contract around the chain: idempotence, force, atomic install with no
debris, the lock file, offline, the search path, lazy fetch, and the tar
extraction refusing traversal. The "libraries" in the fixtures are a few
bytes of text; nothing here dlopens.
"""

from __future__ import annotations

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

FIXTURES = Path(__file__).resolve().parents[2] / "spec" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"

pytestmark = pytest.mark.skipif(
    not EXPECTED_FILE.is_file(),
    reason=(
        f"no fetch fixtures at {FIXTURES}: spec/fixtures/fetch is generated in the core "
        f"repository (docs/fetch.md §9) and must be present for the fetch suite to run"
    ),
)


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


def test_the_signed_release_names_its_key_and_licence(dest: Path) -> None:
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


def test_lock_records_then_enforces(dest: Path, tmp_path: Path) -> None:
    kw = dict(platform=PLATFORM, url=_url("signed"), dest=dest, trusted_keys=_test_keys())
    lock = tmp_path / "chtypes.lock"
    ensure("25.8", lock=lock, **kw)
    pins = read_lock(lock)
    fixture_pins = read_lock(FIXTURES / "chtypes.lock")
    assert pins == {f"{PLATFORM}/25.8": fixture_pins[f"{PLATFORM}/25.8"]}
    assert json.loads(lock.read_text())["schema"] == 1

    # --frozen with the fixture lock installs signed/, every row.
    frozen_dest = tmp_path / "frozen"
    got = fetch_lines(
        all_lines=True, lock=FIXTURES / "chtypes.lock", frozen=True, **{**kw, "dest": frozen_dest}
    )
    assert [p.name for p in got] == list(_expected()["lines"])
    assert read_lock(FIXTURES / "chtypes.lock") == fixture_pins  # never written under --frozen

    # A lock whose sha256 differs refuses with CHTYPES_ARTIFACT_PINNED — before
    # anything is downloaded, and whether or not the line is already installed.
    drifted = tmp_path / "drifted.lock"
    doc = json.loads((FIXTURES / "chtypes.lock").read_text())
    doc["artifacts"][f"{PLATFORM}/25.8"]["sha256"] = "00" * 32
    drifted.write_text(json.dumps(doc))
    for frozen in (True, False):
        with pytest.raises(chtypes.ArtifactPinnedError) as caught:
            ensure("25.8", lock=drifted, frozen=frozen, **{**kw, "dest": tmp_path / "never"})
        assert caught.value.code == "CHTYPES_ARTIFACT_PINNED"
        with pytest.raises(chtypes.ArtifactPinnedError):
            ensure("25.8", lock=drifted, frozen=frozen, **kw)
    assert not (tmp_path / "never").exists()

    # --frozen refuses a line the lock does not pin; --frozen needs --lock.
    with pytest.raises(chtypes.ArtifactPinnedError, match="no pin"):
        ensure("26.7", lock=lock, frozen=True, **kw)
    with pytest.raises(ValueError, match="--frozen"):
        ensure("26.7", frozen=True, **kw)
    # A malformed lock is a usage error, loudly, not a silent "no pins".
    bad = tmp_path / "bad.lock"
    bad.write_text('{"schema": 2, "artifacts": {}}')
    with pytest.raises(ValueError, match="schema"):
        ensure("25.8", lock=bad, **kw)


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
    comment, raw = fetch_module._parse_signature(sig)
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
        fetch_module._parse_signature(b"untrusted comment: x\n!!!\n")
    with pytest.raises(chtypes.ArtifactUntrustedError, match="64"):
        fetch_module._parse_signature(b"untrusted comment: x\nAAAA\n")


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
    real = Path(registry.libraries()[-1].path).parent
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
    # Off by default, and CHTYPES_AUTOFETCH=1 turns it on.
    monkeypatch.setattr("chtypes.registry._AUTOFETCHED", set())
    other = tmp_path / "other"
    with pytest.raises(chtypes.ArtifactMissingError):
        chtypes.Registry(other).for_version(real.name)
    monkeypatch.setenv(chtypes.ENV_AUTOFETCH, "1")
    assert isinstance(chtypes.Registry(other).for_version(real.name), chtypes.Library)
    assert calls == [real.name, real.name]
    with pytest.raises(chtypes.ArtifactMissingError):
        chtypes.Registry(tmp_path / "third", autofetch=False).for_version(real.name)
