/**
 * The discovery kit.
 *
 * This library NEVER talks to ClickHouse; it ships the queries. At connect
 * time a caller (a gateway, WaveHouse) runs these three queries against the
 * deployment's own server with whatever client it already has, feeds the
 * results to the typed parsers below, and then declares what it learned:
 *
 *   profile.version  -> Registry#for / the artifact to load
 *   profile.settings -> compileDdl({ settings }) (compile-time) and per-call settings
 *   columns          -> reconstructDdl -> compileDdl
 *
 * The pattern, in full (spec/bindings.md §Discovery):
 *
 *   1. run QUERY_SERVER_VERSION, QUERY_CHANGED_SETTINGS once per connection
 *   2. cache the ServerProfile per deployment/tenant
 *   3. const lib = registry.for(profile.version)
 *   4. const schema = lib.compileDdl(ddl, { settings: profile.settings, mode: 0 })
 *   5. per call: pass profile.settings (plus any per-INSERT overrides) to rows()
 *
 * Never ask the customer for their settings — ask their server. The queries
 * return exactly what the server believes, spelled the way the server spells
 * it, which is what the settings gate (spec/c-abi.md, Settings rule 2)
 * validates against.
 *
 * The parsers read JSONEachRow bytes through this package's own byte-exact
 * JSON reader, so a numeric field (`position`) is taken from its literal
 * digits — quoted or bare, ClickHouse's stock HTTP output quotes 64-bit
 * integers — and never routed through a float.
 */

import { ChtypesError } from './errors.js';
import { asString, parseJsonValue, rawText, type Json } from './json.js';

// The canonical discovery queries. Each carries its FORMAT clause so the
// bytes an HTTP client gets back are exactly what the matching parse*
// function consumes. A native-protocol client that returns typed rows
// instead can ignore the parsers and fill the structs directly.

/**
 * Names the deployment's exact release — the string `Registry#for` resolves
 * (minor line or exact patch both work). One row: `{"version":"25.8.28.1"}`.
 */
export const QUERY_SERVER_VERSION = 'SELECT version() AS version FORMAT JSONEachRow';

/**
 * Lists every query setting the deployment runs at a NON-default value — the
 * whole declared profile, from the server itself. One row per setting:
 * `{"name":"flatten_nested","value":"0"}`. `system.settings.value` is already
 * a String; the map feeds `compileDdl({ settings })` and per-call settings
 * verbatim.
 */
export const QUERY_CHANGED_SETTINGS = 'SELECT name, value FROM system.settings WHERE changed FORMAT JSONEachRow';

/**
 * Describes one existing table, in declaration order, with everything a
 * column-declaration list needs — INCLUDING `default_kind` and
 * `default_expression`, without which a reconstructed schema silently loses
 * its DEFAULT/MATERIALIZED semantics. Uses ClickHouse's own query parameters:
 * send `param_db` / `param_table` (HTTP) or bind `{db}`/`{table}` (native).
 */
export const QUERY_TABLE_COLUMNS =
  'SELECT name, type, default_kind, default_expression, position ' +
  'FROM system.columns WHERE database = {db:String} AND table = {table:String} ' +
  'ORDER BY position FORMAT JSONEachRow';

/**
 * What discovery learns about one deployment: the exact release and the
 * settings it runs changed from defaults. Cache one per deployment (or per
 * tenant on bring-your-own-ClickHouse) and declare it.
 */
export interface ServerProfile {
  /** e.g. "25.8.28.1" — feed `Registry#for`. */
  readonly version: string;
  /** Changed settings — feed `compileDdl({ settings })` and per-call settings. */
  readonly settings: Record<string, string>;
}

/** One row of `QUERY_TABLE_COLUMNS`. */
export interface DiscoveredColumn {
  readonly name: string;
  readonly type: string;
  /** "" | "DEFAULT" | "MATERIALIZED" | "ALIAS" | "EPHEMERAL" */
  readonly defaultKind: string;
  readonly defaultExpression: string;
  readonly position: number;
}

const asBuffer = (body: Uint8Array | string): Buffer =>
  typeof body === 'string'
    ? Buffer.from(body, 'utf8')
    : Buffer.isBuffer(body)
      ? body
      : Buffer.from(body.buffer, body.byteOffset, body.byteLength);

