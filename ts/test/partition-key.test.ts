/**
 * Revision 6's partition key: the two result fields, the sign rule the setter
 * throws through, and — with a revision-6 artifact — the key end to end.
 *
 * The unit half parses documents through the SAME `rowResultOf` /
 * `batchResultOf` every `row`/`rows` call uses. `Schema#setPartitionBy` maps
 * the C return inside the native layer, through `schemaErrorFor` — the same
 * funnel `setEngine` uses — so the no-artifact half pins that funnel for the
 * codes `chs_schema_partition_by` documents. The artifact half skips LOUDLY,
 * by name, without a revision-6 registry.
 */

import { afterAll, describe, expect, it } from 'vitest';
import { schemaErrorFor } from '../src/errors.js';
import {
  Format,
  Registry,
  SchemaError,
  UnsupportedError,
  looksLikeRegistry,
  resolveRegistryDir,
  type Library,
} from '../src/index.js';
import { parseDocument } from '../src/json.js';
import { Outcome, batchResultOf, rowResultOf } from '../src/results.js';

const doc = (js: string) => parseDocument(Buffer.from(js, 'utf8'));

describe('the partition fields (no artifact)', () => {
  it('parses partition_id per row and partition_count per batch', () => {
    const batch = batchResultOf(
      doc(
        '{"outcome":"accepted","rows_read":3,"partition_count":2,"rows":[' +
          '{"outcome":"accepted","cols":[],"partition_id":"202601"},' +
          '{"outcome":"accepted","cols":[],"partition_id":"202602"},' +
          '{"outcome":"rejected","code":27,"err":"x","cols":[]}]}',
      ),
    );
    expect(batch.partitionCount).toBe(2);
    expect(batch.rows.map((r) => r.partitionId)).toEqual(['202601', '202602', undefined]);
    expect('partitionId' in batch.rows[2]!).toBe(false);
  });

  it('leaves both absent when the document does not carry them', () => {
    const batch = batchResultOf(doc('{"outcome":"accepted","rows":[{"outcome":"accepted","cols":[]}]}'));
    expect('partitionCount' in batch).toBe(false);
    expect('partitionId' in batch.rows[0]!).toBe(false);
    expect(rowResultOf(doc('{"outcome":"accepted","cols":[],"partition_id":"all"}')).partitionId).toBe('all');
    // A present zero is a real answer (a key, and no stored row), not absence.
    expect(batchResultOf(doc('{"outcome":"accepted","partition_count":0}')).partitionCount).toBe(0);
  });

  it('reads 252 as an ordinary rejection, rows itemized', () => {
    const batch = batchResultOf(
      doc(
        '{"outcome":"rejected","code":252,"err":"Too many partitions for single INSERT block","rows_read":2,' +
          '"rows":[{"outcome":"accepted","cols":[],"partition_id":"1"},{"outcome":"accepted","cols":[],"partition_id":"2"}]}',
      ),
    );
    expect(batch.outcome).toBe(Outcome.Rejected);
    expect(batch.errCode).toBe(252);
    expect(batch.rows).toHaveLength(2);
  });

  it("maps the setter's return by the engine SIGN rule", () => {
    // Positive codes are the server's own refusal: 36 BAD_ARGUMENTS is what a
    // non-deterministic key gets on every served line, 549 a key over a type
    // the line will not key on.
    for (const rc of [36, 549]) {
      const refused = schemaErrorFor(rc, "the server's own message");
      expect(refused).toBeInstanceOf(SchemaError);
      expect((refused as SchemaError).code).toBe(rc);
    }
    for (const rc of [-1, -2]) {
      const declined = schemaErrorFor(rc, 'why');
      expect(declined).toBeInstanceOf(UnsupportedError);
      expect(declined).not.toBeInstanceOf(SchemaError);
    }
  });
});

// ------------------------------------------------------------- with artifacts

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

let registry: Registry | undefined;
const rev6: Library[] = [];
if (HAVE_REGISTRY) {
  registry = new Registry(REGISTRY ?? undefined);
  for (const line of registry.versions()) {
    try {
      const library = registry.for(line);
      if (library.abiRevision >= 6) rev6.push(library);
    } catch (err) {
      console.warn(`[chtypes] line ${line} not exercised: ${(err as Error).message}`);
    }
  }
}
if (rev6.length === 0) {
  console.warn(
    '[chtypes] partition-key artifact tests SKIPPED: no ABI revision-6 artifact on the search path — ' +
      'fetch one with scripts/fetch.sh (docs/guides/fetch.md).',
  );
}

afterAll(() => {
  registry?.close();
});

const BODY = Buffer.from(
  '{"ts":"2026-01-15 10:00:00","tenant":"a"}\n' +
    '{"ts":"2026-01-20 10:00:00","tenant":"b"}\n' +
    '{"ts":"2026-02-01 10:00:00","tenant":"a"}\n',
  'utf8',
);

describe.skipIf(rev6.length === 0)('the partition key on a loaded revision-6 library', () => {
  it("ids, counts, refuses over the limit, clears, and passes the server's 36 for a non-deterministic key", () => {
    for (const library of rev6) {
      const schema = library.compileDdl('ts DateTime, tenant String');
      try {
        const plain = schema.rows(Format.JSONEachRow, BODY);
        expect(plain.partitionCount).toBeUndefined();
        expect(plain.rows[0]!.partitionId).toBeUndefined();

        schema.setPartitionBy('toYYYYMM(ts)');
        const keyed = schema.rows(Format.JSONEachRow, BODY);
        expect(keyed.outcome, library.minor).toBe(Outcome.Accepted);
        expect(keyed.partitionCount, library.minor).toBe(2);
        const [p0, p1, p2] = keyed.rows.map((r) => r.partitionId);
        expect(p0).toBeTruthy();
        expect(p1).toBe(p0);
        expect(p2).not.toBe(p0);

        const over = schema.rows(Format.JSONEachRow, BODY, { max_partitions_per_insert_block: '1' });
        expect(over.outcome, library.minor).toBe(Outcome.Rejected);
        expect(over.errCode, library.minor).toBe(252);
        expect(over.rows).toHaveLength(3);

        schema.setPartitionBy('');
        const cleared = schema.rows(Format.JSONEachRow, BODY);
        expect(cleared.partitionCount).toBeUndefined();
        expect(cleared.rows[0]!.partitionId).toBeUndefined();

        // A non-deterministic key is the server's own rejection — 36
        // BAD_ARGUMENTS on every served line — not a decline.
        let refused: unknown;
        try {
          schema.setPartitionBy('rand()');
        } catch (err) {
          refused = err;
        }
        expect(refused, library.minor).toBeInstanceOf(SchemaError);
        expect((refused as SchemaError).code, library.minor).toBe(36);
      } finally {
        schema.close();
      }
    }
  });
});
