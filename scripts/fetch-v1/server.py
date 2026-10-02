#!/usr/bin/env python3
r"""scripts/fetch-v1/server.py — the scripted HTTP server every binding's
conformance runner starts once per test run (plan §3.3, docs/guides/
fetch-v1.md §10). Python stdlib only.

    scripts/fetch-v1/server.py --fixtures <dir> --port 0
        starts the server; prints exactly one line to stdout:
            LISTENING <port> <port2>
        <port> is the primary origin every case's `{base}` expands against
        (`http://127.0.0.1:<port>/v2/s-<case-id>/chtypes/v1`); <port2> is a
        second origin, used only by cross-origin-redirect cases
        (second_origin_routes in an http-script). The server then runs
        until killed (SIGTERM/SIGINT), or until stdin closes — the
        conformance runner's own process lifetime owns it.

    scripts/fetch-v1/server.py --selftest
        starts a real server on loopback, drives it with urllib.request
        (never a mocked transport), and proves: a tree is served verbatim
        over HTTP; a scripted 503-then-success sequence is consumed in
        order and the request log shows both attempts; `close` resets the
        connection with no response; a cross-origin redirect is followed
        to the second origin; and the request log at `/_log/s-<id>` is
        per-case (two different case ids never see each other's requests).
        Exits nonzero and prints which assertion failed, same discipline
        as every other --selftest in this repository.

ROUTING. A request path is always `/v2/s-<case-id>/<repository-relative
path>`, e.g. `/v2/s-line-ok/chtypes/v1/manifests/26.8` — `s-<case-id>`
functions as the "repository segment" for routing purposes, immediately
after the OCI distribution API's own fixed `/v2/` prefix (the same
position a real repository name occupies; this is why a `{base}` for the
http transport is `http://host:port/s-<case-id>/chtypes/v1`: a conformant
OCI client always splices its own `/v2/` in right after the scheme and
authority, never inside a configured base string — `default_bases` in
spec/fetch-v1/constants.json has no `/v2/` in it either, for the same
reason). The server:

  1. requires the path to start with `/v2/s-`, else 404;
  2. reads the case id up to the next `/`;
  3. looks up that case in cases.json for its `tree`;
  4. if an http/<case-id>.json script exists, matches (method, exact path)
     against its `routes` (primary origin) or `second_origin_routes`
     (second origin), consuming one scripted response per request in
     order — the script's own `path` field is the FULL `/v2/...` path
     (without the `s-<case-id>` segment), e.g. `/v2/chtypes/v1/manifests/
     26.12`, which is what `path[len("/v2/s-"+case_id):]` reconstructs to
     with `/v2` added back;
  5. otherwise (or once a script's responses list is exhausted, which
     re-serves its last entry per the schema) serves the path directly
     from `tests/fixtures/fetch-v1/trees/<tree>/v2/<repository-relative
     path>` as a static file, 404 if absent.

Every request (method, path, headers, origin) is appended to that
case id's own log, readable at `GET /_log/s-<case-id>` as a JSON array —
never reset except by restarting the server, so a whole conformance run's
request counts are inspectable after the fact.

TWO SPECIAL-CASED SCRIPTS. The http-script schema (spec/fetch-v1/schema/
http-script.schema.json, frozen by lane 0A) has no way to make a response
conditional on a REQUEST header, so two cases need bespoke server logic
rather than a purely declarative response sequence:

  - "anon-token-flow": the manifest route's first response is always the
    scripted 401 + WWW-Authenticate; its SECOND response is served only
    once this server has seen a `GET .../token` request AND the manifest
    retry carries `Authorization: Bearer <the exact token this server
    issued>` — a wrong or missing header gets another 401, not the
    scripted from_tree, so the retry loop never terminates and the case
    genuinely fails if the token dance is wrong. `GET .../token` returns
    `{"token": "<opaque>"}`.
  - "retry-after-date": a `Retry-After` header value of the literal string
    "@date+N" is replaced with an HTTP-date N seconds after this
    response's own `Date` header — computed fresh per response, which a
    static fixture cannot express.

Every other header value containing `{origin}` or `{second-origin}` is
substituted with that origin's actual `http://127.0.0.1:<port>` — the
ports are not known until the server binds them.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
from collections import defaultdict
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FIXTURES: Path = Path(".")  # set by main() / run_server()


class ScriptState:
    """Per-(case id, method, path) response-sequence cursor, shared by both
    origins' handler threads for one case id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor: dict[tuple[str, str, str], int] = defaultdict(int)
        self.request_log: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.issued_tokens: dict[str, str] = {}

    def next_response(self, case_id: str, method: str, path: str, responses: list[dict]) -> dict:
        key = (case_id, method, path)
        with self._lock:
            i = self._cursor[key]
            self._cursor[key] = min(i + 1, len(responses) - 1)
        return responses[min(i, len(responses) - 1)]

    def log(self, case_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self.request_log[case_id].append(entry)


def load_cases(fixtures: Path) -> dict[str, dict]:
    cases_path = fixtures / "cases.json"
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def load_script(fixtures: Path, case_id: str) -> dict | None:
    p = fixtures / "http" / f"{case_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def substitute_headers(headers: dict[str, str], origin: str, second_origin: str) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        v = v.replace("{origin}", origin).replace("{second-origin}", second_origin)
        if v.startswith("@date+"):
            # retry-after-date: N seconds after THIS response's own Date header.
            n = int(v[len("@date+") :])
            v = formatdate(time.time() + n, usegmt=True)
        out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "chtypes-v1-fixture-server/1"

    # set per-instance by the server factory below
    origin_label: str = "primary"
    state: ScriptState
    cases: dict[str, dict]
    origin_url: str
    second_origin_url: str

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the request log (/_log/s-<id>) is the record; stderr noise is not

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        if path.startswith("/_log/s-"):
            case_id = path[len("/_log/s-") :]
            body = json.dumps(self.state.request_log.get(case_id, [])).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(body)
            return

        if not path.startswith("/v2/s-"):
            self._send_status(404)
            return

        rest = path[len("/v2/s-") :]
        if "/" not in rest:
            self._send_status(404)
            return
        case_id, repo_relative = rest.split("/", 1)
        case = self.cases.get(case_id)
        if case is None:
            self._send_status(404)
            return

        self.state.log(
            case_id,
            {
                "method": method,
                "path": path,
                "origin": self.origin_label,
                "headers": dict(self.headers.items()),
            },
        )

        script_path = f"/v2/{repo_relative}"

        if case_id == "anon-token-flow":
            if self._handle_anon_token_flow(case_id, method, script_path):
                return

        if case_id == "manifest-accept-header" and "/manifests/" in repo_relative:
            if not self._has_required_manifest_accept():
                # A wrong or missing Accept header never succeeds here, so
                # the case genuinely fails for a client that does not send
                # it — the declarative schema has no "assert this header
                # was present" field, so this is enforced the same way
                # anon-token-flow enforces its own header, by making the
                # WRONG behavior visibly fail rather than merely logging it.
                self._send_status(400)
                return

        script = load_script(FIXTURES, case_id)
        if script is not None:
            routes = script["second_origin_routes"] if self.origin_label == "second" else script["routes"]
            for route in routes:
                if route["method"] == method and route["path"] == script_path:
                    resp = self.state.next_response(case_id, method, script_path, route["responses"])
                    self._serve_scripted(case, resp)
                    return

        self._serve_from_tree(case, repo_relative)

    def _has_required_manifest_accept(self) -> bool:
        """The distribution spec's SHOULD (coordinator, 2026-10-01): every
        manifest GET, by tag or by digest, names both the index and the
        manifest media types in Accept — a mirror may refuse to negotiate
        content without it. These are the OCI image-spec's own standard
        media type strings, not anything spec/fetch-v1/constants.json
        defines differently, so they are literals here rather than another
        read of that file."""
        accept = self.headers.get("Accept", "")
        values = {v.strip() for v in accept.split(",")}
        required = {
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
        }
        return required.issubset(values)

    def _handle_anon_token_flow(self, case_id: str, method: str, script_path: str) -> bool:
        """The one stateful exception the declarative schema cannot express
        (see module docstring): gate the manifest route's second response
        on a real bearer token this server itself issued."""
        if script_path == "/v2/chtypes/v1/token":
            token = "anon-flow-token-" + case_id
            self.state.issued_tokens[case_id] = token
            body = json.dumps({"token": token}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(body)
            return True

        if script_path == "/v2/chtypes/v1/manifests/26.12":
            auth = self.headers.get("Authorization", "")
            expected = "Bearer " + self.state.issued_tokens.get(case_id, "\x00never-issued")
            if auth == expected:
                case = self.cases[case_id]
                self._serve_from_tree(case, "chtypes/v1/manifests/26.12")
                return True
            self.send_response(401)
            realm = f"http://{self.headers.get('host', '')}/v2/s-{case_id}/chtypes/v1/token"
            self.send_header("WWW-Authenticate", f'Bearer realm="{realm}"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _serve_scripted(self, case: dict, resp: dict) -> None:
        if resp.get("stall"):
            # Hold the connection open past the test-only idle timeout —
            # never resolves. The client's own read-timeout ends this, not
            # the server; sleeping here just keeps the socket open and
            # silent.
            time.sleep(3600)
            return
        if resp.get("close"):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True
            return
        if resp.get("from_tree"):
            self._serve_from_tree(case, self.path.split("/", 4)[-1] if False else None, raw_path=self.path)
            return

        status = resp["status"]
        headers = substitute_headers(resp.get("headers", {}), self.origin_url, self.second_origin_url)
        body = b""
        if resp.get("body_from_tree"):
            body = self._tree_bytes_for_request()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _tree_bytes_for_request(self) -> bytes:
        path = urlsplit(self.path).path
        rest = path[len("/v2/s-") :]
        _, repo_relative = rest.split("/", 1)
        case_id = rest.split("/", 1)[0]
        case = self.cases[case_id]
        tree = case["tree"]
        f = FIXTURES / "trees" / tree / "v2" / repo_relative
        return f.read_bytes() if f.exists() else b""

    def _serve_from_tree(self, case: dict, repo_relative: str | None, raw_path: str | None = None) -> None:
        if repo_relative is None:
            path = urlsplit(raw_path or self.path).path
            rest = path[len("/v2/s-") :]
            _, repo_relative = rest.split("/", 1)
        tree = case["tree"]
        f = FIXTURES / "trees" / tree / "v2" / repo_relative
        if not f.is_file():
            self._send_status(404)
            return
        body = f.read_bytes()
        content_type = "application/json" if _looks_like_json_route(repo_relative) else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _looks_like_json_route(repo_relative: str) -> bool:
    return "/manifests/" in repo_relative or repo_relative.endswith("/tags/list") or "/referrers/" in repo_relative


def make_handler_class(state: ScriptState, cases: dict[str, dict], origin_label: str, origin_url: str, second_origin_url: str):
    class _H(Handler):
        pass

    _H.state = state
    _H.cases = cases
    _H.origin_label = origin_label
    _H.origin_url = origin_url
    _H.second_origin_url = second_origin_url
    return _H


def run_server(fixtures: Path, port: int = 0) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, ScriptState]:
    global FIXTURES
    FIXTURES = fixtures
    cases = load_cases(fixtures)
    state = ScriptState()

    srv1 = ThreadingHTTPServer(("127.0.0.1", port), make_handler_class(state, cases, "primary", "", ""))
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(state, cases, "second", "", ""))

    origin1 = f"http://127.0.0.1:{srv1.server_address[1]}"
    origin2 = f"http://127.0.0.1:{srv2.server_address[1]}"
    srv1.RequestHandlerClass.origin_url = origin1
    srv1.RequestHandlerClass.second_origin_url = origin2
    srv2.RequestHandlerClass.origin_url = origin1
    srv2.RequestHandlerClass.second_origin_url = origin2

    threading.Thread(target=srv1.serve_forever, daemon=True).start()
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    return srv1, srv2, state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _run_selftest(Path(tmp))
        print("server.py --selftest: OK")
        return 0

    if not args.fixtures:
        print("server.py: --fixtures is required (unless --selftest)", file=sys.stderr)
        return 2

    srv1, srv2, _state = run_server(args.fixtures, args.port)
    print(f"LISTENING {srv1.server_address[1]} {srv2.server_address[1]}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def _run_selftest(tmp: Path) -> None:
    import urllib.error
    import urllib.request

    # A minimal two-case fixture tree, built the same way genfixtures
    # shapes one, without depending on genfixtures itself (server.py has
    # no dependency on the Go generator; it only reads what is on disk).
    tree_dir = tmp / "trees" / "basic" / "v2" / "chtypes" / "v1" / "manifests"
    tree_dir.mkdir(parents=True)
    (tree_dir / "26.8").write_text('{"ok":true}')

    http_dir = tmp / "http"
    http_dir.mkdir()
    (http_dir / "retry-then-ok.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "retry-then-ok",
                "tree": "basic",
                "routes": [
                    {
                        "method": "GET",
                        "path": "/v2/chtypes/v1/manifests/26.8",
                        "responses": [{"status": 503}, {"from_tree": True}],
                    }
                ],
                "second_origin_routes": [],
            }
        )
    )
    (http_dir / "redirect-case.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "redirect-case",
                "tree": "basic",
                "routes": [
                    {
                        "method": "GET",
                        "path": "/v2/chtypes/v1/manifests/26.8",
                        "responses": [
                            {
                                "status": 302,
                                "headers": {"Location": "{second-origin}/v2/s-redirect-case/chtypes/v1/manifests/26.8"},
                            }
                        ],
                    }
                ],
                "second_origin_routes": [
                    {"method": "GET", "path": "/v2/chtypes/v1/manifests/26.8", "responses": [{"from_tree": True}]}
                ],
            }
        )
    )
    (http_dir / "closer.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "closer",
                "tree": "basic",
                "routes": [{"method": "GET", "path": "/v2/chtypes/v1/manifests/26.8", "responses": [{"close": True}]}],
                "second_origin_routes": [],
            }
        )
    )

    (tmp / "cases.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "source": {"generator_commit": "0" * 40, "go_sum_sha256": "0" * 64},
                "cases": [
                    {
                        "id": cid,
                        "tree": "basic",
                        "transports": ["http"],
                        "http_script": cid if cid != "no-script" else None,
                        "setup": {"cache": "empty", "system_dirs": [], "lock": None, "before_index_rename_hook": None},
                        "request": {
                            "spelling": "26.8",
                            "platform": "linux-arm64",
                            "offline": False,
                            "frozen": False,
                            "lock_write": False,
                            "update": False,
                            "allow_unsigned": False,
                            "trust": "test",
                            "bases": ["{base}"],
                        },
                        "env": {},
                        "expect": {
                            "ok": True,
                            "version": None,
                            "build": None,
                            "manifest": None,
                            "library_sha256": None,
                            "code": None,
                            "sleeps": [],
                            "warnings": [],
                            "requests": {"max": None, "none_matching": [], "auth_on_second_origin": False},
                            "lock_after": None,
                        },
                    }
                    for cid in ("retry-then-ok", "redirect-case", "closer", "no-script")
                ],
            }
        )
    )

    srv1, srv2, state = run_server(tmp, port=0)
    try:
        port1 = srv1.server_address[1]

        # 1. no-script: serves the tree verbatim.
        url = f"http://127.0.0.1:{port1}/v2/s-no-script/chtypes/v1/manifests/26.8"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200, f"expected 200, got {r.status}"
            assert json.loads(r.read()) == {"ok": True}

        # 2. retry-then-ok: first request 503s, second succeeds; the log
        # shows exactly two requests for this case id.
        url = f"http://127.0.0.1:{port1}/v2/s-retry-then-ok/chtypes/v1/manifests/26.8"
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("expected the first request to 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503, f"expected 503, got {e.code}"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200
        log = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port1}/_log/s-retry-then-ok", timeout=5).read()
        )
        assert len(log) == 2, f"expected 2 logged requests, got {len(log)}: {log}"

        # 3. redirect-case: the client follows the 302 to the second
        # origin and gets the real content there.
        url = f"http://127.0.0.1:{port1}/v2/s-redirect-case/chtypes/v1/manifests/26.8"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read()) == {"ok": True}

        # 4. closer: the connection resets with no HTTP response at all.
        url = f"http://127.0.0.1:{port1}/v2/s-closer/chtypes/v1/manifests/26.8"
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("expected the connection to reset, got a response")
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

        # 5. per-case logs never cross-contaminate.
        log_closer = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port1}/_log/s-closer", timeout=5).read())
        assert len(log_closer) == 1, f"expected 1 logged request for 'closer', got {len(log_closer)}"
    finally:
        srv1.shutdown()
        srv2.shutdown()


if __name__ == "__main__":
    sys.exit(main())
