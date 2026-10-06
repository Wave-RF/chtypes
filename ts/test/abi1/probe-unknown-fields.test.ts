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
