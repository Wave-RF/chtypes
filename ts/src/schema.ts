/**
 * A compiled schema — one tenant table's column list, compiled inside one
 * version's library, plus the engine and TTL declarations that make `rows()`
 * answer with the storage layer's verdict as well as the type layer's.
 */

import { ChtypesError } from './errors.js';
import type { BlockHandle, FilterHandle, NativeLibrary, SchemaHandle } from './ffi.js';
import { DOC_ALL, EXPORT_NONE, type Format } from './format.js';
import { parseDocument } from './json.js';
import {
  batchResultOf,
  filterResultOf,
  rowResultOf,
  type BatchResult,
  type FilterResult,
  type RowResult,
} from './results.js';
import { encodeSettings, type Settings } from './settings.js';

/** One declared column, as ClickHouse canonicalized it. */
export interface ColumnInfo {
  readonly name: string;
  /** Canonical type — pass it through verbatim, never re-normalize whitespace. */
  readonly type: string;
  /** "" | "DEFAULT" | "MATERIALIZED" | "ALIAS" | "EPHEMERAL" */
  readonly defaultKind: string;
  readonly defaultExpr: string;
  /** True when the DEFAULT is a literal applicable without the interpreter. */
  readonly defaultIsLiteral: boolean;
}

/** Options for `Schema#setEngine` — the table's MergeTree-namespace settings. */
export interface EngineOptions {
  /**
   * The `SETTINGS` clause after the engine — `allow_nullable_key` and friends,
   * a namespace `DB::Settings` cannot carry (the C ABI contract §`chs_schema_engine`).
   * Absent or empty is structurally identical to the plain engine declaration:
   * `chs_schema_engine` takes "{}" either way. Names are validated by the
   * server's own `MergeTreeSettings` object: an unknown name throws the
   * server's own 115; a known name declared at a NON-default value is refused
   * (`unsupported`, naming it) — no MergeTree setting's behavior is modeled
   * yet, and silently ignoring a declared value would mean the declared
   * profile is not in force; a name declared AT its default is inert and
   * accepted.
   */
  readonly mergeTreeSettings?: Settings | undefined;
}

/**
 * Options for `Schema#rows` — the revision-3 export and document-flag
 * channels (the C ABI contract §Rows, for which include/chtypes.h is the
 * public authority). Whatever the
 * options, `rows()` is always ONE `chs_rows` call — never a second call,
 * never re-parsing.
 */
export interface RowsOptions {
  /**
   * A `Format` the artifact can SERIALIZE — this revision exactly
   * `Format.JSONCompactEachRow`. Any other value answers the whole call
   * `outcome: 'unsupported'` and processes nothing — loud, never silent.
   * Absent = no export: today's path, byte-identical to revision 2.
   */
  readonly exportFormat?: Format | undefined;
  /**
   * A bitmask of `DOC_VALUES | DOC_TRANSFORMS | DOC_DEFAULTS` selecting the
   * document groups; the verdict channel is always present and not a flag.
   * Defaults: `DOC_ALL` when no `exportFormat` is given (the full document —
   * plain `rows()` behavior), `0` (LEAN — verdicts only: `values`,
   * `transformed`, `substituted`, `computed` and `unknownFields` all come
   * back empty) when one is. An explicit value always wins; a bit outside
   * `DOC_ALL` is refused loudly by the library, never pre-validated here.
   */
  readonly docFlags?: number | undefined;
}

/**
 * Options for `Schema#compileFilter` — the revision-4 query-parameter
 * bindings.
 */
export interface CompileFilterOptions {
  /**
   * `{name:Type}` query-parameter bindings: name → value STRING, exactly as
   * the server's own parameter channels carry them. Each value is
   * deserialized by the DECLARED type's own reader and injected as a typed
   * literal AFTER SQL parsing, so a value is never SQL text and NEVER needs
   * hand-escaping — injection safety is by construction, not by escaping
   * (the C ABI contract §Filters, Query parameters). Do not render values into
   * the expression yourself.
   *
   * CHOOSE THE BRACE TYPE FOR THE VALUE'S DOMAIN: the declared type's own
   * reader WRAPS an out-of-domain integer — `{p:UInt8}` given `"256"` binds
   * `0` and matches every genuine zero (measured, uniform 24.8–26.7) —
   * while the same constant as a literal PROMOTES (`x = 256` is never
   * true). Size the brace type for the tenant-supplied domain or validate
   * the value first; malformed spellings refuse loudly (457 for
   * `"-1"`/`"+7"`/`"007"` as UInt8, 32 for `""`). A name bound twice at the
   * C boundary takes the LAST binding — the server's own `insert_or_assign`
   * rule (unreachable through this unique-keyed object, stated for
   * completeness).
   *
   * The compiled handle bakes the values in: identity is per
   * (schema, expr, params), so changing a value means compiling a new
   * `Filter`. A caller compiling filters from tenant-influenced values MUST
   * bound its cache (an LRU keyed on schema generation + expr + params-hash)
   * and its compile rate per principal — the key is attacker-influencable,
   * so an unbounded cache is a memory DoS and an unmetered compile path is a
   * CPU DoS.
   */
  readonly params?: Settings | undefined;
}

