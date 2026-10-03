/**
 * The result types and their decoders (`docs/reference/bindings-v1.md` §5).
 *
 * Every field below is a field of the document the library returned, or the
 * bytes of an output buffer; nothing is computed here. The rules every
 * decoder follows:
 *
 *   1. Stock JSON only: `JSON.parse`, after a scan that refuses a duplicate
 *      key (`./abi1/strictjson.ts`). A duplicate key, a document that is not
 *      JSON, or a value of the wrong JSON type is an `InternalError` naming the
 *      document and the key.
 *   2. Absent is the default, and an unknown key is ignored.
 *   3. Names, and every data-derived string (a rendering, a type, SQL, a
 *      message, a partition, a setting name), are BYTES. A document carries one
 *      as `<key>` (a JSON string, when the bytes are valid UTF-8) or as
 *      `<key>_b64` (standard base64 of the raw bytes, otherwise), and the
 *      decoder surfaces a `Buffer` either way. Both present is an
 *      `InternalError`; for a name, neither present is too. A list of names
 *      (`unknown_fields`, `unsupported_settings`, header names) holds `{name}`
 *      or `{name_b64}` objects, never a bare string. `value_b64` is a scalar
 *      String or FixedString value's raw bytes (rule 6), surfaced as `value`.
 *   4. A vocabulary fact (`Transform.lossy`, `Value.isStored`, a verdict's
 *      `answered`) is read from the generated vocabulary table, never from a
 *      list kept here.
 *   5. Unknown is never false and never empty: `framing.bomSkipped` and
 *      `framing.header` are `null` when the document says `null`.
 */

import {
  batchOutcomeOf,
  type DefaultKind,
  defaultKindOf,
  filterOutcomeOf,
  type FilterOutcome,
  type Outcome,
  outcomeOf,
  reasonLossy,
  sourceIsStored,
  type Verdict,
  verdictOf,
} from './abi1/index.js';
import { internalError, parseStrictJson } from './abi1/index.js';

type Obj = Record<string, unknown>;

// ------------------------------------------------------------------- types

/** The byte range of the input body a record occupies: `off` and `len`. */
export interface Span {
  readonly off: number;
  readonly len: number;
}

/** One column of a row result: ClickHouse's own rendering of one value, with its provenance. */
export interface Value {
  /** The column's name, as raw bytes. */
  readonly column: Buffer;
  /** ClickHouse's rendering of the stored value, verbatim, as bytes. */
  readonly text: Buffer;
  /** Whether the stored value is NULL: the library decides it, poisoned cells included. */
  readonly null: boolean;
  /** Where the value came from: a `Source` value. */
  readonly source: string;
  /** Whether a value of this `source` is stored: the description's own fact for it. */
  readonly isStored: boolean;
  /** The raw bytes of a scalar String or FixedString value (rule 6), when the document carries them. */
  readonly value: Buffer | undefined;
}

/** One cell of an engine row: ClickHouse's rendering of a stored value after the engine's insert-time merge. */
export interface EngineCell {
  readonly column: Buffer;
  readonly text: Buffer;
  readonly null: boolean;
  /** The raw bytes of a scalar String or FixedString value (rule 6), when the document carries them. */
  readonly value: Buffer | undefined;
}

/** A silent change: what the input said, what is stored, and why. */
export interface Transform {
  readonly column: Buffer;
  readonly input: Buffer;
  readonly stored: Buffer;
  /** A `Reason` value. */
  readonly reason: string;
  /** Whether this reason loses information: the description's own fact for it. */
  readonly lossy: boolean;
  /** The row's index in the body; 0 in a single-row result. */
  readonly row: number;
}

/** A column the library computed from a DEFAULT or MATERIALIZED expression. */
export interface Computed {
  readonly column: Buffer;
  readonly kind: string;
  readonly text: Buffer;
  /** The raw bytes of a scalar String or FixedString value (rule 6), when the document carries them. */
  readonly value: Buffer | undefined;
}

