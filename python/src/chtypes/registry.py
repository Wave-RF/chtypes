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
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _setup, errors
from ._ocifetch import Options, Request, Resolved, ensure, list_installed, resolve_installed
from ._ocifetch import _constants as _fetch_constants
from ._ocifetch._dsse import TrustedKey
from ._ocifetch._ensure import detect_host_platform
from ._ocifetch._errors import FetchError
from .library import Library, open_image

__all__ = ["FetchOptions", "Registry", "Resolved", "TrustedKey"]

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
    (`CHTYPES_ARTIFACTS_URL`, `CHTYPES_CACHE`, `CHTYPES_DOWNLOAD_TOKEN`,
    `CHTYPES_ALLOW_UNSIGNED`).
    """

    bases: Sequence[str] = ()
    cache_dir: str | os.PathLike[str] | None = None
    system_dirs: Sequence[str | os.PathLike[str]] | None = None
    token: str | None = None
    trusted_keys: Sequence[TrustedKey] | None = None
    allow_unsigned: bool | None = None
    offline: bool = False
    frozen: bool = False
    lock_path: str | os.PathLike[str] | None = None
    lock_write: bool = False
    update: bool = False

    def _to_options(self, platform: str | None = None) -> Options:
        token = self.token
        if token is None:
            token = os.environ.get(_fetch_constants.ENV_TOKEN_NAME) or None
        allow_unsigned = self.allow_unsigned
        if allow_unsigned is None:
            allow_unsigned = os.environ.get(_fetch_constants.ENV_ALLOW_UNSIGNED_NAME) == "1"
        return Options(
            platform=platform,
            bases=tuple(self.bases),
            cache_dir=self.cache_dir,
            system_dirs=None if self.system_dirs is None else tuple(self.system_dirs),
            token=token,
            trusted_keys=None if self.trusted_keys is None else tuple(self.trusted_keys),
            allow_unsigned=allow_unsigned,
            offline=self.offline,
            frozen=self.frozen,
            lock_path=self.lock_path,
            lock_write=self.lock_write,
            update=self.update,
        )


def _wrap(exc: FetchError) -> errors.ArtifactError:
    """The fetch layer's error as the public class of its code, one family with
    the loader's. The original stays as `__cause__`."""
    cls = _FETCH_CLASSES.get(exc.code, errors.ArtifactError)
    if cls is errors.SourceUnreachableError:
        wrapped: errors.ArtifactError = errors.SourceUnreachableError(
            str(exc),
            retryable=getattr(exc, "retryable", False),
            retry_after=getattr(exc, "retry_after", None),
        )
    else:
        wrapped = cls(str(exc))
    wrapped.__cause__ = exc
    return wrapped


class Registry:
    """Opens versions. Safe across threads. `autofetch` defaults to
    `CHTYPES_AUTOFETCH`, and is off when that is unset.

    Construction opens nothing: only `for_version` and `preload` open an
    artifact. `preload` opens each listed request through the loader at
    construction, in list order, and never fetches, even with autofetch on.
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
        self._lock = threading.RLock()
        self._memo: dict[str, Library] = {}
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
        try:
            return tuple(list_installed(self._fetch._to_options()))
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
            # A failed open clears the setup record while no image has completed
            # load step 7, whatever failed: the spelling, the fetch, the
            # signature or any load step (bindings-v1.md section 6, rule 4).
            began = _setup.generation()
            try:
                library = self._resolve_and_open(request, allow_fetch)
            except Exception:
                _setup.open_failed(began)
                raise
            self._memo[request] = library
            return library

    def _resolve_and_open(self, request: str, allow_fetch: bool) -> Library:
        try:
            fetch_request = Request(request)
        except ValueError as exc:
            raise errors.misuse(str(exc)) from None
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
                        f"chtypes: no installed artifact for {request} ({platform}); "
                        "autofetch is off"
                    )
                resolved = ensure(fetch_request, options)
        except FetchError as exc:
            raise _wrap(exc) from exc
        # The adapter: the fetch layer's record, passed on exactly as it came.
        # The predicate is the statement the signature covered, never re-encoded.
        return open_image(str(Path(resolved.library_path)), resolved.predicate, resolved)
