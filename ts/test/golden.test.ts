/**
 * The public golden set — SERVED beside the artifacts as `sdk-goldens.json`.
 *
 * A few dozen cases whose expectations were produced by the library itself
 * and agreed on by every ClickHouse version in the generating registry
 * (the golden-set generator). Every SDK runs the
 * same file, so the four bindings are held to one answer. It is not the
 * corpus; that lives with the rigs.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { Format, Registry, SchemaError, Verdict, looksLikeRegistry, resolveRegistryDir } from '../src/index.js';

// The document spells a verdict in one wire character (`Verdict`); the golden
// set spells it out, deliberately binding-agnostic — Go's verdictName and
// Python's VERDICTS do the same translation before comparing.
const VERDICT_NAMES: Record<Verdict, string> = {
  [Verdict.True]: 'true',
  [Verdict.False]: 'false',
  [Verdict.Error]: 'error',
  [Verdict.Decline]: 'decline',
};

// The set is SERVED, not tracked: core publishes sdk-goldens.json in the rolling
// release as a row in the signed SHA256SUMS, so a fetch installs it beside the
// artifacts and this test reads it offline. CHTYPES_GOLDENS overrides the path.
const REGISTRY = resolveRegistryDir();
const GOLDENS =
  process.env['CHTYPES_GOLDENS'] ?? (REGISTRY === null ? null : path.join(REGISTRY, 'sdk-goldens.json'));

interface GoldenRow {
  // Optional on the wire, same as every other field here: the served
  // document omits `outcome` only on a malformed golden, never legitimately,
  // so the type says so and requireRowOutcome() below rejects an absent one
  // loudly instead of the type-checker's `string` masking a runtime
  // `undefined` (chtypes#199).
  outcome?: string;
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
  /** What the set says about itself; `exact` maps a line to the EXACT ClickHouse version it was generated on. */
  generated: { exact?: Record<string, string>; versions?: string[]; core_commit?: string };
  cases: GoldenCase[];
}

const FORMATS: Record<string, number> = {
  JSONEachRow: Format.JSONEachRow,
  CSV: Format.CSV,
  TSV: Format.TSV,
  Values: Format.Values,
  JSONCompactEachRow: Format.JSONCompactEachRow,
};

// The served document omits every optional field when it is empty: an absent
// key means empty or zero (chtypes#199). These read a case/row exactly the
// way the per-case `it()` below needs to, so the offline describe block at
// the bottom of this file can pin the same behavior with no registry.
function requireCaseOutcome(c: GoldenCase): string {
  // "" (here, `undefined`) is not a valid outcome. Omitting the key is legal
  // only on a compile-error case, which returns before this is ever called;
  // anywhere else a missing outcome is a malformed golden.
  if (c.expect.outcome === undefined) {
    throw new Error(`golden case ${c.id} has no outcome and is not a compile-error case`);
  }
  return c.expect.outcome;
}
function requireRowOutcome(caseId: string, i: number, row: GoldenRow): string {
  if (row.outcome === undefined) {
    throw new Error(`golden case ${caseId} row ${i} has no outcome`);
  }
  return row.outcome;
}
function expectedVerdicts(c: GoldenCase): string[] {
  return c.expect.verdicts ?? [];
}
function expectedRows(c: GoldenCase): GoldenRow[] {
  return c.expect.rows ?? [];
}
function expectedErrCode(c: GoldenCase): number {
  return c.expect.err_code ?? 0;
}
function rowErrCode(row: GoldenRow): number {
  return row.err_code ?? 0;
}
function rowValues(row: GoldenRow): Record<string, string> {
  return row.values ?? {};
}
function rowNulls(row: GoldenRow): string[] {
  return row.nulls ?? [];
}
function rowTransformed(row: GoldenRow): { column: string; reason: string }[] {
  return row.transformed ?? [];
}
function rowSubstituted(row: GoldenRow): string[] {
  return row.substituted ?? [];
}
function rowComputed(row: GoldenRow): Record<string, string> {
  return row.computed ?? {};
}

// A registry fetched before core started serving the set has no file. That is a
// loud skip, not an exception that takes the whole module down.
let doc: GoldenFile | null = null;
if (GOLDENS !== null) {
  try {
    doc = JSON.parse(readFileSync(GOLDENS, 'utf8')) as GoldenFile;
  } catch {
    doc = null;
  }
}
if (doc !== null) {
  if (doc.schema !== 1) throw new Error(`golden set schema ${doc.schema}; this test reads schema 1`);
  if (doc.cases.length === 0) throw new Error('golden set holds no cases');
} else {
  console.warn(
    `[chtypes] golden tests SKIPPED: no golden set at ${GOLDENS ?? '(no registry on the search path)'}` +
      ' — it is served beside the artifacts now; scripts/fetch.sh 25.8 installs it, and CHTYPES_GOLDENS overrides the path',
  );
}

// "Have a registry" is "have at least one artifact in it": the search path
// (docs/guides/fetch.md §1) resolves CHTYPES_REGISTRY without looking inside, so a
// directory that holds nothing must skip exactly as no directory does.
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY) && doc !== null;
if (!HAVE_REGISTRY) {
  console.warn(
    '[chtypes] golden tests SKIPPED: no artifact registry on the search path' +
      (REGISTRY === null ? '' : ` (${REGISTRY} holds no artifact)`) +
      ' — fetch one with scripts/fetch.sh 25.8 (docs/guides/fetch.md), or point CHTYPES_REGISTRY at a registry',
  );
}

