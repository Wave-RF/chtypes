/**
 * `@wavehouse/chtypes`: ClickHouse's own type system, per version, over the
 * ABI v1 C layer (`docs/reference/bindings-v1.md` is the contract).
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
  DefaultKind,
  DiscoverQueryParam,
  DocFlags,
  EXPORT_NONE,
  FilterOutcome,
  Format,
  formatChName,
  Outcome,
  Reason,
  reasonLossy,
  Source,
  sourceIsStored,
  Status,
  Verdict,
  verdictAnswered,
} from './abi1/index.js';
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
} from './abi1/index.js';
export {
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
  SourceUnauthorizedError,
  SourceUnreachableError,
} from './ocifetch/index.js';
export { type FetchOptions, Registry, type RegistryOptions } from './registry.js';
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
export type { BytesIn, Settings } from './settings.js';
export { setup, type SetupOptions } from './setup.js';
