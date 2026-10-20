/**
 * Settings serialization, the per-call zone rule and the process-once rule of
 * `setup` (`docs/reference/bindings-v1.md` §2, §3 and §6). Pure: no library.
 */

import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import { UsageError } from '../src/abi2/errors.js';
import { ArtifactMissingError } from '../src/ocifetch/errors.js';
import { Registry } from '../src/registry.js';
import {
  bytesIn,
  encodeColumns,
  encodeFilterOptions,
  encodeInsertOptions,
  encodeParams,
  encodeSchemaOptions,
  encodeServerProfile,
  encodeSettings,
} from '../src/settings.js';
import { commitSetup, latchSetup, resetSetupForTests, settleFailedOpen, setup, setupGeneration } from '../src/setup.js';

describe('settings values are strings or bytes, and nothing else', () => {
  it('serializes a map of strings with the stock encoder, verbatim', () => {
    expect(encodeSettings({ input_format_defaults_for_omitted_fields: '1', a: 'x' }, undefined).toString()).toBe(
      '{"input_format_defaults_for_omitted_fields":"1","a":"x"}',
    );
  });

  it('sends nothing at all (length 0) when there are neither settings nor a zone', () => {
    expect(encodeSettings(undefined, undefined)).toHaveLength(0);
    expect(encodeParams(undefined)).toHaveLength(0);
    expect(encodeColumns(undefined)).toHaveLength(0);
  });

  it('refuses a boolean or a number as the language type error, never rewriting it', () => {
    expect(() => encodeSettings({ a: true as unknown as string }, undefined)).toThrow(TypeError);
    expect(() => encodeSettings({ a: 1 as unknown as string }, undefined)).toThrow(TypeError);
    expect(() => encodeParams({ p: 1 as unknown as string })).toThrow(TypeError);
    expect(() => encodeServerProfile({ settings: { a: 1 as unknown as string } })).toThrow(TypeError);
  });
});

describe('a value that is not valid UTF-8 is the {"b64": ...} object form (ABI v2, json_object_byte_values)', () => {
  const bad = new Uint8Array([0xff, 0xfe, 0x00]);
  const b64 = Buffer.from(bad).toString('base64');

  it('writes a Uint8Array that is not UTF-8 as {"b64": standard padded base64}, and one that is UTF-8 as a plain string', () => {
    expect(JSON.parse(encodeSettings({ a: bad, b: new Uint8Array([0x78, 0x79]), c: 'z' }, undefined).toString())).toEqual({ a: { b64 }, b: 'xy', c: 'z' });
    expect(JSON.parse(encodeParams({ p: bad }).toString())).toEqual({ p: { b64 } });
    expect(JSON.parse(encodeSettings({ a: new Uint8Array([0x61, 0x62, 0x63]) }, undefined).toString())).toEqual({ a: 'abc' });
  });

  it('keeps a leading U+FEFF (EF BB BF) in a valid-UTF-8 value: the decoder never drops a byte order mark', () => {
    expect(JSON.parse(encodeSettings({ a: new Uint8Array([0xef, 0xbb, 0xbf, 0x78]) }, undefined).toString())).toEqual({ a: '\ufeffx' });
  });

  it('carries a per-call zone that is not UTF-8 in the object form too, and the library validates it', () => {
    expect(JSON.parse(encodeSettings(undefined, bad).toString())).toEqual({ session_timezone: { b64 } });
    expect(() => encodeSettings({ session_timezone: 'UTC' }, bad)).toThrow(UsageError);
  });

  it('writes a server profile setting value in the object form, and still refuses a non-UTF-8 key, macro or zone', () => {
    expect(JSON.parse(encodeServerProfile({ timezone: 'UTC', settings: { s: bad, t: 'v' }, macros: { shard: '01' } }).toString())).toEqual({
      timezone: 'UTC',
      settings: { s: { b64 }, t: 'v' },
      macros: { shard: '01' },
    });
    expect(() => encodeServerProfile({ settings: { ['\udcff']: 'v' } })).toThrow(/settings: the key "\\udcff" is not valid UTF-8/);
    expect(() => encodeServerProfile({ macros: { m: '\udcff' } })).toThrow(UsageError);
    expect(() => encodeServerProfile({ timezone: '\udcff' })).toThrow(UsageError);
  });

  it('the process defaults take the same form', () => {
    resetSetupForTests();
    setup({ defaults: { a: bad, b: '1' } });
    expect(commitSetup().defaults).toEqual({ a: { b64 }, b: '1' });
    expect(() => setup({ defaults: { b: '1', a: bad } })).not.toThrow(); // the same bytes, in any order
    expect(() => setup({ defaults: { a: new Uint8Array([0xff]), b: '1' } })).toThrow(UsageError);
    resetSetupForTests();
  });
});

