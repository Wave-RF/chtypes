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
const present = existsSync(FIXTURE);
if (!present) {
  console.error(`SKIP: insert projection fixture absent (${FIXTURE}); the projection gate did not run (a bare copy of ts/ does not carry tests/fixtures)`);
}
const doc = (present ? JSON.parse(readFileSync(FIXTURE, 'utf8')) : { schema: '', cases: [] }) as { schema: string; cases: readonly Case[] };

const bytes = (b64: string | null): Buffer | undefined => (b64 === null ? undefined : Buffer.from(b64, 'base64'));

function run(c: Case) {
  const batch = decodeBatch(Buffer.from(JSON.stringify(c.batch)), undefined);
  const ins = decodeInsert(Buffer.from(JSON.stringify(c.insert)), undefined);
  // The comparator is handed the RAW insert document and the raw output bytes: the public result has
  // no export_declined* member and hides an output that does not exist.
  return projectionVerdict(ins, bytes(c.insert_rows_b64), batch, bytes(c.batch_export_b64), c.output_format, c.insert as Record<string, unknown>);
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
    expect(doc.cases).toHaveLength(37);
    expect(doc.cases.filter((c) => c.expect === 'equal')).toHaveLength(12);
    expect(doc.cases.filter((c) => typeof c.expect === 'object' && 'exception' in c.expect)).toHaveLength(1);
    expect(reds).toHaveLength(24);
    const byRule: Record<string, number> = {};
    for (const c of reds) byRule[(c.expect as { violates: string }).violates] = (byRule[(c.expect as { violates: string }).violates] ?? 0) + 1;
    expect(byRule).toEqual({
      e_row_rejects: 8,
      shared_members: 3,
      counts: 2,
      line_output: 2,
      non_t_row: 2,
      output_presence: 2,
      export_decline_is_call_decline: 2,
      err_prefix: 1,
      err_suffix: 1,
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
