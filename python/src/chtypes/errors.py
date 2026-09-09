"""Errors, and the one sentinel that must never be confused with a rejection."""

from __future__ import annotations

from collections.abc import Sequence
from os import PathLike
from typing import Final

__all__ = [
    "CODE_ARTIFACT_CORRUPT",
    "CODE_ARTIFACT_MISSING",
    "CODE_ARTIFACT_PINNED",
    "CODE_ARTIFACT_UNPUBLISHED",
    "CODE_ARTIFACT_UNTRUSTED",
    "CODE_SOURCE_UNREACHABLE",
    "CODE_UNSUPPORTED",
    "ArtifactCorruptError",
    "ArtifactError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "ChtypesError",
    "RegistryError",
    "SchemaError",
    "SourceUnreachableError",
    "UnsignedArtifactWarning",
    "UnsupportedError",
]

# CHS_CODE_UNSUPPORTED from include/chtypes.h. Never a real ClickHouse error
# code: it means "a real server might well have accepted this; I decline to
# guess". Mapping it onto a rejection manufactures an over-reject that the
# product never made; mapping it onto an acceptance manufactures an over-accept,
# which is the cardinal sin (spec/c-abi.md "Error model").
CODE_UNSUPPORTED: Final[int] = -2


class ChtypesError(Exception):
    """Base class for every error this binding raises.

    Raised directly only for binding-level faults that are neither a schema
    verdict nor a registry fault: a closed handle, a malformed result
    document, a refused `set_default_settings` payload. Catching it catches
    everything below, including `UnsupportedError` — handle that arm
    explicitly first when the distinction matters (it almost always does).
    """


class RegistryError(ChtypesError):
    """An artifact directory could not be loaded, or a version does not resolve.

    Covers everything on the loading path: an unreadable registry directory, a
    missing/unusable ``manifest.json``, a hash or size mismatch under
    ``verify_hashes=True``, a failed ``dlopen``, a missing mandatory symbol, an
    ABI-revision mismatch (the artifact reports a nonzero revision different
    from `chtypes.ABI_REVISION`), a failed `chs_init`, and a version string
    `Registry.for_version` cannot resolve (the message names the versions that
    ARE loaded — there is deliberately no nearest-version fallback).
    """


# The fetch/verify codes every SDK shares (docs/fetch.md §7). A string code
# rather than a subclass test is what a CLI, a log line or a metric keys on,
# so it is carried on the exception as `.code` and never spelled twice.
CODE_ARTIFACT_MISSING: Final = "CHTYPES_ARTIFACT_MISSING"
CODE_ARTIFACT_UNTRUSTED: Final = "CHTYPES_ARTIFACT_UNTRUSTED"
CODE_ARTIFACT_CORRUPT: Final = "CHTYPES_ARTIFACT_CORRUPT"
CODE_ARTIFACT_PINNED: Final = "CHTYPES_ARTIFACT_PINNED"
CODE_ARTIFACT_UNPUBLISHED: Final = "CHTYPES_ARTIFACT_UNPUBLISHED"
CODE_SOURCE_UNREACHABLE: Final = "CHTYPES_SOURCE_UNREACHABLE"


class ArtifactError(RegistryError):
    """An artifact is missing, could not be obtained, or failed verification.

    The base of the fetch-side family (docs/fetch.md §3, §5, §7). Every
    subclass fixes `code` to one of the six codes the four SDKs share, so a
    caller can key on either the type or the string:

    - `ArtifactMissingError` — `CHTYPES_ARTIFACT_MISSING`: no installed
      artifact for the line anywhere on the search path;
    - `ArtifactUntrustedError` — `CHTYPES_ARTIFACT_UNTRUSTED`: the release is
      unsigned or mis-signed;
    - `ArtifactCorruptError` — `CHTYPES_ARTIFACT_CORRUPT`: any hash
      disagreement, in the release or on disk;
    - `ArtifactPinnedError` — `CHTYPES_ARTIFACT_PINNED`: the release differs
      from what the lock file pins;
    - `ArtifactUnpublishedError` — `CHTYPES_ARTIFACT_UNPUBLISHED`: the
      release has nothing for this platform/line;
    - `SourceUnreachableError` — `CHTYPES_SOURCE_UNREACHABLE`: the source
      could not be read (network, or offline with nothing installed).

    A `RegistryError`, because every one of them is a reason the loader
    cannot serve a version; ``except RegistryError`` still catches them all.
    """

    code: str = ""


