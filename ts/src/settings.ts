/**
 * Settings, the per-call zone and the other call inputs, serialized for the
 * library exactly as `docs/reference/bindings-v1.md` §2 and §3 say.
 *
 * Settings values are strings, and only strings. The map becomes a JSON object
 * of string values through the stock encoder and is passed verbatim: no
 * boolean or integer spelling, no float. A value that is not a string is the
 * language's own type error (`TypeError`).
 *
 * The per-call zone is a settings key and nothing more: `sessionTimezone` is
 * written into the object as `session_timezone`, verbatim, with no default,
 * validation or canonicalization (the library validates it with its own zone
 * database). Passing it alongside a `session_timezone` key in `settings` is a
 * `UsageError`, raised before any call and whether or not the two agree.
 */

import { asBuffer, usageError } from './abi1/index.js';

/** Settings and query parameters: names to string values. */
export type Settings = Readonly<Record<string, string>>;

/** Bytes in: a statement, expression, type, name or literal. A string is encoded as UTF-8; pass a `Uint8Array` for bytes that are not text. */
export type BytesIn = Uint8Array | string;

const ZONE_KEY = 'session_timezone';

/** The bytes of a `BytesIn`: UTF-8 for a string, the same bytes for a `Uint8Array`. */
export function bytesIn(value: BytesIn): Buffer {
  return typeof value === 'string' ? Buffer.from(value, 'utf8') : asBuffer(value);
}

function checkStrings(what: string, map: Settings): void {
  for (const [k, v] of Object.entries(map)) {
    if (typeof v !== 'string') throw new TypeError(`chtypes: ${what}[${JSON.stringify(k)}] must be a string, got ${typeof v}`);
  }
}

/**
 * The `settings` input of a call: nothing (length 0) when there is neither a
 * map nor a zone, else a JSON object of string values with the zone, when
 * given, written in as `session_timezone`.
 */
export function encodeSettings(settings: Settings | undefined, sessionTimezone: string | undefined): Buffer {
  if (settings === undefined && sessionTimezone === undefined) return Buffer.alloc(0);
  const out: Record<string, string> = {};
  if (settings !== undefined) {
    checkStrings('settings', settings);
    if (sessionTimezone !== undefined && Object.hasOwn(settings, ZONE_KEY)) {
      throw usageError(`the zone was given twice: sessionTimezone and a ${ZONE_KEY} key in settings; pass only one`);
    }
    Object.assign(out, settings);
  }
  if (sessionTimezone !== undefined) {
    if (typeof sessionTimezone !== 'string') throw new TypeError(`chtypes: sessionTimezone must be a string, got ${typeof sessionTimezone}`);
    out[ZONE_KEY] = sessionTimezone;
  }
  return Buffer.from(JSON.stringify(out), 'utf8');
}

/** Query parameters: a JSON object of string values, or nothing when absent. */
export function encodeParams(params: Settings | undefined): Buffer {
  if (params === undefined) return Buffer.alloc(0);
  checkStrings('params', params);
  return Buffer.from(JSON.stringify({ ...params }), 'utf8');
}

const utf8 = new TextDecoder('utf-8', { fatal: true });

function isValidUtf8(bytes: Uint8Array): boolean {
  try {
    utf8.decode(bytes);
    return true;
  } catch {
    return false;
  }
}

/** The INSERT column list: a JSON array of names, each an object carrying `name` when its bytes are valid UTF-8 and `name_b64` otherwise, or nothing (length 0) when absent. */
export function encodeColumns(columns: readonly BytesIn[] | undefined): Buffer {
  if (columns === undefined) return Buffer.alloc(0);
  const names = columns.map((c) => {
    if (typeof c === 'string') return { name: c };
    return isValidUtf8(c) ? { name: Buffer.from(c.buffer, c.byteOffset, c.byteLength).toString('utf8') } : { name_b64: asBuffer(c).toString('base64') };
  });
  return Buffer.from(JSON.stringify(names), 'utf8');
}

/** The process default settings, as `chs_set_defaults` takes them. */
export function validateDefaults(defaults: Settings): void {
  checkStrings('defaults', defaults);
}
