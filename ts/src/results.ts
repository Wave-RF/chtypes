/**
 * The result types and the document parsers. Field for field the same concepts
 * as the reference implementation's `RowResult` / `BatchResult`
 * (docs/reference/bindings.md §Result types), in TypeScript spelling.
 *
 * ---------------------------------------------------------------- text vs bytes
 * Every rendered value here comes in both spellings, and which one is
 * authoritative is not a matter of taste:
 *
 *   `bytes`  the exact slice ClickHouse emitted — the answer. Equivalent to the
 *            reference's `json.RawMessage`, and the only form a `String`,
 *            `FixedString` or `AggregateFunction` value survives (it holds
 *            arbitrary bytes; base64 or `Buffer` it, never a JS string).
 *   `text`   those bytes decoded as UTF-8, for logs, comparisons and previews.
 *            **Lossy exactly when the bytes are not valid UTF-8**, where a JS
 *            string can only hold U+FFFD. `isValidUtf8(bytes)` says so.
 *
 * A caller that echoes a value back into an INSERT, hashes it, or compares it
 * against a real server MUST use `bytes`: reporting U+FFFD where the table holds
 * `0xff` is an invented transformation, which is the one thing this product may
 * never do.
 */

import {
  asBool,
  asBytes,
  asInt,
  asString,
  asStringArray,
  field,
  hasField,
  items,
  rawBytes,
  rawText,
  type Json,
} from './json.js';
import { classify, isLossyReason } from './transform.js';

/**
 * The verdict on a row or a batch — the error taxonomy in one string, and
 * conflating any two arms is a scoring error (docs/reference/c-abi.md §Error model):
 *
 * - `'accepted'` — the server would take this, possibly with silent coercions
 *   (read `transformed`), defaults filled and volatile DEFAULTs substituted
 *   (read `substituted` — the caller MUST send those columns explicitly).
 * - `'rejected'` — the server itself would refuse; `errCode` is ClickHouse's
 *   own code (27, 117, 455, …) and `errMsg` its own message. Tell the tenant.
 * - `'accepted_poisoned'` — the insert is ACCEPTED (rc = 0) but the stored
 *   value can never be read back (`errCode` 691). Never report it as a
 *   rejection.
 * - `'unsupported'` — chtypes declines to answer ("a real server might well
 *   have accepted this; I will not guess"). NOT a rejection: fall back to the
 *   server. Mapping it onto either other arm manufactures an over-reject or an
 *   over-accept, both budgeted at zero.
 * - `'skipped'` (2026-08-27) — the row was dropped under
 *   `input_format_allow_errors_*` and the batch continued: the server's own
 *   skip, itemized. `errCode`/`errMsg` carry the error the vendored reader
 *   caught before resyncing, verbatim; `values` is empty — the row is NOT
 *   stored. Only ever seen on rows inside a `BatchResult`, never as a batch
 *   verdict and never from `Schema#row`.
 */
export type Outcome = 'accepted' | 'rejected' | 'accepted_poisoned' | 'unsupported' | 'skipped';

const EMPTY = Buffer.alloc(0);

/**
 * One coerced column value, as ClickHouse itself renders it.
 *
 * `bytes` is that rendering verbatim (`0`, `"2023-11-14 22:13:20"`, `[1,0,3]`,
 * a blob with no valid UTF-8 in it) — the same bytes the server would emit for
 * the row, which is what makes it directly comparable against a real ClickHouse.
 * `text` is `bytes` as UTF-8, and it is kept as text on purpose: parsing it into
 * a JS number would turn 18446744073709551615 into 18446744073709552000.
 */
export interface Value {
  readonly column: string;
  /** ClickHouse's rendering as UTF-8 text; lossy iff `bytes` is not valid UTF-8. */
  readonly text: string;
  /** ClickHouse's rendering, verbatim. **Authoritative.** */
  readonly bytes: Buffer;
  /** True only for a genuine stored null. A poisoned column has no value at all. */
  readonly isNull: boolean;
  /**
   * Where the value came from: `input` | `default` | `default_substituted` |
   * `absent` | `skipped` | `default_volatile_unresolved` | `default_pending` |
   * `default_expr_unsupported` (docs/reference/c-abi.md §`src` values).
   */
  readonly source: string;
}

