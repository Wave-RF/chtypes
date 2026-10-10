"""The v1 error classes: one table (docs/reference/bindings-v1.md section 4).

Two families under one root, `ChtypesError`:

* `CallError` and its four peers, raised for a call's own status: `SchemaError`
  (`CHS_REJECTED`: ClickHouse's own refusal), `UnsupportedError`
  (`CHS_DECLINED`: this build will not answer), `UsageError`
  (`CHS_INVALID_ARGUMENT`, and any misuse the binding detects itself) and
  `InternalError` (`CHS_INTERNAL`, an unknown status, or a document that does
  not decode). The refusal and the decline are PEERS, never one a subtype of the
  other, so a handler that forgot the distinction cannot turn every decline into
  a rejection. Catching all four is the explicit choice `CallError` makes.
* `ArtifactError` and one class per code: the loader's refusals and the fetch
  layer's errors, one family. A caller catching `ArtifactCorruptError` catches
  the fetch layer's corruption and the loader's step 5 alike.

Every call error carries the five fields of `chs_error`, verbatim and nothing
synthesized. `message` and `column` are BYTES; `str(error)` is the one lossy
display form, with invalid UTF-8 replaced for printing only.
"""

from __future__ import annotations

__all__ = [
    "CODE_ARTIFACT_CORRUPT",
    "CODE_ARTIFACT_INCOMPATIBLE",
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
    "ArtifactError",
    "ArtifactIncompatibleError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "CacheUnusableError",
    "CallError",
    "ChtypesError",
    "InternalError",
    "SchemaError",
    "SourceForbiddenError",
    "SourceIncompatibleError",
    "SourceRetiredError",
    "SourceUnauthorizedError",
    "SourceUnreachableError",
    "UnsupportedError",
    "UsageError",
]

