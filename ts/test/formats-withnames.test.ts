/**
 * Issue #119 items 1-5, for `chs_format` 10 (`CSVWithNames`) and 11
 * (`TSVWithNames`): the round trip against a real artifact, the 26.4 / 26.5
 * header-matching boundary from BOTH sides, the INSERT-column-list interplay,
 * and the loud export decline. Until revision 5 was published these formats
 * were declared and executed by nothing; this file executes them.
 *
 * Three rules shape it:
 *
 * - The PROBE, never the enum and never the ABI revision, decides whether an
 *   artifact knows these two values (`docs/reference/bindings.md` §Values a
 *   binding must accept and reject). They joined `enum chs_format` inside
 *   revision 5 after the number was set, so an artifact built from an earlier
 *   revision-5 header reports 5, passes the handshake, and does not know them.
 *   Every line here is asked before it is used, with a header spelled exactly
 *   as the column is declared — a probe whose answer depended on case-folding
 *   would not ask the same question on both sides of the boundary this file
 *   measures.
 * - Every block COUNTS what it ran and fails by name on zero. A boundary case
 *   that skips forever reads as a pass otherwise, which is the one outcome
 *   worse than a red.
 * - Codes and verdicts are asserted; ClickHouse's message text never is.
 */

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  CODE_UNSUPPORTED,
  Format,
  Outcome,
  Registry,
  looksLikeRegistry,
  resolveRegistryDir,
  type BatchResult,
  type Library,
  type RowResult,
  type Value,
} from '../src/index.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

if (!HAVE_REGISTRY) {
  console.warn(
    [
      '',
      '[chtypes] CSVWithNames/TSVWithNames tests SKIPPED: no artifact registry on the search path.',
      '  Fetch one into the per-user cache with scripts/fetch.sh 26.8 (docs/guides/fetch.md),',
      '  or point CHTYPES_REGISTRY at a registry directory.',
      '',
    ].join('\n'),
  );
}

const utf8 = (s: string): Uint8Array => Buffer.from(s, 'utf8');

// The probe schema and the two probe payloads. The header spells `id` and `p`
// exactly as the DDL declares them, so the question is the same on every line.
const WITHNAMES_DDL = 'id UInt8, p String';
const CSV_PROBE = utf8('id,p\n1,a\n');
const TSV_PROBE = utf8('id\tp\n1\ta\n');

// The column-list schema: two DEFAULTs, so "took its DEFAULT" and "took the
// reader's zero" are different observable values rather than both being 0.
const LIST_DDL = 'id UInt8, p UInt8 DEFAULT 7, q UInt8 DEFAULT 9';

const DEFAULTS_FOR_OMITTED = 'input_format_defaults_for_omitted_fields';

interface Line {
  readonly minor: string;
  readonly library: Library;
  readonly supported: boolean;
}

/**
 * True when this line matches header names EXACTLY — through 26.4 — and false
 * from 26.5, where matching is case-insensitive, exactly as those servers do.
 */
function headerMatchingIsExact(minor: string): boolean {
  const [major, feature] = minor.split('.').map((part) => Number.parseInt(part, 10));
  return major! < 26 || (major === 26 && feature! <= 4);
}

function columnValue(row: RowResult, column: string): Value {
  const hit = row.values.find((v) => v.column === column);
  if (hit === undefined) {
    throw new Error(`row has no value for ${column}; it has: ${row.values.map((v) => v.column).join(', ')}`);
  }
  return hit;
}

/**
 * The parts of a row this file compares — verdict, values and unknown fields,
 * never ClickHouse's message text, which the CSV reader change warns consumers
 * not to pin.
 */
function rowShape(row: RowResult): string {
  const values = row.values.map((v) => `${v.column}=${v.text}/${v.source}/null=${v.isNull}`).join(' ');
  const transformed = row.transformed.map((t) => `${t.column}:${t.reason}`).join(' ');
  return (
    `outcome=${row.outcome} code=${row.errCode} values=[${values}] ` +
    `unknown=[${row.unknownFields.join(' ')}] transformed=[${transformed}]`
  );
}

