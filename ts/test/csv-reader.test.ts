/**
 * Issue #119, items 3 and 4 (the revision-5 CSV/TSV reader) and item 1 (the two
 * revision-5 `src` provenances), executed END TO END against a real revision-5
 * artifact rather than against a hand-built document.
 *
 * A test that hand-sets the value the code computes tests the belief, not the
 * computation, so every expectation below comes off a loaded library.
 *
 * ⚠️ Nothing here asserts on an error MESSAGE. With revision-5 artifacts a
 * rejected CSV or TSV row carries ClickHouse's own wording, which changes
 * whenever ClickHouse rewords an error; the CODE is the stable part and is the
 * only thing pinned. 15,245 TSV records in the artifact producer's corpus
 * differ in the message alone — that is the fragility this change exists to
 * warn consumers about. (This file's own `toMatch(/Cannot parse/)`-shaped
 * fragility lives in chtypes.test.ts, on a JSONEachRow rejection; it is
 * untouched by the CSV reader and is not repeated here.)
 */

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  Format,
  Reason,
  Registry,
  looksLikeRegistry,
  resolveRegistryDir,
  type Library,
  type RowResult,
} from '../src/index.js';
import { Outcome, Source } from '../src/results.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

/**
 * The two rejection codes, measured on 24.8, 26.7 and 26.8 (linux-arm64) and on
 * 26.3 through 26.8 (darwin-arm64), identical on every line: ClickHouse's own
 * INCORRECT_DATA for the CSV reader's trailing-garbage refusal and
 * CANNOT_PARSE_INPUT_ASSERTION_FAILED for the TSV reader's.
 */
const CSV_REJECTION_CODE = 117;
const TSV_REJECTION_CODE = 27;

const DEFAULTS_OFF = { input_format_defaults_for_omitted_fields: '0' };
const DEFAULTS_ON = { input_format_defaults_for_omitted_fields: '1' };

const utf8 = (s: string): Buffer => Buffer.from(s, 'utf8');

/**
 * Every line the test registry holds that loads at ABI revision 5.
 *
 * Every block runs against ALL of them — CI fetches three (the newest -lts, the
 * newest -stable and 24.8) — rather than one resolved by a helper, because none
 * of these behaviors is line-sensitive and a silent retarget onto a different
 * ClickHouse would hide that. A line this platform has not relinked to revision
 * 5 refuses to load through the loader's own ABI guard: that is the guard
 * working, so it is reported and passed over.
 */
let registry: Registry | undefined;
let rev5: Library[] = [];

const openRev5 = (): Library[] => {
  const dir = REGISTRY ?? undefined;
  const lines = new Registry(dir).versions();
  registry = new Registry(dir);
  const libraries: Library[] = [];
  for (const line of lines) {
    let library: Library;
    try {
      library = registry.for(line);
    } catch (err) {
      console.warn(`[chtypes] line ${line} not exercised: ${(err as Error).message}`);
      continue;
    }
    if (library.abiRevision < 5) {
      console.warn(`[chtypes] line ${line} not exercised: ABI revision ${library.abiRevision}, needs 5`);
      continue;
    }
    libraries.push(library);
  }
  if (libraries.length === 0) {
    console.warn(
      [
        '',
        '[chtypes] csv-reader tests SKIPPED: the registry holds no ABI revision-5 artifact.',
        '  Every case in this file needs one — fetch one with scripts/fetch.sh (docs/guides/fetch.md).',
        '',
      ].join('\n'),
    );
  }
  return libraries;
};

if (HAVE_REGISTRY) {
  rev5 = openRev5();
} else {
  console.warn('[chtypes] csv-reader tests SKIPPED: no artifact registry on the search path.');
}

afterAll(() => {
  registry?.close();
});

