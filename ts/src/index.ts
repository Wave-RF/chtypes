/**
 * `@wavehouse/chtypes`: ClickHouse's own type system, per version, over the
 * C ABI. This is the 2.0.0-dev binding: it speaks ABI v2, whose description is
 * UNSTABLE (`spec/abi-v2/docs.md`), fetches only from the staging dev channel,
 * and is not for production (public issue #511). `docs/reference/bindings-v1.md`
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
 * `for` and `installed` are asynchronous only because the fetch layer is.
 */

export {
  type BuildInfo,
  type Capabilities,
  type BatchOutcome,
  batchOutcomeKnown,
  DefaultKind,
  defaultKindKnown,
  DiscoverQueryParam,
  discoverQueryParamKnown,
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
  Reason,
  reasonKnown,
  reasonLossy,
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
  isChtypesError,
  LoaderCorruptError,
  type LoaderErrorFields,
  SchemaError,
  UnsupportedError,
  UsageError,
} from './abi2/index.js';
export {
  type AtMergeEntry,
  type BatchResult,
  type Column,
  type Computed,
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
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  type CacheFault,
  CacheUnusableError,
  type Resolved,
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
export type { BytesIn, ServerProfile, Settings } from './settings.js';
export { setup, type SetupOptions } from './setup.js';