/** A silent change: input 256 into UInt8 stored as 0. */
export interface Transform {
  readonly column: string;
  readonly input: string;
  readonly stored: string;
  /** The supplied field, verbatim. **Authoritative** where `input` is lossy. */
  readonly inputBytes: Buffer;
  /** The stored rendering, verbatim. **Authoritative** where `stored` is lossy. */
  readonly storedBytes: Buffer;
  /** One of the stable reason strings — see `Reason`. */
  readonly reason: string;
  /** 0-based index of the row inside the request body. */
  row: number;
  /** False for reformat / default_filled / zero_filled / default_materialised. */
  readonly lossy: boolean;
}

/** A volatile DEFAULT this library resolved instead of the server. */
export interface Substitution {
  readonly column: string;
  /** The DEFAULT as ClickHouse canonicalised it, e.g. `now()`. */
  readonly expr: string;
  /** The value rendered by ClickHouse's own serializer. Send it back verbatim. */
  readonly text: string;
  /** The same value as bytes — this is the form to put in the INSERT. */
  readonly bytes: Buffer;
}

/** One MATERIALIZED column's value: stored at insert, frozen thereafter. */
export interface Computed {
  readonly column: string;
  readonly kind: string;
  readonly text: string;
  /** The stored rendering, verbatim. */
  readonly bytes: Buffer;
}

/**
 * The verdict on one row: what would be stored, what was silently changed, and
 * which of the five outcomes it is. Returned by `Schema#row` (which never
 * answers `'skipped'` — a single row has no batch to continue), and one per
 * row inside `BatchResult#rows`.
 */
export interface RowResult {
  /** The coerced stored row, positional, excluding `skipped` columns. */
  readonly values: Value[];
  /** Every silent change. Not optional — it is the product. */
  readonly transformed: Transform[];
  /** The row verdict — see `Outcome` for the four arms and their obligations. */
  readonly outcome: Outcome;
  /**
   * ClickHouse's own code when `rejected`; 691 when `accepted_poisoned`; 0 when
   * `accepted`. NOT a reliable sentinel for `unsupported`: a row-level decline
   * carries the document's code verbatim, which is 0 for e.g. a
   * `DEFAULT hostName()` decline and -2 only on the clock-skew decline — key on
   * `outcome`, never on this number alone (docs/reference/bindings.md §RowResult).
   */
  readonly errCode: number;
  /** ClickHouse's own message, `''` when none. */
  readonly errMsg: string;
  /** Input fields with no matching column. */
  readonly unknownFields: string[];
  /** Non-empty means the answer must not be scored as agreement. */
  readonly unsupportedSettings: string[];
  /**
   * Columns whose volatile DEFAULT this library resolved from its own clock.
   * THE CALLER MUST SEND EVERY ONE OF THESE AS AN EXPLICIT COLUMN in the INSERT:
   * if the server evaluates the expression instead, preview and stored differ
   * always at now64 resolution and sometimes at now() resolution.
   */
  readonly substituted: Substitution[];
  /** MATERIALIZED values — durable, but not part of `SELECT *`. */
  readonly computed: Computed[];
}

/**
 * The verdict on a whole request body, which may hold many rows. Returned by
 * `Schema#rows`. The batch is where "not stored" lives: a TTL-expired row is
 * `accepted` in its own `RowResult` and reported not-stored here, through
 * `transformed` and `engineRows`.
 */