describe.skipIf(!HAVE_REGISTRY || rev5.length === 0)('#119: the revision-5 CSV/TSV reader', () => {
  beforeAll(() => {
    expect(rev5.length).toBeGreaterThan(0);
  });

  // --------------------------------------- item 3: codes, not messages

  it('keeps the CSV and TSV rejection codes, and reports no columns for a rejected CSV row', () => {
    // `abc` in the second field: the first field parses, so a reader that
    // reported per-column detail for what it managed to read would report one
    // column here. That is the shape the empty-cols assertion pins.
    const cases = [
      { name: 'CSV', format: Format.CSV, row: utf8('1,abc\n'), code: CSV_REJECTION_CODE, emptyCols: true },
      { name: 'TSV', format: Format.TSV, row: utf8('1\tabc\n'), code: TSV_REJECTION_CODE, emptyCols: false },
    ] as const;

    let ran = 0;
    for (const library of rev5) {
      const schema = library.compileDdl('id UInt8, n UInt8');
      try {
        for (const c of cases) {
          const r = schema.row(c.format, c.row);
          const where = `${library.version} ${c.name}`;
          expect(r.outcome, `${where}: the row must be refused, not coerced`).toBe(Outcome.Rejected);
          // The CODE, and only the code. `errMsg` is deliberately never
          // asserted — it is ClickHouse's own text now.
          expect(r.errCode, `${where}: message, not asserted, was ${JSON.stringify(r.errMsg)}`).toBe(c.code);
          if (c.emptyCols) {
            // The revision-5 reader reports no PARTIAL column list for a row
            // it refused, where the previous path could.
            expect(r.values, `${where}: a rejected row must carry no per-column values`).toEqual([]);
            expect(r.transformed, `${where}: there is no stored row to have changed`).toEqual([]);
          } else {
            console.log(`[chtypes] ${where} rejected row carries ${r.values.length} value(s) (not pinned: the empty-cols measurement is CSV-scoped)`);
          }
          ran++;

          // The same refusal through the batch path, where an allowed error
          // budget turns it into a skipped row rather than a rejected batch.
          const b = schema.rows(c.format, c.row, { input_format_allow_errors_num: '10' });
          expect(b.rows.length, `${where}: want one row document`).toBe(1);
          expect(b.rows[0]!.outcome).toBe(Outcome.Skipped);
          expect(b.rows[0]!.errCode, `${where}: message, not asserted, was ${JSON.stringify(b.rows[0]!.errMsg)}`).toBe(c.code);
          if (c.emptyCols) expect(b.rows[0]!.values).toEqual([]);
          ran++;
        }
      } finally {
        schema.close();
      }
    }
    // The count assertion. A block that exercised a library and then ran
    // nothing must say so by name rather than pass.
    expect(ran, 'keeps the CSV and TSV rejection codes: ran the wrong number of cases').toBe(4 * rev5.length);
    expect(ran, 'keeps the CSV and TSV rejection codes ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });

  // ------------------------- item 4: the empty CSV field under `=0`

  /**
   * The three column types and the transform reason MEASURED for each when a
   * CSV empty field arrives under `input_format_defaults_for_omitted_fields=0`.
   *
   * ⚠️ These three are WHAT WAS MEASURED, not a closed set. The detector's
   * switch would assign `date_clamp`, `ip_mangle` and others for other column
   * types, and nothing has measured whether an empty field reaches them.
   * Nothing below asserts the set is exactly three, and a fourth column type
   * arriving must not make this test red.
   *
   * The Enum carries a member at value 0: an empty field is not a member
   * spelling, the reader's zero is, and the coercion between the two is what
   * `enum_coerce` names.
   */
  const emptyFieldCases = [
    { name: 'Enum8', ddl: "id UInt8, c Enum8('a' = 0, 'b' = 1)", reason: Reason.EnumCoerce },
    { name: 'FixedString', ddl: 'id UInt8, c FixedString(4)', reason: Reason.FixedStringPad },
    { name: 'UUID', ddl: 'id UInt8, c UUID', reason: Reason.UuidMangle },
  ] as const;

  const reasonsFor = (r: RowResult): string[] => r.transformed.filter((t) => t.column === 'c').map((t) => t.reason);
  const storedFor = (r: RowResult): string | null => r.values.find((v) => v.column === 'c')?.text ?? null;

  /**
   * An empty CSV field under `input_format_defaults_for_omitted_fields=0` is
   * reported as an INPUT and its coercion as a TRANSFORM, where the previous
   * reader reported no transform: the field's reference value is the empty
   * string rather than absent. The STORED VALUE is unchanged — this changes
   * what is reported, not what is stored.
   *
   * ⚠️ The setting is set explicitly on every call. Under the default (`1`) a
   * bare empty field still takes the column's DEFAULT, so a case that forgot
   * the setting would pass without ever exercising this.
   *
   * ⚠️ TSV is NOT affected by the setting: its reader takes an empty field
   * through the typed parse whether the setting is on or off, exactly as the
   * previous splitter did. What is pinned for TSV is that the two settings give
   * the IDENTICAL answer — that is the asymmetry, and a later change making TSV
   * setting-sensitive like CSV turns it red. Note, measured: TSV DOES report
   * `fixedstring_pad` for a FixedString empty field under BOTH settings and has
   * always done so; "TSV is not affected" means the reader swap did not change
   * TSV's answer, never that TSV reports no transform at all. Do not "fix" that
   * by asserting TSV reports nothing.
   */
  it('reports an empty CSV field under defaults=0 as an input and its coercion as a transform', () => {
    let ran = 0;
    for (const library of rev5) {
      for (const c of emptyFieldCases) {
        const schema = library.compileDdl(c.ddl);
        try {
          const where = `${library.version} ${c.name}`;
          const off = schema.row(Format.CSV, utf8('1,\n'), DEFAULTS_OFF);
          const on = schema.row(Format.CSV, utf8('1,\n'), DEFAULTS_ON);

          // The empty field is reported as an INPUT: it is in the stored row,
          // with src `input` — not absent, not a default.
          const offValue = off.values.find((v) => v.column === 'c');
          expect(offValue, `${where}: CSV =0 reported no value at all for the empty field`).toBeDefined();
          expect(offValue!.source, `${where}: the empty field is an input, not an omitted column`).toBe('input');

          // ... and its coercion as a TRANSFORM, with the measured reason.
          expect(reasonsFor(off), `${where}: CSV =0 must report ${c.reason}`).toContain(c.reason);
          // Under the default the same field reports no such transform — which
          // is what makes the setting load-bearing rather than decorative. (Not
          // "no transforms at all": that would be a claim about column types
          // nobody measured.)
          expect(
            reasonsFor(on),
            `${where}: under the default an empty field still takes the column's DEFAULT and ${c.reason} must not appear, or the =0 case proves nothing`,
          ).not.toContain(c.reason);

          // THE STORED VALUE IS UNCHANGED. This changes what is reported, never
          // what is stored.
          expect(storedFor(on), `${where}: this change is about what is REPORTED, never about what is stored`).toBe(storedFor(off));

          // TSV: the setting reaches nothing. The two answers must be the same
          // answer, whatever that answer is.
          const tsvOff = schema.row(Format.TSV, utf8('1\t\n'), DEFAULTS_OFF);
          const tsvOn = schema.row(Format.TSV, utf8('1\t\n'), DEFAULTS_ON);
          const tsvNote = `${where}: TSV's reader takes an empty field through the typed parse whether the setting is on or off; a difference here means TSV has started behaving like CSV`;
          expect([tsvOff.outcome, tsvOff.errCode], tsvNote).toEqual([tsvOn.outcome, tsvOn.errCode]);
          expect(reasonsFor(tsvOff), tsvNote).toEqual(reasonsFor(tsvOn));
          expect(storedFor(tsvOff), tsvNote).toEqual(storedFor(tsvOn));
          console.log(`[chtypes] ${where}: CSV =0 ${reasonsFor(off)} / =1 ${reasonsFor(on)}   TSV =0 ${reasonsFor(tsvOff)} / =1 ${reasonsFor(tsvOn)}`);
          ran++;
        } finally {
          schema.close();
        }
      }
    }
    expect(ran, 'the empty-CSV-field block ran the wrong number of column types').toBe(emptyFieldCases.length * rev5.length);
    expect(ran, 'the empty-CSV-field block ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });

  // ------------------------- item 1: the two `src` provenances

  /**
   * A listed EPHEMERAL column is READ — it is in scope for the DEFAULT
   * expressions that reference it — and is never stored and never exported.
   *
   * The plant that makes this a test rather than a hope: drop
   * `|| c.src === Source.EphemeralInput` from `results.ts`'s values loop and
   * this test goes red on its "values must not contain e" assertion, naming the
   * source the artifact really reported.
   */
  it('reads a listed EPHEMERAL column, stores it nowhere and exports it nowhere', () => {
    let ran = 0;
    for (const library of rev5) {
      // e is EPHEMERAL: it has no value at all outside a column list, and d
      // reads it. d === 6 is the proof the value was read.
      const schema = library.compileDdl('id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1');
      try {
        const where = library.version;
        const r = schema.row(Format.JSONEachRow, utf8('{"id":3,"e":5}'), undefined, { columns: ['id', 'e'] });
        expect(r.outcome, `${where}: code ${r.errCode}`).toBe(Outcome.Accepted);
        const seen = new Map(r.values.map((v) => [v.column, v.source]));
        expect(
          r.values.find((v) => v.column === 'd')?.text,
          `${where}: a listed EPHEMERAL column's value must reach the DEFAULT expressions referencing it`,
        ).toBe('6');
        expect(
          seen.has('e'),
          `${where}: values contains e with src ${seen.get('e')} — a listed EPHEMERAL column is never stored, so it must never sit where a caller reads the stored row`,
        ).toBe(false);
        expect(
          [...seen.values()],
          `${where}: ${Source.EphemeralInput} is excluded from the stored-row view exactly as ${Source.Skipped} is`,
        ).not.toContain(Source.EphemeralInput);

        // ... and never EXPORTED. The exported tuple is the wire tuple —
        // declared order minus MATERIALIZED/ALIAS/EPHEMERAL — so the row is
        // [id, d] and the ephemeral 5 is nowhere in it. Parsed rather than
        // string-compared: the separator spacing is the vendored writer's, not
        // this repository's, and pinning it would test the wrong thing.
        const b = schema.rows(Format.JSONEachRow, utf8('{"id":3,"e":5}\n'), undefined, {
          exportFormat: Format.JSONCompactEachRow,
          columns: ['id', 'e'],
        });
        expect(b.outcome, `${where}: export declined ${JSON.stringify(b.exportDeclined)}`).toBe(Outcome.Accepted);
        expect(b.payload).toBeDefined();
        const fields = JSON.parse(Buffer.from(b.payload!).toString('utf8')) as unknown[];
        expect(fields, `${where}: the wire tuple is declared order minus MATERIALIZED/ALIAS/EPHEMERAL`).toEqual([3, 6]);
        expect(fields, `${where}: a listed EPHEMERAL column is never exported`).not.toContain(5);
        ran++;
      } finally {
        schema.close();
      }
    }
    expect(ran, 'the EPHEMERAL block ran the wrong number of lines').toBe(rev5.length);
    expect(ran, 'the EPHEMERAL block ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });

  /**
   * The other half: the two provenances get OPPOSITE treatment, and folding
   * them together is the mistake this guards.
   *
   * ⚠️ The supplied value must be the stored one: `m Int64 MATERIALIZED id +
   * 10` with id = 1 would compute 11, and 99 is what was sent. Reading 11 here
   * would mean the supplied value never replaced the expression.
   *
   * ⚠️ The export channel DECLINES this row, loudly, and that is the C ABI
   * contract's own rule rather than a defect: the exported tuple is the wire
   * tuple (declared order minus MATERIALIZED/ALIAS/EPHEMERAL), which has no
   * position for a MATERIALIZED column, so bytes that carried the supplied
   * value could not be re-INSERTed. Fail-closed with the reason in
   * `exportDeclined` is what the header specifies, and it is asserted here so a
   * later silent emission would show up.
   */
  it('stores a listed MATERIALIZED column’s supplied value and keeps it in values', () => {
    const allow = { insert_allow_materialized_columns: '1' };
    let ran = 0;
    for (const library of rev5) {
      const schema = library.compileDdl('id UInt32, p UInt8 DEFAULT 3, m Int64 MATERIALIZED id + 10');
      try {
        const where = library.version;
        const r = schema.row(Format.JSONEachRow, utf8('{"id":1,"m":99}'), allow, { columns: ['id', 'm'] });
        expect(r.outcome, `${where}: code ${r.errCode} ${JSON.stringify(r.errMsg)}`).toBe(Outcome.Accepted);
        const m = r.values.find((v) => v.column === 'm');
        expect(
          m,
          `${where}: values has no m — a listed MATERIALIZED column's supplied value IS stored and stays IN the stored-row view, unlike ${Source.EphemeralInput}`,
        ).toBeDefined();
        expect(m!.source).toBe(Source.MaterializedInput);
        // ⚠️ 24.8 renders a stored Int64 as a JSON string and 25.8/26.8 as a
        // number — ClickHouse's own 64-bit quoting changing between lines,
        // which belongs to the artifact and is never normalized here. Both
        // spellings are the same stored value; neither is 11.
        expect(
          ['99', '"99"'],
          `${where}: m is ${m!.text} — want the SUPPLIED 99, not the expression's 11`,
        ).toContain(m!.text);
        expect(
          r.computed.find((c) => c.column === 'm')?.text,
          `${where}: the same stored value, reported twice`,
        ).toBe(m!.text);

        // The export channel's documented refusal.
        const b = schema.rows(Format.JSONEachRow, utf8('{"id":1,"m":99}\n'), allow, {
          exportFormat: Format.JSONCompactEachRow,
          columns: ['id', 'm'],
        });
        expect(
          b.payload === undefined || b.payload.length === 0,
          `${where}: the wire tuple has no position for a MATERIALIZED column, so bytes carrying the supplied value could not be re-INSERTed; the contract is fail-closed`,
        ).toBe(true);
        expect(b.exportDeclined, `${where}: a decline carries its reason`).not.toBe('');
        ran++;
      } finally {
        schema.close();
      }
    }
    expect(ran, 'the MATERIALIZED block ran the wrong number of lines').toBe(rev5.length);
    expect(ran, 'the MATERIALIZED block ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });
});