export interface RowResult {
  readonly outcome: Outcome;
  readonly errCode: number;
  readonly errMsg: Buffer;
  /** Every entry of `cols`, in document order. */
  readonly columns: readonly Value[];
  /** The entries of `columns` whose source is stored, in order. */
  readonly values: readonly Value[];
  readonly transformed: readonly Transform[];
  readonly unknownFields: readonly Buffer[];
  readonly unsupportedSettings: readonly Buffer[];
  readonly computed: readonly Computed[];
  /** Present only with an attached filter. */
  readonly verdict: Verdict | undefined;
  readonly verdictCode: number;
  readonly verdictErr: Buffer;
  readonly partitionId: Buffer | undefined;
  /** The bytes the reader consumed for this record. */
  readonly inputSpan: Span | undefined;
}

export interface Header {
  readonly consumed: boolean;
  readonly lines: number;
  readonly names: readonly Buffer[];
}

/** What the reader decided about the body's framing. `bomSkipped` and `header` are `null` where the reader does not expose the fact: unknown, never false and never empty. */
export interface Framing {
  readonly bomSkipped: boolean | null;
  /** `array`, `stream`, or `null` for none. */
  readonly container: string | null;
  readonly header: Header | null;
}

export interface BatchResult {
  readonly outcome: Outcome;
  readonly errCode: number;
  readonly errMsg: Buffer;
  readonly rows: readonly RowResult[];
  readonly rowsRead: number;
  readonly rowsSkipped: number;
  /** Every transform in the batch, with its `row`, as the library lists them. */
  readonly transformed: readonly Transform[];
  /** Each stored row after the engine's insert-time merge, as a list of cells. */
  readonly engineRows: readonly (readonly EngineCell[])[] | undefined;
  /** The export buffer, present only when an export was asked for. */
  readonly payload: Buffer | undefined;
  /** Each exported row's place in `payload`. */
  readonly spans: readonly Span[] | undefined;
  readonly exportDeclined: string;
  readonly rowsPassed: number;
  readonly rowsCut: number;
  readonly partitionCount: number | undefined;
  /** The byte ranges the reader's error recovery skipped. */
  readonly unconsumed: readonly Span[];
  readonly framing: Framing | undefined;
}

export interface FilterRowError {
  readonly row: number;
  readonly code: number;
  readonly msg: Buffer;
}

export interface FilterResult {
  readonly outcome: FilterOutcome;
  readonly errCode: number;
  readonly errMsg: Buffer;
  readonly rowsRead: number;
  readonly unsupportedSettings: readonly Buffer[];
  /** One verdict per row, each read through the generated table. `error` and `decline` are never answers. */
  readonly verdicts: readonly Verdict[];
  readonly errors: readonly FilterRowError[];
}

export interface Column {
  readonly name: Buffer;
  /** The canonical type. */
  readonly type: Buffer;
  readonly defaultKind: DefaultKind;
  readonly defaultExpr: Buffer;
}

export interface SchemaDescription {
  readonly columns: readonly Column[];
}

export interface DiscoveredColumn {
  readonly name: Buffer;
  /** The column declaration as ClickHouse's own formatter writes it. */
  readonly declaration: Buffer;
}

export interface Discovery {
  readonly columns: readonly DiscoveredColumn[];
  /** The declarations joined for a `CREATE TABLE`. */
  readonly columnsSql: Buffer;
}

export interface ErrorCodeEntry {
  readonly code: number;
  readonly name: string;
}

/** This build's own error-code table: lookups are map reads over the library's own entries. An unknown code or name is absent, never synthesized; names match exactly. */
export class ErrorCodeTable implements Iterable<ErrorCodeEntry> {
  readonly #entries: readonly ErrorCodeEntry[];
  readonly #byCode: Map<number, string>;
  readonly #byName: Map<string, number>;