# The codes are generated from spec/fetch-v1/constants.json (public issue #500).
from ._codes import (
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_INCOMPATIBLE,
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

# `chs_status` value of CHS_INVALID_ARGUMENT. The binding's own misuse errors
# carry it so a handler sees one shape whichever side caught the misuse. It is
# the description's value (D3: closed and frozen); `chtypes._abi2._vocab.Status`
# is the generated copy, and a test pins the two together.
_STATUS_INVALID_ARGUMENT = 3
_STATUS_INTERNAL = 4


class ChtypesError(Exception):
    """The root of every error this package raises."""


class CallError(ChtypesError):
    """A call's own failure, with the five fields of `chs_error` verbatim.

    Abstract: only its four peers are raised. Catching it catches all four.

    Attributes:
        status: the raw `chs_status` value; compare with `chtypes.Status`.
        ch_code: ClickHouse's own code; nonzero only for a refusal.
        ch_name: the name this build's vendored table gives the code; ASCII;
            empty if none.
        message: ClickHouse's own message for a refusal, the library's
            otherwise; bytes.
        column: the column concerned, empty if none; bytes.
    """

    def __init__(
        self,
        status: int,
        ch_code: int = 0,
        ch_name: str = "",
        message: bytes = b"",
        column: bytes = b"",
    ) -> None:
        super().__init__(message.decode("utf-8", "replace"))
        self.status = status
        self.ch_code = ch_code
        self.ch_name = ch_name
        self.message = message
        self.column = column


class SchemaError(CallError):
    """`CHS_REJECTED`: ClickHouse's own refusal, which a server would also give."""


class UnsupportedError(CallError):
    """`CHS_DECLINED`: this build will not answer; a server might accept. Never
    a rejection, and never scored as agreement."""


class UsageError(CallError):
    """`CHS_INVALID_ARGUMENT`, or a misuse the binding caught before any call: a
    closed object, a conflicting setup, a zone given twice, an unverified open
    without both opt-ins, a refused version spelling."""


class InternalError(CallError):
    """`CHS_INTERNAL`, a status outside the closed set (naming its value), or a
    document that does not decode: a library bug, never the caller's."""


def _misuse(message: str) -> UsageError:
    """The INVALID_ARGUMENT shape for a misuse the binding detects itself:
    status `CHS_INVALID_ARGUMENT`, `ch_code` 0, an empty `ch_name` and
    `column`, and a message naming the misuse."""
    return UsageError(_STATUS_INVALID_ARGUMENT, 0, "", message.encode("utf-8"), b"")


def _internal(message: str) -> InternalError:
    """A library bug the binding caught: a document that breaks its own schema.
    Carries `CHS_INTERNAL`'s shape, like the library's own."""
    return InternalError(_STATUS_INTERNAL, 0, "", message.encode("utf-8"), b"")


class ArtifactError(ChtypesError):
    """The loader's refusals and the fetch layer's errors: one family.

    Attributes:
        code: one of the `CODE_*` constants of this module.
        reason: a loader refusal's reason from the ABI's loader table, with
            `:<symbol>` or `:<field>` appended where it gives a suffix; None
            for a fetch error.
        path: the library path a loader refusal names, else None.
        want, got: the two values a loader refusal names, where it names both.
    """

    code: str = ""

    def __init__(
        self,
        message: str = "",
        *,
        reason: str | None = None,
        path: str | None = None,
        want: object = None,
        got: object = None,
    ) -> None:
        if not message and reason is not None:
            message = f"{reason}: {path}" if path is not None else reason
            if got is not None:
                message += f" (got {got!r}" + (f", want {want!r})" if want is not None else ")")
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.want = want
        self.got = got


class ArtifactMissingError(ArtifactError):
    """No installed artifact answers the request, and autofetch is off."""

    code = CODE_ARTIFACT_MISSING


class ArtifactUntrustedError(ArtifactError):
    """No signature verified under a trusted key."""

    code = CODE_ARTIFACT_UNTRUSTED


class ArtifactCorruptError(ArtifactError):
    """The artifact's own bytes disagree with its signed statement, or
    `chs_build_info()` cannot be parsed or disagrees with it: internally
    inconsistent, which a retry of the same bytes will not fix."""

    code = CODE_ARTIFACT_CORRUPT


class ArtifactPinnedError(ArtifactError):
    """`frozen` found a release that differs from what the lock pins."""

    code = CODE_ARTIFACT_PINNED


class ArtifactUnpublishedError(ArtifactError):
    """The index has nothing for this platform, or every base answers 404."""

    code = CODE_ARTIFACT_UNPUBLISHED


class ArtifactIncompatibleError(ArtifactError):
    """The library cannot be this process's ABI v1 artifact: wrong glibc, a
    failed dlopen, not a v1 artifact, a version or fingerprint mismatch, or a
    missing described symbol."""

    code = CODE_ARTIFACT_INCOMPATIBLE


class SourceUnreachableError(ArtifactError):
    """The source could not be read: the fetch layer has already spent its
    retry table (docs/guides/fetch-v1.md section 7), and a `Retry-After` it
    refused as over budget is named in the message."""

    code = CODE_SOURCE_UNREACHABLE


class SourceUnauthorizedError(ArtifactError):
    """A configured download token was itself rejected (401)."""

    code = CODE_SOURCE_UNAUTHORIZED


class SourceForbiddenError(ArtifactError):
    """The source refused the request outright (403)."""

    code = CODE_SOURCE_FORBIDDEN


class SourceIncompatibleError(ArtifactError):
    """The source served something this fetcher cannot read."""

    code = CODE_SOURCE_INCOMPATIBLE


class SourceRetiredError(ArtifactError):
    """The source answered 410 Gone: a retired repository, which is permanent,
    so the request was never retried and never sent to the next base. The
    message names the URL that answered and carries the registry's own
    message, made safe to print."""

    code = CODE_SOURCE_RETIRED


class CacheUnusableError(ArtifactError):
    """A cache directory or entry the fetch layer could not read or write, or
    one strict mode refuses: never "not installed", never the network's
    `SourceUnreachableError`, never a raw `OSError`.

    Attributes:
        path: the exact path that failed.
        reason: `unreadable_root`, `not_a_directory`, `unreadable_entry`,
            `unacceptable_record`, `layout_0x` or `unwritable`.
        os_error: the errno name (`"EACCES"`), or None when there is none.
    """

    code = CODE_CACHE_UNUSABLE

    def __init__(
        self, message: str = "", *, path: str, reason: str, os_error: str | None = None
    ) -> None:
        super().__init__(message, reason=reason, path=path)
        self.os_error = os_error
