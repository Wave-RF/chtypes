/**
 * The document decoders (`src/documents.ts`), over hand-written documents:
 * pure functions, so no library and no artifact is needed and nothing here can
 * skip. Each case pins one rule of `docs/reference/bindings-v1.md` §5, under
 * ABI v2's reader rules (`spec/abi-v2/docs.md`): r2 (unknown members ignored)
 * and r3 (an unlisted vocabulary value is its unknown(n), kept for that field,
 * never a failure and never a substitute).
 */

import { describe, expect, it } from 'vitest';
import {
  batchOutcomeKnown,
  DefaultKind,
  defaultKindKnown,
  DefaultSupport,
  defaultSupportKnown,
  filterOutcomeKnown,
  MergeReason,
  mergeReasonKnown,
  Outcome,
  outcomeKnown,
  Posture,
  Reason,
  reasonKnown,
  RefusalSite,
  refusalSiteKnown,
  Source,
  sourceKnown,
  verdictAnswered,
  Verdict,
  verdictKnown,
} from '../src/abi2/index.js';
import { InternalError } from '../src/abi2/errors.js';
import {
  decodeBatch,
  decodeDiscovery,
  decodeErrorCodes,
  decodeFilterResult,
  decodeLiveHandles,
  decodeRow,
  decodeSchemaDescription,
} from '../src/documents.js';

const enc = (v: unknown): Buffer => Buffer.from(JSON.stringify(v), 'utf8');
const b64 = (bytes: number[]): string => Buffer.from(bytes).toString('base64');

describe('the row document', () => {
  const doc = {
    outcome: 'accepted',
    code: 0,
    err: '',
    cols: [
      { name: 'a', null: false, input: '1', stored: '1', src: 'input' },
      { name: 'b', null: false, stored: '7', src: 'default' },
      { name: 'c', null: false, stored: '', src: 'ephemeral_input' },
      { name: 'd', null: true, src: 'skipped' },
    ],
    transformed: [{ column: 'a', input: '300', stored: '44', reason: 'overflow_wrap', row: 0 }],
    unknown_fields: [{ name: 'zz' }],
    computed: [{ name: 'm', kind: 'materialized', stored: '9', value_b64: b64([0x39]) }],
    verdict: 't',
    verdict_code: 0,
    partition_id: '202601',
    input_span: { off: 0, len: 12 },
  };

  it('keeps every entry in columns and only the stored ones in values, by the description fact', () => {
    const r = decodeRow(enc(doc));
    expect(r.outcome).toBe(Outcome.Accepted);
    expect(r.columns.map((c) => c.column.toString())).toEqual(['a', 'b', 'c', 'd']);
    expect(r.columns.map((c) => c.isStored)).toEqual([true, true, false, false]);
    expect(r.values.map((c) => c.column.toString())).toEqual(['a', 'b']);
    expect(r.columns[0]?.text.toString()).toBe('1');
    expect(r.columns[3]?.null).toBe(true);
    expect(r.columns[1]?.source).toBe(Source.Default);
  });

  it('reads transforms with their lossy fact, computed columns, the verdict, the partition and the span', () => {
    const r = decodeRow(enc(doc));
    expect(r.transformed).toHaveLength(1);
    expect(r.transformed[0]?.reason).toBe(Reason.OverflowWrap);
    expect(r.transformed[0]?.lossy).toBe(true);
    expect(r.computed[0]?.text.toString()).toBe('9');
    expect(r.unknownFields.map((b) => b.toString())).toEqual(['zz']);
    expect(r.verdict).toBe(Verdict.True);
    expect(r.partitionId?.toString()).toBe('202601');
    expect(r.computed[0]?.value?.toString()).toBe('9');
    expect(r.inputSpan).toEqual({ off: 0, len: 12 });
  });

  it('reads a non-lossy reason as not lossy, and an unlisted reason as its unknown(n), keeping its spelling, with the fallback\'s lossy fact (value_changed: lossy)', () => {
    const r = decodeRow(
      enc({
        ...doc,
        transformed: [
          { column: 'a', input: 'x', stored: 'x', reason: 'reformat' },
          { column: 'a', input: 'x', stored: 'y', reason: 'a_reason_from_the_future' },
        ],
      }),
    );
    expect(r.transformed[0]?.lossy).toBe(false);
    expect(r.transformed[1]?.reason).toBe('a_reason_from_the_future');
    expect(reasonKnown(r.transformed[1]?.reason ?? '')).toBe(false);
    expect(r.transformed[1]?.lossy).toBe(true);
  });

  it('keeps an unlisted outcome as its unknown(n): never the fallback, never accepted (r3)', () => {
    const r = decodeRow(enc({ ...doc, outcome: 'maybe' }));
    expect(r.outcome).toBe('maybe');
    expect(outcomeKnown(r.outcome)).toBe(false);
    expect(r.outcome).not.toBe(Outcome.Accepted);
    expect(r.columns).toHaveLength(4); // the rest of the row decoded
  });

  it('keeps an unlisted source as its unknown(n) for that column alone: not stored, and the row decodes (r3)', () => {
    const r = decodeRow(enc({ ...doc, cols: [{ name: 'a', null: false, stored: '1', src: 'telepathy' }, ...doc.cols] }));
    expect(r.columns[0]?.source).toBe('telepathy');
    expect(sourceKnown(r.columns[0]?.source ?? '')).toBe(false);
    expect(r.columns[0]?.isStored).toBe(false);
    expect(r.columns.slice(1).map((c) => c.source)).toEqual([Source.Input, Source.Default, Source.EphemeralInput, Source.Skipped]);
    expect(r.outcome).toBe(Outcome.Accepted);
  });

  it('refuses a column that carries no source at all: an absent src is no value, not an unknown one', () => {
    const bad = { ...doc, cols: [{ name: 'a', null: false, stored: '1' }] };
    expect(() => decodeRow(enc(bad))).toThrow(InternalError);
  });

  it('leaves absent fields at their defaults and ignores unknown keys', () => {
    const r = decodeRow(enc({ outcome: 'rejected', code: 27, err: 'boom', brand_new_field: 1 }));
    expect(r.outcome).toBe(Outcome.Rejected);
    expect(r.errCode).toBe(27);
    expect(r.errMsg.toString()).toBe('boom');
    expect(r.columns).toEqual([]);
    expect(r.verdict).toBeUndefined();
    expect(r.inputSpan).toBeUndefined();
    expect(r.partitionId).toBeUndefined();
  });
});

