"""`scripts/fetch.sh` — chtypes#365: retrying a transient HTTP 5xx/408/429 or a
connection-level failure within the EXISTING §3a budget, honoring
`Retry-After`, and never retrying a 404/410 or a tarball hash mismatch.

fetch.sh has no suite of its own (see `test_fetch_sh.py`'s own note); this
file serves the shared `tests/fixtures/fetch/signed/` fixture over a small
flaky local HTTP server that can answer one path with an injected failure for
its next N requests — the same technique the Go/Python/TypeScript/Rust
binding suites use for their own chtypes#365 tests.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FETCH_SH = REPO / "scripts" / "fetch.sh"
FIXTURES = REPO / "tests" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"
SIGNED = FIXTURES / "signed"
PLATFORM = "linux-arm64"  # published by every fixture, whatever host runs this

pytestmark = pytest.mark.skipif(
    not (EXPECTED_FILE.is_file() and FETCH_SH.is_file() and shutil.which("bash")),
    reason=f"scripts/fetch.sh or its fixtures are absent ({FETCH_SH}, {FIXTURES}), or bash is",
)


def _expected() -> dict:
    return json.loads(EXPECTED_FILE.read_text()) if EXPECTED_FILE.is_file() else {}


def _signed_entry() -> dict:
    """The index row fetch.sh installs here: 25.8 for linux-arm64 from signed/."""
    index = json.loads((SIGNED / "index.json").read_text())
    (row,) = [
        a
        for a in index["artifacts"]
        if a["clickhouse_minor"] == "25.8" and a["arch"] == "arm64" and a["os"] == "linux"
    ]
    return row


class _FlakyHandler(http.server.SimpleHTTPRequestHandler):
    """Answers one path with an injected HTTP status (optionally with
    `Retry-After`), or drops the connection outright (a connection-level
    failure, from curl's view), for its next N requests — chtypes#365's own
    real transient blip, made real on loopback."""

    def log_message(self, *a: object, **k: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib's naming)
        name = self.path.lstrip("/")
        hits: dict[str, int] = self.server.hits  # type: ignore[attr-defined]
        hits[name] = hits.get(name, 0) + 1
        flakes: dict[str, dict] = self.server.flakes  # type: ignore[attr-defined]
        flake = flakes.get(name)
        if flake is not None and flake["remaining"] > 0:
            flake["remaining"] -= 1
            if flake.get("reset"):
                self.close_connection = True
                return  # drop the connection, unanswered: a reset, from curl's view
            self.send_response(flake["status"])
            if flake.get("retry_after") is not None:
                self.send_header("Retry-After", str(flake["retry_after"]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()


@pytest.fixture
def flaky_server() -> Iterator[tuple[str, dict[str, int], dict[str, dict]]]:
    def factory(*args: object, **kwargs: object) -> _FlakyHandler:
        return _FlakyHandler(*args, directory=str(SIGNED), **kwargs)  # type: ignore[arg-type]

    with socketserver.TCPServer(("127.0.0.1", 0), factory) as server:
        server.hits = {}  # type: ignore[attr-defined]
        server.flakes = {}  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield (f"http://127.0.0.1:{server.server_address[1]}", server.hits, server.flakes)  # type: ignore[attr-defined]
        finally:
            server.shutdown()


def _run_fetch_sh(
    url: str, dest: Path, *, extra_env: dict[str, str] | None = None, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"
    }
    env["CHTYPES_TRUSTED_KEYS"] = ",".join(_expected()["trusted_keys"])
    env["CHTYPES_CORE_DIR"] = str(dest / "no-core-here")
    env["XDG_CACHE_HOME"] = str(dest / "xdg")
    env.update(extra_env or {})
    return subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            "25.8",
            "--platform",
            PLATFORM,
            "--url",
            url,
            "--dest",
            str(dest / "reg"),
            "--abi-revision",
            str(_signed_entry()["abi_revision"]),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def test_fetch_sh_retries_transient_server_errors(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """The issue's own scenario: the artifacts host answers 500 for a short
    blip, then recovers. SHA256SUMS is read before index.json, so failing
    index.json alone exercises the whole retry path."""
    url, hits, flakes = flaky_server
    flakes["index.json"] = {"status": 500, "remaining": 2}
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0"})
    assert proc.returncode == 0, proc.stderr
    assert hits.get("index.json") == 3, f"want exactly 3 reads (2 failures + 1 success): {hits}"
    assert "attempt 1/" in proc.stderr
    assert "attempt 2/" in proc.stderr


def test_fetch_sh_honors_retry_after_delta_seconds(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """A short Retry-After, in delta-seconds, is honored in place of the
    doubling schedule's own (larger, here, so it is clearly not a
    coincidence) delay."""
    url, _hits, flakes = flaky_server
    flakes["index.json"] = {"status": 503, "retry_after": "1", "remaining": 1}
    start = time.monotonic()
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "10"})
    waited = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    assert 0.5 <= waited <= 8.0, f"waited {waited}s, want ~1s (Retry-After honored, not 10s)"


def test_fetch_sh_retry_after_longer_than_budget_fails_at_once(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """A Retry-After far longer than the retry budget could ever wait out is
    not honored by sleeping through it: fetch.sh fails at once, naming the
    requested delay, and reads the flaky asset exactly once."""
    url, hits, flakes = flaky_server
    flakes["index.json"] = {"status": 503, "retry_after": "9999", "remaining": 5}
    start = time.monotonic()
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "1"})
    elapsed = time.monotonic() - start
    assert proc.returncode == 3, proc.stderr
    assert "CHTYPES_SOURCE_UNREACHABLE" in proc.stderr
    assert "9999" in proc.stderr
    assert elapsed < 5.0, "a 9999s Retry-After was waited out instead of refused at once"
    assert hits.get("index.json") == 1, "no retry once the budget cannot fit the requested delay"
    assert not (tmp_path / "reg").exists() or not list((tmp_path / "reg").iterdir())


def test_fetch_sh_404_fails_on_the_first_attempt(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """A 404 is decided at once, never retried."""
    url, hits, flakes = flaky_server
    flakes["index.json"] = {"status": 404, "remaining": 99}
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0"})
    assert proc.returncode == 3, proc.stderr
    assert hits.get("index.json") == 1, "a 404 is never retried"
    assert not (tmp_path / "reg").exists() or not list((tmp_path / "reg").iterdir())


def test_fetch_sh_410_fails_on_the_first_attempt(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """The same as 404, for 410 (chtypes#365: "Never retry a 404 or 410")."""
    url, hits, flakes = flaky_server
    flakes["index.json"] = {"status": 410, "remaining": 99}
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0"})
    assert proc.returncode == 3, proc.stderr
    assert hits.get("index.json") == 1, "a 410 is never retried"
    assert not (tmp_path / "reg").exists() or not list((tmp_path / "reg").iterdir())


def test_fetch_sh_tarball_hash_mismatch_is_never_retried(tmp_path: Path) -> None:
    """A tarball whose hash disagrees with the signed release is the release
    lying about a byte, not a half-finished upload — never retried, even
    though downloading it raised no transient symptom at all."""
    entry = _signed_entry()
    corrupt_root = tmp_path / "corrupt-release"
    shutil.copytree(SIGNED, corrupt_root)
    tarball = corrupt_root / entry["file"]
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF
    tarball.write_bytes(bytes(data))

    # Serve the CORRUPTED copy directly, with no injected flakes.
    def factory(*args: object, **kwargs: object) -> _FlakyHandler:
        return _FlakyHandler(*args, directory=str(corrupt_root), **kwargs)  # type: ignore[arg-type]

    with socketserver.TCPServer(("127.0.0.1", 0), factory) as server:
        server.hits = {}  # type: ignore[attr-defined]
        server.flakes = {}  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            corrupt_url = f"http://127.0.0.1:{server.server_address[1]}"
            proc = _run_fetch_sh(
                corrupt_url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0"}
            )
            hits = server.hits  # type: ignore[attr-defined]
        finally:
            server.shutdown()
    assert proc.returncode == 1, proc.stderr
    assert "CHTYPES_ARTIFACT_CORRUPT" in proc.stderr
    assert hits.get(entry["file"]) == 1, "a hash mismatch is never retried"


def test_fetch_sh_retries_a_transient_tarball_download_failure(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """The tarball half of chtypes#365: a transient 502 on the asset itself
    (after SHA256SUMS/.sig/index.json all verified) is retried the same way
    a metadata blip is."""
    url, hits, flakes = flaky_server
    entry = _signed_entry()
    flakes[entry["file"]] = {"status": 502, "remaining": 2}
    proc = _run_fetch_sh(url, tmp_path, extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0"})
    assert proc.returncode == 0, proc.stderr
    assert hits.get(entry["file"]) == 3, f"want 3 downloads (2 failures + 1 success): {hits}"


def test_fetch_sh_tarball_connection_failure_exhausts_budget(
    flaky_server: tuple[str, dict[str, int], dict[str, dict]], tmp_path: Path
) -> None:
    """Every download of the tarball hits a connection-level failure (the
    connection is accepted, then dropped without a response): fetch.sh still
    fails CHTYPES_SOURCE_UNREACHABLE, naming the attempt count."""
    url, hits, flakes = flaky_server
    entry = _signed_entry()
    flakes[entry["file"]] = {"reset": True, "remaining": 1000}
    proc = _run_fetch_sh(
        url,
        tmp_path,
        extra_env={"CHTYPES_METADATA_RETRY_DELAY": "0", "CHTYPES_METADATA_ATTEMPTS": "3"},
    )
    assert proc.returncode == 3, proc.stderr
    assert "CHTYPES_SOURCE_UNREACHABLE" in proc.stderr
    assert "attempt" in proc.stderr
    assert hits.get(entry["file"]) == 3