export interface BatchResult {
  /**
   * One `RowResult` per row the reader consumed, in input order — including
   * (2026-08-27) rows dropped under `input_format_allow_errors_*`, which
   * appear as `outcome: 'skipped'` with the server's own caught error and no
   * values. Filter on the row outcome when rendering survivors; a skipped
   * row is never stored.
   */
  readonly rows: RowResult[];
  /** The batch verdict — same vocabulary and obligations as a row's. */
  readonly outcome: Outcome;
  /** ClickHouse's own code when the BATCH was rejected; 0 when accepted. */
  readonly errCode: number;
  /** ClickHouse's own message, `''` when none. */
  readonly errMsg: string;
  /** Rows the reader consumed, failed ones included. */
  readonly rowsRead: number;
  /** Rows dropped under `input_format_allow_errors_num` / `_ratio`. */
  readonly rowsSkipped: number;
  /**
   * Every transform in the batch, each tagged with its row index — including the
   * batch-level `storage_transforms` (a TTL-expired row is `accepted` per row and
   * NOT STORED per batch; dropping these tells a tenant a row was stored that the
   * table deletes at merge time).
   */
  readonly transformed: Transform[];
  /**
   * The stored preview AFTER the engine's insert-time merge, one exact JSON text
   * per row, or null when no engine semantics applied. When present this — not
   * `rows` — is the stored truth: it can be shorter (a SummingMergeTree dropping
   * an all-zero row) or reordered (the block is sorted by the sorting key first).
   */
  readonly engineRows: string[] | null;
  /** The same preview as bytes, positionally aligned with `engineRows`. */
  readonly engineRowsBytes: Buffer[] | null;

  /**
   * The export channel (revision 3; only when `rows()` was asked for an
   * `exportFormat`): the batch's accepted rows, serialized once by the
   * vendored writer, copied out of the C buffer and freed before the call
   * returned — no ownership crosses the FFI boundary. Three states, and the
   * distinction is the ABI's own (docs/reference/c-abi.md §Rows):
   *
   *   `undefined`         no export was requested, the export was DECLINED
   *                       (`exportDeclined` then names the reason), or a
   *                       call-level verdict preempted the export machinery
   *                       entirely (the batch `outcome` is then the reason and
   *                       `exportDeclined` stays absent).
   *   zero-length Buffer  EMITTED-EMPTY: an accepted batch with zero accepted
   *                       rows — distinguishable from a decline.
   *   bytes               the exported lines; slice per `spans`.
   */
  readonly payload: Buffer | undefined;
  /**
   * Index-aligned with `rows`: `spans[i]` addresses row i's complete output
   * line (terminating `\n` included) inside `payload` — each slice is itself
   * a valid one-row body in the export format, a non-accepted row carries
   * `{off: 0, len: 0}`, and the concatenation of all non-zero spans
   * reproduces `payload` exactly (batches merge by byte concatenation).
   * `undefined` when no bytes were emitted.
   */
  readonly spans: readonly Span[] | undefined;
  /**
   * The library's reason for withholding requested export bytes (a
   * non-accepted batch, the full-arity guard, a serialization failure,
   * `output_format_json_validate_utf8`) — `undefined` when bytes were emitted
   * or no export was requested. A decline here is -2-class honesty, never a
   * server verdict.
   */
  readonly exportDeclined: string | undefined;
}

/** One row's byte range inside an export `payload` — see `BatchResult#spans`. */
export interface Span {
  readonly off: number;
  readonly len: number;
}

/** One entry of the result document's `cols` array, decoded. */
export interface ColumnDoc {
  readonly name: string;
  readonly type: string;
  readonly base: string;
  readonly src: string;
  readonly input: string;
  readonly refType: string;
  /** Exact JSON text of the stored value; "" when absent or poisoned. */
  readonly stored: string;
  /** Exact JSON text of the reference parse; "" when it does not apply. */
  readonly ref: string;
  /** The raw input field, verbatim — a supplied value can be any byte sequence. */
  readonly inputBytes: Buffer;
  /** The stored rendering, verbatim. **This, not `stored`, is the answer.** */
  readonly storedBytes: Buffer;
  /** The reference parse's rendering, verbatim. */
  readonly refBytes: Buffer;
  readonly nullable: boolean;
  readonly poison: boolean;
  readonly nullInput: boolean;
  readonly dupDropped: boolean;
  /**
   * The C layer found this leaf numeric/temporal but had no reference-ladder
   * entry (audit F4): the classifier treats a visible change as lossy, never
   * `reformat`. Absent from every artifact whose build gate ran.
   */
  readonly refUnclassified: boolean;
  /** True when `stored` is a genuine JSON null. */
  readonly storedIsNull: boolean;
}

