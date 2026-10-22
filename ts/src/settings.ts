/**
 * Settings, the per-call zone and the other call inputs, serialized for the
 * library exactly as `docs/reference/bindings-v1.md` §2 and §3 say.
 *
 * Settings values are strings or bytes, and nothing else. The map becomes a JSON
 * object through the stock encoder and each value is passed verbatim: no boolean
 * or integer spelling, no float. A value that is neither a string nor a
 * `Uint8Array` is the language's own type error (`TypeError`). A value whose
 * bytes are valid UTF-8 is a JSON string; any other bytes (a `Uint8Array`) are
 * the object form `{"b64": "<standard base64>"}` (ABI v2, `json_object_byte_values`),
 * so the library sees the exact bytes. A key that is not valid UTF-8 cannot be
 * carried at all and is a `UsageError`.
 *
 * The per-call zone is a settings key and nothing more: `sessionTimezone` is
 * written into the object as `session_timezone`, verbatim, with no default,
 * validation or canonicalization (the library validates it with its own zone
 * database). Passing it alongside a `session_timezone` key in `settings` is a
 * `UsageError`, raised before any call and whether or not the two agree.
 */

import { asBuffer, type Posture, usageError } from './abi2/index.js';

/** A settings or query-parameter value: text, or the bytes of a value that need not be text (a non-UTF-8 value is sent in the `{"b64": ...}` object form). */
export type SettingValue = string | Uint8Array;

/** Settings and query parameters: names to values. */
export type Settings = Readonly<Record<string, SettingValue>>;

/** A value as the document carries it: a JSON string, or the object form of bytes that are not UTF-8. */
export type WireValue = string | { readonly b64: string };

/** Bytes in: a statement, expression, type, name or literal. A string is encoded as UTF-8; pass a `Uint8Array` for bytes that are not text. */
export type BytesIn = Uint8Array | string;

const ZONE_KEY = 'session_timezone';

/** The bytes of a `BytesIn`: UTF-8 for a string, the same bytes for a `Uint8Array`. */
export function bytesIn(value: BytesIn): Buffer {
  return typeof value === 'string' ? Buffer.from(value, 'utf8') : asBuffer(value);
}

const NON_UTF8_HINT =
  'a JSON input cannot carry these bytes here (only a settings or query-parameter value has the {"b64": ...} object form, and a string holding a lone surrogate has no bytes; pass a Uint8Array)';

/** True for a string with a lone surrogate, which no UTF-8 encoding carries: `JSON.stringify` writes it as a `\udXXX` escape and `TextEncoder` as U+FFFD. */
function hasLoneSurrogate(text: string): boolean {
  return /\p{Surrogate}/u.test(text);
}

// ignoreBOM: a leading U+FEFF is data, and the decoder must not drop it.
const utf8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

/** The text of `bytes` when they are valid UTF-8, else `undefined`. */
function utf8Text(bytes: Uint8Array): string | undefined {
  try {
    return utf8.decode(bytes);
  } catch {
    return undefined;
  }
}

function isValidUtf8(bytes: Uint8Array): boolean {
  return utf8Text(bytes) !== undefined;
}

/**
 * One value of `json_object_byte_values`: a string stays a JSON string, and
 * bytes are a JSON string when they are valid UTF-8 and `{"b64": ...}` (standard
 * base64, padded) otherwise. `where` names the value for a refusal.
 */
function wireValue(what: string, where: string, v: unknown): WireValue {
  if (typeof v === 'string') {
    if (hasLoneSurrogate(v)) throw usageError(`${what}: ${where} is not valid UTF-8, and ${NON_UTF8_HINT}`);
    return v;
  }
  if (v instanceof Uint8Array) {
    return utf8Text(v) ?? { b64: asBuffer(v).toString('base64') };
  }
  throw new TypeError(`chtypes: ${what} ${where} must be a string or a Uint8Array, got ${typeof v}`);
}

function keyName(k: string): string {
  return `the key ${JSON.stringify(k)}`;
}

