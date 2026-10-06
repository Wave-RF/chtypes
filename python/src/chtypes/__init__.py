"""chtypes: ClickHouse's own type system, per version, from Python.

One question, exactly: *if this row were inserted into this ClickHouse table on
this ClickHouse version, what would happen?* The answer comes from ClickHouse's
real C++ machinery, vendored per release into a shared library behind the frozen
C ABI. Nothing here reimplements a coercion rule, which is why the answers are
exact by construction rather than approximately right.

    import chtypes
    from chtypes import Format, Registry

    chtypes.setup(timezone="UTC")                # once, before the first open
    registry = Registry(autofetch=True)
    library = registry.for_version("26.8")       # a release line or an exact version
    ddl = b"CREATE TABLE t (ts DateTime, seq UInt8) ENGINE = Memory"
    with library.compile_table(ddl) as schema:
        batch = schema.rows(Format.JSON_EACH_ROW, b'{"ts":"2026-01-15 10:30:00","seq":256}\\n')

    batch.outcome        # Outcome.ACCEPTED: the insert would succeed
    batch.transformed    # the changes ClickHouse makes and reports as success

The specification is docs/reference/bindings-v1.md. Names, SQL, messages and
renderings come back as `bytes`: a name that is not valid UTF-8 round-trips
exactly, and nothing here decodes one for you.

Three things a caller must not skip:

* **`transformed` is the product.** ClickHouse returns success for every one of
  those changes. Reading `outcome` alone says a row was accepted and nothing
  about the value the table will hold.
* **`Outcome.UNSUPPORTED` is not a rejection.** It means a real server might well
  have accepted this and this build declines to answer.
* **Insert the library's output, not your input**, when a row carries a
  `Source.DEFAULT_GENERATED` column: ask `rows` for an `export` and insert its
  `payload`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError as _PackageNotFound
from importlib.metadata import version as _package_version

from ._abi1._vocab import (
    EXPORT_NONE,
    DefaultKind,
    DocFlags,
    FilterOutcome,
    Format,
    Outcome,
    Reason,
    Source,
    Status,
    Verdict,
)
from ._input import BytesIn, Settings
from ._setup import setup
from .errors import (
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_INCOMPATIBLE,
    CODE_ARTIFACT_MISSING,
    CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_ARTIFACT_UNTRUSTED,
    CODE_CACHE_UNUSABLE,
    CODE_SOURCE_FORBIDDEN,
    CODE_SOURCE_INCOMPATIBLE,
    CODE_SOURCE_UNAUTHORIZED,
    CODE_SOURCE_UNREACHABLE,
    ArtifactCorruptError,
    ArtifactError,
    ArtifactIncompatibleError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    CacheUnusableError,
    CallError,
    ChtypesError,
    InternalError,
    SchemaError,
    SourceForbiddenError,
    SourceIncompatibleError,
    SourceUnauthorizedError,
    SourceUnreachableError,
    UnsupportedError,
    UsageError,
)
from .library import Block, Filter, Library, Schema, open_unverified
from .registry import FetchOptions, Registry, Resolved, TrustedKey
from .results import (
    BatchResult,
    BuildInfo,
    Capabilities,
    Column,
    Computed,
    DiscoveredColumn,
    Discovery,
    EngineCell,
    ErrorCodeEntry,
    ErrorCodeTable,
    FilterResult,
    FilterRowError,
    Framing,
    Header,
    RowResult,
    SchemaDescription,
    Span,
    Transform,
    Value,
)

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
    "CODE_SOURCE_UNAUTHORIZED",
    "CODE_SOURCE_UNREACHABLE",
    "EXPORT_NONE",
    "ArtifactCorruptError",
    "ArtifactError",
    "ArtifactIncompatibleError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "BatchResult",
    "Block",
    "BuildInfo",
    "BytesIn",
    "CacheUnusableError",
    "CallError",
    "Capabilities",
    "ChtypesError",
    "Column",
    "Computed",
    "DefaultKind",
    "DiscoveredColumn",
    "Discovery",
    "EngineCell",
    "DocFlags",
    "ErrorCodeEntry",
    "ErrorCodeTable",
    "FetchOptions",
    "Filter",
    "FilterOutcome",
    "FilterResult",
    "FilterRowError",
    "Format",
    "Framing",
    "Header",
    "InternalError",
    "Library",
    "Outcome",
    "Reason",
    "Registry",
    "Resolved",
    "RowResult",
    "Schema",
    "SchemaDescription",
    "SchemaError",
    "Settings",
    "Source",
    "SourceForbiddenError",
    "SourceIncompatibleError",
    "SourceUnauthorizedError",
    "SourceUnreachableError",
    "Span",
    "Status",
    "Transform",
    "TrustedKey",
    "UnsupportedError",
    "UsageError",
    "Value",
    "Verdict",
    "open_unverified",
    "setup",
]

# Derived, never written twice. pyproject.toml is the single source of truth and
# the build reads it from there; asking importlib for it means this attribute
# cannot drift from the package it names.
try:
    __version__ = _package_version("chtypes")
except _PackageNotFound:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
