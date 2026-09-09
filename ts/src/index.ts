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
  parseChangedSettingsResult,
  parseColumnsResult,
  parseVersionResult,
  QUERY_CHANGED_SETTINGS,
  QUERY_SERVER_VERSION,
  QUERY_TABLE_COLUMNS,
  reconstructDdl,
  type DiscoveredColumn,
  type ServerProfile,
} from './discover.js';
export {
  ABI_REVISION,
  ArtifactCorruptError,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  artifactMissingMessage,
  ChtypesError,
  CODE_UNSUPPORTED,
  FETCH_COMMAND,
  FetchError,
  RegistryError,
  SchemaError,
  SourceUnreachableError,
  UnsupportedError,
  type ArtifactErrorCode,
} from './errors.js';
export {
  compareVersions,
  DEFAULT_ARTIFACTS_URL,
  DEFAULT_RELEASE_TAG,
  ensure,
  ensureAll,
  keyId,
  listArtifacts,
  LOCK_SCHEMA,
  parseSignatureFile,
  parseVersionSpelling,
  readLock,
  RELEASE_PUBLIC_KEYS,
  resolvePlatform,
  selectAll,
  selectArtifact,
  sha256File,
  trustedKeys,
  verifyEd25519,
  verifyInstalled,
  type EnsureOptions,
  type EnsureResult,
  type FetchEvent,
  type IndexArtifact,
  type InstalledArtifact,
  type ListResult,
  type LockEntry,
  type LockFile,
  type ReleaseIndex,
  type VersionRequest,
} from './fetch.js';
export {
  cacheRegistryDir,
  fetchDestination,
  hostPlatform,
  isPlatformKey,
  registrySearchPath,
  systemRegistryDirs,
} from './paths.js';
export { extractTarGz, type ExtractedEntry } from './tar.js';
export {
  DOC_ALL,
  DOC_DEFAULTS,
  DOC_TRANSFORMS,
  DOC_VALUES,
  EXPORT_NONE,
  Format,
  formatName,
} from './format.js';
export { CompileMode, Library, minorOf, type CompileOptions } from './library.js';
export { nativeStats } from './ffi.js';
export {
  compareMinor,
  defaultRegistryDir,
  looksLikeRegistry,
  Registry,
  resolveRegistryDir,
  type Manifest,
  type RegistryOptions,
} from './registry.js';
export {
  isAnswer,
  type BatchResult,
  type ColumnDoc,
  type Computed,
  type FilterOutcome,
  type FilterResult,
  type FilterRowError,
  type Outcome,
  type RowResult,
  type Span,
  type Substitution,
  type Transform,
  type Value,
  type Verdict,
} from './results.js';
export {
  Block,
  Filter,
  Schema,
  type ColumnInfo,
  type CompileFilterOptions,
  type EngineOptions,
  type RowsOptions,
} from './schema.js';
export { encodeSettings, type Settings, type SettingValue } from './settings.js';
export { isLossyReason, Reason } from './transform.js';
export {
  isValidUtf8,
  Json,
  parseDocument,
  parseJsonValue,
  rawBytes,
  rawText,
  repairBareDenormals,
  type JsonKind,
} from './json.js';