/**
 * Map a document's outcome string. An outcome this binding does not recognise
 * degrades to `unsupported`, never to `rejected` (docs/reference/bindings.md
 * §RowResult, rule added 2026-08-26): a future artifact's new verdict is an
 * answer this binding cannot interpret, and `unsupported` is the arm that is
 * never scored as agreement, while a default of `rejected` would manufacture
 * an over-reject — the zero-budget failure — out of pure vocabulary drift.
 */
function outcomeOf(s: string): Outcome {
  switch (s) {
    case 'accepted':
      return 'accepted';
    case 'rejected':
      return 'rejected';
    case 'accepted_poisoned':
      return 'accepted_poisoned';
    case 'unsupported':
      return 'unsupported';
    case 'skipped':
      return 'skipped';
    default:
      return 'unsupported';
  }
}

function columnDocOf(raw: Json): ColumnDoc {
  const poison = asBool(field(raw, 'poison'));
  const inputNode = field(raw, 'input');
  const storedNode = field(raw, 'stored');
  const storedIsNull = storedNode !== undefined && storedNode.kind === 'null';
  const refNode = field(raw, 'ref');
  // Absent, or a literal null: no reference parse applies (the reference
  // implementation's `Ref()` folds both onto "").
  const refPresent = refNode !== undefined && refNode.kind !== 'null';
  // A JSON null in `stored` is a real stored value (a Nullable column holding
  // null), not the absence of one. Only the poison case has no renderable value.
  const storedPresent = storedNode !== undefined && !poison;
  return {
    name: asString(field(raw, 'name')),
    type: asString(field(raw, 'type')),
    base: asString(field(raw, 'base')),
    src: asString(field(raw, 'src')),
    input: asString(inputNode),
    refType: asString(field(raw, 'ref_type')),
    stored: storedPresent ? rawText(storedNode) : '',
    ref: refPresent ? rawText(refNode) : '',
    inputBytes: asBytes(inputNode),
    storedBytes: storedPresent ? rawBytes(storedNode) : EMPTY,
    refBytes: refPresent ? rawBytes(refNode) : EMPTY,
    nullable: asBool(field(raw, 'nullable')),
    poison,
    nullInput: asBool(field(raw, 'null_input')),
    dupDropped: asBool(field(raw, 'dup_dropped')),
    refUnclassified: asBool(field(raw, 'ref_unclassified')),
    storedIsNull,
  };
}

/** Turn one row document into a `RowResult`. */
export function rowResultOf(doc: Json): RowResult {
  const unknownFields = asStringArray(field(doc, 'unknown_fields'));
  const unsupportedSettings = asStringArray(field(doc, 'unsupported_settings'));
  let outcome = outcomeOf(asString(field(doc, 'outcome')));
  // A declined setting must never be scored as agreement.
  if (unsupportedSettings.length > 0 && outcome !== 'rejected') outcome = 'unsupported';

  const values: Value[] = [];
  const transformed: Transform[] = [];
  const substituted: Substitution[] = [];

  for (const rawCol of items(field(doc, 'cols'))) {
    const c = columnDocOf(rawCol);
    if (c.src === 'skipped') continue;
    values.push({
      column: c.name,
      text: c.stored,
      bytes: c.storedBytes,
      isNull: c.storedIsNull && !c.poison,
      source: c.src,
    });
    if (c.src === 'default_substituted') {
      substituted.push({ column: c.name, expr: c.input, text: c.stored, bytes: c.storedBytes });
    }
    transformed.push(...classify(c));
  }

  const computed: Computed[] = items(field(doc, 'computed')).map((raw) => ({
    column: asString(field(raw, 'name')),
    kind: asString(field(raw, 'kind')),
    text: rawText(field(raw, 'stored')),
    bytes: rawBytes(field(raw, 'stored')),
  }));

  return {
    values,
    transformed,
    outcome,
    errCode: asInt(field(doc, 'code')),
    errMsg: asString(field(doc, 'err')),
    unknownFields,
    unsupportedSettings,
    substituted,
    computed,
  };
}

