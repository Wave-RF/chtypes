/**
 * The standing projection gate (ABI v2): every case of
 * `tests/fixtures/abi-v2/insert-projection.json`, decoded with this binding's
 * REAL decoders (`decodeBatch`, `decodeInsert`), through `projectionVerdict`,
 * which mirrors `scripts/abi-v2/check-insert-projection.py` rule by rule. The
 * fixture's planted reds prove the comparator can fail; the decoder is proved
 * by the same run, since a decoder that dropped a member would change a verdict.
 * No library is needed. The fixture lives outside this package, so a bare copy of
 * `ts/` does not have it: then the suite below is skipped by name (and says so on
 * stderr); a fixture that is present but unreadable or malformed still fails.
 * Where CHTYPES_REQUIRE_FIXTURES is "1" (the repository's main CI) an absent
 * fixture fails instead of skipping.
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { type BatchResult, decodeBatch, decodeInsert } from '../../src/documents.js';
import { type BatchOfTRows, type BodyRecord, EXCEPTIONS, projectionVerdict, RULES } from './insert-projection-rule.js';

interface Case {
  readonly id: string;
  readonly output_format: string;
  readonly batch: unknown;
  readonly batch_export_b64: string | null;
  readonly insert: unknown;
  readonly insert_rows_b64: string | null;
  readonly batch_of_t_rows?: { readonly rows: readonly number[]; readonly batch: unknown; readonly export_b64: string | null };
  readonly body_records?: readonly unknown[];
  readonly failing_t_record?: unknown;
  readonly provenance?: Record<string, unknown>;
  readonly expect: 'equal' | { readonly exception: string } | { readonly violates: string };
}

const FIXTURE = path.resolve(import.meta.dirname, '../../../tests/fixtures/abi-v2/insert-projection.json');
const present = existsSync(FIXTURE);
if (!present) {
  console.error(`SKIP: insert projection fixture absent (${FIXTURE}); the projection gate did not run (a bare copy of ts/ does not carry tests/fixtures)`);
}
const doc = (present ? JSON.parse(readFileSync(FIXTURE, 'utf8')) : { schema: '', cases: [] }) as { schema: string; cases: readonly Case[] };

const bytes = (b64: string | null): Buffer | undefined => (b64 === null ? undefined : Buffer.from(b64, 'base64'));

/** provenance names exactly the case's documents, each recorded (`{build, line}`) or marked; a malformed one is a fixture error. */
function checkProvenance(c: Case): void {
  const prov = c.provenance;
  if (prov === undefined) {
    if (c.batch_of_t_rows !== undefined) throw new Error(`${c.id}: batch_of_t_rows without provenance`);
    return;
  }
  const want = ['batch', 'insert', ...(c.batch_of_t_rows === undefined ? [] : ['batch_of_t_rows'])].sort();
  if (JSON.stringify(Object.keys(prov).sort()) !== JSON.stringify(want)) throw new Error(`${c.id}: provenance does not name exactly ${want.join(', ')}`);
  for (const [k, v] of Object.entries(prov)) {
    const recorded =
      typeof v === 'object' &&
      v !== null &&
      JSON.stringify(Object.keys(v).sort()) === '["build","line"]' &&
      Object.values(v).every((x) => typeof x === 'string' && x.length > 0);
    if (!(recorded || v === 'unrecorded' || v === 'planted')) throw new Error(`${c.id}: provenance.${k} is not {build, line}, "unrecorded" or "planted"`);
  }
}

