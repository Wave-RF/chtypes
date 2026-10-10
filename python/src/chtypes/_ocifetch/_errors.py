"""v1 fetch-layer errors (docs/guides/fetch-v1.md "Errors").

Scoped to `chtypes._ocifetch`: the stable, public exception types live in
`chtypes.errors` (the v0 SHA256SUMS-era fetch), which this module never
imports from or edits. A future switch lane decides how (or whether) these
become public types; until then every name here is private.

Every exception carries a `.code` string from one of the twelve codes
`spec/fetch-v1/constants.json`'s `errors` map assigns an exit status to
(the generated `ERROR_EXIT_CODES` table), so a CLI added later needs no
second mapping — it is the same lookup v0's `__main__.py` does today.

`CHTYPES_ARTIFACT_INCOMPATIBLE` is reserved for the FFI loader (the
`abi_fingerprint` / `chs_build_info()` cross-check after `dlopen`,
docs/guides/fetch-v1.md "The seam"): this transport-only layer never raises
it. An unrecognized manifest media type is a transport-side incompatibility
(`CHTYPES_SOURCE_INCOMPATIBLE`), not an artifact-side one.
"""

from __future__ import annotations

from chtypes._ocifetch._constants import ERROR_EXIT_CODES

__all__ = [
    "CODE_ARTIFACT_CORRUPT",
    "CODE_ARTIFACT_MISSING",
    "CODE_ARTIFACT_PINNED",
    "CODE_ARTIFACT_UNPUBLISHED",
    "CODE_ARTIFACT_UNTRUSTED",
    "CODE_CACHE_UNUSABLE",
    "CODE_SOURCE_FORBIDDEN",
    "CODE_SOURCE_INCOMPATIBLE",
    "CODE_SOURCE_RETIRED",
    "CODE_SOURCE_UNAUTHORIZED",
    "CODE_SOURCE_UNREACHABLE",
    "ArtifactCorruptError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "CacheUnusableError",
    "FetchError",
    "SourceForbiddenError",
    "SourceIncompatibleError",
    "SourceRetiredError",
    "SourceUnauthorizedError",
    "SourceUnreachableError",
]

from chtypes._codes import (
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_MISSING,
    CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_ARTIFACT_UNTRUSTED,
    CODE_CACHE_UNUSABLE,
    CODE_SOURCE_FORBIDDEN,
    CODE_SOURCE_INCOMPATIBLE,
    CODE_SOURCE_RETIRED,
    CODE_SOURCE_UNAUTHORIZED,
    CODE_SOURCE_UNREACHABLE,
)

_CODES_USED_HERE = (
    CODE_ARTIFACT_MISSING,
    CODE_ARTIFACT_UNTRUSTED,
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_SOURCE_UNREACHABLE,
    CODE_SOURCE_UNAUTHORIZED,
    CODE_SOURCE_FORBIDDEN,
    CODE_SOURCE_INCOMPATIBLE,
    CODE_CACHE_UNUSABLE,
    CODE_SOURCE_RETIRED,
)

# A drift guard, cheaper than a test: every code this module raises must be
# one the generated constants file (in turn generated from
# spec/fetch-v1/constants.json) actually assigns an exit status to.
assert set(_CODES_USED_HERE) <= set(ERROR_EXIT_CODES), (
    "chtypes._ocifetch._errors: a v1 error code here is missing from "
    "ERROR_EXIT_CODES — spec/fetch-v1/constants.json and this module have drifted"
)


class FetchError(Exception):
    """Base of every v1 fetch-layer error. `code` is one of the constants above."""

    code: str = ""


class ArtifactMissingError(FetchError):
    """No installed artifact for the request anywhere on the search path, offline."""

    code = CODE_ARTIFACT_MISSING


class ArtifactUntrustedError(FetchError):
    """No referrer bundle verified under a trusted key (and unsigned was not allowed)."""

    code = CODE_ARTIFACT_UNTRUSTED


class ArtifactCorruptError(FetchError):
    """A hash, size, structural or predicate-identity check failed.

    Covers: an oversize or malformed index/manifest, a duplicate platform in
    an index, a tampered manifest or layer (size/sha256 mismatch), a
    statement whose subject does not equal the manifest's layer digest, a
    duplicate JSON key in a DSSE payload, an unsafe tar member (symlink,
    hardlink, device, absolute path, `..` traversal, a repeated entry), an
    oversize zstd window, unpacked bytes over the cap, or a library whose
    re-hash disagrees with the signed predicate.
    """

    code = CODE_ARTIFACT_CORRUPT


class ArtifactPinnedError(FetchError):
    """`--frozen` found a release that differs from what the lock file pins."""

    code = CODE_ARTIFACT_PINNED


class ArtifactUnpublishedError(FetchError):
    """The index has nothing for this platform, or every base 404s a tag."""

    code = CODE_ARTIFACT_UNPUBLISHED


class SourceUnreachableError(FetchError):
    """The source could not be read: exhausted retries, a digest 404 on the
    last base (a host fault, never `UNPUBLISHED`), or `offline` with
    nothing installed and nothing to verify against.

    Attributes:
        retryable: whether this was a transient condition (a retry-table
            status, or a connection-level failure) as opposed to, say,
            `offline` with no cache hit.
        retry_after: the source's own requested wait in seconds, when a
            `Retry-After` was both present and within budget. Always
            `None` when the wait was refused as over budget.
    """

    code = CODE_SOURCE_UNREACHABLE

    def __init__(
        self, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class SourceUnauthorizedError(FetchError):
    """A configured `CHTYPES_DOWNLOAD_TOKEN` was itself rejected (401)."""

    code = CODE_SOURCE_UNAUTHORIZED


class SourceForbiddenError(FetchError):
    """The source refused the request outright (403)."""

    code = CODE_SOURCE_FORBIDDEN


class SourceIncompatibleError(FetchError):
    """The source served something this fetcher does not know how to read:
    an index or manifest `mediaType` outside the generated constants."""

    code = CODE_SOURCE_INCOMPATIBLE


class CacheUnusableError(FetchError):
    """A cache directory or entry the fetch layer could not read or write, or
    one strict mode refuses (docs/guides/fetch-v1.md §1, the cache faults;
    public issue #486).

    Attributes:
        path: the exact path that failed.
        reason: `unreadable_root`, `not_a_directory`, `unreadable_entry`,
            `unacceptable_record`, `layout_0x` or `unwritable`.
        os_error: the errno name (`"EACCES"`), or `None` when there is none.
    """

    code = CODE_CACHE_UNUSABLE

    def __init__(
        self, message: str, *, path: str, reason: str, os_error: str | None = None
    ) -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason
        self.os_error = os_error


class SourceRetiredError(FetchError):
    """The source answered 410 Gone: a retired repository, which is permanent,
    so the request was never retried and never sent to the next base
    (docs/guides/fetch-v1.md §2, "A retired repository"; public issue #571).
    The message names the URL that answered and carries the registry's own
    message, made safe to print."""

    code = CODE_SOURCE_RETIRED