/**
 * Turn one batch document into a `BatchResult`. `payload` is the export
 * channel's copy from the FFI layer (`null` = the ABI's `{NULL, 0}` — mapped
 * to `undefined`; a zero-length Buffer is emitted-empty and kept), which the
 * document itself never carries.
 */
export function batchResultOf(doc: Json, payload: Buffer | null = null): BatchResult {
  const rows: RowResult[] = [];
  const transformed: Transform[] = [];

  items(field(doc, 'rows')).forEach((rawRow, index) => {
    const row = rowResultOf(rawRow);
    for (const t of row.transformed) t.row = index;
    transformed.push(...row.transformed);
    rows.push(row);
  });

  // storage_transforms is what the STORAGE layer did to rows the type layer
  // accepted: ttl_expired (the row is not stored at all) and ttl_column_expired
  // (the value was reset to the column DEFAULT). Both are lossy by construction
  // and both must be folded in here.
  for (const rawSt of items(field(doc, 'storage_transforms'))) {
    const reason = asString(field(rawSt, 'reason'));
    const stored = hasField(rawSt, 'stored') ? field(rawSt, 'stored') : undefined;
    transformed.push({
      column: asString(field(rawSt, 'column')),
      input: '',
      stored: stored === undefined ? '' : rawText(stored),
      inputBytes: EMPTY,
      storedBytes: rawBytes(stored),
      reason,
      row: asInt(field(rawSt, 'row')),
      // ttl_expired and ttl_column_expired are both lossy by construction.
      lossy: isLossyReason(reason),
    });
  }

  const engineNode = field(doc, 'engine_rows');
  const enginePresent = engineNode !== undefined && engineNode.kind === 'array';
  const engineRowsBytes = enginePresent ? engineNode.items.map((r) => rawBytes(r)) : null;

  // row_spans is present exactly when export bytes were emitted;
  // export_declined exactly when an export was requested and withheld
  // (docs/reference/c-abi.md §Rows). Both absent = no export requested, or a call-level
  // verdict preempted the machinery.
  const spansNode = field(doc, 'row_spans');
  const spans =
    spansNode === undefined
      ? undefined
      : items(spansNode).map((s) => ({ off: asInt(field(s, 'off')), len: asInt(field(s, 'len')) }));
  const declinedNode = field(doc, 'export_declined');
  const exportDeclined = declinedNode === undefined ? undefined : asString(declinedNode);

  return {
    rows,
    outcome: outcomeOf(asString(field(doc, 'outcome'))),
    errCode: asInt(field(doc, 'code')),
    errMsg: asString(field(doc, 'err')),
    rowsRead: asInt(field(doc, 'rows_read')),
    rowsSkipped: asInt(field(doc, 'rows_skipped')),
    transformed,
    engineRows: engineRowsBytes === null ? null : engineRowsBytes.map((b) => b.toString('utf8')),
    engineRowsBytes,
    payload: payload ?? undefined,
    spans,
    exportDeclined,
  };
}

// ------------------------------------------------------------------ filters

/**
 * One row's answer from `Filter#rows`. Two of the four states are ANSWERS and
 * two are NOT, and the split is load-bearing: a caller enforcing visibility
 * MUST fail closed (hide the row / fail the request) on `'error'` AND
 * `'decline'` — collapsing either into `'false'`-the-answer inverts
 * fail-closed into fail-open under NOT, the measured leak class
 * (docs/reference/bindings.md §Revision 3).
 *
 * - `'true'`    the predicate is non-NULL and non-zero for this row.
 * - `'false'`   false OR NULL — SQL's three-valued logic collapsed at the
 *               WHERE boundary, computed by the vendored functions.
 * - `'error'`   the predicate THREW on this row's values (e.g. NO_COMMON_TYPE
 *               386 from `s = 257` over String). On a real server a WHERE
 *               that throws fails the WHOLE query. NOT an answer.
 * - `'decline'` this library declines to answer for this row — unparseable
 *               under the schema, poisoned, or the admission envelope
 *               tripped. NOT an answer, and the state an unknown verdict
 *               character degrades to.
 */
