"""`Registry.fetch`, the fetch-only call (public issue #492): it installs a build
without opening it, and a concurrent `fetch` and open of one request share one
fetch.

The build served here is a signed artifact whose "library" is not a library at
all, so nothing could load it: a `fetch` that tried to open it would fail. No
stub is needed, and none of these tests skips. The verdicts come from what the
fixture server logged and what each call returned or raised; every wait is on an
event (the server's parked count, the registry's fetch-wait hook), never on a
clock, and a bound only turns a hang into a failure.
"""

from __future__ import annotations

import hashlib
import json
import platform as host
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chtypes import (
    ArtifactError,
    ArtifactMissingError,
    ArtifactUnpublishedError,
    FetchOptions,
    Registry,
    _setup,
)
from chtypes._abi2 import _decls
from chtypes._ocifetch import _ensure, _hold, _prune
from chtypes._ocifetch._ensure import Request, detect_host_platform

from ._stub_tree import build_stub_tree
from .test_registry_flight import BOUND, SERVER, Flight

CASES = ("fetch-installs", "fetch-shared", "fetch-unpublished")
NOT_A_LIBRARY = b"chtypes registry fetch test: these bytes are not a loadable library\n"


@pytest.fixture
def served(tmp_path: Path, clean_process) -> Iterator[Flight]:
    """NOT_A_LIBRARY as tag 26.8, signed with the test key for this SDK's own
    fingerprint, under every case in CASES."""
    try:
        platform_key = detect_host_platform()
    except ValueError:
        pytest.skip(
            f"SKIPPED: this host ({host.system()}-{host.machine()}) is not a chtypes platform"
        )
    os_name, arch = platform_key.split("-")
    library = tmp_path / "not-a-library.so"
    library.write_bytes(NOT_A_LIBRARY)
    predicate = {
        "abi": _decls.CHS_ABI_VERSION,
        "abi_fingerprint": _decls.CHS_ABI_FINGERPRINT,
        "clickhouse_version": "26.8.15.10",
        "clickhouse_minor": "26.8",
        "channel": "lts",
        "build": "20261001.183455",
        "os": os_name,
        "arch": arch,
    }
    fixtures = tmp_path / "fixtures"
    tree = build_stub_tree(fixtures / "trees" / "fetch", str(library), predicate, tag="26.8")
    assert tree.predicate["library_sha256"] == hashlib.sha256(NOT_A_LIBRARY).hexdigest()
    (fixtures / "cases.json").write_text(
        json.dumps({"schema": 1, "cases": [{"id": c, "tree": "fetch"} for c in CASES]})
    )
    repo = fixtures / "trees" / "fetch" / "v2" / "chtypes" / "v1"
    (entry,) = json.loads((repo / "manifests" / "26.8").read_text())["manifests"]
    (layer,) = json.loads((repo / "manifests" / entry["digest"]).read_text())["layers"]
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--fixtures", str(fixtures), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        parts = line.split()
        assert len(parts) == 3 and parts[0] == "LISTENING", f"server.py printed {line!r}"
        yield Flight(
            origin=f"http://127.0.0.1:{parts[1]}",
            cache=tmp_path / "cache",
            trusted=tree.trusted.public_key.hex(),
            layer_path=f"/chtypes/v1/blobs/{layer['digest']}",
        )
    finally:
        proc.kill()
        proc.wait()


def test_fetch_installs_without_opening(served: Flight) -> None:
    """`fetch` installs the build and returns its record, and opens nothing: the
    library it installed cannot be loaded, no Library exists, and the process
    setup is never latched."""
    registry = served.registry("fetch-installs")
    resolved = registry.fetch("26.8")
    assert (resolved.version, resolved.build, resolved.request) == (
        "26.8.15.10",
        "20261001.183455",
        "26.8",
    )
    assert resolved.dir.name == resolved.digests["manifest"].split(":", 1)[1]
    assert Path(resolved.library_path).read_bytes() == NOT_A_LIBRARY
    assert registry.libraries() == ()
    assert not _setup._latched, "fetch latched the process setup; it must touch no image"
    options = registry._fetch._to_options(detect_host_platform())
    found = _ensure.resolve_installed(Request("26.8"), detect_host_platform(), options)
    assert found is not None and found.digests["manifest"] == resolved.digests["manifest"]
    # The build is held by this process, and nothing supersedes it.
    assert _prune.prune(options, keep=1) == []
    assert _hold.hold(resolved.dir) == _hold.HELD
    again = registry.fetch("26.8")
    assert again.digests["manifest"] == resolved.digests["manifest"] and again.already_installed
    assert served.requests("fetch-installs")["GET " + served.layer_path] == 1


def test_fetch_shares_one_fetch_with_an_open(served: Flight) -> None:
    """With the fetch held at the gate, a `fetch` and an open of the same request
    both wait on ONE fetch; when it lands the `fetch` has the build and the open
    goes on to its load (which fails: the bytes are no library), and the
    registry saw one tag request and one layer request."""
    registry = served.registry("fetch-shared")
    waiting = threading.Semaphore(0)
    registry._on_fetch_wait = lambda _request: waiting.release()
    with ThreadPoolExecutor(2) as pool, served.gate("fetch-shared"):
        fetched = pool.submit(registry.fetch, "26.8")
        assert waiting.acquire(timeout=BOUND), "the fetch never waited on the fetch"
        opened = pool.submit(registry.for_version, "26.8")
        assert waiting.acquire(timeout=BOUND), "the open never waited on the same fetch"
        assert served.wait_parked("fetch-shared", 1) == 1
        served.open_gate("fetch-shared")
        resolved = fetched.result(timeout=BOUND)
        refused = opened.exception(timeout=BOUND)
    assert resolved.version == "26.8.15.10"
    assert isinstance(refused, ArtifactError), f"the open of bytes that are no library: {refused!r}"
    requests = served.requests("fetch-shared")
    assert requests["GET /chtypes/v1/manifests/26.8"] == 1, requests
    assert requests["GET " + served.layer_path] == 1, requests


def test_fetch_errors_are_the_fetch_codes(served: Flight, tmp_path: Path) -> None:
    """A line nothing serves is the fetch layer's own code, and an offline fetch
    with nothing installed is the offline miss."""
    with pytest.raises(ArtifactUnpublishedError):
        served.registry("fetch-unpublished").fetch("26.3")
    offline = Registry(
        fetch=FetchOptions(cache_dir=tmp_path / "empty", system_dirs=(), offline=True)
    )
    with pytest.raises(ArtifactMissingError):
        offline.fetch("26.8")