class ArtifactMissingError(ArtifactError):
    """No artifact for the requested line anywhere on the registry search path.

    The one error every SDK raises for a missing artifact (docs/fetch.md §7),
    with the shared message, verbatim apart from the bracketed parts: the
    line, the platform, every directory that was looked in, and this SDK's
    own fetch command. Raised by `Registry.for_version` when lazy fetch is
    off; with `autofetch` on, the fetch runs first and its own failure is
    raised instead.

    Attributes:
        line: the ClickHouse minor line that was asked for.
        platform: ``<os>-<arch>`` of the running host.
        looked_in: the directories searched, in search-path order.
    """

    code = CODE_ARTIFACT_MISSING

    def __init__(self, line: str, platform: str, looked_in: Sequence[str | PathLike[str]]) -> None:
        self.line = line
        self.platform = platform
        self.looked_in = tuple(str(d) for d in looked_in)
        super().__init__(
            f"chtypes: no artifact for ClickHouse {line} ({platform}). "
            f"Looked in: {', '.join(self.looked_in)}.\n"
            f"Install it:  python -m chtypes fetch {line}\n"
            f"or set CHTYPES_AUTOFETCH=1 to fetch on first use."
        )


class ArtifactUntrustedError(ArtifactError):
    """The release's ``SHA256SUMS`` is unsigned, or signed by no trusted key.

    Verification stops here, before a byte of library moves; nothing is ever
    downloaded around it. `CHTYPES_ALLOW_UNSIGNED=1` is the one, loud escape
    (docs/fetch.md §4).
    """

    code = CODE_ARTIFACT_UNTRUSTED


class ArtifactCorruptError(ArtifactError):
    """A hash disagreed somewhere in the chain (docs/fetch.md §3).

    `index.json` against the signed `SHA256SUMS`, the downloaded tarball
    against both, the manifest inside against the index, or the installed
    library against its manifest. A broken release is reported, never
    repaired.
    """

    code = CODE_ARTIFACT_CORRUPT


class ArtifactPinnedError(ArtifactError):
    """The release offers something other than what the lock file pins."""

    code = CODE_ARTIFACT_PINNED


class ArtifactUnpublishedError(ArtifactError):
    """The release has no artifact for this platform, line or exact patch."""

    code = CODE_ARTIFACT_UNPUBLISHED


class SourceUnreachableError(ArtifactError):
    """The source could not be read: a network failure, a missing release
    index, or `offline=True` with nothing installed."""

    code = CODE_SOURCE_UNREACHABLE


class UnsignedArtifactWarning(UserWarning):
    """Emitted, once per fetch, when `CHTYPES_ALLOW_UNSIGNED=1` skips the
    signature check — the one loud warning docs/fetch.md §4 requires."""


