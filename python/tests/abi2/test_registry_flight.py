"""An open never waits for another request's fetch, and concurrent opens of one
request share one fetch (public issue #491).

Each test serves the stub, signed with the fetch fixtures' TEST key, from the
fetch fixture server (scripts/fetch-v1/server.py) over HTTP, and holds a fetch
in flight with the server's test-only gate: the request is logged and parked
until the test opens the gate. Every wait is on an event (the server's parked
count, the registry's wait hook, a future), never on a clock; a bound only
turns a hang into a failure. The verdicts come from what the server logged and
what each open returned. Python has no cancellation of an open, so there is no
cancellation case here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from chtypes import ArtifactError, FetchOptions, Registry

from ._stub_tree import build_stub_tree

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER = REPO_ROOT / "scripts" / "fetch-v1" / "server.py"
CASES = ("flight-install", "flight-held", "flight-one-fetch", "flight-fails")
BOUND = 30.0  # turns a hang into a failure; a working registry answers at once
N = 8


@dataclass
class Flight:
    origin: str
    cache: Path
    trusted: object
    layer_path: str

    def registry(self, case_id: str) -> Registry:
        """Autofetch on, the one base `case_id`'s repository on the server."""
        return Registry(
            fetch=FetchOptions(
                bases=(f"{self.origin}/s-{case_id}/chtypes/v1",),
                cache_dir=self.cache,
                system_dirs=(),
                trusted_keys=(self.trusted,),  # type: ignore[arg-type]
            ),
            autofetch=True,
        )

    def control(self, path: str) -> object:
        with urllib.request.urlopen(self.origin + path, timeout=BOUND + 5) as r:
            return json.loads(r.read())

    @contextmanager
    def gate(self, case_id: str) -> Iterator[None]:
        """Hold `case_id`'s requests at the server; the gate opens on exit,
        whatever happened, so no thread is left parked."""
        self.control(f"/_gate/close/s-{case_id}")
        try:
            yield
        finally:
            self.open_gate(case_id)

    def open_gate(self, case_id: str) -> None:
        self.control(f"/_gate/open/s-{case_id}")

    def wait_parked(self, case_id: str, n: int) -> int:
        """Once n of `case_id`'s requests are parked at its gate: how many are."""
        doc = self.control(f"/_gate/parked/s-{case_id}?n={n}")
        assert isinstance(doc, dict)
        return doc["parked"]

    def requests(self, case_id: str) -> Counter[str]:
        """`case_id`'s logged requests, "METHOD path" relative to its segment."""
        log = self.control(f"/_log/s-{case_id}")
        assert isinstance(log, list)
        prefix = f"/v2/s-{case_id}"
        return Counter(f"{e['method']} {e['path'].removeprefix(prefix)}" for e in log)


@pytest.fixture
def flight(stub_copy, tmp_path) -> Iterator[Flight]:
    path, predicate = stub_copy()
    fixtures = tmp_path / "fixtures"
    tree = build_stub_tree(fixtures / "trees" / "stub", path, predicate, tag="26.8")
    cases = [{"id": case_id, "tree": "stub"} for case_id in CASES]
    (fixtures / "cases.json").write_text(json.dumps({"schema": 1, "cases": cases}))
    repo = fixtures / "trees" / "stub" / "v2" / "chtypes" / "v1"
    (platform,) = json.loads((repo / "manifests" / "26.8").read_text())["manifests"]
    manifest = json.loads((repo / "manifests" / platform["digest"]).read_text())
    (layer,) = manifest["layers"]
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
            trusted=tree.trusted,
            layer_path=f"/chtypes/v1/blobs/{layer['digest']}",
        )
    finally:
        proc.kill()
        proc.wait()


def test_an_installed_line_never_waits_for_another_lines_fetch(flight: Flight) -> None:
    """The issue's regression: with 26.3's fetch held at the gate, an open of
    26.8, which the cache answers, returns while 26.3's request is parked."""
    flight.registry("flight-install").for_version("26.8")  # install 26.8
    registry = flight.registry("flight-held")
    with ThreadPoolExecutor(2) as pool, flight.gate("flight-held"):
        held = pool.submit(registry.for_version, "26.3")
        flight.wait_parked("flight-held", 1)
        installed = pool.submit(registry.for_version, "26.8")
        try:
            library = installed.result(timeout=BOUND)
        except FutureTimeout:
            pytest.fail(f"for_version('26.8'), installed, waited {BOUND} s for 26.3's fetch")
        assert library.version == "26.8.15.10"
        assert not held.done(), f"for_version('26.3') returned while its fetch was held: {held}"
        assert flight.wait_parked("flight-held", 1) == 1
        flight.open_gate("flight-held")
        assert isinstance(held.exception(timeout=BOUND), ArtifactError)
    made = [r for r in flight.requests("flight-held") if "/manifests/26.8" in r]
    assert made == [], f"the open of installed 26.8 made requests: {made}"


def test_concurrent_opens_of_one_request_share_one_fetch(flight: Flight) -> None:
    """N opens of one uninstalled request, all waiting while its fetch is
    held, make one fetch and all get its Library."""
    registry = flight.registry("flight-one-fetch")
    waiting = threading.Semaphore(0)
    registry._on_wait = lambda _request: waiting.release()
    with ThreadPoolExecutor(N) as pool, flight.gate("flight-one-fetch"):
        futures = [pool.submit(registry.for_version, "26.8") for _ in range(N)]
        for i in range(N):
            assert waiting.acquire(timeout=BOUND), f"open {i + 1} of {N} never waited"
        parked = flight.wait_parked("flight-one-fetch", 1)
        assert parked == 1, f"{parked} requests parked with {N} opens waiting"
        flight.open_gate("flight-one-fetch")
        libraries = [f.result(timeout=BOUND) for f in futures]
    assert all(lib is libraries[0] for lib in libraries), "one request, one Library"
    requests = flight.requests("flight-one-fetch")
    assert requests["GET /chtypes/v1/manifests/26.8"] == 1, requests
    assert requests["GET " + flight.layer_path] == 1, requests
    assert set(requests.values()) == {1}, f"a path requested twice: {requests}"
    assert registry.for_version("26.8") is libraries[0]
    assert registry.libraries() == (libraries[0],)


def test_a_failed_fetch_reaches_every_waiter_and_is_not_remembered(flight: Flight) -> None:
    """N opens of a request nothing serves share one failing fetch and all
    raise its error; the next open makes a new request."""
    registry = flight.registry("flight-fails")
    waiting = threading.Semaphore(0)
    registry._on_wait = lambda _request: waiting.release()
    with ThreadPoolExecutor(N) as pool, flight.gate("flight-fails"):
        futures = [pool.submit(registry.for_version, "26.3") for _ in range(N)]
        for i in range(N):
            assert waiting.acquire(timeout=BOUND), f"open {i + 1} of {N} never waited"
        flight.wait_parked("flight-fails", 1)
        flight.open_gate("flight-fails")
        raised = [f.exception(timeout=BOUND) for f in futures]
    first = raised[0]
    assert isinstance(first, ArtifactError), raised
    assert all(exc is first for exc in raised), f"one attempt, one error: {raised}"
    tag = "GET /chtypes/v1/manifests/26.3"
    assert flight.requests("flight-fails")[tag] == 1
    with pytest.raises(type(first)) as again:
        registry.for_version("26.3")
    assert str(again.value) == str(first)
    assert flight.requests("flight-fails")[tag] == 2, "a failure is never remembered"
