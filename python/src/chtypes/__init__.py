"""chtypes — ClickHouse's own type system, per version, from Python.

One question, exactly: *if this row were inserted into this ClickHouse table on
this ClickHouse version, what would happen?* The answer comes from ClickHouse's
real C++ machinery (`DataTypeFactory`, `ISerialization`, `ReadHelpers`,
`evaluateMissingDefaults`, the TTL algorithms, `MergeTreeDataWriter::mergeBlock`)
vendored per release into a shared library behind the frozen `chs_*` C ABI.
Nothing here reimplements a coercion rule, which is why the answers are exact by
construction rather than approximately right.

    from chtypes import Format, Registry

    registry = Registry()                    # the search path: $CHTYPES_REGISTRY, the cache, …
    library = registry.for_version("25.8")   # a minor line or an exact patch
    with library.compile_ddl("ts DateTime, seq UInt8") as schema:
        batch = schema.rows(Format.JSON_EACH_ROW, b'{"ts":"2026-01-15 10:30:00","seq":256}\\n')

    batch.outcome                 # Outcome.ACCEPTED — the insert would succeed
    batch.rows[0].value("seq")    # Value(text='0', ...) — and store 0
    batch.transformed             # Transform(column='seq', input='256', stored='0',
                                  #           reason='overflow_wrap', row=0)

Three things a caller must not skip, each of which cost this repository
something to learn:

* **`transformed` is the product.** ClickHouse returns success for every one of
  those changes. Reading `outcome` alone tells a tenant their row was accepted
  and nothing about the value the table will hold.
* **A row accepted per row may not be stored per batch.** `BatchResult.transformed`
  folds in the storage layer's verdicts (`ttl_expired`, `ttl_column_expired`),
  and `engine_rows` — when present — is the stored truth, not `rows`.
* **`Outcome.UNSUPPORTED` is not a rejection.** It means a real server might well
  have accepted this and this build declines to guess. Treating it as either a
  rejection or an acceptance manufactures a wrong answer the product never gave.

The specification in `docs/reference/` is normative; `go/chtypes` (Go) is the reference
implementation. Where this binding and that package disagree, the package is
right.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError as _PackageNotFound
from importlib.metadata import version as _package_version

from ._native import ABI_REVISION
from ._rawjson import RawNumber, quote_bare_denormals
from .discover import (
    QUERY_CHANGED_SETTINGS,
    QUERY_SERVER_VERSION,
    QUERY_TABLE_COLUMNS,
    DiscoveredColumn,
    ServerProfile,
    parse_changed_settings_result,
    parse_columns_result,
    parse_version_result,
    reconstruct_ddl,
)
from .errors import (
    CODE_ARTIFACT_CORRUPT,
    CODE_ARTIFACT_MISSING,
    CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_ARTIFACT_UNTRUSTED,
    CODE_SOURCE_UNREACHABLE,
    CODE_UNSUPPORTED,
    ArtifactCorruptError,
    ArtifactError,
    ArtifactMissingError,
    ArtifactPinnedError,
    ArtifactUnpublishedError,
    ArtifactUntrustedError,
    ChtypesError,
    RegistryError,
    SchemaError,
    SourceUnreachableError,
    UnsignedArtifactWarning,
    UnsupportedError,
)
from .fetch import (
    ENV_AUTOFETCH,
    RELEASE_KEY_ID,
    RELEASE_PUBLIC_KEY,
    ensure,
    fetch_destination,
    fetch_lines,
    registry_search_path,
)
from .registry import (
    ENV_REGISTRY,
    Block,
    Filter,
    Library,
    Manifest,
    Registry,
    Schema,
    default_registry_dir,
    host_platform,
    minor_of,
    read_manifest,
    verify_library,
)
from .results import (
    COMPILE_DECLARED,
    DOC_ALL,
    DOC_DEFAULTS,
    DOC_TRANSFORMS,
    DOC_VALUES,
    EXPORT_NONE,
    LOSSLESS_REASONS,
    BatchResult,
    Column,
    Computed,
    DefaultKind,
    FilterOutcome,
    FilterResult,
    FilterRowError,
    Format,
    Outcome,
    Reason,
    RowResult,
    Span,
    Substitution,
    Transform,
    Value,
    Verdict,
)

__all__ = [
    "ABI_REVISION",
    "CODE_ARTIFACT_CORRUPT",
    "CODE_ARTIFACT_MISSING",
    "CODE_ARTIFACT_PINNED",
    "CODE_ARTIFACT_UNPUBLISHED",
    "CODE_ARTIFACT_UNTRUSTED",
    "CODE_SOURCE_UNREACHABLE",
    "CODE_UNSUPPORTED",
    "COMPILE_DECLARED",
    "DOC_ALL",
    "DOC_DEFAULTS",
    "DOC_TRANSFORMS",
    "DOC_VALUES",
    "ENV_AUTOFETCH",
    "ENV_REGISTRY",
    "default_registry_dir",
    "EXPORT_NONE",
    "RELEASE_KEY_ID",
    "RELEASE_PUBLIC_KEY",
    "LOSSLESS_REASONS",
    "QUERY_CHANGED_SETTINGS",
    "QUERY_SERVER_VERSION",
    "QUERY_TABLE_COLUMNS",
    "ArtifactCorruptError",
    "ArtifactError",
    "ArtifactMissingError",
    "ArtifactPinnedError",
    "ArtifactUnpublishedError",
    "ArtifactUntrustedError",
    "BatchResult",
    "Block",
    "ChtypesError",
    "Column",
    "Computed",
    "DefaultKind",
    "DiscoveredColumn",
    "Filter",
    "FilterOutcome",
    "FilterResult",
    "FilterRowError",
    "Format",
    "Library",
    "Manifest",
    "Outcome",
    "RawNumber",
    "Reason",
    "Registry",
    "RegistryError",
    "RowResult",
    "Schema",
    "SchemaError",
    "ServerProfile",
    "SourceUnreachableError",
    "Span",
    "Substitution",
    "Transform",
    "UnsignedArtifactWarning",
    "UnsupportedError",
    "Value",
    "Verdict",
    "ensure",
    "fetch_destination",
    "fetch_lines",
    "host_platform",
    "minor_of",
    "parse_changed_settings_result",
    "parse_columns_result",
    "parse_version_result",
    "quote_bare_denormals",
    "read_manifest",
    "reconstruct_ddl",
    "registry_search_path",
    "verify_library",
]

# Derived, never written twice. pyproject.toml is the single source of truth and
# the build reads it from there; asking importlib for it means this attribute
# cannot drift from the package it names. It did: 0.1.1 shipped reporting 0.1.0,
# because a second hand-maintained copy has no way to know the first one moved.
try:
    __version__ = _package_version("chtypes")
except _PackageNotFound:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