describe('names and data-derived strings are bytes', () => {
  it('surfaces name_b64 as the decoded raw bytes, including a NUL and an invalid UTF-8 byte', () => {
    const raw = [0x61, 0x00, 0xff, 0x7a];
    const r = decodeRow(enc({ outcome: 'accepted', cols: [{ name_b64: b64(raw), null: false, stored: '1', src: 'input' }] }));
    expect([...(r.columns[0]?.column ?? [])]).toEqual(raw);
  });

  it('surfaces a plain name carrying a NUL (written as an escape) as the same bytes', () => {
    const r = decodeRow(Buffer.from('{"outcome":"accepted","cols":[{"name":"a\\u0000b","null":false,"src":"input"}]}'));
    expect([...(r.columns[0]?.column ?? [])]).toEqual([0x61, 0x00, 0x62]);
  });

  it('surfaces a stored value and a message sent as <field>_b64 as bytes, and value_b64 as the raw value bytes', () => {
    const raw = [0xde, 0xad, 0xbe, 0xef];
    const r = decodeRow(
      enc({
        outcome: 'rejected',
        err_b64: b64(raw),
        cols: [{ name: 'a', null: false, stored_b64: b64(raw), value_b64: b64([0x01, 0xff]), src: 'input' }],
      }),
    );
    expect([...r.errMsg]).toEqual(raw);
    expect([...(r.columns[0]?.text ?? [])]).toEqual(raw);
    expect([...(r.columns[0]?.value ?? [])]).toEqual([0x01, 0xff]);
  });

  it('leaves value undefined when the document carries no raw value bytes', () => {
    const r = decodeRow(enc({ outcome: 'accepted', cols: [{ name: 'a', null: false, stored: '1', src: 'input' }] }));
    expect(r.columns[0]?.value).toBeUndefined();
  });

  it('refuses a name carried twice (both spellings), and a name carried not at all', () => {
    const both = { outcome: 'accepted', cols: [{ name: 'a', name_b64: b64([0x61]), null: false, src: 'input' }] };
    const neither = { outcome: 'accepted', cols: [{ null: false, src: 'input' }] };
    expect(() => decodeRow(enc(both))).toThrow(InternalError);
    expect(() => decodeRow(enc(neither))).toThrow(InternalError);
  });

  it('refuses a malformed base64 value', () => {
    expect(() => decodeRow(enc({ outcome: 'accepted', cols: [{ name_b64: '%%%', null: false, src: 'input' }] }))).toThrow(InternalError);
  });
});

