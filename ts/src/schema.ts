/**
 * `Schema`, `Filter` and `Block`: the compiled objects of `docs/reference/bindings-v1.md` §2.
 *
 * Every public call makes exactly one ABI call over the generated, typed
 * layer, then decodes the document it returned (`./documents.ts`) and
 * computes nothing. The per-call zone is the `session_timezone` key of the
 * call's settings and nothing more (`./settings.ts`).
 *
 * Handles. Each object holds the generated layer's handle wrapper, whose
 * `ptr` raises a `UsageError` naming the object once it is closed, before any
 * C call. A filter or a block holds a counted reference to its schema inside
 * the library, so a binding object neither holds its parent alive nor orders
 * its frees: closing a schema while its filters are in use is legal, and
 * `close` is idempotent. A `FinalizationRegistry` per handle class frees what
 * the caller abandons. TypeScript runs every call synchronously on one
 * thread, so the close guard of the other bindings reduces to this check: no
 * call is ever in flight while `close` runs.
 */

import type { BlockHandle, Calls, FilterHandle, SchemaHandle } from './abi2/index.js';
import { DocFlags, EXPORT_NONE, type Format } from './abi2/index.js';
import {
  type BatchResult,
  decodeBatch,
  decodeFilterResult,
  decodeRow,
  decodeSchemaDescription,
  type FilterResult,
  type RowResult,
  type SchemaDescription,
} from './documents.js';
import { type BytesIn, bytesIn, encodeColumns, encodeParams, encodeSettings, type Settings } from './settings.js';

/** Options of `Library.compileTable`: the profile settings a schema compiles under, and the zone that profile defaults to. */
export interface CompileOptions {
  readonly settings?: Settings | undefined;
  /** In a compile profile it is a default for later calls on the schema; a compiled type always takes the image zone, never the profile's. */
  readonly sessionTimezone?: string | undefined;
}

/** Options of `Schema.row` and `Schema.parseBlock`. */
export interface RowOptions {
  readonly settings?: Settings | undefined;
  /** The per-call zone: written into the settings as `session_timezone`, verbatim. */
  readonly sessionTimezone?: string | undefined;
  /** The INSERT column list. */
  readonly columns?: readonly BytesIn[] | undefined;
}

/** Options of `Schema.rows`. */
export interface RowsOptions extends RowOptions {
  /** A compiled filter evaluated over the body in the same call. */
  readonly rowFilter?: Filter | undefined;
  /** Ask for an export in this format; absent means none. */
  readonly exportFormat?: Format | undefined;
  /** The document groups to carry; absent means all of them. */
  readonly docFlags?: number | undefined;
}

/** Options of `Schema.compileFilter`. */
export interface FilterOptions {
  /** Query parameters the expression names (`{p:Type}`). */
  readonly params?: Settings | undefined;
  /** The filter's own zone and profile: fixed when it is compiled, and every evaluation of it runs its WHERE under them. */
  readonly settings?: Settings | undefined;
  readonly sessionTimezone?: string | undefined;
}

/** Options of `Filter.rows`. The settings are PARSE settings only: they decide how the body is parsed, never the filter's WHERE. */
export interface EvalOptions {
  readonly settings?: Settings | undefined;
  readonly sessionTimezone?: string | undefined;
}

function bodyIn(body: Uint8Array): Uint8Array {
  if (typeof body === 'string') throw new TypeError('chtypes: a body is bytes (a Uint8Array); it never accepts the text type');
  return body;
}

const filterHandles = new WeakMap<Filter, FilterHandle>();

/** A compiled `WHERE`-style expression bound to a schema. It holds its own zone, fixed at compile. */
export class Filter {
  readonly #calls: Calls;
  readonly #h: FilterHandle;

  /** Not for callers: use `Schema.compileFilter`. */
  constructor(calls: Calls, handle: FilterHandle) {
    this.#calls = calls;
    this.#h = handle;
    filterHandles.set(this, handle);
  }

  /** Evaluate over a body. */
  rows(format: Format, body: Uint8Array, options: EvalOptions = {}): FilterResult {
    return decodeFilterResult(
      this.#calls.filterEvalBody(this.#h, format, bodyIn(body), encodeSettings(options.settings, options.sessionTimezone)),
    );
  }

  /** Evaluate over a parsed block. The filter brings its zone and the block brought its parse zone, so there are no settings here. */
  eval(block: Block): FilterResult {
    return decodeFilterResult(this.#calls.filterEvalBlock(this.#h, blockHandles.get(block) as BlockHandle));
  }

  /** Release this filter; idempotent. */
  close(): void {
    this.#h.close();
  }

  [Symbol.dispose](): void {
    this.close();
  }
}

const blockHandles = new WeakMap<Block, BlockHandle>();

/** A body parsed once, evaluated by any number of filters. */
export class Block {
  readonly #h: BlockHandle;

  /** Not for callers: use `Schema.parseBlock`. */
  constructor(handle: BlockHandle) {
    this.#h = handle;
    blockHandles.set(this, handle);
  }

  /** Release this block; idempotent. */
  close(): void {
    this.#h.close();
  }

  [Symbol.dispose](): void {
    this.close();
  }
}

/** A compiled `CREATE TABLE`: describe it, preview rows and bodies against it, compile filters and parse blocks over it. */
export class Schema {
  readonly #calls: Calls;
  readonly #h: SchemaHandle;

  /** Not for callers: use `Library.compileTable`. */
  constructor(calls: Calls, handle: SchemaHandle) {
    this.#calls = calls;
    this.#h = handle;
  }

  /** The columns, in declared order. */
  describe(): SchemaDescription {
    return decodeSchemaDescription(this.#calls.schemaDescribe(this.#h));
  }

  /** One row. */
  row(format: Format, body: Uint8Array, options: RowOptions = {}): RowResult {
    return decodeRow(
      this.#calls.previewRow(
        this.#h,
        format,
        bodyIn(body),
        encodeSettings(options.settings, options.sessionTimezone),
        encodeColumns(options.columns),
      ),
    );
  }

  /** A whole body. */
  rows(format: Format, body: Uint8Array, options: RowsOptions = {}): BatchResult {
    const filter = options.rowFilter === undefined ? null : (filterHandles.get(options.rowFilter) as FilterHandle);
    const exporting = options.exportFormat !== undefined;
    const out = this.#calls.previewBatch(
      this.#h,
      format,
      bodyIn(body),
      encodeSettings(options.settings, options.sessionTimezone),
      encodeColumns(options.columns),
      filter,
      exporting ? (options.exportFormat as number) : EXPORT_NONE,
      options.docFlags ?? DocFlags.All,
    );
    return decodeBatch(out.out, exporting ? out.outExport : undefined);
  }

  /** Compile a filter. Its zone is fixed here. */
  compileFilter(expr: BytesIn, options: FilterOptions = {}): Filter {
    return new Filter(
      this.#calls,
      this.#calls.filterCreate(
        this.#h,
        bytesIn(expr),
        encodeParams(options.params),
        encodeSettings(options.settings, options.sessionTimezone),
      ),
    );
  }

  /** Parse a body once. */
  parseBlock(format: Format, body: Uint8Array, options: RowOptions = {}): Block {
    return new Block(
      this.#calls.blockCreate(
        this.#h,
        format,
        bodyIn(body),
        encodeSettings(options.settings, options.sessionTimezone),
        encodeColumns(options.columns),
      ),
    );
  }

  /** Release this schema; idempotent. Filters and blocks made from it keep working. */
  close(): void {
    this.#h.close();
  }

  [Symbol.dispose](): void {
    this.close();
  }
}
