"""The v1 transport: `file://`/`http(s)://` GETs, retries, redirects, auth.

docs/guides/fetch-v1.md "Sources": an ordered base list with fallback, one
retry table (the generated constants are its normative copy), redirects that
drop `Authorization` on every hop, the anonymous Bearer-token flow for
mirrors, a static `CHTYPES_DOWNLOAD_TOKEN` bearer, `HTTPS_PROXY`/
`NO_PROXY` and the system certificate store, and size caps.

Nothing here decides fetch-layer policy (tag vs. digest 404 semantics,
UNPUBLISHED vs. SOURCE_UNREACHABLE): this module raises a small, untyped-by-
meaning set of internal exceptions and leaves translating them to the
higher-level `_oci`/`_referrers`/`_ensure` modules, because the same
HTTP outcome (a 404) means a different thing depending on whether the path
was a tag or a digest (layout-v2 spec §7.5/§10 A5).
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from chtypes._ocifetch import _constants as C

__all__ = [
    "Clock",
    "ForbiddenHttpError",
    "FetchPolicy",
    "HttpResponse",
    "NotFoundHttpError",
    "OversizeHttpError",
    "RetryPolicy",
    "TransportError",
    "UnauthorizedHttpError",
    "UnreachableHttpError",
    "fetch_from_bases",
    "get_json",
]

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Well-known Linux CA bundle locations, tried only when ssl's own default
# verify paths resolve to nothing AND neither SSL_CERT_FILE nor
# SSL_CERT_DIR is set. python-build-standalone's OpenSSL (what `uv` installs
# on Linux) may ship with no compiled-in default path at all (PLAN "Lane
# Python" risk; `measured` only by the v1-network CI job, not locally).
_LINUX_CA_BUNDLE_FALLBACKS = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL, Fedora, CentOS, Amazon Linux
    "/etc/ssl/cert.pem",  # Alpine, FreeBSD, some musl builds
)


class TransportError(Exception):
    """Base of every exception this module raises."""


class NotFoundHttpError(TransportError):
    """A 404 (or, for a `file://` base, a missing path)."""


class UnauthorizedHttpError(TransportError):
    """A 401 that is not (or is no longer) resolvable by the anonymous token flow."""

    def __init__(self, message: str, *, www_authenticate: str | None = None) -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


class ForbiddenHttpError(TransportError):
    """A 403."""


class UnreachableHttpError(TransportError):
    """A connection-level failure, or an HTTP-level failure after exhausting
    the retry table (including a digest 404 retried on the last base, a
    host fault per layout-v2 spec §7.5)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class OversizeHttpError(TransportError):
    """A response body exceeded the caller's declared size cap."""


