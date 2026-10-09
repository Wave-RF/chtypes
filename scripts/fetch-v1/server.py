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
        per-case (two different case ids never see each other's requests);
        and a request whose User-Agent is not `chtypes-<binding>/<version>`
        (urllib's default, or none) is answered 400 while a conforming one
        is served, and the log records the agent either way; and a scripted
        status with a `body` (a retired repository's 410 and its error
        document) is sent with exactly those bytes, and one without a body
        with none; and a closed gate (GATES below) logs and parks a case's
        request, serves every other case meanwhile, reports it parked, and
        releases it when opened, after which requests pass straight through
        and a parked count that is never reached answers 504.
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

REFERRERS FILTER. `GET .../referrers/<digest>?artifactType=<t>` is answered
as the real host does (form-decoded query, so an unencoded `+` in a media
type matches nothing; `OCI-Filters-Applied: artifactType` when filtered).

USER-AGENT. A request under `/v2/s-<case-id>/` whose `User-Agent` does not
match `^chtypes-(go|python|ts|rust)/[0-9A-Za-z.+-]+$` is logged and then
answered 400, before any routing, scripting or tree lookup.

Every request (method, path, user_agent, headers, origin) is appended to that
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

GATES. A test can hold one case's requests while it proves what a client does
with a request in flight (public issue #491: a registry must not make an open
of one line wait for another line's fetch). Gates are test-only and keyed by
case id; no case in cases.json uses one, and a case whose gate was never
closed is served exactly as before.

    GET /_gate/close/s-<case-id>
        closes the gate: from now on every request under `/v2/s-<case-id>/` is
        logged, then parked before any routing, until the gate opens (or
        GATE_HOLD_S passes, after which it is served as usual). Answers
        `{"closed": true}`.
    GET /_gate/parked/s-<case-id>?n=<N>[&wait=<seconds>]
        answers `{"parked": <count>}` as soon as at least N requests are
        parked at that gate, or 504 with the current count when `wait`
        seconds (default GATE_WAIT_S) pass first. A test waits on this,
        never on a clock, to know its request reached the server.
    GET /_gate/open/s-<case-id>
        opens the gate, releasing every parked request to be served as
        usual, and answers `{"released": <count>}`.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import socketserver
import sys
import threading
import time
from collections import defaultdict
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES: Path = Path(".")  # set by main() / run_server()

# docs/guides/fetch-v1.md §2: every request a binding's fetch layer makes
# carries `User-Agent: chtypes-<binding>/<version>`, because delivery hosts
# may refuse a generic library agent (Python-urllib, Go-http-client, ...).
# The server answers 400 to anything else, so a binding that forgets the
# header turns every http-transport case red instead of passing here and
# failing against production.
USER_AGENT_PATTERN = re.compile(r"^chtypes-(go|python|ts|rust)/[0-9A-Za-z.+-]+$")

# GATES (module docstring): how long a parked request is held at most, and how
# long `/_gate/parked` waits by default. Both only bound a broken test; a
# working one opens the gate, or sees the count, at once.
GATE_HOLD_S = 300.0
GATE_WAIT_S = 60.0


class Gates:
    """The per-case request gates (module docstring, GATES), shared by both
    origins' handler threads."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._closed: set[str] = set()
        self._parked: dict[str, int] = defaultdict(int)

    def close(self, case_id: str) -> None:
        with self._cond:
            self._closed.add(case_id)

    def open(self, case_id: str) -> int:
        with self._cond:
            self._closed.discard(case_id)
            self._cond.notify_all()
            return self._parked[case_id]

    def parked(self, case_id: str) -> int:
        with self._cond:
            return self._parked[case_id]

    def pass_through(self, case_id: str) -> None:
        """Return at once while the case's gate is open; while it is closed,
        count this request as parked and hold it until the gate opens."""
        with self._cond:
            if case_id not in self._closed:
                return
            self._parked[case_id] += 1
            self._cond.notify_all()
            try:
                self._cond.wait_for(lambda: case_id not in self._closed, timeout=GATE_HOLD_S)
            finally:
                self._parked[case_id] -= 1

    def wait_parked(self, case_id: str, n: int, wait: float) -> int | None:
        """The parked count once it reaches n, or None when `wait` seconds
        pass first."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._parked[case_id] >= n, timeout=wait):
                return None
            return self._parked[case_id]


class ScriptState:
    """Per-(case id, origin, method, path) response-sequence cursor, shared
    by both origins' handler threads for one case id.

    `origin` is part of the key deliberately (measured, lane 0B,
    2026-10-02): a mirror-failover case's `second_origin_routes` entry can
    share the exact same (method, path) as its primary `routes` entry — the
    whole point of the second origin being a mirror of the same repository
    path — and without `origin` in the key, a request to the second origin
    would advance (and read from) the FIRST origin's cursor. That is the
    cursor sharing mirror-failover-5xx and mirror-failover-digest-404 both
    shipped with."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self.request_log: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.issued_tokens: dict[str, str] = {}
        self.gates = Gates()

    def next_response(self, case_id: str, origin: str, method: str, path: str, responses: list[dict]) -> dict:
        key = (case_id, origin, method, path)
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
    server_version = "ocifetch-v1-fixture-server/1"

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

        if path.startswith("/_gate/"):
            self._handle_gate(path[len("/_gate/") :], parsed.query)
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
                "user_agent": self.headers.get("User-Agent"),
                "headers": dict(self.headers.items()),
            },
        )

        agent = self.headers.get("User-Agent")
        if agent is None or USER_AGENT_PATTERN.fullmatch(agent) is None:
            # Logged above (so a test can see what was sent), then refused.
            self._send_status(400)
            return

        # A closed gate (GATES) holds the request here, logged and unrouted.
        self.state.gates.pass_through(case_id)

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
                    resp = self.state.next_response(case_id, self.origin_label, method, script_path, route["responses"])
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
        filtered = False
        if resp.get("body_from_tree"):
            body, filtered = self._referrers_filter(self._tree_bytes_for_request())
        elif "body" in resp:
            # A registry's own error document, sent as given (a retired
            # repository's 410: docs/guides/fetch-v1.md §2).
            body = resp["body"].encode("utf-8")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if filtered:
            self.send_header("OCI-Filters-Applied", "artifactType")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _referrers_filter(self, body: bytes) -> tuple[bytes, bool]:
        """Answer the referrers API's `artifactType` filter the way the real
        host does: the query is decoded as an HTML form (so a raw `+` is a
        space and matches nothing; `%2B` is a plus), `manifests[]` is filtered
        by exact `artifactType`, and `OCI-Filters-Applied: artifactType` is
        sent when it filtered. A request with no filter is served unchanged,
        so a binding that filters client-side still works."""
        parsed = urlsplit(self.path)
        if "/referrers/" not in parsed.path:
            return body, False
        wanted = parse_qs(parsed.query).get("artifactType")
        if not wanted:
            return body, False
        try:
            doc = json.loads(body)
        except ValueError:
            return body, False
        if not isinstance(doc, dict) or not isinstance(doc.get("manifests"), list):
            return body, False
        doc["manifests"] = [m for m in doc["manifests"] if m.get("artifactType") == wanted[0]]
        return json.dumps(doc, indent=2).encode("utf-8"), True

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
        body, filtered = self._referrers_filter(f.read_bytes())
        content_type = "application/json" if _looks_like_json_route(repo_relative) else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if filtered:
            self.send_header("OCI-Filters-Applied", "artifactType")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle_gate(self, rest: str, query: str) -> None:
        """GATES (module docstring): close, report parked, open."""
        action, _, target = rest.partition("/")
        if action not in ("close", "parked", "open") or not target.startswith("s-") or "/" in target:
            self._send_status(404)
            return
        case_id = target[len("s-") :]
        gates = self.state.gates
        if action == "close":
            gates.close(case_id)
            self._send_json(200, {"closed": True})
        elif action == "open":
            self._send_json(200, {"released": gates.open(case_id)})
        else:
            q = parse_qs(query)
            try:
                n = int(q.get("n", ["1"])[0])
                wait = float(q.get("wait", [str(GATE_WAIT_S)])[0])
            except ValueError:
                self._send_status(400)
                return
            parked = gates.wait_parked(case_id, n, max(0.0, min(wait, GATE_HOLD_S)))
            if parked is None:
                self._send_json(504, {"parked": gates.parked(case_id)})
            else:
                self._send_json(200, {"parked": parked})

    def _send_json(self, status: int, doc: dict[str, Any]) -> None:
        body = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without HTTPServer.server_bind's reverse lookup of
    its own address (`socket.getfqdn`), whose only use is `server_name`, which
    nothing here reads. On the darwin-arm64 runner a test that started this
    server waited about 35 s for its LISTENING line (measured through the
    Rust and Python test durations on 2026-10-09; the lookup as the cause is
    inferred), against well under a second on Linux. The socket bind itself is unchanged."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def run_server(fixtures: Path, port: int = 0) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, ScriptState]:
    global FIXTURES
    FIXTURES = fixtures
    cases = load_cases(fixtures)
    state = ScriptState()

    srv1 = LoopbackServer(("127.0.0.1", port), make_handler_class(state, cases, "primary", "", ""))
    srv2 = LoopbackServer(("127.0.0.1", 0), make_handler_class(state, cases, "second", "", ""))

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

    ua = {"User-Agent": "chtypes-python/0.0.0-dev"}

    def get(u: str):  # noqa: ANN202 - selftest helper
        return urllib.request.urlopen(urllib.request.Request(u, headers=ua), timeout=5)

    # A minimal two-case fixture tree, built the same way genfixtures
    # shapes one, without depending on genfixtures itself (server.py has
    # no dependency on the Go generator; it only reads what is on disk).
    ref_dir = tmp / "trees" / "basic" / "v2" / "chtypes" / "v1" / "referrers"
    ref_dir.mkdir(parents=True)
    (ref_dir / "sha256:abc").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {"digest": "sha256:1", "artifactType": "application/vnd.dev.sigstore.bundle.v0.3+json"},
                    {"digest": "sha256:2", "artifactType": "application/vnd.other"},
                ],
            }
        )
    )
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
    gone_body = '{"errors":[{"code":"DENIED","message":"retired \\u001b[31m\u00e9"}]}'
    (http_dir / "gone.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": "gone",
                "tree": "basic",
                "routes": [
                    {
                        "method": "GET",
                        "path": "/v2/chtypes/v1/manifests/26.8",
                        "responses": [
                            {"status": 410, "headers": {"Content-Type": "application/json"}, "body": gone_body},
                            {"status": 410},
                        ],
                    }
                ],
                "second_origin_routes": [],
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
                    for cid in ("retry-then-ok", "redirect-case", "closer", "no-script", "gone", "gated")
                ],
            }
        )
    )

    srv1, srv2, state = run_server(tmp, port=0)
    try:
        port1 = srv1.server_address[1]

        # 1. no-script: serves the tree verbatim.
        url = f"http://127.0.0.1:{port1}/v2/s-no-script/chtypes/v1/manifests/26.8"
        with get(url) as r:
            assert r.status == 200, f"expected 200, got {r.status}"
            assert json.loads(r.read()) == {"ok": True}

        # 2. retry-then-ok: first request 503s, second succeeds; the log
        # shows exactly two requests for this case id.
        url = f"http://127.0.0.1:{port1}/v2/s-retry-then-ok/chtypes/v1/manifests/26.8"
        try:
            get(url)
            raise AssertionError("expected the first request to 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503, f"expected 503, got {e.code}"
        with get(url) as r:
            assert r.status == 200
        log = json.loads(
            get(f"http://127.0.0.1:{port1}/_log/s-retry-then-ok").read()
        )
        assert len(log) == 2, f"expected 2 logged requests, got {len(log)}: {log}"

        # 3. redirect-case: the client follows the 302 to the second
        # origin and gets the real content there.
        url = f"http://127.0.0.1:{port1}/v2/s-redirect-case/chtypes/v1/manifests/26.8"
        with get(url) as r:
            assert r.status == 200
            assert json.loads(r.read()) == {"ok": True}

        # 4. closer: the connection resets with no HTTP response at all.
        url = f"http://127.0.0.1:{port1}/v2/s-closer/chtypes/v1/manifests/26.8"
        try:
            get(url)
            raise AssertionError("expected the connection to reset, got a response")
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

        # 5. per-case logs never cross-contaminate.
        log_closer = json.loads(get(f"http://127.0.0.1:{port1}/_log/s-closer").read())
        assert len(log_closer) == 1, f"expected 1 logged request for 'closer', got {len(log_closer)}"

        # 6. User-Agent enforcement: urllib's own default and a missing
        # header are both refused with 400 and logged; a conforming agent
        # is served; the log records the agent either way.
        url = f"http://127.0.0.1:{port1}/v2/s-no-script/chtypes/v1/manifests/26.8"
        for bad in ("Python-urllib/3.13", "Go-http-client/1.1", "curl/8.0.0", "chtypes-go", "chtypes-go/", "chtypes-go/1.0 x"):
            req = urllib.request.Request(url, headers={"User-Agent": bad})
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError(f"expected 400 for User-Agent {bad!r}")
            except urllib.error.HTTPError as e:
                assert e.code == 400, f"expected 400 for User-Agent {bad!r}, got {e.code}"
        for good in ("chtypes-go/1.0.0", "chtypes-python/1.0.0", "chtypes-ts/1.0.0-rc.1", "chtypes-rust/0.0.0-dev"):
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": good}), timeout=5) as r:
                assert r.status == 200, f"expected 200 for User-Agent {good!r}, got {r.status}"
        # 7. Referrers filter: raw `+` is a space (0 manifests), `%2B` is a
        # plus (the bundle), no filter serves everything.
        rbase = f"http://127.0.0.1:{port1}/v2/s-no-script/chtypes/v1/referrers/sha256:abc"
        for query, want, applied in (
            ("artifactType=application/vnd.dev.sigstore.bundle.v0.3+json", 0, True),
            ("artifactType=application%2Fvnd.dev.sigstore.bundle.v0.3%2Bjson", 1, True),
            ("", 2, False),
        ):
            with get(rbase + ("?" + query if query else "")) as r:
                got = json.loads(r.read())["manifests"]
                assert len(got) == want, f"referrers {query!r}: expected {want}, got {len(got)}"
                has = r.headers.get("OCI-Filters-Applied") == "artifactType"
                assert has == applied, f"referrers {query!r}: OCI-Filters-Applied={has}, want {applied}"

        # 8. A scripted body is sent as exactly its UTF-8 bytes, with the
        # scripted status; a status with no body sends none.
        url = f"http://127.0.0.1:{port1}/v2/s-gone/chtypes/v1/manifests/26.8"
        for want_body in (gone_body.encode("utf-8"), b""):
            try:
                get(url)
                raise AssertionError("expected a 410")
            except urllib.error.HTTPError as e:
                assert e.code == 410, f"expected 410, got {e.code}"
                got_body = e.read()
                assert got_body == want_body, f"expected body {want_body!r}, got {got_body!r}"

        log_ua = json.loads(get(f"http://127.0.0.1:{port1}/_log/s-no-script").read())
        agents = [e["user_agent"] for e in log_ua]
        assert "Python-urllib/3.13" in agents and "chtypes-go/1.0.0" in agents, f"agents not recorded: {agents}"

        # 9. A gate (GATES): a closed case's request is logged and parked,
        # unanswered, until the gate opens; another case is served meanwhile;
        # `parked` answers once the request is parked; once open, requests
        # pass straight through, and a count never reached answers 504.
        origin = f"http://127.0.0.1:{port1}"
        gated = f"{origin}/v2/s-gated/chtypes/v1/manifests/26.8"
        with get(f"{origin}/_gate/close/s-gated") as r:
            assert json.loads(r.read()) == {"closed": True}
        held: dict[str, Any] = {}
        answered = threading.Event()

        def fetch_at_the_gate() -> None:
            try:
                with get(gated) as r:
                    held["status"], held["body"] = r.status, r.read()
            except Exception as exc:  # noqa: BLE001 - reported by the assertion below
                held["error"] = exc
            finally:
                answered.set()

        threading.Thread(target=fetch_at_the_gate, daemon=True).start()
        with get(f"{origin}/_gate/parked/s-gated?n=1") as r:
            assert json.loads(r.read()) == {"parked": 1}
        assert not answered.is_set(), f"a request at a closed gate was answered: {held}"
        with get(f"{origin}/v2/s-no-script/chtypes/v1/manifests/26.8") as r:
            assert r.status == 200, "a closed gate held another case's request"
        log_gated = json.loads(get(f"{origin}/_log/s-gated").read())
        assert [e["path"] for e in log_gated] == ["/v2/s-gated/chtypes/v1/manifests/26.8"], log_gated
        assert not answered.is_set(), f"a request at a closed gate was answered: {held}"
        with get(f"{origin}/_gate/open/s-gated") as r:
            assert json.loads(r.read()) == {"released": 1}
        assert answered.wait(5), "the opened gate did not release its request"
        assert held.get("status") == 200 and json.loads(held["body"]) == {"ok": True}, held
        with get(gated) as r:
            assert r.status == 200 and json.loads(r.read()) == {"ok": True}
        try:
            get(f"{origin}/_gate/parked/s-gated?n=1&wait=0")
            raise AssertionError("expected 504 for a parked count never reached")
        except urllib.error.HTTPError as e:
            assert e.code == 504, f"expected 504, got {e.code}"
            assert json.loads(e.read()) == {"parked": 0}
        for bad in ("/_gate/shut/s-gated", "/_gate/close/gated", "/_gate/close/s-a/b"):
            try:
                get(origin + bad)
                raise AssertionError(f"expected 404 for {bad}")
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"expected 404 for {bad}, got {e.code}"
    finally:
        srv1.shutdown()
        srv2.shutdown()


if __name__ == "__main__":
    sys.exit(main())
