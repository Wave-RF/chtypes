/**
 * The document decoders (`src/documents.ts`), over hand-written documents:
 * pure functions, so no library and no artifact is needed and nothing here can
 * skip. Each case pins one rule of `docs/reference/bindings-v1.md` §5.
 */

import { describe, expect, it } from 'vitest';
import { DefaultKind, Outcome, Reason, Source, verdictAnswered, Verdict } from '../src/abi1/index.js';
import { InternalError } from '../src/abi1/errors.js';
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
    unknown_fields: ['zz'],
    unsupported_settings: [],
    computed: [{ name: 'm', kind: 'materialized', stored: '9' }],
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
    expect(r.partitionId).toBe('202601');
    expect(r.inputSpan).toEqual({ off: 0, len: 12 });
  });

  it('reads a non-lossy reason as not lossy, and an unlisted reason as the fallback (value_changed: lossy), keeping its spelling', () => {
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
    expect(r.transformed[1]?.lossy).toBe(true);
  });

  it('reads an unlisted outcome as the description fallback, unsupported', () => {
    expect(decodeRow(enc({ ...doc, outcome: 'maybe' })).outcome).toBe(Outcome.Unsupported);
  });

  it('refuses a source the vocabulary does not list: there is no is_stored fact to read', () => {
    const bad = { ...doc, cols: [{ name: 'a', null: false, stored: '1', src: 'telepathy' }] };
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
        engine_rows: ['1'],
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
    expect(b.engineRows?.map((e) => e.toString())).toEqual(['1']);
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

  it('refuses an outcome the batch vocabulary does not list only if it has no fallback (it has one: unsupported)', () => {
    expect(decodeBatch(enc({ outcome: 'skipped' }), undefined).outcome).toBe(Outcome.Unsupported);
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

  it('reads an unlisted verdict character as the fallback, the decline', () => {
    expect(decodeFilterResult(enc({ outcome: 'ok', verdicts: 'tz' })).verdicts[1]).toBe(Verdict.Decline);
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
          { name: 'a', type: 'Int32', default_kind: '', default_expr: '' },
          { name_b64: b64([0xff]), type: 'String', default_kind: 'MATERIALIZED', default_expr: 'lower(a)' },
        ],
      }),
    );
    expect(d.columns[0]?.defaultKind).toBe(DefaultKind.None);
    expect(d.columns[1]?.defaultKind).toBe(DefaultKind.Materialized);
    expect(d.columns[1]?.defaultExpr.toString()).toBe('lower(a)');
    expect([...(d.columns[1]?.name ?? [])]).toEqual([0xff]);
  });

  it('refuses a default kind the vocabulary does not list', () => {
    expect(() => decodeSchemaDescription(enc({ columns: [{ name: 'a', type: 'Int32', default_kind: 'MAGIC' }] }))).toThrow(InternalError);
  });

  it('decodes discovered columns', () => {
    const d = decodeDiscovery(enc({ columns: [{ name: 'a', declaration: '`a` Int32' }] }));
    expect(d.columns[0]?.declaration.toString()).toBe('`a` Int32');
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