  constructor(entries: readonly ErrorCodeEntry[]) {
    this.#entries = entries;
    this.#byCode = new Map();
    this.#byName = new Map();
    for (const e of entries) {
      if (!this.#byCode.has(e.code)) this.#byCode.set(e.code, e.name);
      if (!this.#byName.has(e.name)) this.#byName.set(e.name, e.code);
    }
  }

  name(code: number): string | undefined {
    return this.#byCode.get(code);
  }

  code(name: string): number | undefined {
    return this.#byName.get(name);
  }

  all(): readonly ErrorCodeEntry[] {
    return this.#entries;
  }

  [Symbol.iterator](): Iterator<ErrorCodeEntry> {
    return this.#entries[Symbol.iterator]();
  }
}

// ----------------------------------------------------------- field readers

const EMPTY = Buffer.alloc(0);

function kindOf(v: unknown): string {
  return v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v;
}

function bad(doc: string, key: string, want: string, got: unknown): never {
  throw internalError(`the ${doc} document: ${key}: want ${want}, got ${kindOf(got)}`);
}

/** Parse one document: stock JSON after the duplicate-key scan; any failure is an `InternalError` naming the document. */
function parseDoc(doc: string, bytes: Uint8Array): unknown {
  const text = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('utf8');
  try {
    return parseStrictJson(text);
  } catch (err) {
    throw internalError(`the ${doc} document does not decode: ${err instanceof Error ? err.message : String(err)}`);
  }
}

function asObject(doc: string, key: string, v: unknown): Obj {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) bad(doc, key, 'an object', v);
  return v as Obj;
}

function asArray(doc: string, key: string, v: unknown): readonly unknown[] {
  if (!Array.isArray(v)) bad(doc, key, 'an array', v);
  return v;
}

function listOf(doc: string, o: Obj, key: string): readonly unknown[] {
  const v = o[key];
  return v === undefined ? [] : asArray(doc, key, v);
}

function strOr(doc: string, o: Obj, key: string, dflt: string): string {
  const v = o[key];
  if (v === undefined) return dflt;
  if (typeof v !== 'string') bad(doc, key, 'a string', v);
  return v;
}

function optStr(doc: string, o: Obj, key: string): string | undefined {
  const v = o[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== 'string') bad(doc, key, 'a string', v);
  return v;
}

function boolOr(doc: string, o: Obj, key: string, dflt: boolean): boolean {
  const v = o[key];
  if (v === undefined) return dflt;
  if (typeof v !== 'boolean') bad(doc, key, 'a boolean', v);
  return v;
}

/** An integer: a JSON number, or a decimal string where the document spells a value beyond 2^53 that way. Beyond what a JS number holds exactly is an `InternalError`, never a rounded value. */
function intOf(doc: string, key: string, v: unknown): number {
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v)) bad(doc, key, 'a safe integer', v);
    return v;
  }
  if (typeof v === 'string' && /^-?[0-9]+$/.test(v)) {
    const n = Number(v);
    if (Number.isSafeInteger(n)) return n;
    throw internalError(`the ${doc} document: ${key}: ${v} is beyond 2^53 and this binding's number type cannot hold it exactly`);
  }
  return bad(doc, key, 'an integer', v);
}

function intOr(doc: string, o: Obj, key: string, dflt: number): number {
  const v = o[key];
  return v === undefined ? dflt : intOf(doc, key, v);
}

function optInt(doc: string, o: Obj, key: string): number | undefined {
  const v = o[key];
  return v === undefined || v === null ? undefined : intOf(doc, key, v);
}

function decodeB64(doc: string, key: string, text: string): Buffer {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text) || text.length % 4 !== 0) bad(doc, key, 'standard base64', text);
  return Buffer.from(text, 'base64');
}

