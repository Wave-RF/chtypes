"""The transport: retry math, `Retry-After` parsing, and (over a real
loopback HTTP server, pure Python, no fixtures needed) retries, 404
tag-vs-digest semantics, and dropping `Authorization` on redirect."""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from chtypes._ocifetch._http import (
    Clock,
    FetchPolicy,
    ForbiddenHttpError,
    NotFoundHttpError,
    RetryPolicy,
    UnauthorizedHttpError,
    UnreachableHttpError,
    _parse_bearer_challenge,
    _parse_retry_after,
    fetch_from_bases,
)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._now = 1_700_000_000.0

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds


# ---------------------------------------------------------------------------
# Pure-function tests: retry math, Retry-After parsing, bearer challenges.
# ---------------------------------------------------------------------------


def test_retry_policy_wait_sequence_doubles_from_first_wait() -> None:
    policy = RetryPolicy()
    assert [policy.wait_before_attempt(i) for i in range(4)] == [4.0, 8.0, 16.0, 32.0]


def test_retry_policy_remaining_budget() -> None:
    policy = RetryPolicy()
    assert policy.remaining_budget_s(0) == 4.0 + 8.0 + 16.0 + 32.0
    assert policy.remaining_budget_s(3) == 32.0  # one wait left, before the 5th (last) attempt
    assert policy.remaining_budget_s(4) == 0.0  # no attempts left to wait before


def test_parse_retry_after_seconds_form() -> None:
    clock = FakeClock()
    assert _parse_retry_after({"Retry-After": "2"}, clock) == 2.0


def test_parse_retry_after_http_date_form_relative_to_date_header() -> None:
    clock = FakeClock()
    headers = {
        "Date": "Tue, 15 Nov 2022 08:12:31 GMT",
        "Retry-After": "Tue, 15 Nov 2022 08:12:34 GMT",
    }
    assert _parse_retry_after(headers, clock) == pytest.approx(3.0)


def test_parse_retry_after_http_date_form_falls_back_to_now() -> None:
    clock = FakeClock()
    # now() is 1_700_000_000.0 (2023-11-14T22:13:20Z); ask for +5s from there.
    import email.utils

    target = email.utils.formatdate(clock.now() + 5, usegmt=True)
    assert _parse_retry_after({"Retry-After": target}, clock) == pytest.approx(5.0, abs=1.0)


def test_parse_retry_after_missing_header_returns_none() -> None:
    assert _parse_retry_after({}, FakeClock()) is None


def test_parse_bearer_challenge() -> None:
    header = 'Bearer realm="https://auth.example/token",service="registry",scope="repo:pull"'
    params = _parse_bearer_challenge(header)
    assert params == {
        "realm": "https://auth.example/token",
        "service": "registry",
        "scope": "repo:pull",
    }


def test_parse_bearer_challenge_rejects_non_bearer() -> None:
    assert _parse_bearer_challenge('Basic realm="x"') is None


def test_parse_bearer_challenge_requires_realm() -> None:
    assert _parse_bearer_challenge('Bearer service="registry"') is None


# ---------------------------------------------------------------------------
# file:// transport, no server needed.
# ---------------------------------------------------------------------------


def test_fetch_from_bases_file_scheme_success(tmp_path: Path) -> None:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "26.8").write_bytes(b'{"ok": true}')
    policy = FetchPolicy(clock=FakeClock())
    resp = fetch_from_bases(
        (f"file://{tmp_path}",),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=RetryPolicy(),
    )
    assert resp.body == b'{"ok": true}'