describe('JSON strictness', () => {
  it('refuses a duplicate key, at any depth, as an InternalError naming the document', () => {
    const dup = Buffer.from('{"outcome":"accepted","cols":[{"name":"a","name":"b","null":false,"src":"input"}]}');
    expect(() => decodeRow(dup)).toThrow(InternalError);
    expect(() => decodeRow(dup)).toThrow(/row document/);
  });

  it('refuses a document that is not JSON, and one of the wrong shape', () => {
    expect(() => decodeRow(Buffer.from('not json'))).toThrow(InternalError);
    expect(() => decodeRow(Buffer.from('[]'))).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'accepted', cols: 'nope' }))).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'accepted', code: 'x' }))).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'accepted', cols: [{ name: 'a', null: 'no', src: 'input' }] }))).toThrow(InternalError);
  });

  it('reads an integer spelled as a string, and refuses one beyond 2^53 rather than rounding it', () => {
    expect(decodeBatch(enc({ outcome: 'accepted', rows_read: '12' }), undefined).rowsRead).toBe(12);
    expect(() => decodeBatch(enc({ outcome: 'accepted', rows_read: '18446744073709551615' }), undefined)).toThrow(InternalError);
  });
});

describe('the batch document', () => {
  it('decodes rows, the flat transform list, spans, the export payload and the counts', () => {
    const payload = Buffer.from('{"a":1}\n');
    const b = decodeBatch(
      enc({
        outcome: 'accepted',
        rows: [{ outcome: 'accepted', cols: [{ name: 'a', null: false, stored: '1', src: 'input' }], input_span: { off: 0, len: 8 } }],
        rows_read: 1,
        rows_skipped: 0,
        transformed: [{ column: 'a', input: '1', stored: '1', reason: 'reformat', row: 0 }],
        engine_rows: [[{ name: 'a', stored: '1', null: false, value_b64: b64([0x31]) }, { name_b64: b64([0xff]), stored_b64: b64([0x00, 0x80]), null: false }]],
        row_spans: [{ off: 0, len: 8 }],
        export_declined: '',
        rows_passed: 1,
        rows_cut: 0,
        partition_count: 1,
        unconsumed: [{ off: 9, len: 3 }],
        framing: { bom_skipped: false, container: 'stream', header: null },
      }),
      payload,
    );
    expect(b.outcome).toBe(Outcome.Accepted);
    expect(b.rows).toHaveLength(1);
    expect(b.rows[0]?.values).toHaveLength(1);
    expect(b.transformed[0]?.lossy).toBe(false);
    expect(b.engineRows).toHaveLength(1);
    const cells = b.engineRows?.[0] ?? [];
    expect(cells[0]?.text.toString()).toBe('1');
    expect(cells[0]?.value?.toString()).toBe('1');
    expect([...(cells[1]?.column ?? [])]).toEqual([0xff]);
    expect([...(cells[1]?.text ?? [])]).toEqual([0x00, 0x80]);
    expect(cells[1]?.value).toBeUndefined();
    expect(b.payload).toBe(payload);
    expect(b.spans).toEqual([{ off: 0, len: 8 }]);
    expect(b.partitionCount).toBe(1);
    expect(b.unconsumed).toEqual([{ off: 9, len: 3 }]);
  });

  it('reads framing unknown as null, never false and never empty', () => {
    const b = decodeBatch(enc({ outcome: 'accepted', framing: { bom_skipped: null, container: null, header: null } }), undefined);
    expect(b.framing?.bomSkipped).toBeNull();
    expect(b.framing?.header).toBeNull();
    expect(b.framing?.container).toBeNull();
  });

  it('reads a known framing with its header names as bytes', () => {
    const b = decodeBatch(
      enc({
        outcome: 'accepted',
        framing: { bom_skipped: true, container: 'array', header: { consumed: true, lines: 1, names: [{ name: 'a' }, { name_b64: b64([0xff]) }] } },
      }),
      undefined,
    );
    expect(b.framing?.bomSkipped).toBe(true);
    expect(b.framing?.header?.consumed).toBe(true);
    expect(b.framing?.header?.lines).toBe(1);
    expect([...(b.framing?.header?.names[1] ?? [])]).toEqual([0xff]);
  });

  it('has no framing, no export and no engine rows when the document carries none', () => {
    const b = decodeBatch(enc({ outcome: 'rejected', code: 27 }), undefined);
    expect(b.framing).toBeUndefined();
    expect(b.payload).toBeUndefined();
    expect(b.spans).toBeUndefined();
    expect(b.engineRows).toBeUndefined();
    expect(b.partitionCount).toBeUndefined();
  });

  it('keeps an outcome the batch vocabulary does not list as its unknown(n), never accepted (r3)', () => {
    const b = decodeBatch(enc({ outcome: 'skipped', rows: [{ outcome: 'accepted', cols: [] }] }), undefined);
    expect(b.outcome).toBe('skipped');
    expect(batchOutcomeKnown(b.outcome)).toBe(false);
    expect(b.rows[0]?.outcome).toBe(Outcome.Accepted);
  });
});