/** A byte-string field: `<key>` as UTF-8 text or `<key>_b64` as base64, never both. Neither reads as `undefined`. */
function bytesField(doc: string, o: Obj, key: string): Buffer | undefined {
  const plain = o[key];
  const b64 = o[`${key}_b64`];
  if (plain !== undefined && b64 !== undefined) {
    throw internalError(`the ${doc} document: ${key} and ${key}_b64 are both present`);
  }
  if (plain !== undefined) {
    if (typeof plain !== 'string') bad(doc, key, 'a string', plain);
    return Buffer.from(plain, 'utf8');
  }
  if (b64 !== undefined) {
    if (typeof b64 !== 'string') bad(doc, `${key}_b64`, 'a string', b64);
    return decodeB64(doc, `${key}_b64`, b64);
  }
  return undefined;
}

function bytesOr(doc: string, o: Obj, key: string): Buffer {
  return bytesField(doc, o, key) ?? EMPTY;
}

/** A name: exactly one of `<key>` and `<key>_b64`. */
function nameOf(doc: string, o: Obj, key: string): Buffer {
  const b = bytesField(doc, o, key);
  if (b === undefined) throw internalError(`the ${doc} document: neither ${key} nor ${key}_b64 is present`);
  return b;
}

/** A list of names: each element an object carrying exactly one of `name` and `name_b64`. A bare string is an `InternalError`. */
function bytesListOf(doc: string, o: Obj, key: string): readonly Buffer[] {
  return listOf(doc, o, key).map((e) => nameOf(doc, asObject(doc, `${key}[]`, e), 'name'));
}

/** One cell of an engine row. */
function engineCellOf(doc: string, v: unknown): EngineCell {
  const o = asObject(doc, 'engine_rows[][]', v);
  const nullFlag = o.null;
  if (typeof nullFlag !== 'boolean') bad(doc, 'engine_rows[][].null', 'a boolean', nullFlag);
  return { column: nameOf(doc, o, 'name'), text: bytesOr(doc, o, 'stored'), null: nullFlag, value: bytesField(doc, o, 'value') };
}

function spanOf(doc: string, key: string, v: unknown): Span {
  const o = asObject(doc, key, v);
  return { off: intOf(doc, `${key}.off`, o.off), len: intOf(doc, `${key}.len`, o.len) };
}

// ------------------------------------------------------------- the row

function decodeValue(doc: string, v: unknown): Value {
  const o = asObject(doc, 'cols[]', v);
  const source = strOr(doc, o, 'src', '');
  const isStored = sourceIsStored(source);
  if (isStored === undefined) throw internalError(`the ${doc} document: cols[].src: ${JSON.stringify(source)} is not a value of the source vocabulary`);
  const nullFlag = o.null;
  if (typeof nullFlag !== 'boolean') bad(doc, 'cols[].null', 'a boolean', nullFlag);
  return {
    column: nameOf(doc, o, 'name'),
    text: bytesOr(doc, o, 'stored'),
    null: nullFlag,
    source,
    isStored,
    value: bytesField(doc, o, 'value'),
  };
}

function transformOf(doc: string, v: unknown): Transform {
  const o = asObject(doc, 'transformed[]', v);
  const reason = strOr(doc, o, 'reason', '');
  const lossy = reasonLossy(reason);
  if (lossy === undefined) throw internalError(`the ${doc} document: transformed[].reason: ${JSON.stringify(reason)} has no lossy fact`);
  return {
    column: nameOf(doc, o, 'column'),
    input: bytesOr(doc, o, 'input'),
    stored: bytesOr(doc, o, 'stored'),
    reason,
    lossy,
    row: intOr(doc, o, 'row', 0),
  };
}

function computedOf(doc: string, v: unknown): Computed {
  const o = asObject(doc, 'computed[]', v);
  return { column: nameOf(doc, o, 'name'), kind: strOr(doc, o, 'kind', ''), text: bytesOr(doc, o, 'stored'), value: bytesField(doc, o, 'value') };
}

