/**
 * PROBE (do not merge): do the RELEASED 1.0.4 decoders tolerate members no 1.0
 * description names? The "ok-x" stub (scripts/abi-v1/emit/_stubshared.py
 * PROBE_X_DOCS) answers every document-returning call with a document carrying
 * unknown members at every object level; its build_info and live_handles carry
 * unknown members too. Each case decodes one document through the public API
 * and checks the known fields still decode.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { Format } from '../../src/abi1/index.js';
import { openAbi1, type Predicate } from '../../src/abi1/loader.js';
import { type Library, libraryOf } from '../../src/library.js';

const STUBS_DIR = process.env.CHTYPES_ABI1_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;
const BODY = Buffer.from('{"x":1}');
const CREATE = 'CREATE TABLE t (x Int32)';

let cached: Library | undefined;
function probeLibrary(): Library {
  if (cached !== undefined) return cached;
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants['ok-x'];
  if (v === undefined) throw new Error('no stub variant ok-x');
  const image = openAbi1({
    libraryPath: path.join(STUBS_DIR as string, 'ok-x.so'),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  cached = libraryOf(image, undefined);
  return cached;
}

describe.skipIf(!stubsAvailable)('PROBE unknown members in every document (ok-x stub)', () => {
  it('probe build_info', () => {
    const info = probeLibrary().buildInfo;
    expect(info.clickhouseVersion).toBe('26.8.15.10');
    expect(info.channel).toBe('lts');
    expect(info.abi).toBe(1);
    expect(info.capabilities.features).toContain('default_generators');
  });

  it('probe live_handles', () => {
    const live = probeLibrary().liveHandles();
    console.log(`live handles decoded as ${JSON.stringify(live)}`);
    expect(Object.keys(live)).toContain('chs_schema');
  });

  it('probe error_code_table', () => {
    expect(probeLibrary().errorCodes().name(53)).toBe('TYPE_MISMATCH');
  });

  it('probe schema_description', () => {
    const d = probeLibrary().compileTable(CREATE).describe();
    expect(d.columns.length).toBe(1);
    expect(d.columns[0]?.name.toString()).toBe('x');
    expect(d.columns[0]?.type.toString()).toBe('Int32');
  });

  it('probe row', () => {
    const r = probeLibrary().compileTable(CREATE).row(Format.JSONEachRow, BODY);
    expect(r.outcome).toBe('accepted');
    expect(r.columns.length).toBe(1);
    expect(r.columns[0]?.text.toString()).toBe('abc');
    expect(r.inputSpan?.len).toBe(3);
    expect(r.computed.length).toBe(1);
    expect(r.transformed.length).toBe(1);
    expect(r.unknownFields.length).toBe(1);
    expect(r.unsupportedSettings.length).toBe(1);
  });

  it('probe batch', () => {
    const b = probeLibrary().compileTable(CREATE).rows(Format.JSONEachRow, BODY, { exportFormat: Format.JSONEachRow });
    expect(b.outcome).toBe('accepted');
    expect(b.rowsRead).toBe(1);
    expect(b.rows.length).toBe(1);
    expect(b.rows[0]?.columns.length).toBe(1);
    expect(b.engineRows?.length).toBe(1);
    expect(b.spans?.length).toBe(1);
    expect(b.unconsumed.length).toBe(1);
    expect(b.framing?.header?.names.length).toBe(1);
    expect(b.payload?.toString()).toBe('{"s":"abc"}\n');
  });

  it('probe filter_result', () => {
    const f = probeLibrary().compileTable(CREATE).compileFilter('x > 1').rows(Format.JSONEachRow, BODY);
    expect(f.outcome).toBe('ok');
    expect(f.rowsRead).toBe(2);
    expect(f.verdicts.length).toBe(2);
    expect(f.errors.length).toBe(1);
    expect(f.unsupportedSettings.length).toBe(1);
  });

  it('probe discovery', () => {
    const d = probeLibrary().discoverColumns(Buffer.from('{}'));
    expect(d.columns.length).toBe(1);
    expect(d.columns[0]?.declaration.toString()).toBe('c String');
    expect(d.columnsSql.toString()).toBe('c String');
  });
});

// --- the second question: an UNKNOWN ENUM VALUE (stub variants ok-e, ok-e-bi).
// Each probe RECORDS what the released decoder did, as a PROBE-ENUM line; it
// asserts nothing beyond "no crash", because every outcome is a finding.

function openVariant(variant: string): Library {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`no stub variant ${variant}`);
  const image = openAbi1({
    libraryPath: path.join(STUBS_DIR as string, `${variant}.so`),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  return libraryOf(image, undefined);
}

function show(value: unknown): string {
  return JSON.stringify(value, (_k, x) => (Buffer.isBuffer(x) ? `<buf ${x.toString()}>` : x));
}

function report(id: string, fn: () => string): void {
  try {
    console.log(`PROBE-ENUM ts ${id} => ${fn()}`);
  } catch (e) {
    const err = e as Error;
    console.log(`PROBE-ENUM ts ${id} => ERROR ${err.constructor.name}: ${String(err.message).slice(0, 300)}`);
  }
}

describe.skipIf(!stubsAvailable)('PROBE unknown enum values (ok-e, ok-e-bi stubs)', () => {
  it('probe unknown enum values', () => {
    report('build_info.capabilities', () => show(openVariant('ok-e-bi').buildInfo.capabilities));
    const lib = openVariant('ok-e');
    report('status.unknown', () => show(lib.validateType('!U:')));
    report('describe.default_kind', () =>
      show(lib.compileTable('!E:describe.default_kind').describe().columns[0]?.defaultKind),
    );
    const clean = lib.compileTable(CREATE);
    report('describe.control', () => show(clean.describe().columns[0]?.defaultKind));
    const row = (id: string, pick: (r: ReturnType<typeof clean.row>) => unknown) =>
      report(id, () => show(pick(clean.row(Format.JSONEachRow, Buffer.from(`!E:${id}`)))));
    row('row.outcome', (r) => ({ outcome: r.outcome }));
    row('row.cols.src', (r) => ({ source: r.columns[0]?.source, isStored: r.columns[0]?.isStored }));
    row('row.transformed.reason', (r) => ({ reason: r.transformed[0]?.reason, lossy: r.transformed[0]?.lossy }));
    row('row.verdict', (r) => ({ verdict: r.verdict ?? 'undefined' }));
    report('row.control', () => show(clean.row(Format.JSONEachRow, Buffer.from('{}')).outcome));
    const batch = (id: string, pick: (b: ReturnType<typeof clean.rows>) => unknown) =>
      report(id, () =>
        show(pick(clean.rows(Format.JSONEachRow, Buffer.from(`!E:${id}`), { exportFormat: Format.JSONEachRow }))),
      );
    batch('batch.outcome', (b) => ({ outcome: b.outcome }));
    batch('batch.rows.outcome', (b) => ({ outcome: b.rows[0]?.outcome }));
    batch('batch.rows.cols.src', (b) => ({
      source: b.rows[0]?.columns[0]?.source,
      isStored: b.rows[0]?.columns[0]?.isStored,
    }));
    batch('batch.transformed.reason', (b) => ({ reason: b.transformed[0]?.reason, lossy: b.transformed[0]?.lossy }));
    batch('batch.framing.container', (b) => ({ container: b.framing?.container }));
    batch('batch.control', (b) => ({ outcome: b.outcome }));
    const filter = clean.compileFilter('x > 1');
    for (const id of ['filter.outcome', 'filter.verdicts', 'filter.control']) {
      const body = id === 'filter.control' ? Buffer.from('{}') : Buffer.from(`!E:${id}`);
      report(id, () => {
        const f = filter.rows(Format.JSONEachRow, body);
        return show({ outcome: f.outcome, verdicts: f.verdicts });
      });
    }
    report('discovery.default_kind', () => show(lib.discoverColumns(Buffer.from('!E:discovery.default_kind')).columns));
    expect(true).toBe(true);
  });
});
