/**
 * The result types and their decoders (`docs/reference/bindings-v1.md` §5, under
 * ABI v2's reader rules r2 and r3, `spec/abi-v2/docs.md`).
 *
 * Every field below is a field of the document the library returned, or the
 * bytes of an output buffer; nothing is computed here. The rules every
 * decoder follows:
 *
 *   1. Stock JSON only: `JSON.parse`, after a scan that refuses a duplicate
 *      key (`./abi2/strictjson.ts`). A duplicate key, a document that is not
 *      JSON, or a value of the wrong JSON type is an `InternalError` naming the
 *      document and the key.
 *   2. Absent is the default, and an unknown key is ignored, at every object
 *      level, `_b64` members included (rule r2).
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
 *      list kept here. A vocabulary value the description does not list is
 *      that vocabulary's unknown(n), kept verbatim for that field alone (rule
 *      r3): it never fails the document, the row or the batch, and its facts
 *      are its fallback's (an unknown outcome is never accepted, an unknown
 *      verdict is never answered, an unknown source is not stored).
 *   5. Unknown is never false and never empty: `framing.bomSkipped` and
 *      `framing.header` are `null` when the document says `null`.
 */

import {
  batchOutcomeOf,
  type DefaultKind,
  defaultKindOf,
  filterOutcomeOf,
  type FilterOutcome,
  type MergeReason,
  mergeReasonOf,
  type Outcome,
  outcomeOf,
  type Reason,
  reasonLossy,
  reasonOf,
  type Source,
  sourceIsStored,
  sourceOf,
  type Verdict,
  verdictOf,
} from './abi2/index.js';
import { internalError, parseStrictJson } from './abi2/index.js';

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

/**
 * One thing an `OPTIMIZE TABLE ... FINAL` of the part the INSERT writes would do to one of its rows, at the
 * call's clock instant: an entry of the batch's `at_merge` list (ABI v2). `engineRows` stays exactly what the
 * INSERT writer produces, so the two never contradict each other.
 */