// ABI v2's at_merge, in every shape the spec gives an entry, and the batch's
// own unsupported_settings (public issue #544).
const AT_MERGE_BATCH = {
  outcome: 'accepted',
  unsupported_settings: [{ name: 'session_timezone' }, { name_b64: b64([0xff]) }],
  at_merge: [
    { part_row: 0, reason: 'ttl_delete' },
    { part_row: 1, reason: 'ttl_column_reset', column: 'c', stored: '0' },
    { part_row: 2, reason: 'ttl_column_reset', column: 't' },
    { part_row: 3, reason: 'ttl_column_reset', column_b64: b64([0xff, 0x00]), stored_b64: b64([0xff]) },
    { part_row: 4, reason: 'ttl_delete', input_rows: [4, 7] },
    { part_row: 5, reason: 'a_reason_nobody_listed', x_future: { a: [1] }, x_future_b64: '/w==' },
    { part_row: 6, reason: 'ttl_column_reset', column: 'e', stored: '', input_rows: [] },
  ],
};

describe('the batch document: at_merge and the batch\'s own unsupported_settings (ABI v2)', () => {
  const bytes = (b: Buffer | undefined): number[] | undefined => (b === undefined ? undefined : [...b]);

  it('decodes each at_merge entry one to one', () => {
    const got = decodeBatch(enc(AT_MERGE_BATCH), undefined).atMerge.map((e) => ({
      row: e.partRow,
      reason: e.reason,
      known: mergeReasonKnown(e.reason),
      column: bytes(e.column),
      stored: bytes(e.stored),
      inputRows: e.inputRows,
    }));
    expect(got).toEqual([
      { part_row: 0, reason: MergeReason.TtlDelete, known: true, column: undefined, stored: undefined, inputRows: undefined },
      { part_row: 1, reason: MergeReason.TtlColumnReset, known: true, column: [0x63], stored: [0x30], inputRows: undefined },
      // stored absent: the column's DEFAULT is decided at the merge
      { part_row: 2, reason: MergeReason.TtlColumnReset, known: true, column: [0x74], stored: undefined, inputRows: undefined },
      // both by their _b64 form
      { part_row: 3, reason: MergeReason.TtlColumnReset, known: true, column: [0xff, 0x00], stored: [0xff], inputRows: undefined },
      { part_row: 4, reason: MergeReason.TtlDelete, known: true, column: undefined, stored: undefined, inputRows: [4, 7] },
      // an unlisted reason is its unknown(n), kept verbatim, and the unknown members are ignored (r2, r3)
      { part_row: 5, reason: 'a_reason_nobody_listed', known: false, column: undefined, stored: undefined, inputRows: undefined },
      // an empty stored is not an absent one
      { part_row: 6, reason: MergeReason.TtlColumnReset, known: true, column: [0x65], stored: [], inputRows: [] },
    ]);
  });

  it("reads the batch's own unsupported_settings as name objects, as bytes", () => {
    const b = decodeBatch(enc(AT_MERGE_BATCH), undefined);
    expect(b.unsupportedSettings.map((n) => [...n])).toEqual([[...Buffer.from('session_timezone')], [0xff]]);
  });

  it('reads an absent or empty at_merge and unsupported_settings as empty', () => {
    for (const extra of [{}, { at_merge: [], unsupported_settings: [] }]) {
      const b = decodeBatch(enc({ outcome: 'accepted', ...extra }), undefined);
      expect(b.atMerge).toEqual([]);
      expect(b.unsupportedSettings).toEqual([]);
    }
  });

  it('refuses an at_merge that breaks its schema as an InternalError', () => {
    for (const bad of [
      { at_merge: [{ part_row: 0, reason: 'ttl_column_reset', column: 'c', column_b64: 'Yw==' }] },
      { at_merge: [{ part_row: 0, reason: 'ttl_column_reset', column: 'c', stored_b64: '!!' }] },
      { at_merge: [{ part_row: 0, reason: 'ttl_delete', input_rows: ['x'] }] },
      { at_merge: [{ part_row: 0, reason: 'ttl_delete', input_rows: [-1] }] },
      { at_merge: [{ part_row: 0, reason: 7 }] },
      { at_merge: { row: 0 } },
      { unsupported_settings: ['session_timezone'] },
    ]) {
      expect(() => decodeBatch(enc(bad), undefined), JSON.stringify(bad)).toThrow(InternalError);
    }
  });
});

