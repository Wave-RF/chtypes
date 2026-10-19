"""Build-level revocation (docs/guides/fetch-v1.md, "Build-level revocation";
public issue #625): the parts the conformance cases cannot reach. The 410 table
every binding reads, the in-process memo, concurrent checks of one build, the
5 s deadline (shortened here), the v1 contract doing nothing, the override never
pinning a lock, and a registry open refusing a stored record before it loads."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import chtypes.registry as registry_module
from chtypes import FetchOptions, Registry
from chtypes._ocifetch import _channel, _revocation
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import (
    Options,
    Resolved,
    check_installed,
    reset_revocation_memo_for_tests,
)
from chtypes._ocifetch._errors import ArtifactRevokedError, SourceUnreachableError
from chtypes._ocifetch._http import RetiredHttpError, RevokedHttpError, gone_error
from chtypes._ocifetch._layout import resolve_cache_root
from chtypes.errors import ArtifactRevokedError as PublicArtifactRevokedError

ROOT = Path(__file__).resolve().parents[3]
TABLE = ROOT / "tests" / "fixtures" / "revoked-410" / "cases.json"
FETCH_V2 = ROOT / "tests" / "fixtures" / "fetch-v2"
URL = "https://registry.example/v2/chtypes/v2/manifests/26.13"


@pytest.fixture
def prod_v2() -> Iterator[None]:
    """The production generation-2 contract, over the autouse v1 one."""
    restore = _channel.use_prod_v2_for_tests()
    reset_revocation_memo_for_tests()
    try:
        yield
    finally:
        reset_revocation_memo_for_tests()
        restore()


def _rows() -> list[dict]:
    return json.loads(TABLE.read_text(encoding="utf-8"))["cases"]


def _body(row: dict) -> bytes:
    return (
        row["body_text"].encode("utf-8") if "body_text" in row else bytes.fromhex(row["body_hex"])
    )


@pytest.mark.parametrize("row", _rows(), ids=lambda r: r["name"])
def test_revoked_410_table(row: dict) -> None:
    """The one table all four bindings answer alike: the code, the message's text
    and the advisories the typed error carries. Under the v1 contract every 410 is
    a retired repository."""
    error = gone_error(URL, 410, _body(row), revocation=True)
    assert gone_error(URL, 410, _body(row), revocation=False).__class__ is RetiredHttpError
    if row["revoked"] is None:
        assert row["code"] == "CHTYPES_SOURCE_RETIRED"
        assert isinstance(error, RetiredHttpError)
        return
    assert row["code"] == "CHTYPES_ARTIFACT_REVOKED"
    assert isinstance(error, RevokedHttpError)
    assert str(error) == f"{URL} answered 410 Gone: {row['revoked']['text']}"
    assert error.revocation.signed is False and error.revocation.manifest == ""
    got = [
        {
            "id": a.id,
            "url": a.url,
            "class": a.class_,
            "summary": a.summary,
            "build": a.build,
            "superseded_by": a.superseded_by,
        }
        for a in error.revocation.advisories
    ]
    assert got == row["revoked"]["advisories"]
    assert all(a.revoked_at == "" for a in error.revocation.advisories)


def test_the_table_has_revoked_rows() -> None:
    assert any(r["revoked"] is not None for r in _rows()), "the comparison above checked nothing"


def test_the_public_class_is_the_fetch_layers_code() -> None:
    assert (
        PublicArtifactRevokedError.code == ArtifactRevokedError.code == "CHTYPES_ARTIFACT_REVOKED"
    )
    assert C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_REVOKED"] == 11


class _Stub:
    """A registry that answers every request 404 and counts them; `hold`, when
    set, runs first (to stall, or to gate)."""

    def __init__(self, hold=None) -> None:  # noqa: ANN001
        self.requests = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D102
                pass

            def do_GET(self) -> None:  # noqa: N802
                with stub._lock:
                    stub.requests += 1
                if hold is not None:
                    hold()
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/chtypes/v2"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def stub_factory():
    made: list[_Stub] = []

    def make(hold=None) -> _Stub:  # noqa: ANN001
        s = _Stub(hold)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


def _installed_build(cache: Path, seed: str) -> Resolved:
    """A Resolved for a build installed in a fresh cache: what the installed-build
    check needs (its digests, predicate and entry)."""
    manifest = "sha256:" + hashlib.sha256(f"manifest {seed}".encode()).hexdigest()
    layer = "sha256:" + hashlib.sha256(f"layer {seed}".encode()).hexdigest()
    entry = resolve_cache_root(cache) / "unpacked" / "sha256" / manifest.split(":")[1]
    entry.mkdir(parents=True)
    return Resolved(
        abi_generation=2,
        platform="linux-arm64",
        request="26.13",
        version="26.13.1.1",
        channel=None,
        build="20261013.000001",
        library_path=entry / "libchtypes.so",
        dir=entry,
        digests={"manifest": manifest, "layer": layer, "index": None, "bundle": None},
        predicate={
            "abi": 2,
            "os": "linux",
            "arch": "arm64",
            "clickhouse_version": "26.13.1.1",
            "build": "20261013.000001",
        },
        signed_by="",
        source="cache",
        already_installed=True,
    )


def test_installed_check_once_per_build(prod_v2: None, stub_factory, tmp_path: Path) -> None:
    """The first check of a build makes its one listing (the API, then the fallback
    tag) and warns once; a second makes no request and prints nothing; a strict
    call never takes the kept "not checked", checks again and fails; a later call
    without strict mode is answered from what was kept."""
    stub = stub_factory()
    res = _installed_build(tmp_path, "once")
    opts = Options(bases=(stub.base,), cache_dir=tmp_path, system_dirs=())

    warnings = check_installed(res, opts)
    assert len(warnings) == 1
    assert "revocation status not checked for manifest " + res.digests["manifest"] in warnings[0]
    assert stub.requests == 2, "the referrers API and the fallback tag"
    assert check_installed(res, opts) == [] and stub.requests == 2

    strict = Options(
        bases=(stub.base,), cache_dir=tmp_path, system_dirs=(), require_revocation_check=True
    )
    with pytest.raises(SourceUnreachableError) as caught:
        check_installed(res, strict)
    assert "revocation status not checked" in str(caught.value)
    assert stub.requests == 4, "the strict check checks for itself"
    assert check_installed(res, opts) == [] and stub.requests == 4, "the kept outcome"


def test_concurrent_checks_share_one(prod_v2: None, stub_factory, tmp_path: Path) -> None:
    stub = stub_factory(hold=lambda: time.sleep(0.05))
    res = _installed_build(tmp_path, "concurrent")
    opts = Options(bases=(stub.base,), cache_dir=tmp_path, system_dirs=())
    counts: list[int] = []
    errors: list[BaseException] = []

    def check() -> None:
        try:
            counts.append(len(check_installed(res, opts)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=check) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert stub.requests == 2 and sum(counts) == 1, (stub.requests, counts)


def test_the_deadline_bounds_a_stalled_registry(
    prod_v2: None, stub_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that never answers costs at most the check's one deadline, not
    the retry table's minutes, and the open goes on with the warning. The deadline
    is shortened here; the production one is `REVOCATION_CHECK_S`."""
    assert C.REVOCATION_CHECK_S == 5
    monkeypatch.setattr(_revocation, "_check_deadline_s", 0.3)
    release = threading.Event()
    stub = stub_factory(hold=lambda: release.wait(10))
    res = _installed_build(tmp_path, "deadline")
    sleeps: list[float] = []

    class Clock:
        def now(self) -> float:
            return time.time()

        def sleep(self, seconds: float) -> None:
            sleeps.append(seconds)

    opts = Options(
        bases=(stub.base, stub.base + "-mirror"), cache_dir=tmp_path, system_dirs=(), clock=Clock()
    )
    start = time.monotonic()
    try:
        warnings = check_installed(res, opts)
    finally:
        release.set()
    elapsed = time.monotonic() - start
    assert len(warnings) == 1 and "passed its" in warnings[0], warnings
    assert elapsed < 2.0, f"the check took {elapsed:.2f}s, past its deadline"
    assert sleeps == [], "the check has no backoff"