/** body_records: each exactly {off, len}, integers, in body order and not overlapping; every row the whole batch read carries its record's span. */
function bodyRecordsOf(c: Case, batch: BatchResult): BodyRecord[] {
  const recs: BodyRecord[] = [];
  let end = 0;
  for (const [i, r] of (c.body_records ?? []).entries()) {
    const ok =
      typeof r === 'object' && r !== null && JSON.stringify(Object.keys(r).sort()) === '["len","off"]' && Number.isInteger((r as BodyRecord).off) && Number.isInteger((r as BodyRecord).len);
    if (!ok) throw new Error(`${c.id}: body_records[${i}] is not {off, len} with integer members`);
    const { off, len } = r as BodyRecord;
    if (len < 1 || off < end) throw new Error(`${c.id}: body_records[${i}] is empty or overlaps the record before it`);
    recs.push({ off, len });
    end = off + len;
  }
  if (batch.rows.length > recs.length || batch.rowsRead > recs.length) throw new Error(`${c.id}: the batch read more rows than body_records holds`);
  batch.rows.forEach((row, i) => {
    const rec = recs[i] as BodyRecord;
    if (row.inputSpan === undefined || row.inputSpan.off !== rec.off || row.inputSpan.len !== rec.len) throw new Error(`${c.id}: batch row ${i}'s input_span is not body_records[${i}]`);
  });
  return recs;
}

/**
 * failing_t_record: required where the t rows' batch is refused (the post_where_refusal rule handles the case), absent otherwise; an
 * integer (a boolean is not one) that is one of the t rows and, where that batch is refused at one of its rows, the body record that
 * row is. A violation is a fixture error.
 */
function failingOf(c: Case, tRows: BatchOfTRows | undefined): number | undefined {
  const refused = tRows !== undefined && tRows.batch.outcome !== 'accepted';
  const n = c.failing_t_record;
  if (n === undefined) {
    if (refused) throw new Error(`${c.id}: batch_of_t_rows is refused and the case carries no failing_t_record`);
    return undefined;
  }
  if (!refused || tRows === undefined) throw new Error(`${c.id}: failing_t_record where post_where_refusal does not apply`);
  if (typeof n !== 'number' || !Number.isInteger(n)) throw new Error(`${c.id}: failing_t_record is not an integer`);
  if (!tRows.rows.includes(n)) throw new Error(`${c.id}: failing_t_record ${n} is not one of batch_of_t_rows.rows`);
  const sub = tRows.batch;
  if (sub.refusedAt === 'row' && sub.refusedRow !== undefined && sub.refusedRow >= 0 && sub.refusedRow < tRows.rows.length && tRows.rows[sub.refusedRow] !== n) {
    throw new Error(`${c.id}: failing_t_record ${n} is not the record the t rows' batch refused at`);
  }
  return n;
}

/** A case's batch_of_t_rows through the real batch decoder; a malformed one is a fixture error, never a verdict. */
function tRowsOf(c: Case, batch: BatchResult): BatchOfTRows | undefined {
  checkProvenance(c);
  const records = c.body_records === undefined ? undefined : bodyRecordsOf(c, batch);
  const ev = c.batch_of_t_rows;
  if (ev === undefined) {
    failingOf(c, undefined);
    return undefined;
  }
  if (records === undefined) throw new Error(`${c.id}: batch_of_t_rows without body_records`);
  const n = records.length;
  if (!ev.rows.every((i, k) => Number.isInteger(i) && i >= 0 && i < n && (k === 0 || i > (ev.rows[k - 1] as number)))) {
    throw new Error(`${c.id}: batch_of_t_rows.rows is not increasing indexes into body_records`);
  }
  const tRows: BatchOfTRows = { rows: ev.rows, batch: decodeBatch(Buffer.from(JSON.stringify(ev.batch)), undefined), exp: bytes(ev.export_b64), records };
  const failing = failingOf(c, tRows);
  return failing === undefined ? tRows : { ...tRows, failing };
}

function run(c: Case) {
  const batch = decodeBatch(Buffer.from(JSON.stringify(c.batch)), undefined);
  const ins = decodeInsert(Buffer.from(JSON.stringify(c.insert)), undefined);
  // The comparator is handed the RAW insert document and the raw output bytes: the public result has
  // no export_declined* member and hides an output that does not exist.
  return projectionVerdict(ins, bytes(c.insert_rows_b64), batch, bytes(c.batch_export_b64), c.output_format, c.insert as Record<string, unknown>, tRowsOf(c, batch));
}