describe('the filter result document', () => {
  it('maps each verdict character through the table, reading e and d as not answers', () => {
    const f = decodeFilterResult(enc({ outcome: 'ok', rows_read: 4, verdicts: 'tfed', errors: [{ row: 2, code: 70, err: 'bad' }] }));
    expect(f.verdicts).toEqual([Verdict.True, Verdict.False, Verdict.Error, Verdict.Decline]);
    expect(f.verdicts.map((v) => verdictAnswered(v))).toEqual([true, true, false, false]);
    expect(f.errors).toHaveLength(1);
    expect(f.errors[0]?.msg.toString()).toBe('bad');
  });

  it('keeps an unlisted verdict character as its unknown(n), never an answer (the fallback d\'s fact), the others intact (r3)', () => {
    const f = decodeFilterResult(enc({ outcome: 'ok', verdicts: 'tz' }));
    expect(f.verdicts).toEqual([Verdict.True, 'z']);
    expect(verdictKnown(f.verdicts[1] ?? '')).toBe(false);
    expect(verdictAnswered(f.verdicts[1] ?? '')).toBe(false);
  });

  it('keeps an unlisted filter outcome as its unknown(n), never ok (r3)', () => {
    const f = decodeFilterResult(enc({ outcome: 'maybe', verdicts: 't' }));
    expect(f.outcome).toBe('maybe');
    expect(filterOutcomeKnown(f.outcome)).toBe(false);
    expect(f.verdicts).toEqual([Verdict.True]);
  });

  it('keeps a non-ok result as the document holds it', () => {
    const f = decodeFilterResult(enc({ outcome: 'unsupported', code: 0, err: 'no', verdicts: '' }));
    expect(f.outcome).toBe('unsupported');
    expect(f.verdicts).toEqual([]);
  });
});

describe('the schema description, discovery, error-code and live-handle documents', () => {
  it('decodes the columns with their default kind as a vocabulary value', () => {
    const d = decodeSchemaDescription(
      enc({
        columns: [
          { name: 'a', type: 'Int32', default_kind: '', default_expression: '' },
          { name_b64: b64([0xff]), type: 'String', default_kind: 'MATERIALIZED', default_expression: 'lower(a)' },
        ],
      }),
    );
    expect(d.columns[0]?.defaultKind).toBe(DefaultKind.None);
    expect(d.columns[1]?.defaultKind).toBe(DefaultKind.Materialized);
    expect(d.columns[1]?.defaultExpr.toString()).toBe('lower(a)');
    expect([...(d.columns[1]?.name ?? [])]).toEqual([0xff]);
  });

  it('keeps a default kind the vocabulary does not list as its unknown(n), the column decoded (r3)', () => {
    const d = decodeSchemaDescription(enc({ columns: [{ name: 'a', type: 'Int32', default_kind: 'MAGIC' }] }));
    expect(d.columns[0]?.defaultKind).toBe('MAGIC');
    expect(defaultKindKnown(d.columns[0]?.defaultKind ?? '')).toBe(false);
    expect(d.columns[0]?.name.toString()).toBe('a');
  });

  it('decodes discovered columns', () => {
    const d = decodeDiscovery(enc({ columns: [{ name: 'a', declaration: '`a` Int32' }], columns_sql_b64: b64([0xff, 0x61]) }));
    expect(d.columns[0]?.declaration.toString()).toBe('`a` Int32');
    expect([...d.columnsSql]).toEqual([0xff, 0x61]);
  });

  it('builds the error-code table from the library entries only: exact names, unknown absent', () => {
    const t = decodeErrorCodes(enc([{ code: 27, name: 'CANNOT_PARSE_INPUT_ASSERTION_FAILED' }, { code: 60, name: 'UNKNOWN_TABLE' }]));
    expect(t.name(60)).toBe('UNKNOWN_TABLE');
    expect(t.code('UNKNOWN_TABLE')).toBe(60);
    expect(t.name(1)).toBeUndefined();
    expect(t.code('unknown_table')).toBeUndefined();
    expect(t.all()).toHaveLength(2);
    expect([...t].map((e) => e.code)).toEqual([27, 60]);
  });

  it('refuses an error-code table that is not an array', () => {
    expect(() => decodeErrorCodes(enc({ code: 1 }))).toThrow(InternalError);
  });

  it('decodes the live handle counts', () => {
    expect(decodeLiveHandles(enc({ chs_schema: 2, chs_filter: 0 }))).toEqual({ chs_schema: 2, chs_filter: 0 });
  });
});