function batchShape(batch: BatchResult): string {
  return (
    `outcome=${batch.outcome} code=${batch.errCode} rowsRead=${batch.rowsRead} ` +
    `rowsSkipped=${batch.rowsSkipped}\n    ${batch.rows.map(rowShape).join('\n    ')}`
  );
}

describe.skipIf(!HAVE_REGISTRY)('#119: CSVWithNames (10) and TSVWithNames (11) over a real artifact', () => {
  let registry: Registry;
  let lines: Line[] = [];
  let supported: Line[] = [];
  let opened = 0;

  beforeAll(() => {
    // Construction reads manifests and dlopens nothing; each line is asked for
    // individually so one artifact at a refused ABI revision does not take the
    // whole registry down with it. That refusal is abi-revision.test.ts's
    // subject, not this file's.
    registry = new Registry(REGISTRY ?? undefined);
    const gathered: Line[] = [];
    for (const minor of registry.versions()) {
      let library: Library;
      try {
        library = registry.for(minor);
      } catch (error) {
        console.warn(`line ${minor} did not open, so it is not measured here: ${String(error)}`);
        continue;
      }
      opened += 1;
      const schema = library.compileDdl(WITHNAMES_DDL);
      let probed = true;
      try {
        // Ask about format 10 and format 11 SEPARATELY — one payload each
        // through chs_rows, and only `accepted` counts. These two need no era
        // refinement: both formats exist on every ClickHouse line measured, so
        // unlike Buffers there is no 73 era to allow for.
        for (const [format, body] of [
          [Format.CSVWithNames, CSV_PROBE],
          [Format.TSVWithNames, TSV_PROBE],
        ] as const) {
          if (schema.rows(format, body).outcome !== Outcome.Accepted) probed = false;
        }
      } finally {
        schema.close();
      }
      if (!probed) {
        console.warn(
          `line ${minor} (ClickHouse ${library.version}) does not answer the ` +
            'CSVWithNames/TSVWithNames probe: a pre-value artifact, not measured for the format cases',
        );
      }
      gathered.push({ minor, library, supported: probed });
    }
    lines = gathered;
    supported = gathered.filter((l) => l.supported);
  });

  afterAll(() => {
    registry?.close();
  });

  it('offers at least one artifact that knows chs_format 10 and 11', (ctx) => {
    if (opened === 0) {
      ctx.skip(
        `no line on the registry ${REGISTRY} opened in this build — fetch a current one with scripts/fetch.sh 26.8`,
      );
    }
    // A registry of pre-value artifacts is a real finding, and a suite that
    // skipped past it would look exactly like one that proved these formats
    // work. These values joined enum chs_format inside ABI revision 5 without
    // bumping it, so an artifact built from an earlier revision-5 header
    // reports 5 and still does not know them.
    expect(
      supported.length,
      `${opened} line(s) opened in ${REGISTRY} and every one of them refused the ` +
        'CSVWithNames/TSVWithNames probe — fetch a current line (scripts/fetch.sh 26.8)',
    ).toBeGreaterThan(0);
  });

  // ---------------------------------------------------------------- item 1

  it('accepts a WithNames body and answers exactly as the headerless equivalent does', () => {
    // The reordered-header cases are the point of the formats: the data is
    // addressed by NAME, so a header naming the columns in the other order
    // still produces the declared-order stored row.
    let cases = 0;
    for (const line of supported) {
      const schema = line.library.compileDdl(WITHNAMES_DDL);
      try {
        for (const [name, named, plain, body, control] of [
          ['CSVWithNames', Format.CSVWithNames, Format.CSV, 'id,p\n1,a\n2,b\n', '1,a\n2,b\n'],
          ['CSVWithNames/reordered header', Format.CSVWithNames, Format.CSV, 'p,id\na,1\nb,2\n', '1,a\n2,b\n'],
          ['TSVWithNames', Format.TSVWithNames, Format.TSV, 'id\tp\n1\ta\n2\tb\n', '1\ta\n2\tb\n'],
          [
            'TSVWithNames/reordered header',
            Format.TSVWithNames,
            Format.TSV,
            'p\tid\na\t1\nb\t2\n',
            '1\ta\n2\tb\n',
          ],
        ] as const) {
          const where = `${line.minor} (${line.library.version}) ${name}`;
          const withNames = schema.rows(named, utf8(body));
          const headerless = schema.rows(plain, utf8(control));
          // Not vacuously equal: two identical failures would satisfy the
          // comparison below and prove nothing.
          expect(
            `${headerless.outcome}/${headerless.rows.length}`,
            `${where}: the headerless control did not accept two rows:\n${batchShape(headerless)}`,
          ).toBe(`${Outcome.Accepted}/2`);
          expect(
            `${withNames.outcome}/${withNames.rows.length}`,
            `${where}: format ${named} did not accept two rows:\n${batchShape(withNames)}`,
          ).toBe(`${Outcome.Accepted}/2`);
          expect(
            batchShape(withNames),
            `${where}: format ${named} answered differently from format ${plain} for the same rows`,
          ).toBe(batchShape(headerless));
          cases += 1;
        }
      } finally {
        schema.close();
      }
    }
    expect(
      cases,
      'this block ran ZERO cases: no line in the registry answered the format 10/11 probe, so ' +
        'nothing was compared against a headerless body',
    ).toBeGreaterThan(0);
    console.log(`${cases} round-trip case(s) over ${supported.length} line(s)`);
  });

  // ---------------------------------------------------------------- item 2

  it('matches header names exactly through 26.4 and case-insensitively from 26.5', () => {
    // The header is `ID,p` (and `ID<TAB>p`) against `id UInt8, p String`, with
    // the row `1,a`:
    //
    //   through 26.4  `ID` names no column: it is an UNKNOWN FIELD and `id`
    //                 takes the reader's zero, absent from the input.
    //   from 26.5     `ID` names `id` case-insensitively: `id` is 1, from
    //                 input, and there is no unknown field.
    let below = 0;
    let above = 0;
    let exactLines = 0;
    let foldingLines = 0;
    for (const line of supported) {
      const exact = headerMatchingIsExact(line.minor);
      if (exact) exactLines += 1;
      else foldingLines += 1;
      const schema = line.library.compileDdl(WITHNAMES_DDL);
      try {
        for (const [name, format, body] of [
          ['CSVWithNames', Format.CSVWithNames, 'ID,p\n1,a\n'],
          ['TSVWithNames', Format.TSVWithNames, 'ID\tp\n1\ta\n'],
        ] as const) {
          const where = `ClickHouse ${line.library.version} ${name}`;
          const res = schema.rows(format, utf8(body));
          expect(`${res.outcome}/${res.rows.length}`, `${where}: want one accepted row:\n${batchShape(res)}`).toBe(
            `${Outcome.Accepted}/1`,
          );
          const row = res.rows[0]!;
          const identifier = columnValue(row, 'id');
          if (exact) {
            expect(
              `${identifier.source}|${identifier.text}|${row.unknownFields.join(' ')}`,
              `${where} matches header names exactly (through 26.4), so \`ID\` must not bind to ` +
                `\`id\`: \`id\` takes the reader's zero and \`ID\` is an unknown field; got ${rowShape(row)}`,
            ).toBe('absent|0|ID');
            below += 1;
          } else {
            expect(
              `${identifier.source}|${identifier.text}|${row.unknownFields.join(' ')}`,
              `${where} matches header names case-insensitively (from 26.5), so \`ID\` must bind ` +
                `to \`id\` and leave no unknown field; got ${rowShape(row)}`,
            ).toBe('input|1|');
            above += 1;
          }
          console.log(`${where}: ${rowShape(row)}`);
        }
      } finally {
        schema.close();
      }
    }
    // Both sides, or this proved nothing. A registry holding only lines above
    // the boundary answers every case the same way and says nothing about where
    // the behavior changes.
    expect(
      below,
      'this block ran ZERO cases BELOW the 26.5 boundary: the registry holds no line at or under ' +
        `26.4 that knows formats 10 and 11 (it offered ${foldingLines} line(s) above it). Header ` +
        'matching is exact through 26.4 and case-insensitive from 26.5, and one side alone cannot ' +
        'show that — install an older line (scripts/fetch.sh 24.8, or 26.4)',
    ).toBeGreaterThan(0);
    expect(
      above,
      'this block ran ZERO cases AT OR ABOVE the 26.5 boundary: the registry holds no line from ' +
        `26.5 on that knows formats 10 and 11 (it offered ${exactLines} line(s) below it). Install ` +
        'a current line (scripts/fetch.sh 26.8)',
    ).toBeGreaterThan(0);
    console.log(
      `${below} case(s) below the boundary over ${exactLines} line(s), ` +
        `${above} at or above it over ${foldingLines} line(s)`,
    );
  });

  // ---------------------------------------------------------------- item 3

  it('lets the column list decide the block while the header decides the layout', () => {
    // A listed column the header omits takes its DEFAULT under
    // input_format_defaults_for_omitted_fields=1 and the reader's zero under 0;
    // an unlisted column the header names is an unknown field; an unlisted
    // column takes its DEFAULT whatever that setting says. The last rule is NOT
    // a WithNames rule — the 0.3.0 CHANGELOG states it applies to every format
    // — so it is executed on JSONEachRow and on headerless CSV and TSV too.
    let omitted = 0;
    let unknown = 0;
    let unlisted = 0;
    for (const line of supported) {
      const schema = line.library.compileDdl(LIST_DDL);
      try {
        for (const [name, format, defaults, want] of [
          ['CSVWithNames', Format.CSVWithNames, '1', '7/default'],
          ['CSVWithNames', Format.CSVWithNames, '0', '0/absent'],
          ['TSVWithNames', Format.TSVWithNames, '1', '7/default'],
          ['TSVWithNames', Format.TSVWithNames, '0', '0/absent'],
        ] as const) {
          const where = `ClickHouse ${line.library.version} ${name} omitted-defaults=${defaults}`;
          const res = schema.rows(format, utf8('id\n1\n'), { [DEFAULTS_FOR_OMITTED]: defaults }, {
            columns: ['id', 'p'],
          });
          expect(`${res.outcome}/${res.rows.length}`, `${where}: want one accepted row:\n${batchShape(res)}`).toBe(
            `${Outcome.Accepted}/1`,
          );
          const p = columnValue(res.rows[0]!, 'p');
          expect(
            `${p.text}/${p.source}`,
            `${where}: \`p\` is listed and the header omits it; got ${rowShape(res.rows[0]!)}`,
          ).toBe(want);
          omitted += 1;
        }

        for (const [name, format, body] of [
          ['CSVWithNames', Format.CSVWithNames, 'id,p\n1,3\n'],
          ['TSVWithNames', Format.TSVWithNames, 'id\tp\n1\t3\n'],
        ] as const) {
          const where = `ClickHouse ${line.library.version} ${name}`;
          const res = schema.rows(format, utf8(body), undefined, { columns: ['id'] });
          expect(`${res.outcome}/${res.rows.length}`, `${where}: want one accepted row:\n${batchShape(res)}`).toBe(
            `${Outcome.Accepted}/1`,
          );
          const row = res.rows[0]!;
          expect(
            row.unknownFields.join(' '),
            `${where}: \`p\` is not in the column list, so the header naming it is an unknown ` +
              `field; got ${rowShape(row)}`,
          ).toBe('p');
          const p = columnValue(row, 'p');
          expect(
            `${p.text}/${p.source}`,
            `${where}: \`p\` is unlisted, so it takes its DEFAULT 7; got ${rowShape(row)}`,
          ).toBe('7/default');
          unknown += 1;
        }

        for (const [name, format, body] of [
          ['JSONEachRow', Format.JSONEachRow, '{"id":1}\n'],
          ['CSV', Format.CSV, '1\n'],
          ['TSV', Format.TSV, '1\n'],
        ] as const) {
          for (const defaults of ['0', '1'] as const) {
            const where = `ClickHouse ${line.library.version} ${name} omitted-defaults=${defaults}`;
            const res = schema.rows(format, utf8(body), { [DEFAULTS_FOR_OMITTED]: defaults }, {
              columns: ['id'],
            });
            expect(`${res.outcome}/${res.rows.length}`, `${where}: want one accepted row:\n${batchShape(res)}`).toBe(
              `${Outcome.Accepted}/1`,
            );
            const row = res.rows[0]!;
            for (const [column, text] of [
              ['p', '7'],
              ['q', '9'],
            ] as const) {
              const value = columnValue(row, column);
              expect(
                `${value.text}/${value.source}`,
                `${where}: \`${column}\` is not in the column list, so it takes its DEFAULT ` +
                  `${text} whatever ${DEFAULTS_FOR_OMITTED} says; got ${rowShape(row)}`,
              ).toBe(`${text}/default`);
            }
            unlisted += 1;
          }
        }
      } finally {
        schema.close();
      }
    }
    expect(
      `${omitted > 0}/${unknown > 0}/${unlisted > 0}`,
      'this block ran ZERO cases in at least one sub-block: ' +
        `listed-column-the-header-omits=${omitted}, unlisted-column-the-header-names=${unknown}, ` +
        `unlisted-column-takes-its-DEFAULT=${unlisted} — each must run at least once`,
    ).toBe('true/true/true');
    console.log(
      `${omitted} omitted-listed-column case(s), ${unknown} unknown-field case(s), ` +
        `${unlisted} unlisted-DEFAULT case(s) over ${supported.length} line(s)`,
    );
  });

  // ---------------------------------------------------------------- item 4

  it('declines formats 10 and 11 as an export format, loudly', () => {
    // This one needs no probe — an artifact that cannot SERIALIZE a format
    // declines it whether or not it can READ it — so it runs against every line
    // that opened.
    let cases = 0;
    for (const line of lines) {
      const schema = line.library.compileDdl(WITHNAMES_DDL);
      try {
        for (const format of [Format.CSVWithNames, Format.TSVWithNames] as const) {
          const where = `ClickHouse ${line.library.version} exportFormat ${format}`;
          const res = schema.rows(Format.JSONEachRow, utf8('{"id":1,"p":"a"}\n'), undefined, {
            exportFormat: format,
          });
          expect(res.outcome, `${where} must be declined, loudly:\n${batchShape(res)}`).toBe(Outcome.Unsupported);
          expect(res.errCode, `${where} declined with the wrong code`).toBe(CODE_UNSUPPORTED);
          expect(res.payload, `${where} was declined and still produced payload bytes`).toBeUndefined();
          expect(
            `${res.rows.length}/${res.rowsRead}`,
            `${where}: a declined export processes nothing:\n${batchShape(res)}`,
          ).toBe('0/0');
          cases += 1;
        }
      } finally {
        schema.close();
      }
    }
    expect(
      cases,
      'this block ran ZERO cases: no line in the registry opened, so the export decline for ' +
        'formats 10 and 11 was never asked for',
    ).toBeGreaterThan(0);
    console.log(`${cases} export-decline case(s) over ${lines.length} line(s)`);
  });
});
