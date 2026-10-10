"""`Registry`: from a version request to a loaded `Library`
(docs/reference/bindings-v1.md section 6).

The sequence of `for_version(request)`:

1. the registry's memo: a request opened before returns the same `Library`, for
   the registry's life (a line request never moves mid-process);
2. `resolve_installed`, never the network;
3. on a miss, `ensure` when autofetch is on (it may use the network), else
   `ArtifactMissingError` naming the request and the platform;
4. the adapter, `Resolved -> (path, predicate)`, which passes the predicate
   exactly as the fetch layer returned it;
5. loader steps 1 to 7, once per image.

Construction opens nothing. The binding orders and matches no versions itself:
which installed build a floating request means, and the spelling rules, are the
fetch layer's.

Opens of different requests never wait for each other's fetch (public issue
#491): the registry's lock guards its memo and the attempts in progress, and is
never held across steps 2 to 5. Concurrent opens of one request share one
attempt, and so one fetch, and each gets that attempt's `Library` or its error;
a failed attempt is never remembered, so the next open starts a new one.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _setup, errors
from ._ocifetch import Options, Request, Resolved, ensure, list_installed, resolve_installed
from ._ocifetch import _channel as _fetch_channel
from ._ocifetch import _constants as _fetch_constants
from ._ocifetch._dsse import TrustedKey as _TrustedKey
from ._ocifetch._dsse import trusted_keys_from_hex
from ._ocifetch._ensure import detect_host_platform, missing_notes, with_notes
from ._ocifetch._errors import FetchError
from ._ocifetch._layout import resolve_cache_root, search_roots
from ._ocifetch._oci import version_within_request
from .library import Library, open_image

__all__ = ["FetchOptions", "Registry", "Resolved", "cache_root", "search_dirs"]

_TRUTHY = ("1", "true", "yes", "on")

_FETCH_CLASSES: dict[str, type[errors.ArtifactError]] = {
    errors.CODE_ARTIFACT_MISSING: errors.ArtifactMissingError,
    errors.CODE_ARTIFACT_UNTRUSTED: errors.ArtifactUntrustedError,
    errors.CODE_ARTIFACT_CORRUPT: errors.ArtifactCorruptError,
    errors.CODE_ARTIFACT_PINNED: errors.ArtifactPinnedError,
    errors.CODE_ARTIFACT_UNPUBLISHED: errors.ArtifactUnpublishedError,
    errors.CODE_SOURCE_UNREACHABLE: errors.SourceUnreachableError,
    errors.CODE_SOURCE_UNAUTHORIZED: errors.SourceUnauthorizedError,
    errors.CODE_SOURCE_FORBIDDEN: errors.SourceForbiddenError,
    errors.CODE_SOURCE_INCOMPATIBLE: errors.SourceIncompatibleError,
    errors.CODE_CACHE_UNUSABLE: errors.CacheUnusableError,
    errors.CODE_SOURCE_RETIRED: errors.SourceRetiredError,
}


@dataclass(frozen=True)
class FetchOptions:
    """Everything the fetch layer configures that a caller may set: the bases to
    read, the cache directory, the read-only system directories searched after
    it (`None` keeps the default list, an empty sequence searches none), trust,
    offline and frozen modes, and the lock.

    The test hooks of the fetch layer (its clock, retry policy and
    pre-rename callback) are deliberately not here. A field left at its default
    falls back to the fetch layer's own defaults and environment
    (`CHTYPES_CACHE`, `CHTYPES_DOWNLOAD_TOKEN`).

    2.0.0-dev (spec/abi-v2/docs.md, rules r5 and r6): this SDK fetches only from
    the staging dev channel and trusts only its key. `bases`, `trusted_keys` and
    `allow_unsigned`, and `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and
    `CHTYPES_ALLOW_UNSIGNED`, are ignored, each with one warning. `frozen`,
    `lock_path`, `lock_write` and `update` are refused before any network call,
    as a `UsageError`. A `cache_dir` (or `CHTYPES_CACHE`) is used through its
    `v2-dev` subroot.
    """

    bases: Sequence[str] = ()
    cache_dir: str | os.PathLike[str] | None = None
    system_dirs: Sequence[str | os.PathLike[str]] | None = None
    token: str | None = None
    # Raw 32-byte ed25519 public keys, each as 64 hex digits, as CHTYPES_TRUSTED_KEYS
    # spells them; the key id is derived (sha256-first16hex). A non-empty list
    # REPLACES the default trust. A key that is not 64 hex digits is a UsageError.
    trusted_keys: Sequence[str] | None = None
    allow_unsigned: bool | None = None
    # Offline reads the cache only and makes no request. It is on when this is
    # set OR CHTYPES_OFFLINE=1; neither turns the other off (public issue #528).
    offline: bool = False
    frozen: bool = False
    lock_path: str | os.PathLike[str] | None = None
    lock_write: bool = False
    update: bool = False
    # Strict mode (public issue #486): every fault of the cache and of an
    # existing system dir is a `CacheUnusableError` naming the path, never
    # "not installed", and never a fall-through to a system dir. None reads
    # CHTYPES_CACHE_STRICT ("1" is on), else off.
    strict_cache: bool | None = None

    def _trusted_keys(self) -> tuple[_TrustedKey, ...] | None:
        if self.trusted_keys is None:
            return None
        if isinstance(self.trusted_keys, str):
            raise errors._misuse("chtypes: trusted_keys is a sequence of hex keys, not one string")
        try:
            return trusted_keys_from_hex(self.trusted_keys, "the trusted_keys option")
        except (ValueError, AttributeError) as exc:
            raise errors._misuse(str(exc)) from None

    def _to_options(self, platform: str | None = None) -> Options:
        token = self.token
        if token is None:
            token = os.environ.get(_fetch_constants.ENV_TOKEN_NAME) or None
        allow_unsigned = self.allow_unsigned
        if allow_unsigned is None:
            # The dev channel does not honor the variable (rule r6); the fetch
            # layer names it, once, when it is set.
            allow_unsigned = (
                _fetch_channel.active().overridable
                and os.environ.get(_fetch_constants.ENV_ALLOW_UNSIGNED_NAME) == "1"
            )
        return Options(
            platform=platform,
            bases=tuple(self.bases),
            cache_dir=self.cache_dir,
            system_dirs=None if self.system_dirs is None else tuple(self.system_dirs),
            token=token,
            trusted_keys=self._trusted_keys(),
            allow_unsigned=allow_unsigned,
            offline=self.offline,
            frozen=self.frozen,
            lock_path=self.lock_path,
            lock_write=self.lock_write,
            update=self.update,
            strict_cache=self.strict_cache,
        )


def cache_root(options: FetchOptions | None = None) -> Path:
    """The cache root a fetch, list or `chtypes where` with `options` would use:
    `options.cache_dir`, else `CHTYPES_CACHE` (each used through its `v2-dev`
    subroot, spec/abi-v2/docs.md rule r5), else
    `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`.

    It is the fetch layer's own resolution, `search_dirs(options)[0]`. It
    creates nothing and reads no cache."""
    return resolve_cache_root((options or FetchOptions()).cache_dir)


def search_dirs(options: FetchOptions | None = None) -> tuple[Path, ...]:
    """Every directory a lookup reads for installed builds, in the order it reads
    them: the cache root first, then each read-only system directory
    (`options.system_dirs`; `None` is the built-in list, an empty sequence none).
    The order is the fetch layer's own, and on a tie the earlier directory wins.
    It creates nothing and touches no file."""
    o = options or FetchOptions()
    return search_roots(o.cache_dir, o.system_dirs)


def _wrap(exc: FetchError) -> errors.ArtifactError:
    """The fetch layer's error as the public class of its code, one family with
    the loader's. The original stays as `__cause__`."""
    cls = _FETCH_CLASSES.get(exc.code, errors.ArtifactError)
    if cls is errors.CacheUnusableError:
        wrapped: errors.ArtifactError = errors.CacheUnusableError(
            str(exc),
            path=getattr(exc, "path", ""),
            reason=getattr(exc, "reason", ""),
            os_error=getattr(exc, "os_error", None),
        )
    else:
        wrapped = cls(str(exc))
    wrapped.__cause__ = exc
    return wrapped


class _Flight:
    """One attempt to open one request: the installed lookup, the fetch when
    there is one, and the load. Every open of that request made while it is in
    progress waits on it and gets its answer."""

    __slots__ = ("done", "error", "library")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.library: Library | None = None
        self.error: BaseException | None = None


class Registry:
    """Opens versions. Safe across threads. `autofetch` defaults to
    `CHTYPES_AUTOFETCH`, and is off when that is unset.

    Construction opens nothing: only `for_version` and `preload` open an
    artifact. `preload` opens each listed request through the loader at
    construction, in list order, and never fetches, even with autofetch on.

    An open never waits for another request's fetch, and concurrent opens of
    one request share one fetch (public issue #491).
    """

    def __init__(
        self,
        *,
        fetch: FetchOptions | None = None,
        autofetch: bool | None = None,
        preload: Sequence[str] = (),
    ) -> None:
        self._fetch = fetch if fetch is not None else FetchOptions()
        if autofetch is None:
            env = os.environ.get(_fetch_constants.ENV_AUTOFETCH_NAME, "")
            autofetch = env.strip().lower() in _TRUTHY
        self._autofetch = autofetch
        # Guards the memo and the attempts in progress, never a lookup, a fetch
        # or a load.
        self._lock = threading.Lock()
        self._memo: dict[str, Library] = {}
        self._flights: dict[str, _Flight] = {}
        # A test hook, None outside tests: called each time an open starts
        # waiting on an attempt, its own or another open's.
        self._on_wait: Callable[[str], None] | None = None
        if isinstance(preload, str):
            raise TypeError("preload must be a sequence of requests, not a single string")
        for request in preload:
            self._open(request, allow_fetch=False)

    def for_version(self, request: str) -> Library:
        """Open a version: `26.8`, `26.8.15` or `26.8.15.10` (no `v` prefix, no
        channel suffix). A request the registry opened before returns the same
        `Library`."""
        return self._open(request, allow_fetch=self._autofetch)

    def installed(self) -> tuple[Resolved, ...]:
        """What is installed, from the fetch layer's own listing."""
        options = self._fetch._to_options()
        _refuse_pinning(options)
        try:
            return tuple(list_installed(options))
        except FetchError as exc:
            raise _wrap(exc) from exc

    def libraries(self) -> tuple[Library, ...]:
        """What is open in this registry, in the order it was opened."""
        with self._lock:
            seen: list[Library] = []
            for library in self._memo.values():
                if not any(library is other for other in seen):
                    seen.append(library)
            return tuple(seen)

    def _open(self, request: str, *, allow_fetch: bool) -> Library:
        with self._lock:
            hit = self._memo.get(request)
        if hit is not None:
            return hit
        # A refused version spelling is the caller's own misuse, refused before
        # anything is attempted, and unlocks nothing.
        try:
            fetch_request = Request(request)
        except ValueError as exc:
            raise errors._misuse(str(exc)) from None
        # So is a pinning request on the dev channel (rule r6).
        _refuse_pinning(self._fetch._to_options())
        with self._lock:
            hit = self._memo.get(request)
            if hit is not None:
                return hit
            flight = self._flights.get(request)
            lead = flight is None
            if flight is None:
                flight = self._flights[request] = _Flight()
        on_wait = self._on_wait
        if on_wait is not None:
            on_wait(request)
        if lead:
            self._attempt(flight, request, fetch_request, allow_fetch)
        else:
            flight.done.wait()
        if flight.error is not None:
            raise flight.error
        assert flight.library is not None
        return flight.library

    def _attempt(
        self, flight: _Flight, request: str, fetch_request: Request, allow_fetch: bool
    ) -> None:
        """Run an attempt on the leading open's thread, with no lock held, then
        release every open waiting on it. An attempt that tried a load and
        failed unlocks the setup record while no image has completed load step
        7, whatever failed: the resolve, the fetch, the signature or any load
        step (bindings-v1.md section 6, rule 4). It is never remembered."""
        began = _setup.generation()
        library: Library | None = None
        error: BaseException | None = None
        try:
            library = self._resolve_and_open(request, fetch_request, allow_fetch)
        except Exception as exc:
            _setup.open_failed(began)
            error = exc
        except BaseException as exc:
            error = exc
        with self._lock:
            del self._flights[request]
            if error is None:
                assert library is not None
                self._memo[request] = library
            flight.library, flight.error = library, error
        flight.done.set()

    def _resolve_and_open(self, request: str, fetch_request: Request, allow_fetch: bool) -> Library:
        try:
            platform = detect_host_platform()
        except ValueError as exc:
            raise errors.ArtifactIncompatibleError(str(exc), reason="host_platform") from None
        options = self._fetch._to_options(platform)
        try:
            resolved = resolve_installed(fetch_request, platform, options)
            if resolved is None:
                if not allow_fetch:
                    raise errors.ArtifactMissingError(
                        with_notes(
                            f"chtypes: no installed artifact for {request} ({platform}); "
                            "autofetch is off",
                            missing_notes(options),
                        )
                    )
                resolved = ensure(fetch_request, options)
        except FetchError as exc:
            raise _wrap(exc) from exc
        # The adapter: the fetch layer's record, passed on exactly as it came.
        # The predicate is the statement the signature covered, never re-encoded.
        library = open_image(str(Path(resolved.library_path)), resolved.predicate, resolved)
        _check_within_request(library, request)
        return library


def _refuse_pinning(options: Options) -> None:
    """The fetch layer's contract check, ahead of everything else: a 2.0.0-dev
    SDK refuses a lock, frozen or update request as the caller's misuse, before
    any network call, and names each ignored override once (rule r6)."""
    try:
        _fetch_channel.enforce(options)
    except _fetch_channel.PinningRefusedError as exc:
        raise errors._misuse(str(exc)) from None


def _check_within_request(library: Library, request: str) -> None:
    """The load-time assertion (docs/guides/fetch-v1.md section 9; public
    issue #481): the library opened for `request` must report, in its own
    build_info, a clickhouse_version within that request (equal to an exact
    request, within a line one), whatever the cache answered. Otherwise the
    open fails as `ArtifactCorruptError` with reason
    `build_info_mismatch:clickhouse_version`, the code section 4 gives a
    signed version outside the request. A request that is not a version
    spelling names no version and is not checked. The image stays loaded for
    the requests it does answer."""
    try:
        ok = version_within_request(library.version, request)
    except ValueError:
        ok = False
    if not ok:
        raise errors.ArtifactCorruptError(
            f"chtypes: {library.path} refused: build_info_mismatch:clickhouse_version "
            f"(want a build within {request!r}, got {library.version!r}): the library "
            f"opened for ClickHouse {request} reports another version",
            reason="build_info_mismatch:clickhouse_version",
            path=str(library.path),
            want=request,
            got=library.version,
        )