describe('name lists and engine rows (rule 5)', () => {
  it('reads unknown_fields and unsupported_settings as name objects in either form', () => {
    const r = decodeRow(enc({ outcome: 'accepted', unknown_fields: [{ name: 'a' }, { name_b64: b64([0xff, 0x00]) }] }));
    expect(r.unknownFields.map((b) => [...b])).toEqual([[0x61], [0xff, 0x00]]);
    const b = decodeBatch(enc({ outcome: 'accepted', unsupported_settings: [{ name: 'x' }], export_columns: [{ name: 'a' }, { name_b64: b64([0xff]) }] }), undefined);
    expect(b.unsupportedSettings.map((n) => n.toString())).toEqual(['x']);
    expect(b.exportColumns?.map((n) => [...n])).toEqual([[0x61], [0xff]]);
  });

  it('refuses a bare string in a name list, an element carrying both forms, and one carrying neither', () => {
    expect(() => decodeRow(enc({ outcome: 'accepted', unknown_fields: ['zz'] }))).toThrow(InternalError);
    expect(() => decodeBatch(enc({ outcome: 'accepted', unsupported_settings: ['zz'] }), undefined)).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'accepted', unknown_fields: [{ name: 'a', name_b64: 'YQ==' }] }))).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'accepted', unknown_fields: [{}] }))).toThrow(InternalError);
  });

  it('refuses an engine row that is an object keyed by name, or a cell with no name', () => {
    expect(() => decodeBatch(enc({ outcome: 'accepted', engine_rows: [{ a: '1' }] }), undefined)).toThrow(InternalError);
    expect(() => decodeBatch(enc({ outcome: 'accepted', engine_rows: [[{ stored: '1', null: false }]] }), undefined)).toThrow(InternalError);
  });

  it('reads export_declined as bytes from either form: text, or base64 decoded to its exact bytes', () => {
    const text = decodeBatch(enc({ outcome: 'accepted', export_declined: 'no export for this format' }), undefined);
    expect(text.exportDeclined.toString('utf8')).toBe('no export for this format');
    const raw = [0xff, 0x00, 0xc3, 0x28, 0x80];
    const b64Form = decodeBatch(enc({ outcome: 'accepted', export_declined_b64: b64(raw) }), undefined);
    expect([...b64Form.exportDeclined]).toEqual(raw);
    expect(decodeBatch(enc({ outcome: 'accepted' }), undefined).exportDeclined.length).toBe(0);
  });

  it('a declined export has no payload and no spans, whatever the buffer; an accepted zero-row export is present and empty', () => {
    // The library returns a NULL out_export for a declined export (builds up to 20261011.105019 returned a
    // non-NULL, zero-length buffer): the document decides.
    for (const member of [{ export_declined: 'only accepted batches export' }, { export_declined_b64: b64([0xff]) }]) {
      for (const buffer of [Buffer.alloc(0), Buffer.from('x')]) {
        const declined = decodeBatch(enc({ outcome: 'rejected', row_spans: [{ off: 0, len: 1 }], ...member }), buffer);
        expect(declined.payload).toBeUndefined();
        expect(declined.spans).toBeUndefined();
        expect(declined.exportDeclined.length).toBeGreaterThan(0);
      }
    }
    const empty = decodeBatch(enc({ outcome: 'accepted', row_spans: [], export_declined: '' }), Buffer.alloc(0));
    expect(empty.payload).toBeDefined();
    expect(empty.payload?.length).toBe(0);
    expect(empty.spans).toEqual([]);
    expect(decodeBatch(enc({ outcome: 'accepted' }), undefined).payload).toBeUndefined();
  });

  it('refuses an export_declined given in both forms (InternalError)', () => {
    expect(() => decodeBatch(enc({ outcome: 'accepted', export_declined: 'x', export_declined_b64: b64([0x78]) }), undefined)).toThrow(InternalError);
  });

  it('reads the message, partition and filter setting names as bytes from either form', () => {
    const r = decodeRow(enc({ outcome: 'rejected', err_b64: b64([0xc3]), verdict_err_b64: b64([0xfe]), partition_id_b64: b64([0xff]) }));
    expect([...r.errMsg]).toEqual([0xc3]);
    expect([...r.verdictErr]).toEqual([0xfe]);
    expect([...(r.partitionId ?? [])]).toEqual([0xff]);
    const f = decodeFilterResult(enc({ outcome: 'ok', unsupported_settings: [{ name_b64: b64([0xff]) }] }));
    expect([...(f.unsupportedSettings[0] ?? [])]).toEqual([0xff]);
  });
});