/** The wire form of a map of byte values; refuses a key that is not valid UTF-8 (the key is shown through JSON.stringify, which escapes a lone surrogate: never raw). */
function wireMap(what: string, map: Settings): Record<string, WireValue> {
  const shown = what === 'params' ? 'query parameters' : what;
  const out: Record<string, WireValue> = {};
  for (const [k, v] of Object.entries(map)) {
    if (hasLoneSurrogate(k)) throw usageError(`${shown}: ${keyName(k)} is not valid UTF-8, and ${NON_UTF8_HINT}`);
    if (typeof v !== 'string' && !(v instanceof Uint8Array)) {
      throw new TypeError(`chtypes: ${what}[${JSON.stringify(k)}] must be a string or a Uint8Array, got ${typeof v}`);
    }
    // defineProperty, so a key such as "__proto__" is an own member and never the prototype.
    Object.defineProperty(out, k, { value: wireValue(shown, `the value for key ${JSON.stringify(k)}`, v), enumerable: true, writable: true, configurable: true });
  }
  return out;
}

/**
 * The `settings` input of a call: nothing (length 0) when there is neither a
 * map nor a zone, else a JSON object of string values with the zone, when
 * given, written in as `session_timezone`.
 */
export function encodeSettings(settings: Settings | undefined, sessionTimezone: SettingValue | undefined): Buffer {
  if (settings === undefined && sessionTimezone === undefined) return Buffer.alloc(0);
  let out: Record<string, WireValue> = {};
  if (settings !== undefined) {
    if (sessionTimezone !== undefined && Object.hasOwn(settings, ZONE_KEY)) {
      throw usageError(`the zone was given twice: sessionTimezone and a ${ZONE_KEY} key in settings; pass only one`);
    }
    out = wireMap('settings', settings);
  }
  if (sessionTimezone !== undefined) {
    if (typeof sessionTimezone !== 'string' && !(sessionTimezone instanceof Uint8Array)) {
      throw new TypeError(`chtypes: sessionTimezone must be a string or a Uint8Array, got ${typeof sessionTimezone}`);
    }
    out[ZONE_KEY] = wireValue('the zone', 'the value', sessionTimezone);
  }
  return Buffer.from(JSON.stringify(out), 'utf8');
}