describe('the options documents (rule r1)', () => {
  const doc = (b: Buffer) => JSON.parse(b.toString());

  it('sends nothing at all (length 0) when no member is set', () => {
    expect(encodeInsertOptions({})).toHaveLength(0);
    expect(encodeFilterOptions({})).toHaveLength(0);
    expect(encodeSchemaOptions({})).toHaveLength(0);
  });

  it('builds insert_options from the members set: name objects, column defaults in both forms, the two enums', () => {
    expect(
      doc(
        encodeInsertOptions({
          insertableColumns: ['a', new Uint8Array([0xff])],
          columnDefaults: [
            { name: 'a', value: '7' },
            { name: new Uint8Array([0xfe]), value: new Uint8Array([0xff, 0x00]) },
          ],
          verdictBasis: 'export',
          deferDefaults: 'server',
        }),
      ),
    ).toEqual({
      insertable_columns: [{ name: 'a' }, { name_b64: '/w==' }],
      column_defaults: [
        { name: 'a', value: '7' },
        { name_b64: '/g==', value_b64: '/wA=' },
      ],
      verdict_basis: 'export',
      defer_defaults: 'server',
    });
  });

  it('column_defaults wire form: UTF-8, value_b64, name_b64, an empty value, and order', () => {
    const cd = (columnDefaults: { name: string | Uint8Array; value: string | Uint8Array }[]) => doc(encodeInsertOptions({ columnDefaults }));
    expect(cd([{ name: 'a', value: 'h\u00e9llo' }])).toEqual({ column_defaults: [{ name: 'a', value: 'h\u00e9llo' }] });
    expect(cd([{ name: 'a', value: new Uint8Array([0xff, 0x00]) }])).toEqual({ column_defaults: [{ name: 'a', value_b64: '/wA=' }] });
    expect(cd([{ name: new Uint8Array([0xfe]), value: 'v' }])).toEqual({ column_defaults: [{ name_b64: '/g==', value: 'v' }] });
    // An empty value is a supplied value: sent as "", never omitted.
    expect(cd([{ name: 'a', value: '' }])).toEqual({ column_defaults: [{ name: 'a', value: '' }] });
    expect(cd([{ name: 'a', value: new Uint8Array(0) }])).toEqual({ column_defaults: [{ name: 'a', value: '' }] });
    expect(encodeInsertOptions({ columnDefaults: [{ name: 'z', value: '1' }, { name: 'a', value: '2' }] }).toString()).toBe(
      '{"column_defaults":[{"name":"z","value":"1"},{"name":"a","value":"2"}]}',
    );
  });

  it('an absent list leaves the member out, and a present empty list is sent as []', () => {
    expect(doc(encodeInsertOptions({ insertableColumns: [], columnDefaults: [] }))).toEqual({ insertable_columns: [], column_defaults: [] });
    expect(encodeInsertOptions({ insertableColumns: undefined })).toHaveLength(0);
  });

  it('passes a member it has no name for through verbatim as JSON text, and a repeated key keeps the last value', () => {
    expect(encodeInsertOptions({ insertOptionsExtra: { x_future: '{"a":[1,2]}' } }).toString()).toBe('{"x_future":{"a":[1,2]}}');
    expect(doc(encodeInsertOptions({ verdictBasis: 'input', insertOptionsExtra: { verdict_basis: '"export"' } }))).toEqual({ verdict_basis: 'export' });
    expect(() => encodeInsertOptions({ insertOptionsExtra: { x: '{not json' } })).toThrow(UsageError);
    expect(() => encodeFilterOptions({ filterOptionsExtra: { x: '' } })).toThrow(UsageError);
  });

  it('builds filter_options and schema_options from their members', () => {
    expect(doc(encodeFilterOptions({ paramEncoding: 'raw', filterOptionsExtra: { y: '1' } }))).toEqual({ param_encoding: 'raw', y: 1 });
    expect(doc(encodeSchemaOptions({ posture: 'load', schemaOptionsExtra: { z: 'null' } }))).toEqual({ posture: 'load', z: null });
  });

  it('validates no member: an unlisted value goes to the library, which refuses it', () => {
    expect(doc(encodeInsertOptions({ verdictBasis: 'sideways' as never }))).toEqual({ verdict_basis: 'sideways' });
  });
});

