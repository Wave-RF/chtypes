"""Errors, and the one sentinel that must never be confused with a rejection."""

from __future__ import annotations

from typing import Final

__all__ = [
    "CODE_UNSUPPORTED",
    "ChtypesError",
    "RegistryError",
    "SchemaError",
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