class Clock:
    """The real wall clock. Tests inject a fake with the same interface so
    retry cases run instantly and sleeps are asserted exactly."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """The one retry table every v1 fetcher shares (constants: `retry`)."""

    attempts: int = C.RETRY_ATTEMPTS
    first_wait_s: float = C.RETRY_FIRST_WAIT_S
    multiplier: float = C.RETRY_MULTIPLIER
    retry_statuses: frozenset[int] = field(default_factory=lambda: frozenset(C.RETRY_STATUSES))
    retry_after_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset(C.RETRY_AFTER_STATUSES)
    )

    def wait_before_attempt(self, attempt_index: int) -> float:
        """The planned backoff before retry number `attempt_index` (0-based:
        0 is the wait before the second overall attempt)."""
        return self.first_wait_s * (self.multiplier**attempt_index)

    def remaining_budget_s(self, attempt_index: int) -> float:
        """The sum of every planned wait from `attempt_index` through the
        last retry. A `Retry-After` that exceeds this is refused outright
        (constants: `retry_after_over_budget: "refuse"`) rather than
        shortened — `decided-here`, since the spec states the rule but not
        the exact budget arithmetic: the remaining planned schedule is the
        only number already fixed by the shared retry table."""
        return sum(self.wait_before_attempt(i) for i in range(attempt_index, self.attempts - 1))


@dataclass(frozen=True)
class FetchPolicy:
    """Per-request configuration: auth, timeouts, redirect and proxy limits."""

    token: str | None = None
    max_redirects: int = C.MAX_REDIRECTS
    connect_timeout_s: float = C.CONNECT_TIMEOUT_S
    idle_timeout_s: float = C.IDLE_READ_TIMEOUT_S
    clock: Clock = field(default_factory=Clock)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


def _default_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    has_cafile = bool(paths.cafile) and os.path.exists(paths.cafile)
    has_capath = bool(paths.capath) and os.path.isdir(paths.capath)
    env_override = os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")
    if not has_cafile and not has_capath and not env_override:
        for candidate in _LINUX_CA_BUNDLE_FALLBACKS:
            if os.path.exists(candidate):
                ctx.load_verify_locations(cafile=candidate)
                break
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never auto-follow: `urlopen` raises `HTTPError` on every 3xx instead,
    so the caller controls hop counting and `Authorization` dropping."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _opener(url: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if urlsplit(url).scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=_default_ssl_context()))
    # build_opener adds the rest of the defaults (ProxyHandler among them,
    # which reads HTTPS_PROXY/HTTP_PROXY/NO_PROXY from the environment) for
    # any handler class not already supplied above.
    return urllib.request.build_opener(*handlers)


def _single_request(
    url: str, *, headers: Mapping[str, str], policy: FetchPolicy, max_bytes: int | None
) -> HttpResponse:
    req = urllib.request.Request(url, method="GET", headers=dict(headers))
    timeout = policy.connect_timeout_s + policy.idle_timeout_s
    try:
        with _opener(url).open(req, timeout=timeout) as resp:
            body = _read_capped(resp, max_bytes)
            return HttpResponse(
                status=resp.status, headers=dict(resp.headers), body=body, url=resp.geturl()
            )
    except urllib.error.HTTPError as e:
        try:
            body = _read_capped(e, max_bytes) if e.code not in _REDIRECT_CODES else b""
        finally:
            e.close()
        return HttpResponse(status=e.code, headers=dict(e.headers or {}), body=body, url=url)
    except urllib.error.URLError as e:
        raise UnreachableHttpError(f"{url}: {e.reason}", retryable=True) from e
    except (TimeoutError, ConnectionError, OSError) as e:
        raise UnreachableHttpError(f"{url}: {e}", retryable=True) from e


def _read_capped(fp, max_bytes: int | None) -> bytes:  # noqa: ANN001
    if max_bytes is None:
        return fp.read()
    chunk = fp.read(max_bytes + 1)
    if len(chunk) > max_bytes:
        raise OversizeHttpError(f"response exceeded {max_bytes} bytes")
    return chunk


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname or "", parts.port)


def _parse_retry_after(headers: Mapping[str, str], clock: Clock) -> float | None:
    raw = None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            raw = v.strip()
            break
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    base = None
    for k, v in headers.items():
        if k.lower() == "date":
            try:
                base = parsedate_to_datetime(v)
                if base.tzinfo is None:
                    base = base.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                base = None
            break
    if base is None:
        base = datetime.fromtimestamp(clock.now(), tz=UTC)
    return max(0.0, (target - base).total_seconds())


def _parse_bearer_challenge(header: str) -> dict[str, str] | None:
    """`WWW-Authenticate: Bearer realm="...",service="...",scope="..."`."""
    header = header.strip()
    if not header.lower().startswith("bearer"):
        return None
    params: dict[str, str] = {}
    rest = header[len("bearer") :].strip()
    for part in rest.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip()] = value.strip().strip('"')
    return params if "realm" in params else None


def _anon_token(params: Mapping[str, str], policy: FetchPolicy) -> str | None:
    """The anonymous Bearer-token flow mirrors use: a plain GET to the
    challenge's `realm`, with `service`/`scope` as query parameters, anonymous
    (no credentials of ours), returning `{"token": "..."}` (or `access_token`)."""
    realm = params["realm"]
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    from urllib.parse import urlencode

    url = realm + ("&" if "?" in realm else "?") + urlencode(query) if query else realm
    try:
        resp = _single_request(url, headers={}, policy=policy, max_bytes=C.BUNDLE_MAX_BYTES)
    except TransportError:
        return None
    if resp.status != 200:
        return None
    try:
        payload = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    token = payload.get("token") or payload.get("access_token")
    return token if isinstance(token, str) else None


def _http_get_once(
    url: str,
    *,
    accept: Sequence[str] | None,
    max_bytes: int | None,
    policy: FetchPolicy,
    treat_404_as_retryable: bool,
    retry: RetryPolicy,
) -> HttpResponse:
    """One logical request against one base: follows redirects (dropping
    `Authorization` on every hop), runs the anonymous token flow on a first
    401, and applies the shared retry table for transient statuses."""
    headers: dict[str, str] = {}
    if accept:
        headers["Accept"] = ", ".join(accept)
    if policy.token:
        headers["Authorization"] = f"Bearer {policy.token}"
    tried_anon_flow = False
    attempt = 0
    current_url = url
    hops = 0
    while True:
        try:
            resp = _single_request(current_url, headers=headers, policy=policy, max_bytes=max_bytes)
        except UnreachableHttpError:
            if attempt >= retry.attempts - 1:
                raise
            policy.clock.sleep(retry.wait_before_attempt(attempt))
            attempt += 1
            continue

        if resp.status in _REDIRECT_CODES:
            hops += 1
            if hops > policy.max_redirects:
                raise UnreachableHttpError(f"{url}: exceeded {policy.max_redirects} redirects")
            location = resp.headers.get("Location") or resp.headers.get("location")
            if not location:
                raise UnreachableHttpError(f"{url}: redirect with no Location header")
            current_url = urljoin(current_url, location)
            # Drop Authorization on every hop, not only cross-origin ones:
            # redirects commonly land on a storage origin where our bearer
            # (static or anonymous) would be both wrong and a leak (layout-v2
            # spec §10 A7/A8). `decided-here`: stronger than "cross-origin
            # only", chosen because A8 says the download token is "never
            # forwarded across a redirect" without that qualifier.
            headers.pop("Authorization", None)
            continue

        if resp.status == 401:
            if not tried_anon_flow and not policy.token:
                challenge = resp.headers.get("WWW-Authenticate") or resp.headers.get(
                    "www-authenticate"
                )
                params = _parse_bearer_challenge(challenge) if challenge else None
                if params:
                    tried_anon_flow = True
                    token = _anon_token(params, policy)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        continue
            raise UnauthorizedHttpError(
                f"{current_url}: 401", www_authenticate=resp.headers.get("WWW-Authenticate")
            )

        if resp.status == 403:
            raise ForbiddenHttpError(f"{current_url}: 403")

        if resp.status == 404:
            if not treat_404_as_retryable:
                raise NotFoundHttpError(f"{current_url}: 404")
            if attempt >= retry.attempts - 1:
                raise UnreachableHttpError(
                    f"{current_url}: 404 after {attempt + 1} attempts (a listed digest that "
                    f"404s is a host fault)",
                    retryable=True,
                )
            policy.clock.sleep(retry.wait_before_attempt(attempt))
            attempt += 1
            continue

        if 200 <= resp.status < 300:
            return resp

        if resp.status in retry.retry_statuses:
            if attempt >= retry.attempts - 1:
                raise UnreachableHttpError(
                    f"{current_url}: HTTP {resp.status} after {attempt + 1} attempts",
                    retryable=True,
                )
            wait = retry.wait_before_attempt(attempt)
            if resp.status in retry.retry_after_statuses:
                requested = _parse_retry_after(resp.headers, policy.clock)
                if requested is not None:
                    budget = retry.remaining_budget_s(attempt)
                    if requested > budget:
                        raise UnreachableHttpError(
                            f"{current_url}: Retry-After {requested:g}s exceeds the "
                            f"{budget:g}s remaining retry budget; refusing rather than "
                            f"shortening it",
                            retryable=False,
                        )
                    wait = requested
            policy.clock.sleep(wait)
            attempt += 1
            continue

        raise UnreachableHttpError(
            f"{current_url}: HTTP {resp.status} (not retryable)", retryable=False
        )


def _file_path_for(base: str, path: str) -> str:
    # layout-v2 / PLAN §3.2: "never append a query string to a `file://`
    # URL"; a file base names the repository path directly, so joining is
    # plain string concatenation of the URL path components, never urljoin
    # (which would reinterpret `..` or drop the base's own path).
    base_parts = urlsplit(base)
    joined = urlunsplit((base_parts.scheme, base_parts.netloc, base_parts.path + path, "", ""))
    from urllib.request import url2pathname

    return url2pathname(urlsplit(joined).path)


def _fetch_file(base: str, path: str, *, max_bytes: int | None) -> HttpResponse:
    fs_path = _file_path_for(base, path)
    try:
        with open(fs_path, "rb") as f:
            body = _read_capped(f, max_bytes)
    except FileNotFoundError as e:
        raise NotFoundHttpError(f"{base}{path}: not found ({fs_path})") from e
    return HttpResponse(status=200, headers={}, body=body, url=f"file://{fs_path}")


def fetch_from_bases(
    bases: Sequence[str],
    path: str,
    *,
    mode: str,
    accept: Sequence[str] | None = None,
    max_bytes: int | None = None,
    policy: FetchPolicy,
    retry: RetryPolicy,
    query: str | None = None,
) -> HttpResponse:
    """GET `path` off the first base that serves it, in order.

    `query` (a raw, already-encoded query string, no leading `?`) is
    appended only for an `http`/`https` base — PLAN §3.2's "Client rules
    this forces": "never append a query string to a `file://` URL". A
    file-served route tree therefore sees the plain path only; server-side
    `artifactType` filtering is an HTTP-only convenience on top of this
    fetcher's own client-side filter, never something a `file://` fixture
    needs to understand.

    `mode` is `"tag"` or `"digest"` (layout-v2 spec §7.5 / §10 A5):

    - `tag`: a 404 moves to the next base; if every base 404s, the final
      exception is `NotFoundHttpError` (the caller raises
      `ArtifactUnpublishedError`). A transient failure also moves to the
      next base; if every base is unreachable, the final exception is
      `UnreachableHttpError`.
    - `digest`: a 404 moves to the next base, except on the LAST base,
      where it is retried within the shared budget before giving up — a
      listed digest that 404s is a host fault, never "unpublished", so the
      final exception for this mode is always `UnreachableHttpError`.

    `decided-here` (the spec does not state it): when bases disagree — some
    404, some unreachable — the outcome follows the LAST base tried, since
    bases are an explicit preference order and the last one is the final
    word a caller configured.
    """
    if not bases:
        raise UnreachableHttpError("no base URLs configured")
    last_index = len(bases) - 1
    last_error: TransportError | None = None
    for i, base in enumerate(bases):
        is_last = i == last_index
        scheme = urlsplit(base).scheme
        if scheme not in C.ALLOWED_SCHEMES:
            raise UnreachableHttpError(f"{base}: scheme not in {C.ALLOWED_SCHEMES}")
        try:
            if scheme == "file":
                return _fetch_file(base, path, max_bytes=max_bytes)
            full_path = f"{path}?{query}" if query else path
            return _http_get_once(
                base.rstrip("/") + full_path,
                accept=accept,
                max_bytes=max_bytes,
                policy=policy,
                treat_404_as_retryable=(mode == "digest" and is_last),
                retry=retry,
            )
        except NotFoundHttpError as e:
            last_error = e
            continue
        except UnreachableHttpError as e:
            last_error = e
            continue
    assert last_error is not None
    if mode == "tag" and isinstance(last_error, NotFoundHttpError):
        raise last_error
    if isinstance(last_error, (UnauthorizedHttpError, ForbiddenHttpError)):
        raise last_error
    raise UnreachableHttpError(
        f"{path}: unreachable on every configured base ({last_error})"
    ) from last_error


def get_json(
    bases: Sequence[str],
    path: str,
    *,
    mode: str,
    accept: Sequence[str] | None,
    max_bytes: int,
    policy: FetchPolicy,
    retry: RetryPolicy,
    query: str | None = None,
) -> tuple[dict, HttpResponse]:
    """`fetch_from_bases` plus strict JSON decoding, for manifests, indexes
    and referrer listings. Raises `OversizeHttpError` via the body cap and
    lets a `json.JSONDecodeError` propagate (the caller maps it to CORRUPT)."""
    resp = fetch_from_bases(
        bases,
        path,
        mode=mode,
        accept=accept,
        max_bytes=max_bytes,
        policy=policy,
        retry=retry,
        query=query,
    )
    return json.loads(resp.body.decode("utf-8")), resp
