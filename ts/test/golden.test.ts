/**
 * The public golden set — `goldens/cases.json` at the repository root.
 *
 * A few dozen cases whose expectations were produced by the library itself
 * and agreed on by every ClickHouse version in the generating registry
 * (chtypes-core: `tests/conformance/go/cmd/goldens-gen`). Every SDK runs the
 * same file, so the four bindings are held to one answer. It is not the
 * corpus; that lives with the rigs.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { Format, Registry, SchemaError, resolveRegistryDir } from '../src/index.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GOLDENS = process.env['CHTYPES_GOLDENS'] ?? path.resolve(HERE, '..', '..', 'goldens', 'cases.json');

interface GoldenRow {
  outcome: string;
  err_code?: number;
  values?: Record<string, string>;
  nulls?: string[];
  transformed?: { column: string; reason: string }[];
  substituted?: string[];
  computed?: Record<string, string>;
}
interface GoldenCase {
  id: string;
  ddl: string;
  format: string;
  body: string;
  settings?: Record<string, string>;
  filter?: string;
  expect: {
    compile_error_code?: number;
    outcome?: string;
    err_code?: number;
    rows?: GoldenRow[];
    verdicts?: string[];
  };
}
interface GoldenFile {
  schema: number;
  cases: GoldenCase[];
}

const FORMATS: Record<string, number> = {
  JSONEachRow: Format.JSONEachRow,
  CSV: Format.CSV,
  TSV: Format.TSV,
  Values: Format.Values,
  JSONCompactEachRow: Format.JSONCompactEachRow,
};

const doc = JSON.parse(readFileSync(GOLDENS, 'utf8')) as GoldenFile;
if (doc.schema !== 1) throw new Error(`golden set schema ${doc.schema}; this test reads schema 1`);
if (doc.cases.length === 0) throw new Error('golden set holds no cases');

const REGISTRY = resolveRegistryDir();
if (REGISTRY === null) {
  console.warn('[chtypes] golden tests SKIPPED: no artifact registry (set CHTYPES_REGISTRY or run scripts/fetch.sh)');
}

describe.skipIf(REGISTRY === null)('goldens', () => {
  const registry = new Registry(REGISTRY ?? undefined);
  const libraries = registry.libraries();
  it('has at least one artifact to run against', () => {
    expect(libraries.length).toBeGreaterThan(0);
  });
  for (const lib of libraries) {
    for (const c of doc.cases) {
      it(`${lib.version}/${c.id}`, () => {
        const format = FORMATS[c.format];
        if (format === undefined) throw new Error(`${c.id}: unknown format ${c.format}`);
        const body = Buffer.from(c.body, 'utf8');
        if (c.expect.compile_error_code !== undefined) {
          let caught: unknown;
          try {
            lib.compileDdl(c.ddl).close();
          } catch (e) {
            caught = e;
          }
          expect(caught).toBeInstanceOf(SchemaError);
          expect((caught as SchemaError).code).toBe(c.expect.compile_error_code);
          return;
        }
        const schema = lib.compileDdl(c.ddl);
        try {
          if (c.filter !== undefined) {
            const filter = schema.compileFilter(c.filter);
            try {
              const fr = filter.rows(format as never, body, c.settings);
              expect(fr.outcome).toBe('ok');
              expect(c.expect.outcome).toBe('ok');
              expect(fr.verdicts).toEqual(c.expect.verdicts);
            } finally {
              filter.close();
            }
            return;
          }
          const br = schema.rows(format as never, body, c.settings);
          expect(br.outcome, br.errMsg).toBe(c.expect.outcome);
          expect(br.errCode, br.errMsg).toBe(c.expect.err_code ?? 0);
          const rows = c.expect.rows ?? [];
          expect(br.rows.length).toBe(rows.length);
          br.rows.forEach((row, i) => {
            const want = rows[i]!;
            expect(row.outcome, `row ${i}: ${row.errMsg}`).toBe(want.outcome);
            if (want.outcome === 'rejected' || want.outcome === 'skipped') {
              expect(row.errCode, `row ${i}: ${row.errMsg}`).toBe(want.err_code ?? 0);
              return;
            }
            const values = Object.fromEntries(row.values.map((v) => [v.column, v.text]));
            expect(values).toEqual(want.values ?? {});
            const nulls = row.values.filter((v) => v.isNull).map((v) => v.column).sort();
            expect(nulls).toEqual(want.nulls ?? []);
            const tr = row.transformed.map((t) => `${t.column}:${t.reason}`).sort();
            expect(tr).toEqual((want.transformed ?? []).map((t) => `${t.column}:${t.reason}`).sort());
            const sub = row.substituted.map((s) => s.column).sort();
            expect(sub).toEqual(want.substituted ?? []);
            const comp = Object.fromEntries(row.computed.map((k) => [k.column, k.text]));
            expect(comp).toEqual(want.computed ?? {});
          });
        } finally {
          schema.close();
        }
      });
    }
  }
});