/** Query parameters: a JSON object of string values, or nothing when absent. */
export function encodeParams(params: Settings | undefined): Buffer {
  if (params === undefined) return Buffer.alloc(0);
  return Buffer.from(JSON.stringify(wireMap('params', params)), 'utf8');
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

/** The process default settings, as `chs_set_defaults` takes them: the wire form of each value (a value that is not valid UTF-8 is `{"b64": ...}`). */
export function validateDefaults(defaults: Settings): Readonly<Record<string, WireValue>> {
  return wireMap('defaults', defaults);
}

/**
 * One ClickHouse server, as `chs_server_create` takes it (`input:server_profile`).
 * Every member is optional, and every value is passed through as given: the
 * library validates the zone with DateLUT, each setting with the server's own
 * SET check, and the macros with ClickHouse's own reader.
 */
export interface ServerProfile {
  /** The server's zone, the one its `timezone()` returns. Absent or `''` omits it: the server does not describe it, and the image zone applies. */
  readonly timezone?: string | undefined;
  /** What the server's profile applies to every query. Absent omits it (no server settings layer); `{}` is sent as `{}`. A value that is not valid UTF-8 is a `Uint8Array`, sent in the `{"b64": ...}` form. */
  readonly settings?: Settings | undefined;
  /**
   * The server's `<macros>`, which a Replicated engine's ZooKeeper path and
   * replica name read (validated, and applied only by a build that advertises
   * `server_profile.macros`). ABSENT is not `{}`: absent means the server's macros
   * are UNKNOWN, and a schema whose engine reads one is declined; present, even
   * `{}`, is the server's COMPLETE set. A macro's name and value are text: they
   * have no byte form.
   */
  readonly macros?: Readonly<Record<string, string>> | undefined;
}

/** The profile document: an empty or absent zone, absent settings and absent macros are omitted; a present map is sent as given, `{}` when empty. */
export function encodeServerProfile(profile: ServerProfile): Buffer {
  const out: Record<string, unknown> = {};
  if (profile.timezone !== undefined && profile.timezone !== '') {
    if (typeof profile.timezone !== 'string') throw new TypeError(`chtypes: timezone must be a string, got ${typeof profile.timezone}`);
    if (hasLoneSurrogate(profile.timezone)) throw usageError(`server profile: the timezone is not valid UTF-8, and ${NON_UTF8_HINT}`);
    out.timezone = profile.timezone;
  }
  if (profile.settings !== undefined) {
    out.settings = wireMap('settings', profile.settings);
  }
  if (profile.macros !== undefined) {
    for (const [k, v] of Object.entries(profile.macros)) {
      if (typeof v !== 'string') throw new TypeError(`chtypes: macros[${JSON.stringify(k)}] must be a string, got ${typeof v}`);
      if (hasLoneSurrogate(k)) throw usageError(`macros: ${keyName(k)} is not valid UTF-8, and ${NON_UTF8_HINT}`);
      if (hasLoneSurrogate(v)) throw usageError(`macros: the value for key ${JSON.stringify(k)} is not valid UTF-8, and ${NON_UTF8_HINT}`);
    }
    out.macros = { ...profile.macros };
  }
  return Buffer.from(JSON.stringify(out), 'utf8');
}

// ---- the options documents (rule r1). Each named member is its own option, and a
// key the binding has no name for goes through an `...Extra` record as JSON text.
// The binding builds the document from the members the caller set, sends nothing
// when none was set, and validates no member: the library refuses a key it does not
// know, or a value that is not one of the member's, with CHS_INVALID_ARGUMENT,
// naming it.

/** `insert_options.verdict_basis`: what row a filter verdict answers for. */
export const VerdictBasis = {
  /** The default: the verdict answers for the input row. */
  Input: 'input',
  /** The row the export would store. A build that does not implement it declines it. */
  Export: 'export',
} as const;
export type VerdictBasis = (typeof VerdictBasis)[keyof typeof VerdictBasis];

/** `insert_options.defer_defaults`. */
export const DeferDefaults = {
  /** The default: no DEFAULT is deferred. */
  None: 'none',
  /** Defer to the server each DEFAULT the build would otherwise decline. A build that does not implement it declines it. */
  Server: 'server',
} as const;
export type DeferDefaults = (typeof DeferDefaults)[keyof typeof DeferDefaults];

/** `filter_options.param_encoding`: how a query parameter's value is read. */
export const ParamEncoding = {
  /** The default: ClickHouse's escaped-text reader. */
  Escaped: 'escaped',
  /** The bytes as the value. A build that does not implement it declines it. */
  Raw: 'raw',
} as const;
export type ParamEncoding = (typeof ParamEncoding)[keyof typeof ParamEncoding];

/** One entry of `insert_options.column_defaults`: the value the call uses in a cell the server would fill from the column's DEFAULT. Both are byte strings: bytes that are not valid UTF-8 are sent in the `name_b64` and `value_b64` forms. */
export interface ColumnDefault {
  readonly name: BytesIn;
  readonly value: BytesIn;
}

/** The `insert_options` members of the four calls that model an INSERT of a body (`row`, `rows`, `parseBlock`, and a filter's `rows`): what `encodeInsertOptions` reads from those calls' options objects. */
export interface InsertOptions {
  /** The columns an INSERT may supply (the server's column-level INSERT grant). Absent leaves the member out (every column); `[]` is sent as `[]` (no column). */
  readonly insertableColumns?: readonly BytesIn[] | undefined;
  /** Per-call DEFAULT overrides. Absent leaves the member out; `[]` is sent as `[]`. */
  readonly columnDefaults?: readonly ColumnDefault[] | undefined;
  readonly verdictBasis?: VerdictBasis | undefined;
  readonly deferDefaults?: DeferDefaults | undefined;
  /** Any other member of `insert_options`: key to its value as JSON text, passed through as given (rule r1). A key also named above keeps this value. Text that is not valid JSON is a `UsageError` before any call. */
  readonly insertOptionsExtra?: Readonly<Record<string, string>> | undefined;
}

/** The `filter_options` members of `Schema.compileFilter`. */
export interface FilterOptionMembers {
  readonly paramEncoding?: ParamEncoding | undefined;
  /** Any other member of `filter_options`, as `insertOptionsExtra` is for `insert_options`. */
  readonly filterOptionsExtra?: Readonly<Record<string, string>> | undefined;
}

/** The `schema_options` members of `Library.compileTable`. */
export interface SchemaOptionMembers {
  /** How the statement is compiled: `Posture.Create` (the default), as a server's CREATE TABLE, or `Posture.Load`, as a server loads the stored definition of a table it holds. */
  readonly posture?: Posture | undefined;
  /** Any other member of `schema_options`, as `insertOptionsExtra` is for `insert_options`. */
  readonly schemaOptionsExtra?: Readonly<Record<string, string>> | undefined;
}

/** A name or value as a byte-string member: `<key>` when it is text, `<key>_b64` otherwise. */
function byteMember(what: string, key: string, v: BytesIn): Record<string, string> {
  if (typeof v === 'string') {
    if (hasLoneSurrogate(v)) throw usageError(`${what}: the ${key} is not valid UTF-8, and ${NON_UTF8_HINT}`);
    return { [key]: v };
  }
  if (!(v instanceof Uint8Array)) throw new TypeError(`chtypes: ${what}: the ${key} must be a string or a Uint8Array, got ${typeof v}`);
  return isValidUtf8(v) ? { [key]: Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString('utf8') } : { [`${key}_b64`]: asBuffer(v).toString('base64') };
}

function nameObject(what: string, name: BytesIn): Record<string, string> {
  return byteMember(what, 'name', name);
}

/** The members of an options document under construction, each as its JSON text, by key; a key set twice keeps the last. */
class OptionDoc {
  readonly #members = new Map<string, string>();
  readonly what: string;

  constructor(what: string) {
    this.what = what;
  }

  set(key: string, value: unknown): void {
    this.#members.set(key, JSON.stringify(value));
  }

  /** Members given as JSON text, passed through verbatim after a validity check. */
  setRaw(extra: Readonly<Record<string, string>> | undefined): void {
    if (extra === undefined) return;
    for (const [key, text] of Object.entries(extra)) {
      if (hasLoneSurrogate(key)) throw usageError(`${this.what}: ${keyName(key)} is not valid UTF-8, and ${NON_UTF8_HINT}`);
      if (typeof text !== 'string') throw new TypeError(`chtypes: ${this.what}[${JSON.stringify(key)}] must be JSON text (a string), got ${typeof text}`);
      try {
        JSON.parse(text);
      } catch {
        throw usageError(`${this.what}: the value for key ${JSON.stringify(key)} is not valid JSON`);
      }
      this.#members.set(key, text);
    }
  }

  /** The document, or nothing (length 0, which the library reads as `{}`) when no member was set. */
  encode(): Buffer {
    if (this.#members.size === 0) return Buffer.alloc(0);
    const members = [...this.#members].map(([k, text]) => `${JSON.stringify(k)}:${text}`);
    return Buffer.from(`{${members.join(',')}}`, 'utf8');
  }
}

/** The `insert_options` document of a call that models an INSERT. */
export function encodeInsertOptions(o: InsertOptions): Buffer {
  const doc = new OptionDoc('insert options');
  if (o.insertableColumns !== undefined) doc.set('insertable_columns', o.insertableColumns.map((n) => nameObject('insert options', n)));
  if (o.columnDefaults !== undefined) {
    doc.set(
      'column_defaults',
      o.columnDefaults.map((d) => ({ ...nameObject('insert options', d.name), ...byteMember('insert options', 'value', d.value) })),
    );
  }
  if (o.verdictBasis !== undefined) doc.set('verdict_basis', o.verdictBasis);
  if (o.deferDefaults !== undefined) doc.set('defer_defaults', o.deferDefaults);
  doc.setRaw(o.insertOptionsExtra);
  return doc.encode();
}

/** The `filter_options` document of `chs_filter_create`. */
export function encodeFilterOptions(o: FilterOptionMembers): Buffer {
  const doc = new OptionDoc('filter options');
  if (o.paramEncoding !== undefined) doc.set('param_encoding', o.paramEncoding);
  doc.setRaw(o.filterOptionsExtra);
  return doc.encode();
}

/** The `schema_options` document of `chs_schema_create`. */
export function encodeSchemaOptions(o: SchemaOptionMembers): Buffer {
  const doc = new OptionDoc('schema options');
  if (o.posture !== undefined) doc.set('posture', o.posture);
  doc.setRaw(o.schemaOptionsExtra);
  return doc.encode();
}
