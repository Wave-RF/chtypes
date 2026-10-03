/**
 * Settings serialization, the per-call zone rule and the process-once rule of
 * `setup` (`docs/reference/bindings-v1.md` §2, §3 and §6). Pure: no library.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { UsageError } from '../src/abi1/errors.js';
import { bytesIn, encodeColumns, encodeParams, encodeSettings } from '../src/settings.js';
import { commitSetup, resetSetupForTests, setup } from '../src/setup.js';

describe('settings values are strings, and only strings', () => {
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