/** Split a JSONEachRow body into one parsed object per line. */
function jsonEachRowDocs(body: Uint8Array | string): Json[] {
  const out: Json[] = [];
  for (const line of splitLines(asBuffer(body))) {
    if (line[0] !== 0x7b /* { */) {
      throw new ChtypesError(`chtypes: not a JSONEachRow line: ${line.toString('utf8').slice(0, 60)}`);
    }
    const doc = parseJsonValue(line);
    if (doc === null || doc.kind !== 'object') {
      throw new ChtypesError(`chtypes: not a JSONEachRow line: ${line.toString('utf8').slice(0, 60)}`);
    }
    out.push(doc);
  }
  return out;
}

function splitLines(body: Buffer): Buffer[] {
  const out: Buffer[] = [];
  let start = 0;
  for (let i = 0; i <= body.length; i++) {
    if (i === body.length || body[i] === 0x0a) {
      const line = trimSpace(body.subarray(start, i));
      if (line.length > 0) out.push(line);
      start = i + 1;
    }
  }
  return out;
}

/** ASCII whitespace trim over bytes, so no decode happens before the parse. */
function trimSpace(line: Buffer): Buffer {
  let a = 0;
  let b = line.length;
  const ws = (c: number | undefined): boolean => c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d;
  while (a < b && ws(line[a])) a++;
  while (b > a && ws(line[b - 1])) b--;
  return line.subarray(a, b);
}

/** A field of a discovery row, by key — the reference's `row[k]`, last wins. */
function member(doc: Json, key: string): Json | undefined {
  let found: Json | undefined;
  for (let i = 0; i < doc.keys.length; i++) {
    if (doc.keys[i]!.toString('utf8') === key) found = doc.items[i];
  }
  return found;
}

/**
 * A field's value that may arrive quoted or bare (ClickHouse quotes 64-bit
 * integers in JSON output by default), digits preserved exactly — never
 * through a float. Mirrors the reference's `jsonString`.
 */
function jsonString(value: Json | undefined): string {
  if (value === undefined) return '';
  // A parsed value's raw slice carries no surrounding whitespace, so the
  // reference's TrimSpace has nothing to do here — and JS's own `trim()` is
  // deliberately avoided in this package (its WhiteSpace set includes U+FEFF).
  return value.kind === 'string' ? asString(value) : rawText(value);
}

/**
 * Read `QUERY_SERVER_VERSION`'s JSONEachRow body.
 *
 * @param body - the query result bytes (or text) as the server sent them.
 * @returns the version string, e.g. `"25.8.28.1"` — feed `Registry#for`.
 * @throws {ChtypesError} when the body is not exactly one JSONEachRow row with
 *   a non-empty `version` field.
 */
export function parseVersionResult(body: Uint8Array | string): string {
  const docs = jsonEachRowDocs(body);
  if (docs.length !== 1) {
    throw new ChtypesError(`chtypes: version query returned ${docs.length} rows, want 1`);
  }
  const v = member(docs[0]!, 'version');
  if (v === undefined) {
    throw new ChtypesError('chtypes: version query row has no `version` field');
  }
  const out = jsonString(v);
  if (out === '') {
    throw new ChtypesError('chtypes: version query returned an empty version');
  }
  return out;
}

/**
 * Read `QUERY_CHANGED_SETTINGS`' JSONEachRow body. An empty body is a stock
 * server: an empty map, not an error.
 *
 * @param body - the query result bytes (or text) as the server sent them.
 * @returns setting name → value, ready for `ServerProfile#settings`,
 *   `compileDdl({ settings })` and per-call settings.
 * @throws {ChtypesError} when a row is not JSONEachRow or lacks `name`/`value`.
 */
export function parseChangedSettingsResult(body: Uint8Array | string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const doc of jsonEachRowDocs(body)) {
    const name = member(doc, 'name');
    if (name === undefined) {
      throw new ChtypesError(`chtypes: settings row has no \`name\` field: ${rawText(doc).slice(0, 60)}`);
    }
    const value = member(doc, 'value');
    if (value === undefined) {
      throw new ChtypesError(`chtypes: settings row has no \`value\` field: ${rawText(doc).slice(0, 60)}`);
    }
    out[jsonString(name)] = jsonString(value);
  }
  return out;
}

/**
 * Read `QUERY_TABLE_COLUMNS`' JSONEachRow body, in the query's ORDER BY
 * position order. `position` survives both spellings — quoted on stock HTTP
 * output (`output_format_json_quote_64bit_integers=1`) and bare when a client
 * unsets that — parsed from its literal digits, never through `JSON.parse`'s
 * double. A position is small, so it fits a JS number once those digits are
 * in hand; a malformed one is left 0, as the reference leaves it.
 *
 * @param body - the query result bytes (or text) as the server sent them.
 * @returns one `DiscoveredColumn` per row, in the query's position order —
 *   feed `reconstructDdl`.
 * @throws {ChtypesError} when a row is malformed, lacks `name`/`type`, or the
 *   body holds no rows at all (wrong database/table, or no access).
 */
