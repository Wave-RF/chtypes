/**
 * `@wavehouse/chtypes`: ClickHouse's own type system, per version, over the
 * C ABI. This is the ABI v2 binding: it speaks the locked ABI v2 description
 * (`docs/reference/abi-v2.md`) and fetches from the production channel,
 * https://registry.wavehouse.dev/chtypes/v2 under the release key
 * (`docs/guides/fetch-v1.md`, "Generation 2 after the lock"). `docs/reference/bindings-v1.md`
 * is the shape it keeps; every vocabulary gains rule r3's unknown(n) member,
 * told apart by its `*Known` function.
 *
 *   setup(...)                                    optional, once, before the first open
 *   Registry.open() --for(request)--> Library --compileTable(createTable)--> Schema --rows(...)--> BatchResult
 *                                       |                                      |
 *                              validateType(expr)               row(...) --> RowResult
 *
 * Nothing here reimplements a ClickHouse rule: every answer comes from
 * ClickHouse's own C++, vendored per release behind the C ABI, and every result
 * is a one-to-one decode of the document the library returned. `Registry.open`,
 * `for`, `installed` and `fetch` are asynchronous only because the fetch layer
 * is; `fetch` installs a build and opens nothing.
 */

export {
  type BuildInfo,
  type Capabilities,
  DeclinedLayer,
  declinedLayerKnown,
  DeclinedTier,
  declinedTierKnown,
  DefaultKind,
  defaultKindKnown,
  DefaultSupport,
  defaultSupportKnown,
  DiscoverQueryParam,
  discoverQueryParamKnown,
  CODE_DECLINED,
  DocFlags,
  EXPORT_NONE,
  FilterOutcome,
  filterOutcomeKnown,
  Format,
  formatChName,
  formatKnown,
  MergeReason,
  mergeReasonKnown,
  Outcome,
  outcomeKnown,
  Posture,
  postureKnown,
  Reason,
  reasonKnown,
  reasonLossy,
  RefusalSite,
  refusalSiteKnown,
  Source,
  sourceIsStored,
  sourceKnown,
  Status,
  statusKnown,
  statusName,
  type Unknown,
  Verdict,
  verdictAnswered,
  verdictKnown,
} from './abi2/index.js';
export {
  ArtifactCorruptError,
  ArtifactError,
  ArtifactIncompatibleError,
  CallError,
  type CallErrorFields,
  ChtypesError,
  InternalError,
  SchemaError,
  UnsupportedError,
  UsageError,
} from './abi2/index.js';
export {
  type AtMergeEntry,
  type BatchResult,
  type Column,
  type Computed,
  type DeclinedSetting,
  type DiscoveredColumn,
  type EngineCell,
  type Discovery,
  type ErrorCodeEntry,
  ErrorCodeTable,
  type FilterResult,
  type FilterRowError,
  type Framing,
  type Header,
  type RowResult,
  type SchemaDescription,
  type SchemaReplicated,
  type SchemaServer,
  type Span,
  type Transform,
  type Value,
} from './documents.js';
export { Library, openUnverified } from './library.js';
export {
  type Advisory,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactRevokedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  type CacheFault,
  CacheUnusableError,
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
  type ErrorCode,
  type Resolved,
  type Revocation,
  SourceForbiddenError,
  SourceIncompatibleError,
  SourceRetiredError,
  SourceUnauthorizedError,
  SourceUnreachableError,
} from './ocifetch/index.js';
export { cacheRoot, type FetchOptions, Registry, type RegistryOptions, searchDirs } from './registry.js';
export {
  Block,
  type CompileOptions,
  type EvalOptions,
  Filter,
  type FilterOptions,
  type RowOptions,
  type RowsOptions,
  Schema,
} from './schema.js';
export { Server } from './server.js';
export { DeferDefaults, ParamEncoding, VerdictBasis } from './settings.js';
export type { BytesIn, ColumnDefault, ServerProfile, SettingValue, Settings } from './settings.js';
export { setup, type SetupOptions } from './setup.js';