function expected(c: Case) {
  if (c.expect === 'equal') return { kind: 'equal' };
  if ('exception' in c.expect) return { kind: 'exception', name: c.expect.exception };
  return { kind: 'violates', rule: c.expect.violates };
}

// An absent fixture fails, not skips, where the repository's CI requires every shared fixture.
it.runIf(!present && process.env.CHTYPES_REQUIRE_FIXTURES === '1')('the insert projection fixture is present', () => {
  throw new Error(`CHTYPES_REQUIRE_FIXTURES=1 and the insert projection fixture is absent: ${FIXTURE}`);
});

describe.skipIf(!present)('the insert projection gate over the shared fixture', () => {
  it('reads the fixture it was written for, and refuses a zero-run', () => {
    expect(doc.schema).toBe('chtypes.abi-v2.insert-projection-fixture/1');
    const reds = doc.cases.filter((c) => typeof c.expect === 'object' && 'violates' in c.expect);
    expect(doc.cases).toHaveLength(63);
    expect(doc.cases.filter((c) => c.expect === 'equal')).toHaveLength(24);
    expect(doc.cases.filter((c) => typeof c.expect === 'object' && 'exception' in c.expect)).toHaveLength(1);
    expect(reds).toHaveLength(38);
    const byRule: Record<string, number> = {};
    for (const c of reds) byRule[(c.expect as { violates: string }).violates] = (byRule[(c.expect as { violates: string }).violates] ?? 0) + 1;
    expect(byRule).toEqual({
      e_row_rejects: 8,
      shared_members: 3,
      counts: 3,
      line_output: 2,
      non_t_row: 2,
      output_presence: 2,
      export_decline_is_call_decline: 3,
      post_where_cut: 4,
      post_where_refusal: 7,
      err_prefix: 1,
      err_suffix: 2,
      native_output: 1,
    });
  });

  it('output is absent unless accepted', () => {
    const c = doc.cases.find((x) => x.id === 'equal-declined-output-is-the-call-decline');
    expect(c, 'the fixture case is missing').toBeDefined();
    if (c === undefined) return;
    // Hand the decoder bytes anyway: a library that returned a buffer for a declined output changes nothing.
    const declined = decodeInsert(Buffer.from(JSON.stringify(c.insert)), Buffer.from('stray'));
    expect(declined.outcome).toBe('unsupported');
    expect(declined.output).toBeUndefined();
    expect(declined.exportColumns).toBeUndefined();
    // An accepted call with output keeps its bytes.
    const acc = doc.cases.find((x) => x.expect === 'equal' && x.insert_rows_b64 !== null);
    expect(acc, 'no accepted case with an output').toBeDefined();
    if (acc === undefined) return;
    const want = bytes(acc.insert_rows_b64) as Buffer;
    const got = decodeInsert(Buffer.from(JSON.stringify(acc.insert)), want);
    expect(got.outcome).toBe('accepted');
    expect(got.output).toBeDefined();
    expect((got.output as Buffer).equals(want)).toBe(true);
  });

  it('plants a red for every rule and a case for every exception', () => {
    for (const rule of RULES) expect(doc.cases.some((c) => typeof c.expect === 'object' && 'violates' in c.expect && c.expect.violates === rule), rule).toBe(true);
    for (const name of EXCEPTIONS) expect(doc.cases.some((c) => typeof c.expect === 'object' && 'exception' in c.expect && c.expect.exception === name), name).toBe(true);
  });

  for (const c of doc.cases) {
    it(`${c.id}: ${JSON.stringify(c.expect)}`, () => {
      expect(run(c)).toEqual(expected(c));
    });
  }
});