function rowOf(doc: string, o: Obj): RowResult {
  const rawOutcome = strOr(doc, o, 'outcome', '');
  const outcome = outcomeOf(rawOutcome);
  if (outcome === undefined) throw internalError(`the ${doc} document: outcome: ${JSON.stringify(rawOutcome)} is not an outcome`);
  const columns = listOf(doc, o, 'cols').map((c) => decodeValue(doc, c));
  const verdictRaw = optStr(doc, o, 'verdict');
  const verdict = verdictRaw === undefined ? undefined : verdictOf(verdictRaw);
  const spanRaw = o.input_span;
  const partitionId = bytesField(doc, o, 'partition_id');
  return {
    outcome,
    errCode: intOr(doc, o, 'code', 0),
    errMsg: bytesOr(doc, o, 'err'),
    columns,
    values: columns.filter((c) => c.isStored),
    transformed: listOf(doc, o, 'transformed').map((t) => transformOf(doc, t)),
    unknownFields: bytesListOf(doc, o, 'unknown_fields'),
    unsupportedSettings: bytesListOf(doc, o, 'unsupported_settings'),
    computed: listOf(doc, o, 'computed').map((c) => computedOf(doc, c)),
    verdict,
    verdictCode: intOr(doc, o, 'verdict_code', 0),
    verdictErr: bytesOr(doc, o, 'verdict_err'),
    partitionId,
    inputSpan: spanRaw === undefined ? undefined : spanOf(doc, 'input_span', spanRaw),
  };
}

/** Decode a `row` document. */
export function decodeRow(bytes: Uint8Array): RowResult {
  return rowOf('row', asObject('row', '$', parseDoc('row', bytes)));
}

// ----------------------------------------------------------- the batch

function headerOf(doc: string, v: unknown): Header | null {
  if (v === null) return null;
  const o = asObject(doc, 'framing.header', v);
  return { consumed: boolOr(doc, o, 'consumed', false), lines: intOr(doc, o, 'lines', 0), names: bytesListOf(doc, o, 'names') };
}

function framingOf(doc: string, v: unknown): Framing {
  const o = asObject(doc, 'framing', v);
  const bom = o.bom_skipped;
  if (bom !== undefined && bom !== null && typeof bom !== 'boolean') bad(doc, 'framing.bom_skipped', 'a boolean or null', bom);
  const container = o.container;
  if (container !== undefined && container !== null && typeof container !== 'string') {
    bad(doc, 'framing.container', 'a string or null', container);
  }
  return {
    bomSkipped: bom ?? null,
    container: container ?? null,
    header: o.header === undefined ? null : headerOf(doc, o.header),
  };
}

/** Decode a `batch` document and the export buffer, when an export was asked for. */
export function decodeBatch(bytes: Uint8Array, payload: Buffer | undefined): BatchResult {
  const doc = 'batch';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  const rawOutcome = strOr(doc, o, 'outcome', '');
  const outcome = batchOutcomeOf(rawOutcome);
  if (outcome === undefined) throw internalError(`the ${doc} document: outcome: ${JSON.stringify(rawOutcome)} is not an outcome`);
  const spansRaw = o.row_spans;
  const engineRaw = o.engine_rows;
  return {
    outcome,
    errCode: intOr(doc, o, 'code', 0),
    errMsg: bytesOr(doc, o, 'err'),
    rows: listOf(doc, o, 'rows').map((r) => rowOf(doc, asObject(doc, 'rows[]', r))),
    rowsRead: intOr(doc, o, 'rows_read', 0),
    rowsSkipped: intOr(doc, o, 'rows_skipped', 0),
    transformed: listOf(doc, o, 'transformed').map((t) => transformOf(doc, t)),
    engineRows:
      engineRaw === undefined
        ? undefined
        : asArray(doc, 'engine_rows', engineRaw).map((row) => asArray(doc, 'engine_rows[]', row).map((c) => engineCellOf(doc, c))),
    payload,
    spans: spansRaw === undefined ? undefined : asArray(doc, 'row_spans', spansRaw).map((s) => spanOf(doc, 'row_spans[]', s)),
    exportDeclined: strOr(doc, o, 'export_declined', ''),
    rowsPassed: intOr(doc, o, 'rows_passed', 0),
    rowsCut: intOr(doc, o, 'rows_cut', 0),
    partitionCount: optInt(doc, o, 'partition_count'),
    unconsumed: listOf(doc, o, 'unconsumed').map((s) => spanOf(doc, 'unconsumed[]', s)),
    framing: o.framing === undefined ? undefined : framingOf(doc, o.framing),
  };
}