describe('a key or value that is not valid UTF-8 is refused, never replaced', () => {
  // A lone surrogate is what a JavaScript string holds where the bytes were not UTF-8:
  // JSON.stringify would write it as an escape and TextEncoder as U+FFFD.
  const lone = '\udcff';

  it('refuses a settings value, naming the input and the escaped key', () => {
    let error: unknown;
    try {
      encodeSettings({ k: lone }, undefined);
    } catch (e) {
      error = e;
    }
    expect(error).toBeInstanceOf(UsageError);
    const message = (error as UsageError).message;
    expect(message).toContain('settings');
    expect(message).toContain('the value for key "k"');
    expect(message).toContain('not valid UTF-8');
    expect(message).toContain('Uint8Array');
  });

  it('refuses a settings key, a query-parameter key or value, and a zone, each naming its input', () => {
    expect(() => encodeSettings({ [`k${lone}`]: 'v' }, undefined)).toThrow(/settings: the key "k\\udcff" is not valid UTF-8/);
    expect(() => encodeSettings({ k: lone }, 'UTC')).toThrow(UsageError);
    expect(() => encodeSettings(undefined, lone)).toThrow(/the zone: the value is not valid UTF-8/);
    expect(() => encodeParams({ p: lone })).toThrow(/query parameters: the value for key "p" is not valid UTF-8/);
    expect(() => encodeParams({ [lone]: 'v' })).toThrow(/query parameters: the key "\\udcff" is not valid UTF-8/);
    expect(() => encodeParams({ p: lone })).toThrow(UsageError);
  });

  it('carries valid non-ASCII, including an astral pair and a real U+FFFD, unchanged', () => {
    expect(encodeParams({ p: 'é日本' }).toString('utf8')).toBe('{"p":"é日本"}');
    expect([...encodeParams({ p: 'é' })]).toEqual([...Buffer.from('{"p":"', 'utf8'), 0xc3, 0xa9, ...Buffer.from('"}', 'utf8')]);
    expect(encodeSettings({ 'é': '😀\ufffd' }, 'Asia/Tokyo').toString('utf8')).toBe('{"é":"😀\ufffd","session_timezone":"Asia/Tokyo"}');
  });
});

describe('the per-call zone is a settings key, verbatim', () => {
  it('writes the zone into the settings object as session_timezone with no validation or canonicalization', () => {
    expect(encodeSettings(undefined, 'Europe/Berlin').toString()).toBe('{"session_timezone":"Europe/Berlin"}');
    expect(encodeSettings({ a: '1' }, 'not a real zone').toString()).toBe('{"a":"1","session_timezone":"not a real zone"}');
  });

  it('refuses the option and a session_timezone key together, whether or not they agree, before any call', () => {
    expect(() => encodeSettings({ session_timezone: 'UTC' }, 'UTC')).toThrow(UsageError);
    expect(() => encodeSettings({ session_timezone: 'UTC' }, 'Asia/Tokyo')).toThrow(UsageError);
  });

  it('carries the key inside settings on its own', () => {
    expect(encodeSettings({ session_timezone: 'UTC' }, undefined).toString()).toBe('{"session_timezone":"UTC"}');
  });
});

describe('bytes in, and the INSERT column list', () => {
  it('encodes a string as UTF-8 and passes bytes through unchanged', () => {
    expect([...bytesIn('é')]).toEqual([0xc3, 0xa9]);
    expect([...bytesIn(new Uint8Array([0xff, 0x00]))]).toEqual([0xff, 0x00]);
  });

  it('writes each column as a name when its bytes are valid UTF-8, and as name_b64 otherwise', () => {
    const json = JSON.parse(encodeColumns(['a', new Uint8Array([0x62, 0x00]), new Uint8Array([0xff, 0xfe])]).toString());
    expect(json).toEqual([{ name: 'a' }, { name: 'b\u0000' }, { name_b64: Buffer.from([0xff, 0xfe]).toString('base64') }]);
  });
});