/**
 * A compiled schema — one tenant table's column list, compiled inside one
 * version's library. Obtained from `Library#compileDdl`; never constructed
 * directly. Release with `close()` (idempotent) or `using` / `Symbol.dispose`.
 *
 * Thread-safety: the C ABI forbids using one `chs_schema *` from two threads
 * at once; on a single JS thread every call here is synchronous, so ordinary
 * Node code satisfies that by construction. Do not share a `Schema` across
 * `worker_threads`.
 */
export class Schema {
  /**
   * The declared columns as ClickHouse canonicalized them, in declaration
   * order — flattened under `flatten_nested=1`, DEFAULT-rewritten types
   * (`x Int64 DEFAULT NULL` compiles as `Nullable(Int64)`), ALIAS types
   * inferred. A gateway detects EPHEMERAL columns here, at compile time
   * (`defaultKind === 'EPHEMERAL'`), not per row.
   */
  readonly columns: readonly ColumnInfo[];
  private handle: SchemaHandle | null;
  /**
   * Every open `Filter` compiled from this handle, so `close()` can free them
   * FIRST — the C layer does not refcount, and freeing the schema under a
   * live filter is use-after-free (the C ABI contract §Filters, handle lifetime).
   */
  private readonly filters = new Set<Filter>();
  /**
   * Every open `Block` parsed from this handle — the same non-owning rule,
   * the same free-before-schema order (the C ABI contract §Blocks).
   */
  private readonly blocks = new Set<Block>();

  /** @internal — obtained from `Library#compileDdl`. */
  constructor(
    private readonly native: NativeLibrary,
    handle: SchemaHandle,
    /** The column-declaration list this schema was compiled from, verbatim. */
    readonly ddl: string,
  ) {
    this.handle = handle;
    this.columns = native.columns(handle);
  }

  private live(): SchemaHandle {
    if (this.handle === null) throw new ChtypesError('chtypes: schema is closed');
    return this.handle;
  }

  /**
   * Declare the table engine, so `rows()` applies the engine's own insert-time
   * merge (`optimize_on_insert = 1`): a CollapsingMergeTree refusing an invalid
   * Sign with code 117 before anything is stored, a SummingMergeTree summing
   * equal keys and dropping all-zero rows, a ReplacingMergeTree deduplicating.
   *
   * `options.mergeTreeSettings` declares the table's MergeTree-namespace
   * settings — see `EngineOptions` for the error contract (unknown name ⇒ the
   * server's 115; non-default declared value ⇒ `unsupported`, never silently
   * ignored). Absent/empty is structurally the plain engine declaration:
   * `chs_schema_engine` takes "{}" either way.
   *
   * The two error classes here are the refusal/decline split and MUST be
   * handled as peers — `UnsupportedError` is deliberately NOT
   * `instanceof SchemaError` (docs/reference/bindings.md rule 12):
   *
   * @param engine - the engine expression, e.g. `"SummingMergeTree"`,
   *   `"CollapsingMergeTree(sign)"`.
   * @param orderBy - the sorting key, e.g. `"(day, key)"`.
   * @param options - the MergeTree-namespace `SETTINGS` clause, if any.
   * @throws {SchemaError} when the SERVER refused (positive code): this DDL
   *   can never exist and the tenant has to be told. Today that is 115, an
   *   unknown MergeTree setting name, with the server's own message verbatim.
   * @throws {UnsupportedError} when this LIBRARY declined: an engine or
   *   sorting key this build does not model, a known MergeTree setting
   *   declared at a non-default value, or a guarded exception. A real server
   *   might well have accepted it — validate cautiously, fall back to the
   *   server, and never present the decline as a rejection.
   */
  setEngine(engine: string, orderBy: string, options?: EngineOptions): void {
    this.native.schemaEngine(this.live(), engine, orderBy, encodeSettings(options?.mergeTreeSettings));
  }