// ABI v2 spec batch 2 (the sixth interface batch): a refusal or decline names its code (-2 is
// CHS_CODE_DECLINED), its catalog id and where it sits.
describe('spec batch 2: the refusal members (reason, refused_at, refused_row)', () => {
  it('reads a declined batch: code -2, its catalog id, the site and the row', () => {
    const b = decodeBatch(
      enc({ outcome: 'unsupported', code: -2, reason: 'preview-batch-zone-mismatch', refused_at: 'body_read', refused_row: 3, rows_read: 0, rows: [] }),
      undefined,
    );
    expect(b.errCode).toBe(-2);
    expect(b.reason).toBe('preview-batch-zone-mismatch');
    expect(b.refusedAt).toBe(RefusalSite.BodyRead);
    expect(refusalSiteKnown(b.refusedAt ?? '')).toBe(true);
    expect(b.refusedRow).toBe(3);
  });

  it('an accepted batch carries no refusal members: they are absent, never empty', () => {
    const b = decodeBatch(enc({ outcome: 'accepted', code: 0, rows_read: 1 }), undefined);
    expect(b.reason).toBeUndefined();
    expect(b.refusedAt).toBeUndefined();
    expect(b.refusedRow).toBeUndefined();
  });

  it('keeps an unlisted site as its unknown(n), never a failure (r3)', () => {
    const b = decodeBatch(enc({ outcome: 'rejected', refused_at: 'a_site_nobody_listed' }), undefined);
    expect(b.refusedAt).toBe('a_site_nobody_listed');
    expect(refusalSiteKnown(b.refusedAt ?? '')).toBe(false);
  });

  it('reads a row: code, reason, site, row, and the declined verdict with its own catalog id', () => {
    const r = decodeRow(
      enc({ outcome: 'unsupported', code: -2, reason: 'drawn-value', refused_at: 'row', refused_row: 0, verdict: 'd', verdict_code: -2, verdict_reason: 'defer-defaults-reader' }),
    );
    expect(r.errCode).toBe(-2);
    expect(r.reason).toBe('drawn-value');
    expect(r.refusedAt).toBe(RefusalSite.Row);
    expect(r.refusedRow).toBe(0);
    expect(r.verdict).toBe(Verdict.Decline);
    expect(r.verdictCode).toBe(-2);
    expect(r.verdictReason).toBe('defer-defaults-reader');
    const ok = decodeRow(enc({ outcome: 'accepted', code: 0 }));
    expect([ok.reason, ok.refusedAt, ok.refusedRow, ok.verdictReason]).toEqual([undefined, undefined, undefined, undefined]);
  });

  it('reads a filter result: code, reason, site, and an errors entry with its own reason', () => {
    const f = decodeFilterResult(
      enc({ outcome: 'unsupported', code: -2, reason: 'filter-zone', refused_at: 'call', rows_read: 0, verdicts: '', errors: [{ row: 0, code: -2, reason: 'drawn-value', err: 'x' }], unsupported_settings: [] }),
    );
    expect(f.errCode).toBe(-2);
    expect(f.reason).toBe('filter-zone');
    expect(f.refusedAt).toBe(RefusalSite.Call);
    expect(f.refusedRow).toBeUndefined();
    expect(f.errors[0]?.code).toBe(-2);
    expect(f.errors[0]?.reason).toBe('drawn-value');
    expect(decodeFilterResult(enc({ outcome: 'ok', errors: [{ row: 1, code: 70 }] })).errors[0]?.reason).toBeUndefined();
  });

  it('a member of the wrong JSON type is an InternalError', () => {
    expect(() => decodeBatch(enc({ outcome: 'rejected', refused_row: 'x' }), undefined)).toThrow(InternalError);
    expect(() => decodeRow(enc({ outcome: 'rejected', reason: 7 }))).toThrow(InternalError);
  });
});