describe('setup: the process-once rule', () => {
  beforeEach(() => resetSetupForTests());

  it('is optional: the first open commits the empty setup', () => {
    expect(commitSetup()).toEqual({ timezone: '', defaults: {} });
  });

  it('records, and the same setup again (zone byte for byte, same defaults in any order) is a no-op', () => {
    setup({ timezone: 'Europe/Berlin', defaults: { a: '1', b: '2' } });
    expect(() => setup({ timezone: 'Europe/Berlin', defaults: { b: '2', a: '1' } })).not.toThrow();
    expect(commitSetup()).toEqual({ timezone: 'Europe/Berlin', defaults: { a: '1', b: '2' } });
  });

  it('refuses a different zone or different defaults as a UsageError naming both, and the first setup stands', () => {
    setup({ timezone: 'Europe/Berlin' });
    expect(() => setup({ timezone: 'Asia/Tokyo' })).toThrow(UsageError);
    expect(() => setup({ timezone: 'Asia/Tokyo' })).toThrow(/Europe\/Berlin.*Asia\/Tokyo/s);
    expect(() => setup({ timezone: 'Europe/Berlin', defaults: { a: '1' } })).toThrow(UsageError);
    expect(commitSetup().timezone).toBe('Europe/Berlin');
  });

  it('compares the spelling byte for byte: a different spelling of one zone is a different setup', () => {
    setup({ timezone: 'UTC' });
    expect(() => setup({ timezone: 'Etc/UTC' })).toThrow(UsageError);
  });

  it('after the first open committed the empty setup, succeeds only with exactly that setup', () => {
    commitSetup();
    expect(() => setup()).not.toThrow();
    expect(() => setup({ timezone: '' })).not.toThrow();
    expect(() => setup({ timezone: 'UTC' })).toThrow(UsageError);
  });

  it('refuses a non-string default as the language type error', () => {
    expect(() => setup({ defaults: { a: 1 as unknown as string } })).toThrow(TypeError);
  });
});

describe('setup: a failed open before the first step 7 unlocks the record', () => {
  beforeEach(() => resetSetupForTests());

  it('a refused spelling unlocks nothing; a failed resolve (nothing installed, autofetch off) keeps the record and lets a different setup replace it', async () => {
    setup({ timezone: 'Asia/Tokyo' });
    const cacheDir = mkdtempSync(path.join(os.tmpdir(), 'setup-latch-empty-cache-'));
    const registry = await Registry.open({ fetch: { cacheDir, systemDirs: [], offline: true }, autofetch: false });
    await expect(registry.for('v26.8')).rejects.toBeInstanceOf(UsageError);
    expect(() => setup({ timezone: 'UTC' })).toThrow(UsageError); // misuse unlocked nothing
    await expect(registry.for('26.8')).rejects.toBeInstanceOf(ArtifactMissingError);
    expect(commitSetup().timezone).toBe('Asia/Tokyo'); // kept: a retry runs under it, never the empty setup
  });

  it('after a failed attempt, a different setup replaces the record, and the replacement is locked again', () => {
    setup({ timezone: 'Asia/Tokyo' });
    settleFailedOpen(setupGeneration());
    expect(() => setup({ timezone: 'UTC' })).not.toThrow();
    expect(commitSetup().timezone).toBe('UTC');
    expect(() => setup({ timezone: 'Europe/Berlin' })).toThrow(UsageError);
  });

  it('never unlocks a setup recorded after the open began, nor a latched one', () => {
    const began = setupGeneration();
    setup({ timezone: 'Asia/Tokyo' });
    settleFailedOpen(began); // it began before Asia/Tokyo was recorded
    expect(() => setup({ timezone: 'UTC' })).toThrow(UsageError);
    latchSetup();
    settleFailedOpen(setupGeneration());
    expect(() => setup({ timezone: 'UTC' })).toThrow(UsageError);
    expect(() => setup({ timezone: 'Asia/Tokyo' })).not.toThrow();
  });
});