export interface AtMergeEntry {
  /** Indexes `engineRows`: the part's row, not the input body's. */
  readonly row: number;
  /** A `MergeReason` value; one the description does not list is its unknown(n), kept verbatim (rule r3). */
  readonly reason: MergeReason;
  /** The column a `ttl_column_reset` resets, as bytes (`column` or `column_b64`); `undefined` when the entry names none. */
  readonly column: Buffer | undefined;
  /**
   * The column's value after the reset, as bytes (`stored` or `stored_b64`); `undefined` when the document carries
   * none, which it omits when the column's DEFAULT reads the clock or a generator and the value is decided at the merge.
   */
  readonly stored: Buffer | undefined;
  /** The input rows, each by its index in the body, that formed the part's row; `undefined` when the document carries none. */
  readonly inputRows: readonly number[] | undefined;
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
  /** The call's own settings the library declined, from the batch's top-level list (ABI v2); empty when it names none. */
  readonly unsupportedSettings: readonly Buffer[];
  /** Each stored row after the engine's insert-time merge, as a list of cells. */
  readonly engineRows: readonly (readonly EngineCell[])[] | undefined;
  /**
   * What an `OPTIMIZE TABLE ... FINAL` of the part would do to its rows, at least: a later background merge can remove
   * more as more rows expire, and a row both removed and reset lists only `ttl_delete` (ABI v2). Empty when the
   * document carries none.
   */
  readonly atMerge: readonly AtMergeEntry[];
  /** The export buffer, present only when an export was asked for. */
  readonly payload: Buffer | undefined;
  /** Each exported row's place in `payload`. */
  readonly spans: readonly Span[] | undefined;
  /** Why an asked-for export was declined, as bytes (`export_declined` or `export_declined_b64`); empty when none was. */
  readonly exportDeclined: Buffer;
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

/**
 * The server a schema was compiled on, as the library holds it. Its strings are
 * the caller's own profile values given back, so they are plain text.
 */
export interface SchemaServer {
  /** The zone the schema's types bind: the profile's, else the image zone. */
  readonly timezone: string;
  /** The server's settings as the profile gave them; `{}` when it gave none. */
  readonly settings: Readonly<Record<string, string>> | undefined;
  /** The server's macro set: `undefined` exactly when the profile carried no macros (unknown); present, even `{}`, is the complete set. */
  readonly macros: Readonly<Record<string, string>> | undefined;
}

/** What ClickHouse's own TableZnodeInfo resolved for a Replicated engine, fully expanded. Both expand DDL bytes, so both are bytes. */
export interface SchemaReplicated {
  readonly zookeeperPath: Buffer;
  readonly replicaName: Buffer;
}

export interface SchemaDescription {
  readonly columns: readonly Column[];
  /** The server the schema was compiled on: `undefined` exactly when it was compiled without one. */
  readonly server: SchemaServer | undefined;
  /** A Replicated engine's resolved ZooKeeper path and replica name: `undefined` when the document carries none. */
  readonly replicated: SchemaReplicated | undefined;
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

/** One entry of `at_merge`. An unlisted reason is its unknown(n): kept, never a failure (r3). */
function atMergeOf(doc: string, v: unknown): AtMergeEntry {
  const o = asObject(doc, 'at_merge[]', v);
  const reason: MergeReason = mergeReasonOf(strOr(doc, o, 'reason', ''));
  const rowsRaw = o.input_rows;
  return {
    row: intOr(doc, o, 'row', 0),
    reason,
    column: bytesField(doc, o, 'column'),
    stored: bytesField(doc, o, 'stored'),
    inputRows:
      rowsRaw === undefined || rowsRaw === null
        ? undefined
        : asArray(doc, 'at_merge[].input_rows', rowsRaw).map((n) => {
            const i = intOf(doc, 'at_merge[].input_rows[]', n);
            if (i < 0) bad(doc, 'at_merge[].input_rows[]', 'a non-negative integer', n);
            return i;
          }),
  };
}

function spanOf(doc: string, key: string, v: unknown): Span {
  const o = asObject(doc, key, v);
  return { off: intOf(doc, `${key}.off`, o.off), len: intOf(doc, `${key}.len`, o.len) };
}

// ------------------------------------------------------------- the row

function decodeValue(doc: string, v: unknown): Value {
  const o = asObject(doc, 'cols[]', v);
  // An unlisted src is its unknown(n): kept, never a failure (r3). An ABSENT
  // src is no value at all, and stays a malformed entry.
  if (o.src === undefined || o.src === null) throw internalError(`the ${doc} document: cols[].src: missing`);
  const source: Source = sourceOf(strOr(doc, o, 'src', ''));
  const isStored = sourceIsStored(source);
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
  // An unlisted reason is its unknown(n), with its fallback's lossy fact (r3).
  const reason: Reason = reasonOf(strOr(doc, o, 'reason', ''));
  const lossy = reasonLossy(reason);
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
  // An unlisted outcome is its unknown(n): kept, never accepted (r3).
  const outcome = outcomeOf(strOr(doc, o, 'outcome', ''));
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
  const outcome = batchOutcomeOf(strOr(doc, o, 'outcome', ''));
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
    unsupportedSettings: bytesListOf(doc, o, 'unsupported_settings'),
    engineRows:
      engineRaw === undefined
        ? undefined
        : asArray(doc, 'engine_rows', engineRaw).map((row) => asArray(doc, 'engine_rows[]', row).map((c) => engineCellOf(doc, c))),
    payload,
    spans: spansRaw === undefined ? undefined : asArray(doc, 'row_spans', spansRaw).map((s) => spanOf(doc, 'row_spans[]', s)),
    exportDeclined: bytesOr(doc, o, 'export_declined'),
    rowsPassed: intOr(doc, o, 'rows_passed', 0),
    rowsCut: intOr(doc, o, 'rows_cut', 0),
    partitionCount: optInt(doc, o, 'partition_count'),
    unconsumed: listOf(doc, o, 'unconsumed').map((s) => spanOf(doc, 'unconsumed[]', s)),
    framing: o.framing === undefined ? undefined : framingOf(doc, o.framing),
    atMerge: listOf(doc, o, 'at_merge').map((a) => atMergeOf(doc, a)),
  };
}

// ----------------------------------------------------------- the filter

/** Decode a `filter_result` document. */
export function decodeFilterResult(bytes: Uint8Array): FilterResult {
  const doc = 'filter_result';
  const o = asObject(doc, '$', parseDoc(doc, bytes));
  const outcome = filterOutcomeOf(strOr(doc, o, 'outcome', ''));
  // One verdict per character; an unlisted one is its unknown(n), never an answer (r3).
  const verdicts: Verdict[] = [];
  for (const ch of strOr(doc, o, 'verdicts', '')) verdicts.push(verdictOf(ch));
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
  const serverValue = o.server;
  const replicatedValue = o.replicated;
  let server: SchemaServer | undefined;
  if (serverValue !== undefined && serverValue !== null) {
    const so = asObject(doc, '$.server', serverValue);
    server = {
      timezone: strOr(doc, so, 'timezone', ''),
      settings: stringMapOf(doc, so, 'settings', '$.server'),
      macros: stringMapOf(doc, so, 'macros', '$.server'),
    };
  }
  let replicated: SchemaReplicated | undefined;
  if (replicatedValue !== undefined && replicatedValue !== null) {
    const ro = asObject(doc, '$.replicated', replicatedValue);
    replicated = { zookeeperPath: bytesOr(doc, ro, 'zookeeper_path'), replicaName: bytesOr(doc, ro, 'replica_name') };
  }
  return {
    server,
    replicated,
    columns: listOf(doc, o, 'columns').map((c) => {
      const co = asObject(doc, 'columns[]', c);
      // An unlisted default_kind is its unknown(n): kept, never a failure (r3).
      const defaultKind = defaultKindOf(strOr(doc, co, 'default_kind', ''));
      return { name: nameOf(doc, co, 'name'), type: bytesOr(doc, co, 'type'), defaultKind, defaultExpr: bytesOr(doc, co, 'default_expression') };
    }),
  };
}

/** An object whose values are plain JSON strings: `undefined` when absent, an object (even an empty one) when present, so absent and `{}` stay apart. */
function stringMapOf(doc: string, o: Obj, key: string, path: string): Readonly<Record<string, string>> | undefined {
  const v = o[key];
  if (v === undefined || v === null) return undefined;
  const m = asObject(doc, `${path}.${key}`, v);
  const out: Record<string, string> = {};
  for (const [k, e] of Object.entries(m)) {
    if (typeof e !== 'string') bad(doc, `${path}.${key}.${k}`, 'a JSON string', e);
    Object.defineProperty(out, k, { value: e, enumerable: true, writable: true, configurable: true });
  }
  return out;
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