  /**
   * Declare the table's rows TTL (`ts + INTERVAL 30 DAY`). An expired row is
   * reported NOT STORED through the batch's `transformed` list.
   *
   * Column-level TTLs need no call: they are part of the declaration list.
   *
   * @param ttl - the rows-TTL expression, e.g. `"ts + INTERVAL 30 DAY"`. It is
   *   validated under the handle's declared compile profile when one exists.
   * @throws {UnsupportedError} for a TTL form this build refuses rather than
   *   guesses: WHERE / GROUP BY TTLs, TO DISK/VOLUME moves, RECOMPRESS, any
   *   clock-reading TTL expression, and guarded exceptions. Fall back to the
   *   server; do not report a tenant error.
   */
  setTtl(ttl: string): void {
    this.native.schemaTtl(this.live(), ttl);
  }

  /**
   * Validate and coerce ONE row body, answering exactly as this ClickHouse
   * version's insert path would.
   *
   * There is no exception for a bad row: rejection, poisoning and declines all
   * arrive as the `RowResult`'s `outcome` (see `Outcome` for the taxonomy and
   * the caller's obligations per arm).
   *
   * @param format - the wire format code (`Format`). Binary-format support
   *   depends on the loaded artifact — probe, don't assume.
   * @param raw - the row's bytes, exactly as they would arrive in an INSERT
   *   body. Always bytes, never a JS string: binary formats contain NUL bytes
   *   and text rows can carry invalid UTF-8 on purpose.
   * @param settings - per-call settings; they win over the compile profile and
   *   the library defaults (except a type gate the profile declared, which the
   *   handle has already settled, as a real server's CREATE does).
   * @returns the `RowResult` — verdict, stored values, and every silent change.
   * @throws {ChtypesError} when the schema is closed, or a settings value is a
   *   JS `number`.
   * @throws {UnsupportedError} when the artifact predates `chs_row`.
   */
  row(format: Format, raw: Uint8Array, settings?: Settings): RowResult {
    const doc = this.native.row(this.live(), format, raw, encodeSettings(settings));
    return rowResultOf(parseDocument(doc));
  }

  /**
   * Validate and coerce a whole request body, which may hold many rows.
   *
   * This is not `row()` in a loop and must never be implemented as one: row
   * separation is format-specific (a quoted CSV field can contain a newline),
   * `input_format_allow_errors_num` / `_ratio` decide whether a bad row is
   * skipped or aborts the batch, and one batch is one clock instant for the
   * volatile-DEFAULT guarantee.
   *
   * @param format - the wire format code (`Format`).
   * @param body - the whole request body as bytes (never a JS string).
   * @param settings - per-call settings; same precedence as `row`.
   * @returns the `BatchResult`. When `engineRows` is present it — not `rows` —
   *   is the stored truth, and batch-level storage transforms (`ttl_expired`,
   *   `ttl_column_expired`) are folded into `transformed`.
   * With `options.exportFormat` the same ONE call also serializes the batch's
   * accepted rows through the vendored writer: `payload` carries the bytes
   * (zero-length = emitted-empty, an accepted batch with zero accepted rows;
   * `undefined` + `exportDeclined` = withheld, with the reason), and `spans`
   * is index-aligned with `rows` — `payload.subarray(s.off, s.off + s.len)`
   * IS row i's line. With `options.docFlags` the per-row documents are
   * thinned to the selected groups; the verdict channel is never thinned.
   * See `RowsOptions` for the defaults and `BatchResult` for the three
   * payload states.
   *
   * @param format - the wire format code (`Format`).
   * @param body - the whole request body as bytes (never a JS string).
   * @param settings - per-call settings; same precedence as `row`.
   * @param options - the revision-3 export/doc-flag channels; absent =
   *   today's full document, byte-identical to revision 2.
   * @returns the `BatchResult`. When `engineRows` is present it — not `rows` —
   *   is the stored truth, and batch-level storage transforms (`ttl_expired`,
   *   `ttl_column_expired`) are folded into `transformed`.
   * @throws {ChtypesError} when the schema is closed, a settings value is a JS
   *   `number`, or the artifact predates `chs_rows` (a mandatory symbol).
   */
  rows(format: Format, body: Uint8Array, settings?: Settings, options?: RowsOptions): BatchResult {
    const exportFormat = options?.exportFormat ?? EXPORT_NONE;
    const docFlags = options?.docFlags ?? (options?.exportFormat === undefined ? DOC_ALL : 0);
    const { doc, payload } = this.native.rows(
      this.live(),
      format,
      body,
      encodeSettings(settings),
      exportFormat,
      docFlags,
    );
    return batchResultOf(parseDocument(doc), payload);
  }