describe('spec batch 2: the export members (row_spans, export_columns, export_declined_reason)', () => {
  it('row_spans is one entry per row, a null for a row not exported, aligned with rows; export_columns are bytes', () => {
    const b = decodeBatch(
      enc({ outcome: 'accepted', rows_read: 3, row_spans: [{ off: 0, len: 5 }, null, { off: 5, len: 6 }], export_columns: [{ name: 'a' }, { name_b64: b64([0xff]) }] }),
      Buffer.from('0123456789a'),
    );
    expect(b.spans).toEqual([{ off: 0, len: 5 }, null, { off: 5, len: 6 }]);
    expect(b.exportColumns?.map((n) => [...n])).toEqual([[0x61], [0xff]]);
    expect(b.exportDeclinedReason).toBeUndefined();
  });

  it('a declined export has no payload, no spans and no export columns, and names its reason', () => {
    const d = decodeBatch(
      enc({ outcome: 'unsupported', export_declined: 'nope', export_declined_reason: 'export-batch-not-accepted', row_spans: [null], export_columns: [{ name: 'a' }] }),
      Buffer.from('x'),
    );
    expect(d.exportDeclined.toString()).toBe('nope');
    expect(d.exportDeclinedReason).toBe('export-batch-not-accepted');
    expect(d.payload).toBeUndefined();
    expect(d.spans).toBeUndefined();
    expect(d.exportColumns).toBeUndefined();
  });

  it('no export asked for: none of the export members', () => {
    const b = decodeBatch(enc({ outcome: 'accepted' }), undefined);
    expect([b.spans, b.exportColumns, b.exportDeclinedReason]).toEqual([undefined, undefined, undefined]);
  });
});

describe('spec batch 2: the schema description members', () => {
  const described = decodeSchemaDescription(
    enc({
      columns: [
        { name: 'a', type: 'Int32', default_kind: '', default_support: 'deferred', default_support_reason: 'r-x', reads: [{ name: 'b' }, { name_b64: b64([0xff]) }] },
        { name: 'b', type: 'Int32', default_kind: 'DEFAULT', default_support: 'a_support_nobody_listed' },
      ],
      filter_declined_settings: [],
      posture: 'load',
      server: { timezone: 'UTC', settings: { s: 'v', t: { b64: b64([0xff]) } } },
    }),
  );

  it('reads the posture, and each column\'s default support, its reason and the columns it reads', () => {
    expect(described.posture).toBe(Posture.Load);
    expect(described.columns[0]?.defaultSupport).toBe(DefaultSupport.Deferred);
    expect(described.columns[0]?.defaultSupportReason).toBe('r-x');
    expect(described.columns[0]?.reads?.map((n) => [...n])).toEqual([[0x62], [0xff]]);
  });

  it('keeps an unlisted default support as its unknown(n); an absent reads is undefined', () => {
    const c = described.columns[1];
    expect(c?.defaultSupport).toBe('a_support_nobody_listed');
    expect(defaultSupportKnown(c?.defaultSupport ?? '')).toBe(false);
    expect(c?.reads).toBeUndefined();
    expect(c?.defaultSupportReason).toBeUndefined();
  });

  it('reads a server settings value in the {"b64": ...} form as its bytes', () => {
    expect(described.server?.settings?.s).toBe('v');
    expect([...((described.server?.settings?.t as Buffer | undefined) ?? [])]).toEqual([0xff]);
  });

  it('a create-posture schema and a build that reports no default support carry none of the members', () => {
    const d = decodeSchemaDescription(enc({ columns: [{ name: 'a', type: 'Int32', default_kind: '' }], filter_declined_settings: [] }));
    expect(d.posture).toBeUndefined();
    expect(d.columns[0]?.defaultSupport).toBeUndefined();
    expect(d.columns[0]?.reads).toBeUndefined();
  });

  it('a b64 value that is not the one-member object form is an InternalError', () => {
    expect(() => decodeSchemaDescription(enc({ columns: [], server: { settings: { t: { b64: b64([0xff]), x: 1 } } } }))).toThrow(InternalError);
    expect(() => decodeSchemaDescription(enc({ columns: [], server: { settings: { t: { b64: 7 } } } }))).toThrow(InternalError);
    expect(() => decodeSchemaDescription(enc({ columns: [], server: { settings: { t: 7 } } }))).toThrow(InternalError);
  });
});

describe('spec batch 2: value_src gains default_deferred and default_override', () => {
  it('reads their stored facts from the description: deferred is not stored, an override is', () => {
    const r = decodeRow(
      enc({
        outcome: 'accepted',
        code: 0,
        cols: [
          { name: 'a', null: false, stored: '', src: 'default_deferred' },
          { name: 'b', null: false, stored: '5', src: 'default_override' },
        ],
      }),
    );
    expect(r.columns.map((c) => c.isStored)).toEqual([false, true]);
    expect(r.values.map((c) => c.column.toString())).toEqual(['b']);
  });
});
