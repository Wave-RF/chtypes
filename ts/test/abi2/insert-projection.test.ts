/**
 * The standing projection gate (ABI v2): every case of
 * `tests/fixtures/abi-v2/insert-projection.json`, decoded with this binding's
 * REAL decoders (`decodeBatch`, `decodeInsert`), through `projectionVerdict`,
 * which mirrors `scripts/abi-v2/check-insert-projection.py` rule by rule. The
 * fixture's planted reds prove the comparator can fail; the decoder is proved
 * by the same run, since a decoder that dropped a member would change a verdict.
 * No library is needed, so nothing here skips.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { decodeBatch, decodeInsert } from '../../src/documents.js';
import { EXCEPTIONS, projectionVerdict, RULES } from './insert-projection-rule.js';

interface Case {
  readonly id: string;
  readonly output_format: string;
  readonly batch: unknown;
  readonly batch_export_b64: string | null;
  readonly insert: unknown;
  readonly insert_rows_b64: string | null;
  readonly expect: 'equal' | { readonly exception: string } | { readonly violates: string };
}

const FIXTURE = path.resolve(import.meta.dirname, '../../../tests/fixtures/abi-v2/insert-projection.json');
const doc = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { schema: string; cases: readonly Case[] };

const bytes = (b64: string | null): Buffer | undefined => (b64 === null ? undefined : Buffer.from(b64, 'base64'));

function run(c: Case) {
  const batch = decodeBatch(Buffer.from(JSON.stringify(c.batch)), undefined);
  const ins = decodeInsert(Buffer.from(JSON.stringify(c.insert)), undefined);
  return projectionVerdict(ins, bytes(c.insert_rows_b64), batch, bytes(c.batch_export_b64), c.output_format);
}

function expected(c: Case) {
  if (c.expect === 'equal') return { kind: 'equal' };
  if ('exception' in c.expect) return { kind: 'exception', name: c.expect.exception };
  return { kind: 'violates', rule: c.expect.violates };
}

describe('the insert projection gate over the shared fixture', () => {
  it('reads the fixture it was written for, and refuses a zero-run', () => {
    expect(doc.schema).toBe('chtypes.abi-v2.insert-projection-fixture/1');
    const reds = doc.cases.filter((c) => typeof c.expect === 'object' && 'violates' in c.expect);
    expect(doc.cases).toHaveLength(33);
    expect(doc.cases.filter((c) => c.expect === 'equal')).toHaveLength(11);
    expect(doc.cases.filter((c) => typeof c.expect === 'object' && 'exception' in c.expect)).toHaveLength(1);
    expect(reds).toHaveLength(21);
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
