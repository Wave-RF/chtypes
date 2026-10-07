"""A retired repository (docs/guides/fetch-v1.md §2, "A retired repository";
public issue #571): the sanitizer against the table all four bindings read, and
a 410 over a real loopback server, which is never retried and never sent to the
next base."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import _translate_transport_error
from chtypes._ocifetch._errors import SourceRetiredError
from chtypes._ocifetch._http import (
    Clock,
    FetchPolicy,
    RetiredHttpError,
    RetryPolicy,
    fetch_from_bases,
)
from chtypes._ocifetch._retired import retired_message

TABLE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "retired-message" / "cases.json"
)

CONTRACT_MESSAGE = (
    "chtypes/v1 is retired: use chtypes/v2 (this registry no longer serves chtypes/v1)"
)
CONTRACT_BODY = json.dumps(
    {
        "errors": [
            {"code": "DENIED", "message": CONTRACT_MESSAGE, "detail": {"retired": "chtypes/v1"}}
        ]
    }
).encode()


def _table_cases() -> list[dict]:
    return json.loads(TABLE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _table_cases(), ids=lambda c: c["name"])
def test_retired_message_shared_table(case: dict) -> None:
    body = (
        case["body_text"].encode("utf-8")
        if "body_text" in case
        else bytes.fromhex(case["body_hex"])
    )
    assert retired_message(body) == case["message"]


def test_the_table_holds_the_required_kinds() -> None:
    names = {c["name"] for c in _table_cases()}
    for name in (
        "c0-escape-sequence",
        "bidi-override",
        "over-cap-300",
        "non-bmp-is-the-256th",
        "empty-string",
        "message-number",
    ):
        assert name in names


class _FakeClock(Clock):
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> float:
        return 1_700_000_000.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class _Server:
    """`routes[path]` is `(status, headers, body)`; every request is logged."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D102
                pass

            def do_GET(self) -> None:  # noqa: N802
                server.requests.append(self.path)
                status, headers, body = server.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._httpd.shutdown()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.stop()


def _get(bases, path: str, mode: str, clock: _FakeClock, max_bytes: int | None = None):
    return fetch_from_bases(
        bases,
        path,
        mode=mode,
        max_bytes=max_bytes,
        policy=FetchPolicy(clock=clock),
        retry=RetryPolicy(),
    )


@pytest.mark.parametrize("mode", ["tag", "digest", "alias"])
def test_a_410_is_retired_never_retried_and_never_sent_to_the_next_base(
    server: _Server, mode: str
) -> None:
    server.routes["/v2/gone/manifests/26.9"] = (
        410,
        {"Content-Type": "application/json"},
        CONTRACT_BODY,
    )
    server.routes["/v2/serving/manifests/26.9"] = (200, {}, b"{}")
    clock = _FakeClock()
    with pytest.raises(RetiredHttpError) as caught:
        # The request's own cap (16 bytes) does not apply to a 410's message.
        _get(
            (server.origin + "/gone", server.origin + "/serving"),
            "/manifests/26.9",
            mode,
            clock,
            16,
        )
    assert server.requests == ["/v2/gone/manifests/26.9"]
    assert clock.sleeps == []
    text = str(caught.value)
    assert f"{server.origin}/v2/gone/manifests/26.9 answered 410 Gone" in text
    assert text.endswith(f"; the registry says: {CONTRACT_MESSAGE}")
    translated = _translate_transport_error(caught.value)
    assert isinstance(translated, SourceRetiredError)
    assert translated.code == "CHTYPES_SOURCE_RETIRED"
    assert C.ERROR_EXIT_CODES[translated.code] == 10


def test_a_410_after_a_redirect_names_the_url_that_answered(server: _Server) -> None:
    server.routes["/v2/start/manifests/26.9"] = (302, {"Location": "/v2/gone/manifests/26.9"}, b"")
    server.routes["/v2/gone/manifests/26.9"] = (410, {}, b"")
    with pytest.raises(RetiredHttpError) as caught:
        _get((server.origin + "/start",), "/manifests/26.9", "tag", _FakeClock())
    text = str(caught.value)
    assert (
        text
        == f"{server.origin}/v2/gone/manifests/26.9 answered 410 Gone: the repository is retired"
    )


def test_only_the_cap_of_a_410_body_is_read(server: _Server) -> None:
    long = json.dumps({"errors": [{"message": "x" * C.RETIRED_BODY_MAX_BYTES}]}).encode()
    server.routes["/v2/gone/tags/list"] = (410, {}, long)
    with pytest.raises(RetiredHttpError) as caught:
        _get((server.origin + "/gone",), "/tags/list", "tag", _FakeClock())
    assert "registry says" not in str(caught.value)
