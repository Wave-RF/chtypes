/**
 * `Schema.insert` over the generated v2 stub (variant `ok`). The generic stub
 * echoes the arguments it received into `out` and `out_rows`, so these cases read
 * what reached the ABI off the stub's own echo, not off a value the test set:
 * the format, the output format, the filter NULL or set, and `memory_limit_bytes`
 * in the options document. The echo carries no outcome, so the result decodes to
 * unknown(''), never accepted (rule r3). Without `$CHTYPES_ABI2_STUBS` every case
 * SKIPS LOUDLY by name.
 */

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { EXPORT_NONE, Format, outcomeKnown, UsageError } from '../../src/abi2/index.js';
import { type Library, libraryOf } from '../../src/library.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

function openStub(variant: string): { image: LoadedImage; library: Library } {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`no stub variant ${variant}`);
  const image = openAbi2({
    libraryPath: path.join(STUBS_DIR as string, `${variant}.so`),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  return { image, library: libraryOf(image, undefined) };
}

const CREATE = 'CREATE TABLE t (x Int32) ENGINE = Memory';
const BODY = Buffer.from('{"x":1}\n');

/** The stub's echo of one buffer: `{"args":[...],"fn":...,"out":...}`. */
function echo(b: Buffer | undefined): { args: unknown[]; fn: string; out: string } {
  return JSON.parse((b as Buffer).toString());
}

describe.skipIf(!stubsAvailable)('Schema.insert over the stub library', () => {
  it('passes the defaults to the one ABI call: the format, no settings, no columns, NULL filter, the out format, no options', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const spy = vi.spyOn(image.calls, 'previewInsert');
    const r = schema.insert(Format.JSONEachRow, BODY, Format.CSV);
    expect(spy).toHaveBeenCalledTimes(1);
    const args = spy.mock.calls[0] as unknown[];
    expect(args[1]).toBe(Format.JSONEachRow);
    expect((args[3] as Buffer).length).toBe(0);
    expect((args[4] as Buffer).length).toBe(0);
    expect(args[5]).toBeNull();
    expect(args[6]).toBe(Format.CSV);
    expect((args[7] as Buffer).length).toBe(0);
    // The echo names the call and carries no outcome: unknown, never accepted.
    expect(outcomeKnown(r.outcome)).toBe(false);
    spy.mockRestore();
    schema.close();
  });

  it('the stub saw the format, the out format, a NULL filter, and the body, in the ABI order', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const spy = vi.spyOn(image.calls, 'previewInsert');
    schema.insert(Format.JSONEachRow, BODY, Format.CSV);
    const args = spy.mock.calls[0] as unknown[];
    // Replay the same arguments on the generated layer and read the stub's own echo of them.
    const raw = (image.calls.previewInsert as (...a: unknown[]) => { out: Buffer; outRows: Buffer })(...args);
    const e = echo(raw.out);
    expect(e.fn).toBe('chs_preview_insert');
    expect(e.out).toBe('out');
    expect(e.args[1]).toBe(Format.JSONEachRow);
    expect(e.args[5]).toBeNull(); // the filter reached the library as NULL
    expect(e.args[6]).toBe(Format.CSV);
    const rows = echo(raw.outRows);
    expect(rows.fn).toBe('chs_preview_insert');
    expect(rows.out).toBe('out_rows');
    spy.mockRestore();
    schema.close();
  });

  it('a filter reaches the ABI as a handle, settings, columns and memory_limit_bytes as documents', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1');
    const spy = vi.spyOn(image.calls, 'previewInsert');
    schema.insert(Format.JSONEachRow, BODY, Format.JSONCompactEachRow, {
      rowFilter: filter,
      columns: ['x'],
      settings: { a: '1' },
      sessionTimezone: undefined,
      memoryLimitBytes: 4096,
      deferDefaults: 'none',
    });
    const args = spy.mock.calls[0] as unknown[];
    expect(args[5]).not.toBeNull();
    const raw = (image.calls.previewInsert as (...a: unknown[]) => { out: Buffer; outRows: Buffer })(...args);
    const e = echo(raw.out);
    expect(e.args[5]).not.toBeNull();
    expect(e.args[6]).toBe(Format.JSONCompactEachRow);
    // The stub echoes a byte argument as its length and sha256: the options document that reached the library.
    const optionsDoc = Buffer.from('{"defer_defaults":"none","memory_limit_bytes":"4096"}');
    expect(e.args[7]).toMatchObject({ len: optionsDoc.length, sha256: createHash('sha256').update(optionsDoc).digest('hex') });
    expect(JSON.parse((args[7] as Buffer).toString())).toEqual({ defer_defaults: 'none', memory_limit_bytes: '4096' });
    expect(JSON.parse((args[4] as Buffer).toString())).toEqual([{ name: 'x' }]);
    spy.mockRestore();
    filter.close();
    schema.close();
  });

  it('returns the output only when one was asked for, and EXPORT_NONE validates only', () => {
    const { library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const withOut = schema.insert(Format.JSONEachRow, BODY, Format.CSV);
    expect(Buffer.isBuffer(withOut.output)).toBe(true);
    expect(echo(withOut.output).out).toBe('out_rows');
    const none = schema.insert(Format.JSONEachRow, BODY, EXPORT_NONE);
    expect(none.output).toBeUndefined();
    schema.close();
  });

  it('memory_limit_bytes reaches every insert-options call: row, rows, parseBlock and the filter rows', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1');
    const opts = { memoryLimitBytes: 7 };
    const pr = vi.spyOn(image.calls, 'previewRow');
    const pb = vi.spyOn(image.calls, 'previewBatch');
    const bc = vi.spyOn(image.calls, 'blockCreate');
    const fe = vi.spyOn(image.calls, 'filterEvalBody');
    schema.row(Format.JSONEachRow, BODY, opts);
    schema.rows(Format.JSONEachRow, BODY, opts);
    schema.parseBlock(Format.JSONEachRow, BODY, opts).close();
    filter.rows(Format.JSONEachRow, BODY, opts);
    for (const [spy, idx] of [
      [pr, 5],
      [pb, 8],
      [bc, 5],
      [fe, 5],
    ] as const) {
      expect(JSON.parse(((spy.mock.calls[0] as unknown[])[idx] as Buffer).toString())).toEqual({ memory_limit_bytes: '7' });
    }
    vi.restoreAllMocks();
    filter.close();
    schema.close();
  });

  it('refuses a closed schema and a closed filter with a UsageError before any call, never passing NULL', () => {
    const { library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1');
    filter.close();
    expect(() => schema.insert(Format.JSONEachRow, BODY, Format.CSV, { rowFilter: filter })).toThrow(/Filter was already closed/);
    expect(() => schema.insert(Format.JSONEachRow, BODY, Format.CSV, { rowFilter: filter })).toThrow(UsageError);
    schema.close();
    expect(() => schema.insert(Format.JSONEachRow, BODY, Format.CSV)).toThrow(/Schema was already closed/);
    expect(() => schema.insert(Format.JSONEachRow, '{"x":1}' as unknown as Uint8Array, Format.CSV)).toThrow(TypeError);
  });
});