describe.skipIf(!HAVE_REGISTRY)('goldens', () => {
  // A skipped describe still evaluates its body, so the registry is opened
  // only when there is one. Without it no per-case test is generated and the
  // sentinel below is what the census shows as skipped, by name.
  // Construction opens nothing, so the goldens ask for every line they are
  // about to score: `libraries()` lists what is open, and an empty list here
  // would score nothing and report itself green.
  const libraries =
    REGISTRY !== null && HAVE_REGISTRY
      ? new Registry(REGISTRY, { preload: new Registry(REGISTRY).versions() }).libraries()
      : [];
  it('has at least one artifact to run against', () => {
    expect(libraries.length).toBeGreaterThan(0);
  });
  const exact = doc?.generated?.exact ?? {};
  for (const lib of libraries) {
    // A case is only a golden for the EXACT build it was generated against. The
    // rolling index keeps older patch rows, so a machine can hold a patch the
    // generator never saw; that is a loud skip, never a failure.
    const want = exact[lib.minor];
    if (want === undefined) {
      it.skip(`${lib.version}/(the set was not generated on line ${lib.minor})`, () => {});
      continue;
    }
    if (want !== lib.version) {
      it.skip(`${lib.version}/(generated on ClickHouse ${want} for line ${lib.minor}; fetch ${lib.minor} to run these cases)`, () => {});
      continue;
    }
    for (const c of doc!.cases) {
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
              expect(requireCaseOutcome(c)).toBe('ok');
              expect(fr.verdicts.map((v) => VERDICT_NAMES[v])).toEqual(expectedVerdicts(c));
            } finally {
              filter.close();
            }
            return;
          }
          const br = schema.rows(format as never, body, c.settings);
          expect(br.outcome, br.errMsg).toBe(requireCaseOutcome(c));
          expect(br.errCode, br.errMsg).toBe(expectedErrCode(c));
          const rows = expectedRows(c);
          expect(br.rows.length).toBe(rows.length);
          br.rows.forEach((row, i) => {
            const want = rows[i]!;
            const wantOutcome = requireRowOutcome(c.id, i, want);
            expect(row.outcome, `row ${i}: ${row.errMsg}`).toBe(wantOutcome);
            if (wantOutcome === 'rejected' || wantOutcome === 'skipped') {
              expect(row.errCode, `row ${i}: ${row.errMsg}`).toBe(rowErrCode(want));
              return;
            }
            const values = Object.fromEntries(row.values.map((v) => [v.column, v.text]));
            expect(values).toEqual(rowValues(want));
            const nulls = row.values.filter((v) => v.isNull).map((v) => v.column).sort();
            expect(nulls).toEqual(rowNulls(want));
            const tr = row.transformed.map((t) => `${t.column}:${t.reason}`).sort();
            expect(tr).toEqual(rowTransformed(want).map((t) => `${t.column}:${t.reason}`).sort());
            const sub = row.substituted.map((s) => s.column).sort();
            expect(sub).toEqual(rowSubstituted(want));
            const comp = Object.fromEntries(row.computed.map((k) => [k.column, k.text]));
            expect(comp).toEqual(rowComputed(want));
          });
        } finally {
          schema.close();
        }
      });
    }
  }
});

// Pins the served document's omit-when-empty shape rule (chtypes#199): a key
// absent from a case or a row means empty or zero, never a crash on a
// required read. This needs no artifact or registry — it calls the same
// helpers the per-case test above does, so a future unconditional
// `c.expect.x` (or `row.x`) creeping back into one of them fails here
// immediately.
describe('golden reading — omitted optional fields', () => {
  it('reads an omitted case-level verdicts as empty', () => {
    const c: GoldenCase = { id: 'synthetic-filter', ddl: '', format: 'CSV', body: '', expect: { outcome: 'ok' } };
    expect(expectedVerdicts(c)).toEqual([]);
  });

  it('reads an omitted case-level rows and err_code as empty/zero', () => {
    const c: GoldenCase = { id: 'synthetic-batch', ddl: '', format: 'CSV', body: '', expect: { outcome: 'ok' } };
    expect(expectedRows(c)).toEqual([]);
    expect(expectedErrCode(c)).toBe(0);
  });

  it('reads every omitted row-level field as empty/zero', () => {
    const row: GoldenRow = { outcome: 'accepted' };
    expect(rowErrCode(row)).toBe(0);
    expect(rowValues(row)).toEqual({});
    expect(rowNulls(row)).toEqual([]);
    expect(rowTransformed(row)).toEqual([]);
    expect(rowSubstituted(row)).toEqual([]);
    expect(rowComputed(row)).toEqual({});
  });

  it('requires outcome on a case unless it is a compile-error case', () => {
    const c: GoldenCase = { id: 'synthetic-no-outcome', ddl: '', format: 'CSV', body: '', expect: {} };
    expect(() => requireCaseOutcome(c)).toThrow(/synthetic-no-outcome has no outcome and is not a compile-error case/);
  });

  it('requires outcome on every row', () => {
    const row: GoldenRow = {};
    expect(() => requireRowOutcome('synthetic-no-outcome', 0, row)).toThrow(
      /synthetic-no-outcome row 0 has no outcome/,
    );
  });
});
