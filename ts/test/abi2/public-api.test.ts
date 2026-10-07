/**
 * The public API over the stub library (`$CHTYPES_ABI2_STUBS`): the typed
 * wrappers, the error table, the handle rules and the finalizers, end to end
 * through a real `dlopen`.
 *
 * Without `$CHTYPES_ABI2_STUBS` every case here SKIPS LOUDLY by name; it never
 * passes silently. The stub echoes its inputs and answers a forced status when
 * an input starts with `!S:<status name>:<code>:<name>:<message>`, so a
 * document is the stub's echo and an error is whichever one the test forces.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { HANDLE_INFO } from '../../src/abi2/decls.gen.js';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import {
  CallError,
  ChtypesError,
  DocFlags,
  EXPORT_NONE,
  Format,
  filterOutcomeKnown,
  InternalError,
  outcomeKnown,
  SchemaError,
  Status,
  UnsupportedError,
  UsageError,
} from '../../src/abi2/index.js';
import { type Library, libraryOf, openUnverified } from '../../src/library.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

interface StubVariant {
  readonly predicate: Predicate;
}

function openStub(variant: string): { image: LoadedImage; library: Library } {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, StubVariant>;
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

const schemaKind = Object.keys(HANDLE_INFO).filter((k) => HANDLE_INFO[k]?.className === 'Schema')[0] as string;
const filterKind = Object.keys(HANDLE_INFO).filter((k) => HANDLE_INFO[k]?.className === 'Filter')[0] as string;
const blockKind = Object.keys(HANDLE_INFO).filter((k) => HANDLE_INFO[k]?.className === 'Block')[0] as string;

const CREATE = 'CREATE TABLE t (x Int32) ENGINE = Memory';
const BODY = Buffer.from('{"x":1}\n');

function forced(status: string): string {
  return `!S:${status}:77:TEST_CODE:forced ${status}`;
}

describe.skipIf(!stubsAvailable)('the public API over the stub library', () => {
  it('reads the build info the loader decoded: version and minor are read, not derived', () => {
    const { library } = openStub('ok');
    expect(library.version).toBe('26.8.15.10');
    expect(library.minor).toBe('26.8');
    expect(library.buildInfo.channel).toBe('lts');
    expect(library.buildInfo.capabilities.features).toContain('default_generators');
    expect(library.buildInfo.raw.toString('ascii').startsWith('{"schema":1')).toBe(true);
    expect(library.resolved).toBeUndefined();
    expect(library.path.endsWith('ok.so')).toBe(true);
  });

  it('hands out one Library per image', () => {
    const { image, library } = openStub('ok');
    expect(libraryOf(image, undefined)).toBe(library);
    expect(openStub('ok').library).toBe(library);
  });

  it('returns each byte output as a Buffer and decodes the documents without computing anything', () => {
    const { library } = openStub('ok');
    expect(Buffer.isBuffer(library.validateType('Int32'))).toBe(true);
    expect(Buffer.isBuffer(library.quoteIdentifier(new Uint8Array([0xff, 0x00])))).toBe(true);
    expect(Buffer.isBuffer(library.quoteIdentifierIfNeeded('a'))).toBe(true);
    expect(Buffer.isBuffer(library.quoteLiteral('a'))).toBe(true);
    expect(Buffer.isBuffer(library.discoverQuery())).toBe(true);
    expect(library.discoverColumns(Buffer.from('{}')).columns).toEqual([]);
    const schema = library.compileTable(CREATE);
    expect(schema.describe().columns).toEqual([]);
    // The stub's echo carries no outcome: rule r3 keeps it as unknown(''), never a fallback and never accepted.
    const echoed = schema.row(Format.JSONEachRow, BODY).outcome;
    expect(echoed).toBe('');
    expect(outcomeKnown(echoed)).toBe(false);
    schema.close();
  });

  it('treats a document that does not decode as the library bug it is: an InternalError', () => {
    const { library } = openStub('ok');
    // The stub's error-code table is an object, not the array the document contract requires.
    expect(() => library.errorCodes()).toThrow(InternalError);
  });

  it('maps each call status to its class, with the five fields verbatim; the classes are peers, never subtypes', () => {
    const { library } = openStub('ok');
    const cases: [string, new (...a: never[]) => CallError, number][] = [
      ['CHS_REJECTED', SchemaError, Status.Rejected],
      ['CHS_DECLINED', UnsupportedError, Status.Declined],
      ['CHS_INVALID_ARGUMENT', UsageError, Status.InvalidArgument],
      ['CHS_INTERNAL', InternalError, Status.Internal],
    ];
    for (const [status, cls, value] of cases) {
      let caught: unknown;
      try {
        library.compileTable(forced(status));
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(cls);
      expect(caught).toBeInstanceOf(CallError);
      expect(caught).toBeInstanceOf(ChtypesError);
      const e = caught as CallError;
      expect(e.status).toBe(value);
      expect(e.chCode).toBe(77);
      expect(e.chName).toBe('TEST_CODE');
      expect(e.messageBytes.toString()).toBe(`forced ${status}`);
      expect(e.column).toHaveLength(0);
      for (const [, other] of cases) {
        if (other !== cls) expect(caught).not.toBeInstanceOf(other);
      }
    }
  });

  it('raises a UsageError naming the object once it is closed, before any call, for every method', () => {
    const { library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1');
    const block = schema.parseBlock(Format.JSONEachRow, BODY);
    schema.close();
    schema.close(); // idempotent
    for (const use of [
      () => schema.describe(),
      () => schema.row(Format.JSONEachRow, BODY),
      () => schema.rows(Format.JSONEachRow, BODY),
      () => schema.compileFilter('x = 1'),
      () => schema.parseBlock(Format.JSONEachRow, BODY),
    ]) {
      expect(use).toThrow(UsageError);
      expect(use).toThrow(/Schema was already closed/);
    }
    // A schema closed first leaves its filter and block working: they hold their own reference.
    expect(filterOutcomeKnown(filter.rows(Format.JSONEachRow, BODY).outcome)).toBe(false);
    expect(filterOutcomeKnown(filter.eval(block).outcome)).toBe(false);
    block.close();
    expect(() => filter.eval(block)).toThrow(/Block was already closed/);
    filter.close();
    expect(() => filter.rows(Format.JSONEachRow, BODY)).toThrow(/Filter was already closed/);
    const err = (() => {
      try {
        filter.rows(Format.JSONEachRow, BODY);
      } catch (e) {
        return e as UsageError;
      }
    })();
    expect(err?.status).toBe(Status.InvalidArgument);
    expect(err?.chCode).toBe(0);
    expect(err?.chName).toBe('');
    expect(err?.column).toHaveLength(0);
  });

  it('passes the documented defaults to the one ABI call: no export, every document group, no filter, no settings', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const spy = vi.spyOn(image.calls, 'previewBatch');
    schema.rows(Format.JSONEachRow, BODY);
    expect(spy).toHaveBeenCalledTimes(1);
    const args = spy.mock.calls[0] as unknown[];
    expect(args[1]).toBe(Format.JSONEachRow);
    expect((args[3] as Buffer).length).toBe(0); // settings: none
    expect((args[4] as Buffer).length).toBe(0); // columns: none
    expect(args[5]).toBeNull(); // no filter
    expect(args[6]).toBe(EXPORT_NONE);
    expect(args[7]).toBe(DocFlags.All);
    spy.mockRestore();
    schema.close();
  });

  it('asks for the export it was given, attaches the filter, and returns the export payload only when one was asked for', () => {
    const { image, library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1', { params: { p: '1' }, sessionTimezone: 'Europe/Berlin' });
    const spy = vi.spyOn(image.calls, 'previewBatch');
    const without = schema.rows(Format.JSONEachRow, BODY, { sessionTimezone: 'Asia/Tokyo' });
    const withExport = schema.rows(Format.JSONEachRow, BODY, {
      exportFormat: Format.CSV,
      docFlags: DocFlags.Values,
      rowFilter: filter,
      columns: ['x'],
      settings: { a: '1' },
    });
    expect(without.payload).toBeUndefined();
    expect(Buffer.isBuffer(withExport.payload)).toBe(true);
    const second = spy.mock.calls[1] as unknown[];
    expect(second[5]).not.toBeNull();
    expect(second[6]).toBe(Format.CSV);
    expect(second[7]).toBe(DocFlags.Values);
    expect((second[3] as Buffer).toString()).toBe('{"a":"1"}');
    expect(JSON.parse((second[4] as Buffer).toString())).toEqual([{ name: 'x' }]);
    expect(((spy.mock.calls[0] as unknown[])[3] as Buffer).toString()).toBe('{"session_timezone":"Asia/Tokyo"}');
    spy.mockRestore();
    filter.close();
    schema.close();
  });

  it('refuses the zone twice, and a text body, before any call', () => {
    const { library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    expect(() => schema.row(Format.JSONEachRow, BODY, { settings: { session_timezone: 'UTC' }, sessionTimezone: 'UTC' })).toThrow(UsageError);
    expect(() => schema.row(Format.JSONEachRow, '{"x":1}' as unknown as Uint8Array)).toThrow(TypeError);
    schema.close();
  });

  it('counts zero live handles after every handle is closed', () => {
    const { library } = openStub('ok');
    const before = library.liveHandles();
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x = 1');
    const block = schema.parseBlock(Format.JSONEachRow, BODY);
    const during = library.liveHandles();
    expect(during[schemaKind]).toBe((before[schemaKind] ?? 0) + 1);
    expect(during[filterKind]).toBe((before[filterKind] ?? 0) + 1);
    expect(during[blockKind]).toBe((before[blockKind] ?? 0) + 1);
    block.close();
    filter.close();
    schema.close();
    expect(library.liveHandles()).toEqual(before);
  });

  it('frees what the caller abandons: abandon handles, collect, require every count to be zero', async () => {
    const { library } = openStub('ok');
    (() => {
      for (let i = 0; i < 25; i++) {
        const schema = library.compileTable(CREATE);
        schema.compileFilter('x = 1');
        schema.parseBlock(Format.JSONEachRow, BODY);
      }
    })();
    const gc = (globalThis as { gc?: () => void }).gc;
    expect(typeof gc, 'run under --expose-gc (vitest.config.ts passes it)').toBe('function');
    const allZero = (counts: Readonly<Record<string, number>>): boolean => Object.values(counts).every((n) => n === 0);
    let counts = library.liveHandles();
    for (let i = 0; i < 100 && !allZero(counts); i++) {
      gc?.();
      await new Promise((r) => setTimeout(r, 20));
      counts = library.liveHandles();
    }
    expect(counts).toEqual(Object.fromEntries(Object.keys(counts).map((k) => [k, 0])));
  });

  it('decodes the stub\'s real document mode: a string with bytes FF 00 80 comes out as exact bytes, NUL included', () => {
    const { library } = openStub('ok');
    const schema = library.compileTable(CREATE);
    const binary = schema.row(Format.JSONEachRow, Buffer.from([0x21, 0x44, 0x3a, 0xff, 0x00, 0x80]));
    const v = binary.columns[0];
    expect(v?.column.toString()).toBe('s');
    expect([...(v?.text ?? [])]).toEqual([0xff, 0x00, 0x80]);
    expect([...(v?.value ?? [])]).toEqual([0xff, 0x00, 0x80]);
    expect(binary.outcome).toBe('accepted');
    expect(binary.inputSpan).toEqual({ off: 0, len: 6 });
    const text = schema.row(Format.JSONEachRow, Buffer.from([0x21, 0x44, 0x3a, 0x61, 0x00, 0x62]));
    expect([...(text.columns[0]?.text ?? [])]).toEqual([0x61, 0x00, 0x62]);
    expect([...(text.columns[0]?.value ?? [])]).toEqual([0x61, 0x00, 0x62]);
    schema.close();
  });

  it('decodes the stub\'s discovery document with a non-UTF-8 name and joined declarations', () => {
    const { library } = openStub('ok');
    const d = library.discoverColumns(Buffer.from([0x21, 0x44, 0x3a, 0xff, 0x63, 0x6f, 0x6c]));
    expect([...(d.columns[0]?.name ?? [])]).toEqual([0xff, 0x63, 0x6f, 0x6c]);
    expect([...(d.columns[0]?.declaration ?? [])]).toEqual([...Buffer.from('/2NvbCBTdHJpbmc=', 'base64')]);
    expect([...d.columnsSql]).toEqual([...Buffer.from('/2NvbCBTdHJpbmc=', 'base64')]);
  });

  it('refuses an unverified open unless BOTH allow and the environment variable say so, with a UsageError', () => {
    const saved = process.env.CHTYPES_ALLOW_UNVERIFIED_LIBRARY;
    try {
      const target = path.join(STUBS_DIR as string, 'ok.so');
      delete process.env.CHTYPES_ALLOW_UNVERIFIED_LIBRARY;
      expect(() => openUnverified(target, { allow: true })).toThrow(UsageError);
      process.env.CHTYPES_ALLOW_UNVERIFIED_LIBRARY = '1';
      expect(() => openUnverified(target, { allow: false })).toThrow(UsageError);
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const lib = openUnverified(target, { allow: true });
      expect(lib.version).toBe('26.8.15.10');
      warn.mockRestore();
    } finally {
      if (saved === undefined) delete process.env.CHTYPES_ALLOW_UNVERIFIED_LIBRARY;
      else process.env.CHTYPES_ALLOW_UNVERIFIED_LIBRARY = saved;
    }
  });
});
