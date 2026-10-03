"""ABI v1's own exception classes, hand-written and stable across a
regeneration -- only the STATUS/REASON -> class MAPPING is generated
(_errmap.py), from spec/abi-v1/sdk.json's `errors.classes`.

These stay LOCAL to python/src/chtypes/_abi1 until wave C (the FFI brief,
"Errors stay abi1-local until wave C"): the public `chtypes.errors` module
(python/src/chtypes/errors.py) is v0's, and nothing here imports it or is
imported by it. Wave C's public API rewrite decides how -- or whether -- the
names converge; until then an abi1-local `SchemaError` is a different class
from the public `chtypes.SchemaError`, even though sdk.json gives them the
same name on purpose (that IS the intended eventual mapping, spelled out
early so the wave C rename is just moving these classes, not renaming them).
"""

from __future__ import annotations


class Abi1Error(Exception):
    """Base class for every exception this abi1 layer raises."""


class CallError(Abi1Error):
    """A fallible call returned a status other than CHS_OK, with its chs_error
    fields read through the error accessors (spec/abi-v1/sdk.json `errors.fields`)."""

    def __init__(
        self, status: str, ch_code: int, ch_name: bytes, message: bytes, column: bytes
    ) -> None:
        super().__init__(message.decode("utf-8", "replace"))
        self.status = status
        self.ch_code = ch_code
        self.ch_name = ch_name
        self.message = message
        self.column = column


class SchemaError(CallError):
    """CHS_REJECTED: ClickHouse's own refusal, with its own code, name and message."""


class UnsupportedError(CallError):
    """CHS_DECLINED: this build will not answer; a server might accept.
    Never scored as agreement."""


class UsageError(CallError):
    """CHS_INVALID_ARGUMENT: caller misuse -- a NULL pointer with a nonzero
    length, a wrong-kind, freed or cross-image handle, or a NULL required
    out-parameter."""


class InternalError(CallError):
    """CHS_INTERNAL, or a status value outside the closed chs_status set
    (which cannot happen under a matching fingerprint, but is handled rather
    than trusted away)."""


class LoaderError(Abi1Error):
    """A loader refusal (plan section 3.2): `reason` is one of
    spec/abi-v1/sdk.json's `loader.refusals` reasons, with a `:<suffix>` for
    `missing_symbol` and `build_info_mismatch`. `want`/`got` are set only
    where the refusal names both values."""

    def __init__(self, reason: str, path: str, want: object = None, got: object = None) -> None:
        super().__init__(f"{reason}: {path}")
        self.reason = reason
        self.path = path
        self.want = want
        self.got = got


class ArtifactIncompatibleError(LoaderError):
    """The library cannot be this process's ABI v1 artifact: wrong glibc,
    dlopen failed, not an ABI v1+ artifact, a version or fingerprint
    mismatch, or a missing described symbol."""


class ArtifactCorruptError(LoaderError):
    """The artifact's own bytes disagree with its signed predicate, or
    `chs_build_info()` cannot be parsed: the artifact is internally
    inconsistent, which a retry of the same bytes will not fix."""