class SchemaError(ChtypesError):
    """ClickHouse itself REFUSED a type expression, a DDL, an engine or a TTL.

    A genuine rejection: `code` is ALWAYS a real ClickHouse error code (`50`
    unknown type family, `115` unknown setting name at compile or on the
    engine's MergeTree `SETTINGS`, `455`/`44` a type gate declared at a
    refusing value, ...) and `msg` is the server's own message, passed
    through verbatim. This DDL can never exist on that version, and the
    tenant has to be told.

    "This build declines to answer" is a DIFFERENT TYPE — `UnsupportedError`,
    a PEER of this one since 2026-08-26, deliberately NOT a subclass — so no
    `SchemaError` ever carries `CODE_UNSUPPORTED` and ``except SchemaError``
    never catches a decline (spec/bindings.md rule 12). The two must never be
    conflated: reporting a decline as a rejection manufactures an over-reject
    (silent data loss), and hiding a rejection behind a decline lets a DDL
    that can never exist look merely unmodelled. Both budgets are zero
    (spec/c-abi.md §Error model).

    Attributes:
        code: the ClickHouse error code — always the server's own.
        msg: ClickHouse's message, verbatim.
        column: the column the error is attributed to, ``""`` when none.
            Never guessed: populated only when the C layer's own structured
            answer names one, which no schema-path entry point does today.
    """

    def __init__(self, code: int, msg: str, column: str = "") -> None:
        self.code = code
        self.msg = msg
        self.column = column
        if column:
            super().__init__(f"chtypes: column {column!r}: [{code}] {msg}")
        else:
            super().__init__(f"chtypes: [{code}] {msg}")


class UnsupportedError(ChtypesError):
    """This build DECLINES to answer: a real server might well have accepted this.

    Not a rejection, and treating it as one is a scoring error — the caller
    must fall back to the server (validate cautiously, forward unpreviewed)
    rather than tell the tenant their DDL or row is wrong. Raised for: engines
    and sorting keys this build does not model, refused TTL forms, a
    MergeTree setting declared at a non-default value, DEFAULTs this build
    refuses to evaluate (server/session properties, `sleep`, admission-budget
    breaches), a compile `mode` the library does not define, and any symbol
    the loaded artifact predates.

    A PEER of `SchemaError`, deliberately NOT a subclass (spec/bindings.md
    rule 12; the pre-2026-08-26 subtype was grandfathered and is gone): a
    decline that still satisfied ``except SchemaError`` would let every
    handler that forgot the distinction silently convert declines into
    rejections — a manufactured over-reject, budgeted at zero. As a peer, the
    same omission raises past the handler, which is loud. Handle the two arms
    explicitly; ``except ChtypesError`` still catches both when both is what
    you mean.

    It carries NO `code` attribute — there is no ClickHouse code to carry.
    The rendered message keeps the frozen ``[-2]`` shape (the ABI's
    `CODE_UNSUPPORTED` wire sentinel) that `SchemaError` renders its code
    with: the conformance drivers put that exact string on the protocol wire
    as an `unsupported` scope, so the rendering is part of the contract even
    though the sentinel is not a field.

    Attributes:
        msg: why this build declines, in its own words.
        column: the column the decline is attributed to, ``""`` when none —
            for callers that KNOW one (a gateway's EPHEMERAL decline), never
            guessed.
    """

    def __init__(self, msg: str, column: str = "") -> None:
        self.msg = msg
        self.column = column
        if column:
            super().__init__(f"chtypes: column {column!r}: [{CODE_UNSUPPORTED}] {msg}")
        else:
            super().__init__(f"chtypes: [{CODE_UNSUPPORTED}] {msg}")


def _error_for(code: int, msg: str, column: str = "") -> ChtypesError:
    """The ONE place an ABI error code becomes an exception, so the
    refusal/decline split cannot be decided differently in two files.

    The SIGN decides (spec/bindings.md rule 12, spec/c-abi.md §Error model):
    a positive code is the server's own refusal and rides through verbatim;
    any negative code is this library declining (`-2` "I will not guess",
    `-1` a guarded exception) and becomes an `UnsupportedError`. Keying on
    the sign rather than on ``== CODE_UNSUPPORTED`` means a negative sentinel
    a later era adds can never become "a SchemaError with a negative code",
    which that type's own contract forbids.
    """
    if code < 0:
        return UnsupportedError(msg, column)
    return SchemaError(code, msg, column)