  /**
   * Compile one boolean SQL expression over this schema's PHYSICAL columns
   * (ordinary + MATERIALIZED; naming an ALIAS/EPHEMERAL column fails with
   * ClickHouse's own UNKNOWN_IDENTIFIER, exactly where a real CREATE fails) —
   * the same TreeRewriter + ExpressionAnalyzer pipeline the CONSTRAINT CHECK
   * path runs, so comparison semantics are WHERE-side by construction:
   * `x = 256` over UInt8 promotes (false for every row), it never wraps
   * (the C ABI contract §Filters).
   *
   * The expression may contain `{name:Type}` query parameters, bound with
   * `options.params` (revision 4) — substitution is the server's own
   * `ReplaceQueryParameterVisitor`, run before analysis, exactly where a
   * real server runs it; see `CompileFilterOptions` for the injection-safety
   * and cache-discipline contract. Close the filter when done (`close()` /
   * `Symbol.dispose`); `schema.close()` also closes every open filter FIRST,
   * so no caller ordering can free the schema under a live filter.
   *
   * ENFORCEMENT GATE: nothing may enforce read-side security on this surface
   * until the WHERE-truth rig gates green (zero over-admit, zero over-hide);
   * until then it is a shadow/replay surface.
   *
   * @param expr - one boolean expression, e.g. `"x = 256"` or
   *   `"tenant = {t:String}"`.
   * @param options - the `{name:Type}` bindings, if any.
   * @returns the compiled `Filter`.
   * @throws {SchemaError} when ClickHouse itself refuses the expression —
   *   unknown identifier (47), unknown function, an analyzer-raised
   *   NO_COMMON_TYPE — and, since revision 4, the server's own parameter
   *   refusals: an UNBOUND `{name:Type}` is **456** UNKNOWN_QUERY_PARAMETER
   *   ("Substitution `name` is not set"), a value the declared type cannot
   *   parse completely is **457** BAD_QUERY_PARAMETER — the server's own
   *   code and message, verbatim. A bound name the expression never uses is
   *   ignored, as a live server ignores an unused `param_*`.
   * @throws {UnsupportedError} when this build declines: a non-deterministic
   *   expression (clock reads — `now() > ts` —, `rand()`, server-constants;
   *   the scan runs AFTER substitution, so a value can never smuggle one
   *   in), or an artifact that predates the filter trio.
   */
  compileFilter(expr: string, options?: CompileFilterOptions): Filter {
    const handle = this.native.filterCompile(this.live(), expr, encodeSettings(options?.params));
    const filter = new Filter(this.native, this, handle, expr);
    this.filters.add(filter);
    return filter;
  }

  /** @internal — `Filter#close` deregisters itself here. */
  forgetFilter(filter: Filter): void {
    this.filters.delete(filter);
  }

  /**
   * Parse a body ONCE into a `Block` (`chs_block_parse`) — the parse half of
   * `Filter#rows`, exported so K filters can evaluate one event with no
   * re-parse (`Filter#eval`; the C ABI contract §Blocks). Same formats and
   * settings contract as `rows` (`settings` is the PARSE-side map: format
   * settings, clock keys; evaluation takes none). Volatile DEFAULTs resolve
   * against THIS call's clock instant, so
   * `filter.eval(schema.parseBlock(...))` ≡ `filter.rows(...)` exactly when
   * the clock is pinned (`chtypes_now_epoch_nanos`) or the schema has no
   * volatile DEFAULT.
   *
   * Per-row parse failures do NOT throw — they are recorded IN the block and
   * answer `'decline'` from every filter, with the recorded error. Release
   * with `close()` / `Symbol.dispose`; `schema.close()` closes open blocks
   * FIRST, the C-required order.
   *
   * @param format - the wire format code (`Format`).
   * @param body - the rows as bytes (never a JS string).
   * @param settings - parse-side settings; same precedence as `Schema#rows`.
   * @returns the parsed `Block`.
   * @throws {SchemaError} on a call-level failure — an unknown setting's
   *   115, an unsplittable body, a binary decode fault, the deferred
   *   JSONEachRow framing verdict: a malformed body yields no block and no
   *   partial answers.
   * @throws {UnsupportedError} when this build declines the call, or the
   *   artifact predates the block twin.
   */
  parseBlock(format: Format, body: Uint8Array, settings?: Settings): Block {
    const handle = this.native.blockParse(this.live(), format, body, encodeSettings(settings));
    const block = new Block(this.native, this, handle);
    this.blocks.add(block);
    return block;
  }