export function parseColumnsResult(body: Uint8Array | string): DiscoveredColumn[] {
  const out: DiscoveredColumn[] = [];
  for (const doc of jsonEachRowDocs(body)) {
    const name = jsonString(member(doc, 'name'));
    const type = jsonString(member(doc, 'type'));
    if (name === '' || type === '') {
      throw new ChtypesError(`chtypes: columns row missing name/type: ${rawText(doc).slice(0, 80)}`);
    }
    let position = 0;
    const raw = member(doc, 'position');
    if (raw !== undefined) {
      const digits = jsonString(raw);
      const n = Number(digits);
      if (/^\d+$/.test(digits) && Number.isSafeInteger(n)) position = n;
    }
    out.push({
      name,
      type,
      defaultKind: jsonString(member(doc, 'default_kind')),
      defaultExpression: jsonString(member(doc, 'default_expression')),
      position,
    });
  }
  if (out.length === 0) {
    throw new ChtypesError('chtypes: columns query returned no rows — wrong database/table, or no access');
  }
  return out;
}

/**
 * Quote an identifier the way ClickHouse DDL requires: plain
 * `[A-Za-z_][A-Za-z0-9_]*` stays bare, anything else is backticked with
 * backticks doubled. `system.columns` can return anything — the flattened
 * Nested idiom (`n.a`), spaces, keywords.
 */
function backquoteIfNeeded(name: string): string {
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return name;
  return '`' + name.replaceAll('`', '``') + '`';
}

/**
 * Turn `QUERY_TABLE_COLUMNS`' rows back into the column-declaration list
 * `compileDdl` takes. It is a spelling exercise, not a semantic one: types
 * and expressions are the server's own text, passed through verbatim, and the
 * library's own compile is the judge of the result.
 *
 * Two facts a caller must know, both properties of the server rather than of
 * this function:
 *
 *  - `system.columns` reports the table AS STORED. Under the default
 *    `flatten_nested=1` a `Nested(a,b)` column appears as its flattened
 *    `n.a`/`n.b` Array columns and reconstructs to exactly those — which is
 *    the same table. Under `flatten_nested=0` it appears as one column of
 *    type `Nested(...)`, which also reconstructs directly. Reconstruction is
 *    therefore shape-faithful either way, PROVIDED the same `flatten_nested`
 *    value is declared to `compileDdl({ settings })` that the table was
 *    created under — which is what the discovered `ServerProfile.settings`
 *    carries.
 *  - a MATERIALIZED/ALIAS column reconstructs with its expression; an
 *    EPHEMERAL column may legitimately have an empty `default_expression`.
 *
 * @param cols - the discovered columns, e.g. from `parseColumnsResult`.
 * @returns the column-declaration list for `Library#compileDdl`.
 * @throws {ChtypesError} when a column is inconsistent: no name/type, a
 *   `default_expression` with no kind, a DEFAULT/MATERIALIZED/ALIAS kind with
 *   no expression, an unknown kind, or no columns at all.
 */
export function reconstructDdl(cols: readonly DiscoveredColumn[]): string {
  if (cols.length === 0) {
    throw new ChtypesError('chtypes: no columns to reconstruct');
  }
  const parts: string[] = [];
  for (let i = 0; i < cols.length; i++) {
    const c = cols[i]!;
    if (c.name === '' || c.type === '') {
      throw new ChtypesError(`chtypes: column ${i} has no name/type`);
    }
    let decl = `${backquoteIfNeeded(c.name)} ${c.type}`;
    switch (c.defaultKind) {
      case '':
        if (c.defaultExpression !== '') {
          throw new ChtypesError(`chtypes: column ${c.name} has a default_expression but no default_kind`);
        }
        break;
      case 'DEFAULT':
      case 'MATERIALIZED':
      case 'ALIAS':
        if (c.defaultExpression === '') {
          throw new ChtypesError(`chtypes: column ${c.name} is ${c.defaultKind} but has no default_expression`);
        }
        decl += ` ${c.defaultKind} ${c.defaultExpression}`;
        break;
      case 'EPHEMERAL':
        decl += ' EPHEMERAL';
        if (c.defaultExpression !== '') decl += ` ${c.defaultExpression}`;
        break;
      default:
        throw new ChtypesError(`chtypes: column ${c.name} has unknown default_kind ${JSON.stringify(c.defaultKind)}`);
    }
    parts.push(decl);
  }
  return parts.join(', ');
}