def test_fetch_from_bases_file_scheme_missing_is_not_found(tmp_path: Path) -> None:
    policy = FetchPolicy(clock=FakeClock())
    with pytest.raises(NotFoundHttpError):
        fetch_from_bases(
            (f"file://{tmp_path}",),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_fetch_from_bases_rejects_disallowed_scheme() -> None:
    policy = FetchPolicy(clock=FakeClock())
    with pytest.raises(UnreachableHttpError, match="scheme"):
        fetch_from_bases(
            ("ftp://example.com",),
            "/x",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_fetch_from_bases_no_bases_is_unreachable() -> None:
    with pytest.raises(UnreachableHttpError):
        fetch_from_bases((), "/x", mode="tag", policy=FetchPolicy(), retry=RetryPolicy())


# ---------------------------------------------------------------------------
# A real loopback HTTP server, for retries/redirects/auth.
# ---------------------------------------------------------------------------


class _Server:
    """A minimal scripted HTTP server: `routes[path]` is a list of response
    callables, consumed in order (the last repeats), each returning
    `(status, headers, body)`."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Callable[[], tuple[int, dict[str, str], bytes]]]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._httpd = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # noqa: D102
                pass

            def do_GET(self) -> None:  # noqa: N802
                server.requests.append((self.path, dict(self.headers.items())))
                route_path = self.path.split("?", 1)[0]
                # Every real request carries the OCI distribution-spec `/v2`
                # prefix the client splices in itself (docs/guides/fetch-v1.md
                # §10); route keys below are repository-relative, so strip it
                # here. A request with no `/v2` prefix is a client bug and
                # correctly 404s, same as an unmatched route.
                if route_path.startswith("/v2/") or route_path == "/v2":
                    route_path = route_path[len("/v2") :]
                responses = server.routes.get(route_path)
                if not responses:
                    self.send_response(404)
                    self.end_headers()
                    return
                item = responses[0] if len(responses) == 1 else responses.pop(0)
                status, headers, body = item() if callable(item) else item
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

        return Handler

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.stop()


def _ok(body: bytes = b"ok") -> tuple[int, dict[str, str], bytes]:
    return 200, {}, body


def test_http_basic_success(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [_ok(b'{"ok":1}')]
    policy = FetchPolicy(clock=FakeClock())
    resp = fetch_from_bases(
        (server.base_url,),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=RetryPolicy(),
    )
    assert resp.body == b'{"ok":1}'


def test_http_tag_404_raises_not_found(server: _Server) -> None:
    policy = FetchPolicy(clock=FakeClock())
    with pytest.raises(NotFoundHttpError):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.99",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_http_digest_404_on_only_base_is_retried_then_unreachable(server: _Server) -> None:
    clock = FakeClock()
    policy = FetchPolicy(clock=clock)
    retry = RetryPolicy(attempts=3, first_wait_s=0.01, multiplier=2.0)
    with pytest.raises(UnreachableHttpError):
        fetch_from_bases(
            (server.base_url,),
            "/blobs/sha256:" + "a" * 64,
            mode="digest",
            policy=policy,
            retry=retry,
        )
    assert clock.sleeps == [0.01, 0.02]


def test_http_retries_503_then_succeeds(server: _Server) -> None:
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            return 503, {}, b""
        return _ok(b"finally")

    server.routes["/manifests/26.8"] = [flaky, flaky, flaky]
    clock = FakeClock()
    policy = FetchPolicy(clock=clock)
    retry = RetryPolicy(attempts=5, first_wait_s=0.01, multiplier=2.0)
    resp = fetch_from_bases(
        (server.base_url,),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=retry,
    )
    assert resp.body == b"finally"
    assert clock.sleeps == [0.01, 0.02]


def test_http_retry_after_seconds_is_honored(server: _Server) -> None:
    # Retry-After is RFC 9110 delta-seconds: a non-negative INTEGER, never a
    # fraction. The FakeClock never really sleeps, so a 1s value here still
    # runs the test in milliseconds; what matters is that it overrides the
    # much larger planned backoff below.
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            return 429, {"Retry-After": "1"}, b""
        return _ok()

    server.routes["/manifests/26.8"] = [flaky, flaky]
    clock = FakeClock()
    policy = FetchPolicy(clock=clock)
    retry = RetryPolicy(attempts=5, first_wait_s=10.0, multiplier=2.0)
    fetch_from_bases(
        (server.base_url,),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=retry,
    )
    assert clock.sleeps == [1.0]


def test_http_retry_after_over_budget_refuses_without_sleeping(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [lambda: (429, {"Retry-After": "99999"}, b"")]
    clock = FakeClock()
    policy = FetchPolicy(clock=clock)
    retry = RetryPolicy(attempts=5, first_wait_s=1.0, multiplier=2.0)
    with pytest.raises(UnreachableHttpError, match="exceeds"):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=retry,
        )
    assert clock.sleeps == []


def test_http_no_retry_on_400(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [lambda: (400, {}, b"")]
    clock = FakeClock()
    policy = FetchPolicy(clock=clock)
    with pytest.raises(UnreachableHttpError, match="not retryable"):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )
    assert clock.sleeps == []


def test_http_download_token_401_is_unauthorized(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [lambda: (401, {}, b"")]
    policy = FetchPolicy(token="my-token", clock=FakeClock())
    with pytest.raises(UnauthorizedHttpError):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_http_download_token_403_is_forbidden(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [lambda: (403, {}, b"")]
    policy = FetchPolicy(token="my-token", clock=FakeClock())
    with pytest.raises(ForbiddenHttpError):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_http_download_token_is_sent(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [_ok()]
    policy = FetchPolicy(token="my-secret-token", clock=FakeClock())
    fetch_from_bases(
        (server.base_url,),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=RetryPolicy(),
    )
    _path, headers = server.requests[-1]
    assert headers.get("Authorization") == "Bearer my-secret-token"


def test_http_anon_token_flow(server: _Server) -> None:
    challenge = f'Bearer realm="{server.base_url}/token",service="registry"'
    first_try = {"done": False}

    def manifests():
        if not first_try["done"]:
            first_try["done"] = True
            return 401, {"WWW-Authenticate": challenge}, b""
        return _ok(b"with-token")

    server.routes["/manifests/26.8"] = [manifests, manifests]
    server.routes["/token"] = [lambda: (200, {}, b'{"token": "anon-tok-123"}')]
    policy = FetchPolicy(clock=FakeClock())  # no static token: eligible for the anon flow
    resp = fetch_from_bases(
        (server.base_url,),
        "/manifests/26.8",
        mode="tag",
        policy=policy,
        retry=RetryPolicy(),
    )
    assert resp.body == b"with-token"
    _path, token_req_headers = server.requests[-2]
    _path2, final_headers = server.requests[-1]
    assert final_headers.get("Authorization") == "Bearer anon-tok-123"


def test_http_redirect_drops_authorization(server: _Server) -> None:
    other = _Server()
    try:
        server.routes["/manifests/26.8"] = [
            lambda: (302, {"Location": f"{other.base_url}/elsewhere"}, b"")
        ]
        other.routes["/elsewhere"] = [_ok(b"redirected")]
        policy = FetchPolicy(token="should-not-cross", clock=FakeClock())
        resp = fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )
        assert resp.body == b"redirected"
        _path, headers = other.requests[-1]
        assert "Authorization" not in headers
    finally:
        other.stop()


def test_http_redirect_limit(server: _Server) -> None:
    server.routes["/manifests/26.8"] = [
        lambda: (302, {"Location": f"{server.base_url}/manifests/26.8"}, b"")
    ]
    policy = FetchPolicy(clock=FakeClock(), max_redirects=2)
    with pytest.raises(UnreachableHttpError, match="redirects"):
        fetch_from_bases(
            (server.base_url,),
            "/manifests/26.8",
            mode="tag",
            policy=policy,
            retry=RetryPolicy(),
        )


def test_mirror_failover_on_digest_404_no_sleeps(server: _Server) -> None:
    other = _Server()
    try:
        # First base 404s a digest (not the last base): moves on immediately,
        # no retry sleeps.
        other.routes["/blobs/sha256:" + "a" * 64] = [_ok(b"from-second-base")]
        clock = FakeClock()
        policy = FetchPolicy(clock=clock)
        resp = fetch_from_bases(
            (server.base_url, other.base_url),
            "/blobs/sha256:" + "a" * 64,
            mode="digest",
            policy=policy,
            retry=RetryPolicy(),
        )
        assert resp.body == b"from-second-base"
        assert clock.sleeps == []
    finally:
        other.stop()


def test_manifest_requests_send_both_media_types_in_accept(server: _Server) -> None:
    """Relayed from the delivery side (2026-10-01): every `GET …/manifests/<ref>`
    — by tag and by digest — must send both media types in `Accept`. Our
    host ignores it; a mirror may not."""
    import hashlib

    from chtypes._ocifetch import _constants as C
    from chtypes._ocifetch._oci import fetch_manifest_by_digest, fetch_manifest_by_tag
    from chtypes._ocifetch._referrers import discover_referrers

    want_accept = f"{C.MEDIA_TYPE_INDEX}, {C.MEDIA_TYPE_MANIFEST}"
    index_body = b'{"mediaType": "' + C.MEDIA_TYPE_INDEX.encode() + b'", "manifests": []}'
    digest = f"sha256:{hashlib.sha256(index_body).hexdigest()}"
    server.routes["/manifests/26.8"] = [_ok(index_body)]
    policy = FetchPolicy(clock=FakeClock())
    retry = RetryPolicy()

    fetch_manifest_by_tag((server.base_url,), "26.8", policy=policy, retry=retry)
    assert server.requests[-1][1].get("Accept") == want_accept

    server.routes[f"/manifests/{digest}"] = [_ok(index_body)]
    fetch_manifest_by_digest((server.base_url,), digest, policy=policy, retry=retry)
    assert server.requests[-1][1].get("Accept") == want_accept

    # The referrers-API fallback tag is also a `/manifests/<tag>` request.
    server.routes[f"/referrers/{digest}"] = [lambda: (404, {}, b"")]
    fallback_tag = f"sha256-{digest.split(':', 1)[1]}"
    server.routes[f"/manifests/{fallback_tag}"] = [_ok(index_body)]
    discover_referrers(
        (server.base_url,),
        digest,
        C.MEDIA_TYPE_BUNDLE,
        policy=policy,
        retry=retry,
    )
    assert server.requests[-1][1].get("Accept") == want_accept
