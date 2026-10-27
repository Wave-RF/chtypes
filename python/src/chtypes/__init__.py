"""chtypes: ClickHouse's own type system, per version, from Python.

This is the ABI v2 binding: it speaks the locked ABI v2 description
(spec/abi-v2/docs.md), refuses a library built from any other fingerprint, and
fetches from the production channel, https://registry.wavehouse.dev/chtypes/v2,
under the release key, caching under its own `v2` root (docs/guides/fetch-v1.md,
"Generation 2 after the lock").

One question, exactly: *if this row were inserted into this ClickHouse table on
this ClickHouse version, what would happen?* The answer comes from ClickHouse's
real C++ machinery, vendored per release into a shared library behind the frozen
C ABI. Nothing here reimplements a coercion rule, which is why the answers are
exact by construction rather than approximately right.

    import chtypes
    from chtypes import DocFlags, Format, Registry

    chtypes.setup(timezone="UTC")                # once, before the first open
    registry = Registry(autofetch=True)
    library = registry.for_version("26.8")       # a release line or an exact version
    ddl = b"CREATE TABLE t (ts DateTime, seq UInt8) ENGINE = Memory"
    with library.compile_table(ddl) as schema:
        body = b'{"ts":"2026-01-15 10:30:00","seq":256}\\n'
        batch = schema.rows(Format.JSON_EACH_ROW, body, doc_flags=DocFlags.ALL)

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

from ._abi2._vocab import (
    CODE_DECLINED,
    EXPORT_NONE,
    DeclinedLayer,
    DeclinedTier,
    DefaultKind,
    DefaultSupport,
    DiscoverQueryParam,
    DocFlags,
    FilterOutcome,
    Format,
    MergeReason,
    Outcome,
    Posture,
    Reason,
    RefusalSite,
    Source,
    Status,
    Verdict,
)
from ._input import (
    BytesIn,
    ColumnDefault,
    DeferDefaults,
    ParamEncoding,
    ServerProfile,
    Settings,
    VerdictBasis,
)
from ._setup import setup
from .errors import (
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_INCOMPATIBLE,
    CODE_ARTIFACT_MISSING,
    CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_REVOKED,
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_ARTIFACT_UNTRUSTED,
    CODE_CACHE_UNUSABLE,
    CODE_SOURCE_FORBIDDEN,
    CODE_SOURCE_INCOMPATIBLE,
    CODE_SOURCE_RETIRED,
    CODE_SOURCE_UNAUTHORIZED,
    CODE_SOURCE_UNREACHABLE,
    Advisory,
    ArtifactCorruptError,
    ArtifactError,
    ArtifactIncompatibleError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactRevokedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    CacheUnusableError,
    CallError,
    ChtypesError,
    InternalError,
    Revocation,
    SchemaError,
    SourceForbiddenError,
    SourceIncompatibleError,
    SourceRetiredError,
    SourceUnauthorizedError,
    SourceUnreachableError,
    UnsupportedError,
    UsageError,
)
from .library import Block, Filter, Library, Schema, Server, open_unverified
from .registry import FetchOptions, Registry, Resolved, cache_root, search_dirs
from .results import (
    AtMergeEntry,
    BatchResult,
    BuildInfo,
    Capabilities,
    Column,
    Computed,
    DeclinedSetting,
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
    SchemaReplicated,
    SchemaServer,
    Span,
    Transform,
    Value,
)

__all__ = [
    "CODE_ARTIFACT_CORRUPT",
    "CODE_ARTIFACT_INCOMPATIBLE",
    "CODE_ARTIFACT_MISSING",
    "CODE_ARTIFACT_PINNED",
    "CODE_ARTIFACT_REVOKED",
    "CODE_ARTIFACT_UNPUBLISHED",
    "CODE_ARTIFACT_UNTRUSTED",
    "CODE_CACHE_UNUSABLE",
    "CODE_SOURCE_FORBIDDEN",
    "CODE_SOURCE_INCOMPATIBLE",
    "CODE_SOURCE_RETIRED",
    "CODE_SOURCE_UNAUTHORIZED",
    "CODE_SOURCE_UNREACHABLE",
    "CODE_DECLINED",
    "EXPORT_NONE",
    "Advisory",
    "ArtifactCorruptError",
    "ArtifactError",
    "ArtifactIncompatibleError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactRevokedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "AtMergeEntry",
    "BatchResult",
    "Block",
    "BuildInfo",
    "BytesIn",
    "CacheUnusableError",
    "CallError",
    "Capabilities",
    "ChtypesError",
    "Column",
    "ColumnDefault",
    "Computed",
    "DeclinedLayer",
    "DeclinedSetting",
    "DeclinedTier",
    "DeferDefaults",
    "DefaultKind",
    "DefaultSupport",
    "DiscoverQueryParam",
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
    "MergeReason",
    "Outcome",
    "ParamEncoding",
    "Posture",
    "Reason",
    "Registry",
    "RefusalSite",
    "Resolved",
    "Revocation",
    "RowResult",
    "Schema",
    "SchemaDescription",
    "SchemaError",
    "SchemaReplicated",
    "SchemaServer",
    "Server",
    "ServerProfile",
    "Settings",
    "Source",
    "SourceForbiddenError",
    "SourceIncompatibleError",
    "SourceRetiredError",
    "SourceUnauthorizedError",
    "SourceUnreachableError",
    "Span",
    "Status",
    "Transform",
    "UnsupportedError",
    "UsageError",
    "Value",
    "Verdict",
    "VerdictBasis",
    "cache_root",
    "open_unverified",
    "search_dirs",
    "setup",
]

# Derived, never written twice. pyproject.toml is the single source of truth and
# the build reads it from there; asking importlib for it means this attribute
# cannot drift from the package it names.
try:
    __version__ = _package_version("chtypes")
except _PackageNotFound:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