def test_the_v1_contract_has_none_of_it(stub_factory, tmp_path: Path) -> None:
    """The check makes no request and says nothing, and a stored record is not read."""
    reset_revocation_memo_for_tests()
    stub = stub_factory()
    res = _installed_build(tmp_path, "v1")  # the autouse fixture speaks the v1 contract
    (Path(res.dir) / C.CACHE_REVOCATION_RECORD).write_text("{}")
    opts = Options(bases=(stub.base,), cache_dir=tmp_path, system_dirs=())
    assert check_installed(res, opts) == [] and stub.requests == 0


def test_offline_makes_no_request(prod_v2: None, stub_factory, tmp_path: Path) -> None:
    stub = stub_factory()
    res = _installed_build(tmp_path, "offline")
    opts = Options(bases=(stub.base,), cache_dir=tmp_path, system_dirs=(), offline=True)
    assert check_installed(res, opts) == [] and stub.requests == 0


def test_allow_revoked_never_pins_a_lock(prod_v2: None) -> None:
    """The override is dropped for a lock and an update, whatever the caller set."""
    assert not Options(allow_revoked=True, lock_write=True).resolved_allow_revoked()
    assert not Options(allow_revoked=True, update=True).resolved_allow_revoked()
    assert Options(allow_revoked=True).resolved_allow_revoked()


def test_reset_is_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="test-only"):
        reset_revocation_memo_for_tests()


def _stored_cache(tmp_path: Path) -> tuple[Path, str]:
    """A cache holding the fetch-v2 corpus's installed build with a stored
    revocation, and the test key that signed it."""
    layout = FETCH_V2 / "layouts" / "revocation-stored"
    key = (FETCH_V2 / "test-key" / "public.hex").read_text().strip()
    cache = tmp_path / "cache"
    shutil.copytree(layout, cache / C.PROD_V2_CACHE_DIR)
    return cache, key


def test_open_refuses_a_stored_revocation_before_load(
    prod_v2: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry open of a build whose entry holds a verifying record is refused
    with `ArtifactRevokedError` carrying the revocation, with no reachable base,
    before the loader ever reads the library."""
    cache, key = _stored_cache(tmp_path)
    monkeypatch.setattr(registry_module, "detect_host_platform", lambda: "linux-arm64")

    def loaded(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("the loader was reached for a revoked build")

    monkeypatch.setattr(registry_module, "open_image", loaded)
    reg = Registry(
        fetch=FetchOptions(
            cache_dir=cache,
            system_dirs=(),
            trusted_keys=[key],
            bases=("http://127.0.0.1:1/chtypes/v2",),
        ),
        autofetch=False,
    )
    with pytest.raises(PublicArtifactRevokedError) as caught:
        reg.for_version("26.13.2.1")
    rev = caught.value.revocation
    assert rev is not None and rev.signed and len(rev.advisories) == 1
    assert rev.advisories[0].id == "GHSA-fx01-test-0102"
    assert rev.advisories[0].class_ == "crash"
    assert rev.advisories[0].build == "20261013.000002"
    assert rev.advisories[0].revoked_at, "a signed revocation carries its revoked_at"