// ----------------------------------------------------------- the filter

/** Decode a `filter_result` document. */
export function decodeFilterResult(bytes: Uint8Array): FilterResult {
  const doc = 'filter_result';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  const rawOutcome = strOr(doc, o, 'outcome', '');
  const outcome = filterOutcomeOf(rawOutcome);
  if (outcome === undefined) throw internalError(`the ${doc} document: outcome: ${JSON.stringify(rawOutcome)} is not an outcome`);
  const verdicts: Verdict[] = [];
  for (const ch of strOr(doc, o, 'verdicts', '')) {
    const v = verdictOf(ch);
    if (v === undefined) throw internalError(`the ${doc} document: verdicts: ${JSON.stringify(ch)} is not a verdict`);
    verdicts.push(v);
  }
  return {
    outcome,
    errCode: intOr(doc, o, 'code', 0),
    errMsg: bytesOr(doc, o, 'err'),
    rowsRead: intOr(doc, o, 'rows_read', 0),
    unsupportedSettings: bytesListOf(doc, o, 'unsupported_settings'),
    verdicts,
    errors: listOf(doc, o, 'errors').map((e) => {
      const eo = asObject(doc, 'errors[]', e);
      return { row: intOr(doc, eo, 'row', 0), code: intOr(doc, eo, 'code', 0), msg: bytesOr(doc, eo, 'err') };
    }),
  };
}

// --------------------------------------------- schema, discovery, tables

/** Decode a `schema_description` document. */
export function decodeSchemaDescription(bytes: Uint8Array): SchemaDescription {
  const doc = 'schema_description';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  return {
    columns: listOf(doc, o, 'columns').map((c) => {
      const co = asObject(doc, 'columns[]', c);
      const kindRaw = strOr(doc, co, 'default_kind', '');
      const defaultKind = defaultKindOf(kindRaw);
      if (defaultKind === undefined) {
        throw internalError(`the ${doc} document: columns[].default_kind: ${JSON.stringify(kindRaw)} is not a default kind`);
      }
      return { name: nameOf(doc, co, 'name'), type: bytesOr(doc, co, 'type'), defaultKind, defaultExpr: bytesOr(doc, co, 'default_expression') };
    }),
  };
}

/** Decode a `discovery` document. */
export function decodeDiscovery(bytes: Uint8Array): Discovery {
  const doc = 'discovery';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  return {
    columns: listOf(doc, o, 'columns').map((c) => {
      const co = asObject(doc, 'columns[]', c);
      return { name: nameOf(doc, co, 'name'), declaration: bytesOr(doc, co, 'declaration') };
    }),
    columnsSql: bytesOr(doc, o, 'columns_sql'),
  };
}

/** Decode an `error_code_table` document: a JSON array of `{code, name}`. */
export function decodeErrorCodes(bytes: Uint8Array): ErrorCodeTable {
  const doc = 'error_code_table';
  const entries = asArray(doc, '$', parseDoc(doc, bytes)).map((e) => {
    const eo = asObject(doc, '[]', e);
    const name = eo.name;
    if (typeof name !== 'string') bad(doc, '[].name', 'a string', name);
    return { code: intOf(doc, '[].code', eo.code), name };
  });
  return new ErrorCodeTable(entries);
}

/** Decode a `live_handles` document: handle kind to live count. */
export function decodeLiveHandles(bytes: Uint8Array): Readonly<Record<string, number>> {
  const doc = 'live_handles';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(o)) out[k] = intOf(doc, k, v);
  return out;
}