export type Verdict = 'true' | 'false' | 'error' | 'decline';

/** Whether a verdict is an ANSWER (`'true'`/`'false'`) rather than an error
 * or a decline. A security-enforcing caller hides the row when this is false. */
export function isAnswer(v: Verdict): boolean {
  return v === 'true' || v === 'false';
}

/**
 * The CALL-level verdict of `Filter#rows` — whether evaluation completed at
 * all; per-row failures live in the verdicts, not here. An outcome spelling
 * this binding does not recognise degrades to `'unsupported'`, never to
 * `'rejected'` (the unknown-outcome rule, docs/reference/bindings.md §RowResult).
 */
export type FilterOutcome = 'ok' | 'rejected' | 'unsupported';

/** One `'error'` or `'decline'` row, itemized: its 0-based index and the code
 * and message verbatim — ClickHouse's own for an error row, this library's
 * decline otherwise. */
export interface FilterRowError {
  readonly row: number;
  readonly code: number;
  readonly err: string;
}

/** One `Filter#rows` answer. */
export interface FilterResult {
  /** The call-level verdict. On anything but `'ok'`, `verdicts` and `errors`
   * are empty — a malformed body yields no partial answers. */
  readonly outcome: FilterOutcome;
  /** The call-level code: the server's own on `'rejected'` (an unknown
   * setting's 115, a framing error, a binary decode fault); -2 on a decline. */
  readonly errCode: number;
  readonly errMsg: string;
  /** Rows the splitter/decoder yielded. */
  readonly rowsRead: number;
  /** Non-empty means the answer must not be scored as agreement. */
  readonly unsupportedSettings: string[];
  /**
   * One verdict per row, in input order, index-aligned with the rows the
   * reader consumed — the itemized addressing contract holds even across
   * resync tails after a bad text row.
   */
  readonly verdicts: Verdict[];
  /** Every `'error'` and `'decline'` row, itemized. */
  readonly errors: FilterRowError[];
}

/** Turn one filter document into a `FilterResult`. */
export function filterResultOf(doc: Json): FilterResult {
  const rawOutcome = asString(field(doc, 'outcome'));
  // 'unsupported', and every spelling this binding does not know, degrades to
  // the decline arm — never the rejection arm.
  const outcome: FilterOutcome =
    rawOutcome === 'ok' ? 'ok' : rawOutcome === 'rejected' ? 'rejected' : 'unsupported';
  const base = {
    outcome,
    errCode: asInt(field(doc, 'code')),
    errMsg: asString(field(doc, 'err')),
    rowsRead: asInt(field(doc, 'rows_read')),
    unsupportedSettings: asStringArray(field(doc, 'unsupported_settings')),
  };
  if (outcome !== 'ok') return { ...base, verdicts: [], errors: [] };

  const verdicts: Verdict[] = [];
  for (const ch of asString(field(doc, 'verdicts'))) {
    switch (ch) {
      case 't':
        verdicts.push('true');
        break;
      case 'f':
        verdicts.push('false');
        break;
      case 'e':
        verdicts.push('error');
        break;
      default:
        // 'd', and any character this binding does not know: decline — fail
        // closed, mirroring the unknown-outcome rule.
        verdicts.push('decline');
    }
  }
  const errors: FilterRowError[] = items(field(doc, 'errors')).map((e) => ({
    row: asInt(field(e, 'row')),
    code: asInt(field(e, 'code')),
    err: asString(field(e, 'err')),
  }));
  return { ...base, verdicts, errors };
}
