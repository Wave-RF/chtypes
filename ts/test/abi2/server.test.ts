/**
 * The server profile (`Library.newServer`, `CompileOptions.server`,
 * `Server.close` and the description's `server` and `replicated` members). The
 * profile document and the description decoder are checked with no library
 * (they never skip); everything else runs over the ABI v2 test stub
 * (`$CHTYPES_ABI2_STUBS`), whose `chs_schema_create` takes a counted reference
 * to the server it RECEIVES, so the stub's own `live_handles` counters say
 * whether a non-NULL server reached it. Without the stub those cases skip
 * loudly by name.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { DeclinedLayer, declinedLayerKnown, DeclinedTier, declinedTierKnown, InternalError, Status, UsageError } from '../../src/abi2/index.js';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { decodeSchemaDescription } from '../../src/documents.js';
import { type Library, libraryOf } from '../../src/library.js';
import { encodeServerProfile, type ServerProfile } from '../../src/settings.js';

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

const STMT = 'CREATE TABLE t (k UInt8) ENGINE = Memory';
const liveServers = (lib: Library): number => lib.liveHandles().chs_server as number;

describe('the profile document, byte for byte (no library)', () => {
  const cases: [string, ServerProfile, string][] = [
    ['empty: nothing described', {}, '{}'],
    ['timezone', { timezone: 'Asia/Tokyo' }, '{"timezone":"Asia/Tokyo"}'],
    ['an empty timezone is omitted', { timezone: '' }, '{}'],
    ['absent settings omitted', { timezone: 'UTC', settings: undefined }, '{"timezone":"UTC"}'],
    ['empty settings sent', { settings: {} }, '{"settings":{}}'],
    ['absent macros omitted (unknown)', { macros: undefined }, '{}'],
    ['empty macros sent (the complete set is empty)', { macros: {} }, '{"macros":{}}'],
    [
      'every member',
      { timezone: 'Europe/Berlin', settings: { date_time_input_format: 'best_effort', max_threads: '8' }, macros: { replica: 'r1', shard: '01' } },
      '{"timezone":"Europe/Berlin","settings":{"date_time_input_format":"best_effort","max_threads":"8"},"macros":{"replica":"r1","shard":"01"}}',
    ],
    // Passed through as given: the library refuses a zone it cannot load, a setting no
    // server knows, a value no server parses and a macro name no reader keeps.
    [
      'never validated',
      { timezone: 'Not/AZone', settings: { no_such_setting: 'eight' }, macros: { '': 'x', 'a.b': '<&>' } },
      '{"timezone":"Not/AZone","settings":{"no_such_setting":"eight"},"macros":{"":"x","a.b":"<&>"}}',
    ],
  ];
  for (const [name, profile, want] of cases) {
    it(name, () => {
      expect(encodeServerProfile(profile).toString('utf8')).toBe(want);
    });
  }

  it('absent macros is not empty macros: fails if the two serialize alike', () => {
    const absent = encodeServerProfile({ timezone: 'UTC' }).toString();
    const empty = encodeServerProfile({ timezone: 'UTC', macros: {} }).toString();
    expect(absent).not.toBe(empty);
    expect(absent).not.toContain('macros');
    expect(empty).toContain('"macros":{}');
  });

  it('a value that is not a string is the language type error, not a rewritten value', () => {
    expect(() => encodeServerProfile({ settings: { max_threads: 8 as unknown as string } })).toThrow(TypeError);
    expect(() => encodeServerProfile({ macros: { shard: 1 as unknown as string } })).toThrow(TypeError);
  });
});

describe('the description decoder: server and replicated (no library)', () => {
  const cols = '"columns":[{"name":"k","type":"UInt8","default_kind":"","default_expression":""}]';
  const decode = (s: string) => decodeSchemaDescription(Buffer.from(s, 'utf8'));

  it('absent: the document is the one from before servers existed', () => {
    const d = decode(`{${cols}}`);
    expect(d.server).toBeUndefined();
    expect(d.replicated).toBeUndefined();
    expect(d.columns).toHaveLength(1);
  });

  it('a server described by {}: the image zone, settings {}, macros absent (unknown)', () => {
    const d = decode(`{${cols},"server":{"timezone":"UTC","settings":{}}}`);
    expect(d.server?.timezone).toBe('UTC');
    expect(d.server?.settings).toEqual({});
    expect(d.server?.macros).toBeUndefined();
  });

  it('macros present but empty is the complete (empty) set, never read as absent', () => {
    const d = decode(`{${cols},"server":{"timezone":"Asia/Tokyo","settings":{"max_threads":"8"},"macros":{}}}`);
    expect(d.server?.timezone).toBe('Asia/Tokyo');
    expect(d.server?.settings).toEqual({ max_threads: '8' });
    expect(d.server?.macros).toEqual({});
    expect(d.server?.macros).not.toBeUndefined();
  });

  it('macros, a Replicated engine\'s resolved path, a _b64 name and unknown members (rule r2)', () => {
    const d = decode(
      `{${cols},"server":{"timezone":"UTC","settings":{},"macros":{"shard":"01","replica":"r1"},"x_future":1},` +
        '"replicated":{"zookeeper_path":"/clickhouse/tables/01/db/t","replica_name_b64":"/3Ix","x_future":{"a":[1]}},"x_future_top":[1]}',
    );
    expect(d.server?.macros).toEqual({ shard: '01', replica: 'r1' });
    expect(d.replicated?.zookeeperPath.toString()).toBe('/clickhouse/tables/01/db/t');
    expect([...(d.replicated?.replicaName ?? [])]).toEqual([0xff, 0x72, 0x31]);
  });

  it('refusals are each an InternalError naming the document', () => {
    for (const doc of [
      `{${cols},"server":"UTC"}`,
      `{${cols},"server":{"timezone":"UTC","settings":{"max_threads":8}}}`,
      `{${cols},"server":{"timezone":"UTC","settings":{},"macros":[]}}`,
      `{${cols},"server":{"timezone":1,"settings":{}}}`,
      `{${cols},"replicated":{"zookeeper_path":"/a","zookeeper_path_b64":"L2E=","replica_name":"r"}}`,
      `{${cols},"replicated":{"zookeeper_path":"/a","replica_name_b64":"*"}}`,
    ]) {
      expect(() => decode(doc), doc).toThrow(InternalError);
      expect(() => decode(doc), doc).toThrow(/schema_description/);
    }
  });
});

describe('the description decoder: the top-level filter_declined_settings (no library)', () => {
  const cols = '"columns":[{"name":"k","type":"UInt8","default_kind":"","default_expression":""}]';
  const server = '"server":{"timezone":"UTC","settings":{"final":"1"}}';
  const decode = (s: string) => decodeSchemaDescription(Buffer.from(s, 'utf8'));
  const listed = (list: string) => decode(`{${cols},"filter_declined_settings":${list},${server}}`);

  it('[] is present and empty; absent reads as empty too, never a failure', () => {
    expect(listed('[]').filterDeclinedSettings).toEqual([]);
    expect(decode(`{${cols},${server}}`).filterDeclinedSettings).toEqual([]);
  });

  it('entries in the document\'s own order, a name_b64 entry, an unlisted tier and layer (rule r3) and unknown members (rule r2)', () => {
    const got = listed(
      '[{"name":"aggregate_functions_null_for_empty","tier":"predicate","layer":"defaults","x_future":1},' +
        '{"name":"final","tier":"result-content","layer":"server"},' +
        '{"name":"additional_result_filter","tier":"result-content","layer":"schema"},' +
        '{"name_b64":"eP95","tier":"predicate-unflipped","layer":"schema"},' +
        '{"name":"x_future_setting","tier":"x_future_tier","layer":"x_future_layer","x_future_obj":{"a":[1]}}]',
    ).filterDeclinedSettings;
    // The document's own order, never re-sorted (here not name order).
    expect(got.map((e) => e.name.toString('hex'))).toEqual([
      Buffer.from('aggregate_functions_null_for_empty').toString('hex'),
      Buffer.from('final').toString('hex'),
      Buffer.from('additional_result_filter').toString('hex'),
      '78ff79',
      Buffer.from('x_future_setting').toString('hex'),
    ]);
    expect(got.map((e) => e.tier)).toEqual([
      DeclinedTier.Predicate,
      DeclinedTier.ResultContent,
      DeclinedTier.ResultContent,
      DeclinedTier.PredicateUnflipped,
      'x_future_tier',
    ]);
    expect(got.map((e) => e.layer)).toEqual([
      DeclinedLayer.Defaults,
      DeclinedLayer.Server,
      DeclinedLayer.Schema,
      DeclinedLayer.Schema,
      'x_future_layer',
    ]);
    expect(got.map((e) => declinedTierKnown(e.tier))).toEqual([true, true, true, true, false]);
    expect(got.map((e) => declinedLayerKnown(e.layer))).toEqual([true, true, true, true, false]);
  });

  it('a schema with no server lists its own layers; the removed server-level list is an unknown member (rule r2)', () => {
    const own = decode(`{${cols},"filter_declined_settings":[{"name":"final","tier":"result-content","layer":"schema"}]}`);
    expect(own.server).toBeUndefined();
    expect(own.filterDeclinedSettings.map((e) => [e.name.toString('utf8'), e.tier, e.layer])).toEqual([
      ['final', DeclinedTier.ResultContent, DeclinedLayer.Schema],
    ]);
    const old = decode(
      `{${cols},"filter_declined_settings":[],"server":{"timezone":"UTC","settings":{},"filter_declined_settings":[{"name":"final","tier":"result-content"}]}}`,
    );
    expect(old.filterDeclinedSettings).toEqual([]);
    expect(old.server?.timezone).toBe('UTC');
  });

  it('refusals are each an InternalError naming the document', () => {
    for (const list of [
      '{"name":"final","tier":"predicate","layer":"server"}',
      '["final"]',
      '[{"name":"final","name_b64":"ZmluYWw=","tier":"predicate","layer":"server"}]',
      '[{"tier":"predicate","layer":"server"}]',
      '[{"name_b64":"*","tier":"predicate","layer":"server"}]',
      '[{"name":"final","tier":1,"layer":"server"}]',
      '[{"name":"final","tier":"predicate","layer":2}]',
    ]) {
      expect(() => listed(list), list).toThrow(InternalError);
      expect(() => listed(list), list).toThrow(/schema_description/);
    }
  });
});

describe.skipIf(!stubsAvailable)('the server handle over the stub library', () => {
  it('accepts a profile with every member (the stub checks the closed document of input:server_profile)', () => {
    const { library } = openStub('ok');
    const srv = library.newServer({ timezone: 'Asia/Tokyo', settings: { max_threads: '8' }, macros: {} });
    srv.close();
  });

  it('sends the serialized profile and an empty options document to the one ABI call', () => {
    const { image, library } = openStub('ok');
    const spy = vi.spyOn(image.calls, 'serverCreate');
    library.newServer({ timezone: 'UTC', macros: {} }).close();
    library.newServer({}).close();
    const first = spy.mock.calls[0] as unknown as Buffer[];
    expect(first[0]?.toString()).toBe('{"timezone":"UTC","macros":{}}');
    expect(first[1]?.length).toBe(0);
    expect((spy.mock.calls[1] as unknown as Buffer[])[0]?.toString()).toBe('{}');
    spy.mockRestore();
  });

  it('new_server, then close twice', () => {
    const { library } = openStub('ok');
    const before = liveServers(library);
    const srv = library.newServer({ timezone: 'UTC' });
    expect(liveServers(library)).toBe(before + 1);
    srv.close();
    srv.close(); // idempotent
    expect(liveServers(library)).toBe(before);
  });

  it('Symbol.dispose closes the server', () => {
    const { library } = openStub('ok');
    const before = liveServers(library);
    const srv = library.newServer({});
    expect(liveServers(library)).toBe(before + 1);
    srv[Symbol.dispose]();
    expect(liveServers(library)).toBe(before);
  });

  it('compile with a server passes a non-NULL server to chs_schema_create, asserted from what the stub received', () => {
    const { image, library } = openStub('ok');
    const base = liveServers(library);
    const spy = vi.spyOn(image.calls, 'schemaCreate');
    const srv = library.newServer({ timezone: 'Asia/Tokyo' });
    const schema = library.compileTable(STMT, { server: srv, settings: { a: 'b' } });
    expect(spy.mock.calls[0]?.[0]).not.toBeNull();
    srv.close();
    // The stub took its own counted reference to the server it RECEIVED.
    expect(liveServers(library)).toBe(base + 1);
    expect(() => schema.describe()).not.toThrow(); // a schema whose server the caller closed keeps working
    schema.close();
    expect(liveServers(library)).toBe(base);
    spy.mockRestore();
  });

  it('without a server the stub receives NULL and holds nothing', () => {
    const { image, library } = openStub('ok');
    const base = liveServers(library);
    const spy = vi.spyOn(image.calls, 'schemaCreate');
    for (const options of [undefined, {}, { server: undefined }]) {
      const srv = library.newServer({});
      const plain = library.compileTable(STMT, options);
      srv.close();
      expect(liveServers(library)).toBe(base);
      plain.close();
    }
    for (const call of spy.mock.calls) expect(call[0]).toBeNull();
    spy.mockRestore();
  });

  it('a server and its schemas close in any order', () => {
    const { library } = openStub('ok');
    const base = liveServers(library);
    const a = library.newServer({});
    const sa = library.compileTable(STMT, { server: a });
    sa.close();
    a.close();
    const b = library.newServer({});
    const sb = library.compileTable(STMT, { server: b });
    b.close();
    sb.close();
    expect(liveServers(library)).toBe(base);
  });

  it('a closed server is a UsageError, raised before any call and before a schema exists', () => {
    const { image, library } = openStub('ok');
    const srv = library.newServer({ timezone: 'UTC' });
    srv.close();
    const spy = vi.spyOn(image.calls, 'schemaCreate');
    const before = library.liveHandles().chs_schema;
    expect(() => library.compileTable(STMT, { server: srv })).toThrow(UsageError);
    expect(() => library.compileTable(STMT, { server: srv })).toThrow(/Server was already closed/);
    expect(library.liveHandles().chs_schema).toBe(before);
    spy.mockRestore();
  });

  it("a server from another library is that library's own CHS_INVALID_ARGUMENT, as a UsageError", () => {
    const a = openStub('ok').library;
    const b = openStub('ok-b').library;
    const srv = a.newServer({});
    try {
      let caught: unknown;
      try {
        b.compileTable(STMT, { server: srv });
      } catch (e) {
        caught = e;
      }
      expect(caught).toBeInstanceOf(UsageError);
      expect((caught as UsageError).status).toBe(Status.InvalidArgument);
    } finally {
      srv.close();
    }
  });

  it("the stub's description without a server decodes with server and replicated absent", () => {
    const { library } = openStub('r2-unknown-members');
    const schema = library.compileTable('CREATE TABLE t (x Int32)');
    const d = schema.describe();
    expect(d.columns).toHaveLength(1);
    expect(d.server).toBeUndefined();
    expect(d.replicated).toBeUndefined();
    schema.close();
  });

  it('frees a server the caller abandons', async () => {
    const { library } = openStub('ok');
    (() => {
      for (let i = 0; i < 25; i++) library.compileTable(STMT, { server: library.newServer({ timezone: 'UTC' }) });
    })();
    const gc = (globalThis as { gc?: () => void }).gc;
    expect(typeof gc, 'run under --expose-gc (vitest.config.ts passes it)').toBe('function');
    let n = liveServers(library);
    for (let i = 0; i < 100 && n !== 0; i++) {
      gc?.();
      await new Promise((r) => setTimeout(r, 20));
      n = liveServers(library);
    }
    expect(n).toBe(0);
  });
});
