/**
 * `@wavehouse/chtypes` — ClickHouse's own type system, per version.
 *
 *   Registry --for(version)--> Library --compileDdl(ddl)--> Schema --rows(...)--> BatchResult
 *                                 |                            |
 *                          validateType(expr)          setEngine / setTtl    row(...) --> RowResult
 *
 * Nothing here reimplements a coercion rule: every answer comes from ClickHouse's
 * own C++ (`DataTypeFactory`, `ISerialization`, `ReadHelpers`,
 * `evaluateMissingDefaults`, the TTL algorithms, `MergeTreeDataWriter::mergeBlock`)
 * vendored per release behind the frozen `chs_*` C ABI. The one derived answer is
 * `transformed`, and it is the reason the product exists.
 */

export {
  type DiscoveredColumn,
  parseChangedSettingsResult,
  parseColumnsResult,
  parseVersionResult,
  QUERY_CHANGED_SETTINGS,
  QUERY_SERVER_VERSION,
  QUERY_TABLE_COLUMNS,
  type ServerProfile,
} from './discover.js';
export { type ErrorCodeEntry, ErrorCodeTable } from './error-codes.js';
export {
  ABI_REVISION,
  ArtifactCorruptError,
  ArtifactError,
  type ArtifactErrorCode,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  artifactMissingMessage,
  ChtypesError,
  CODE_ARTIFACT_CORRUPT,
  CODE_ARTIFACT_MISSING,
  CODE_ARTIFACT_PINNED,
  CODE_ARTIFACT_UNPUBLISHED,
  CODE_ARTIFACT_UNTRUSTED,
  CODE_SOURCE_UNREACHABLE,
  CODE_UNSUPPORTED,
  FETCH_COMMAND,
  FetchError,
  RegistryError,
  SchemaError,
  SourceUnreachableError,
  UnsupportedError,
} from './errors.js';
export {
  compareVersions,
  DEFAULT_ARTIFACTS_URL,
  DEFAULT_LOCK_FILE,
  DEFAULT_RELEASE_TAG,
  type EnsureOptions,
  type EnsureResult,
  ensure,
  ensureAll,
  type FetchEvent,
  type IndexArtifact,
  type InstalledArtifact,
  keyId,
  type ListResult,
  LOCK_SCHEMA,
  type LockEntry,
  type LockFile,
  listArtifacts,
  parseSignatureFile,
  parseVersionSpelling,
  RELEASE_KEY_ID,
  RELEASE_PUBLIC_KEYS,
  type ReleaseIndex,
  readLock,
  resolvePlatform,
  selectAll,
  selectArtifact,
  sha256File,
  trustedKeys,
  type VersionRequest,
  verifyEd25519,
  verifyInstalled,
} from './fetch.js';
export { nativeStats } from './ffi.js';
export {
  DOC_ALL,
  DOC_DEFAULTS,
  DOC_TRANSFORMS,
  DOC_VALUES,
  EXPORT_NONE,
  Format,
  formatName,
} from './format.js';
export {
  isValidUtf8,
  Json,
  type JsonKind,
  parseDocument,
  parseJsonValue,
  rawBytes,
  rawText,
  repairBareDenormals,
} from './json.js';
export { CompileMode, type CompileOptions, Library, minorOf } from './library.js';
export {
  cacheRegistryDir,
  ENV_AUTOFETCH,
  ENV_REGISTRY,
  fetchDestination,
  hostPlatform,
  isPlatformKey,
  registrySearchPath,
  systemRegistryDirs,
} from './paths.js';
export {
  compareMinor,
  defaultRegistryDir,
  looksLikeRegistry,
  type Manifest,
  Registry,
  type RegistryOptions,
  type Resolution,
  resolveRegistryDir,
} from './registry.js';
export {
  type BatchResult,
  type ColumnDoc,
  type Computed,
  DefaultKind,
  FilterOutcome,
  type FilterResult,
  type FilterRowError,
  isAnswer,
  Outcome,
  type RowResult,
  Source,
  type Span,
  type Substitution,
  type Transform,
  type Value,
  Verdict,
} from './results.js';
export {
  Block,
  type ColumnInfo,
  type CompileFilterOptions,
  type EngineOptions,
  Filter,
  type RowOptions,
  type RowsOptions,
  Schema,
} from './schema.js';
export { encodeSettings, type Settings, type SettingValue } from './settings.js';
export { type ExtractedEntry, extractTarGz } from './tar.js';
export { isLossyReason, Reason } from './transform.js';