  /** @internal — `Block#close` deregisters itself here. */
  forgetBlock(block: Block): void {
    this.blocks.delete(block);
  }

  /**
   * Release the native schema. Idempotent. After it, `row` / `rows` /
   * `setEngine` / `setTtl` throw `ChtypesError` ("schema is closed").
   * Any `Filter` or `Block` still open on this schema is closed FIRST, in
   * the same call — the handles-before-schema free order the C layer
   * requires, enforced here so no dispose ordering can get it backwards.
   */
  close(): void {
    for (const filter of this.filters) filter.close();
    this.filters.clear();
    for (const block of this.blocks) block.close();
    this.blocks.clear();
    if (this.handle === null) return;
    this.native.schemaFree(this.handle);
    this.handle = null;
  }

  /** `using schema = lib.compileDdl(...)` releases it at scope exit. */
  [Symbol.dispose](): void {
    this.close();
  }
}

/**
 * One boolean SQL expression compiled against a `Schema`'s columns
 * (`chs_filter_compile`). Obtained from `Schema#compileFilter`; never
 * constructed directly.
 *
 * LIFETIME: a filter REFERENCES its schema handle — the C layer does not copy
 * and does not refcount (the C ABI contract §Filters). This binding enforces the
 * free order structurally, both ways: the `Filter` holds its `Schema` (so the
 * schema stays reachable), and `Schema#close` closes every open filter before
 * freeing the schema. `close()` is idempotent, and `using` / `Symbol.dispose`
 * work on both objects in any nesting — the schema's dispose runs the
 * filter's first when the caller forgot. A filter compiled from a schema
 * answers for THAT handle: recompile filters when the schema is recompiled.
 *
 * THREADS: the header's rule, verbatim — one `chs_filter` "must not be used
 * from two threads at once, and a chs_filter call is ALSO a use of its schema
 * handle" (two filters over ONE schema must not run concurrently either). On
 * a single JS thread every call here is synchronous, so ordinary Node code
 * satisfies both by construction (docs/reference/bindings.md §Concurrency); do not
 * share a `Filter` — or its `Schema` — across `worker_threads`.
 *
 * ENFORCEMENT GATE: `'error'` and `'decline'` verdicts are NOT answers — a
 * caller enforcing visibility MUST fail closed on both — and NO caller may
 * enforce read-side security on this surface until the WHERE-truth rig gates
 * green; until then it is a shadow/replay surface (the C ABI contract §Filters).
 */
export class Filter {
  private handle: FilterHandle | null;

  /** @internal — obtained from `Schema#compileFilter`. */
  constructor(
    private readonly native: NativeLibrary,
    private readonly schema: Schema,
    handle: FilterHandle,
    /** The expression text as compiled, for logging and cache keys. */
    readonly expr: string,
  ) {
    this.handle = handle;
  }

  /**
   * Evaluate the filter over a body of rows (`chs_filter_rows`) — the same
   * formats and settings contract as `Schema#rows`, ONE C call. Rows are
   * evaluated INDEPENDENTLY (there is no INSERT to abort):
   * `input_format_allow_errors_*` does not apply, a bad text row declines
   * (`'decline'`) and the tail resyncs so verdict indexes keep matching input
   * rows, and volatile DEFAULTs resolve against one clock instant per call.
   *
   * @param format - the wire format code (`Format`).
   * @param body - the rows as bytes (never a JS string).
   * @param settings - per-call settings; same precedence as `Schema#rows`.
   * @returns the `FilterResult` — the call-level outcome, and one verdict per
   *   row when it is `'ok'`.
   * @throws {ChtypesError} when the filter is closed, or a settings value is
   *   a JS `number`.
   */
  rows(format: Format, body: Uint8Array, settings?: Settings): FilterResult {
    if (this.handle === null) throw new ChtypesError('chtypes: filter is closed');
    const doc = this.native.filterRows(this.handle, format, body, encodeSettings(settings));
    return filterResultOf(parseDocument(doc));
  }

  /**
   * Evaluate this filter over an already-parsed `Block` (`chs_filter_eval`)
   * — the SAME result document `rows` returns: same `FilterResult` fields,
   * same verdicts, same `errors` rule (a row the parse recorded as
   * unparseable answers `'decline'` with the recorded error). Evaluation is
   * a pure function of (filter, block): no settings, and the block is
   * neither consumed nor mutated, so one block can be evaluated by K filters
   * sequentially with no re-parse — the live-SSE call shape.
   *
   * Filter and block MUST come from the SAME schema: a mismatched pair
   * answers a REJECTED result (code 1002) — the C layer's loud refusal,
   * never undefined behavior. A pair from two different libraries throws
   * `ChtypesError`: no handle ever crosses a dlopen'd image boundary.
   *
   * @param block - a `Block` from `Schema#parseBlock`.
   * @returns the `FilterResult`, exactly as `rows` would answer it.
   * @throws {ChtypesError} when the filter or block is closed, or they come
   *   from two different libraries.
   */
  eval(block: Block): FilterResult {
    if (this.handle === null) throw new ChtypesError('chtypes: filter is closed');
    if (block.nativeLib !== this.native) {
      throw new ChtypesError('chtypes: filter and block come from different libraries');
    }
    const doc = this.native.filterEval(this.handle, block.liveHandle());
    return filterResultOf(parseDocument(doc));
  }

  /**
   * Release the native filter (`chs_filter_free`). Idempotent, and also
   * performed by the schema's own `close()` — filters first, then the schema,
   * the C-required order.
   */
  close(): void {
    if (this.handle === null) return;
    this.native.filterFree(this.handle);
    this.handle = null;
    this.schema.forgetFilter(this);
  }

  /** `using filter = schema.compileFilter(...)` releases it at scope exit. */
  [Symbol.dispose](): void {
    this.close();
  }
}

/**
 * One body, parsed ONCE under one schema handle and one clock instant
 * (`chs_block_parse`). Obtained from `Schema#parseBlock`; evaluated by
 * `Filter#eval`. The parse-once/eval-many twin of `Filter#rows`
 * (the C ABI contract §Blocks): the live-SSE hot path is K filters × 1 event, and
 * the block sheds the re-parse.
 *
 * LIFETIME: a block REFERENCES its schema handle exactly as a filter does —
 * the C layer does not copy and does not refcount. This binding enforces the
 * free order structurally, both ways: the `Block` holds its `Schema` (so the
 * schema stays reachable), and `Schema#close` closes every open block before
 * freeing the schema — `using` / `Symbol.dispose` work on all three objects
 * in any nesting, and the schema's dispose runs the block's first when the
 * caller forgot. A block may be evaluated by MANY filters, sequentially;
 * evaluation does not consume or mutate it. Recompile blocks when the schema
 * is recompiled.
 *
 * THREADS: one block must not be used from two threads at once, and an eval
 * is a use of BOTH handles. On a single JS thread every call here is
 * synchronous, so ordinary Node code satisfies both by construction; do not
 * share a `Block` — or its `Schema` — across `worker_threads`.
 */
export class Block {
  private handle: BlockHandle | null;

  /** @internal — obtained from `Schema#parseBlock`. */
  constructor(
    private readonly native: NativeLibrary,
    private readonly schema: Schema,
    handle: BlockHandle,
  ) {
    this.handle = handle;
  }

  /** @internal — the loaded library this block's handle belongs to. */
  get nativeLib(): NativeLibrary {
    return this.native;
  }

  /** @internal — the live handle, for `Filter#eval`. */
  liveHandle(): BlockHandle {
    if (this.handle === null) throw new ChtypesError('chtypes: block is closed');
    return this.handle;
  }

  /**
   * Release the native block (`chs_block_free`). Idempotent, and also
   * performed by the schema's own `close()` — blocks first, then the schema,
   * the C-required order.
   */
  close(): void {
    if (this.handle === null) return;
    this.native.blockFree(this.handle);
    this.handle = null;
    this.schema.forgetBlock(this);
  }

  /** `using block = schema.parseBlock(...)` releases it at scope exit. */
  [Symbol.dispose](): void {
    this.close();
  }
}
