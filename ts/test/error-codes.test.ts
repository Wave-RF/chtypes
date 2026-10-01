/**
 * Revision 6's error-code table.
 *
 * The first half needs no artifact: it parses documents in the shape
 * `chs_error_codes` returns through the SAME parser `Library#errorCodes()`
 * uses, and drives the success-only cache with a stand-in for the native call.
 * What it pins is the binding's own contract — unknown is absent, `all()` is
 * ascending, names match exactly, a null answer is never remembered — and none
 * of it is a ClickHouse fact.
 *
 * The middle drives `chs_error_codes` through the REAL FFI path on the ABI
 * fixture's at-revision stub, built from this header, when
 * `$CHTYPES_ABI_FIXTURES` names one. The last part asks a loaded revision-6
 * library and skips LOUDLY, by name, without one.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { ErrorCodeCache, parseErrorCodes } from '../src/error-codes.js';
import {
  ChtypesError,
  ErrorCodeTable,
  Registry,
  SchemaError,
  UnsupportedError,
  looksLikeRegistry,
  resolveRegistryDir,
  type Library,
} from '../src/index.js';
import { REAL_ARTIFACT_TIMEOUT_MS } from './real-artifact-timeout.js';

// Out of order on purpose, with unknown keys at both levels, an entry with no
// name and a negative code — none of which may reach a lookup.
const FAKE = JSON.stringify({
  error_codes: [
    { code: 252, name: 'TOO_MANY_PARTS', since: 'whatever' },
    { code: 0, name: 'OK' },
    { code: 1, name: 'UNSUPPORTED_METHOD' },
    { code: 7, name: '' },
    { code: -2, name: 'NOT_A_CLICKHOUSE_CODE' },
    { code: 47, name: 'UNKNOWN_IDENTIFIER' },
  ],
  generator: 'ignored',
});

const KNOWN: ReadonlyArray<readonly [number, string]> = [
  [0, 'OK'],
  [1, 'UNSUPPORTED_METHOD'],
  [47, 'UNKNOWN_IDENTIFIER'],
  [252, 'TOO_MANY_PARTS'],
];

describe('the error-code table (no artifact)', () => {
  it('answers the document and nothing else', () => {
    const table = parseErrorCodes(FAKE);
    expect(table).toBeInstanceOf(ErrorCodeTable);
    for (const [code, name] of KNOWN) {
      expect(table.name(code)).toBe(name);
      expect(table.code(name)).toBe(code);
    }
    // Absent, never synthesized: an unknown code, the unnamed entry, a
    // negative code even though the document carried one, the ABI sentinels.
    for (const code of [2, 7, 999_999, -1, -2, -3]) expect(table.name(code), String(code)).toBeUndefined();
    // Exact and case-sensitive; no trimming, no folding.
    for (const name of ['too_many_parts', 'Too_Many_Parts', ' TOO_MANY_PARTS', 'TOO_MANY_PARTS ', '', 'NOT_A_CLICKHOUSE_CODE', 'NO_SUCH_ERROR']) {
      expect(table.code(name), name).toBeUndefined();
    }
  });

  it('lists every entry in ascending code order, by all() and by iteration', () => {
    const table = parseErrorCodes(Buffer.from(FAKE, 'utf8'));
    const want = KNOWN.map(([code, name]) => ({ code, name }));
    expect(table.all()).toEqual(want);
    expect([...table]).toEqual(want);
  });

  it('keeps the first entry on a repeat', () => {
    const table = parseErrorCodes(
      '{"error_codes":[{"code":5,"name":"A_NAME"},{"code":5,"name":"B_NAME"},{"code":6,"name":"A_NAME"}]}',
    );
    expect(table.name(5)).toBe('A_NAME');
    expect(table.name(6)).toBeUndefined();
    expect(table.all()).toHaveLength(1);
  });

  it.each(['{}', '{"error_codes":null}', '{"error_codes":[]}', '{"something_else":[1,2,3]}'])(
    'reads %s as an empty table',
    (doc) => {
      const table = parseErrorCodes(doc);
      expect(table.all()).toEqual([]);
      expect(table.name(0)).toBeUndefined();
    },
  );

  // The same list every binding's bad-document test runs: a truncated
  // document, a top-level value that is not an object, error_codes that is not
  // an array, an entry that is not an object, and a field of the wrong type.
  it.each([
    '{"error_codes":[',
    '[]',
    'null',
    '42',
    '"x"',
    '{"error_codes":{}}',
    '{"error_codes":[null]}',
    '{"error_codes":[[252,"X"]]}',
    '{"error_codes":[{"code":"252","name":"X"}]}',
    '{"error_codes":[{"code":252.5,"name":"X"}]}',
    '{"error_codes":[{"code":1,"name":5}]}',
  ])(
    'refuses %s with the general error',
    (doc) => {
      let caught: unknown;
      try {
        parseErrorCodes(doc);
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(ChtypesError);
      expect(caught).not.toBeInstanceOf(SchemaError);
      expect(caught).not.toBeInstanceOf(UnsupportedError);
    },
  );

  it('caches a built table and nothing else', () => {
    const cache = new ErrorCodeCache();
    const calls: string[] = [];
    // A null answer: the general error, not a decline, and not remembered.
    expect(() =>
      cache.get(() => {
        calls.push('null');
        return null;
      }),
    ).toThrow(ChtypesError);
    // A missing symbol: the decline type, not remembered either.
    expect(() =>
      cache.get(() => {
        calls.push('missing');
        throw new UnsupportedError('this artifact predates chs_error_codes (rebuild it)');
      }),
    ).toThrow(UnsupportedError);
    const first = cache.get(() => {
      calls.push('built');
      return Buffer.from(FAKE, 'utf8');
    });
    expect(first.name(252)).toBe('TOO_MANY_PARTS');
    expect(
      cache.get(() => {
        throw new Error('the cache asked the library again after a table was built');
      }),
    ).toBe(first);
    expect(calls).toEqual(['null', 'missing', 'built']);
  });
});

// ---------------------------------------------------- through the ABI fixture

const FIXTURES = process.env['CHTYPES_ABI_FIXTURES'];
const HAVE_FIXTURES = FIXTURES !== undefined && FIXTURES !== '';

/**
 * The at-revision stub is the one library that exists before any revision-6
 * artifact does, and the one every pull request's abi-fixtures job loads. It
 * answers by return type, not with a table, so this pins the wiring rather than
 * an answer: the symbol resolves (never the decline type, never a refusal), and
 * whatever it answers takes the rule — a null is the general error and is not
 * kept, a document is kept.
 */
describe.skipIf(!HAVE_FIXTURES)('the error-code table through the ABI fixture', () => {
  it('resolves chs_error_codes through the real FFI path, and keeps only a built table', () => {
    const root = FIXTURES!;
    const doc = JSON.parse(readFileSync(path.join(root, 'fixture.json'), 'utf8')) as { clickhouse_minor: string };
    const fixtureRegistry = new Registry(path.join(root, 'at-revision'), { preload: [doc.clickhouse_minor] });
    try {
      const library = fixtureRegistry.for(doc.clickhouse_minor);
      let first: ErrorCodeTable | undefined;
      let caught: unknown;
      try {
        first = library.errorCodes();
      } catch (err) {
        caught = err;
      }
      if (first !== undefined) {
        expect(library.errorCodes(), 'a built table was not kept').toBe(first);
        return;
      }
      expect(caught).toBeInstanceOf(ChtypesError);
      expect(caught, 'the symbol is declared by this header: never a decline').not.toBeInstanceOf(UnsupportedError);
      expect(caught, 'and never a refusal').not.toBeInstanceOf(SchemaError);
      expect((caught as Error).message).toMatch(/returned no document/);
      // Not kept: the next call asks the library again, and gets null again.
      expect(() => library.errorCodes()).toThrow(/returned no document/);
    } finally {
      fixtureRegistry.close();
    }
  }, REAL_ARTIFACT_TIMEOUT_MS);
});

// ------------------------------------------------------------- with artifacts

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

let registry: Registry | undefined;
const rev6: Library[] = [];
if (HAVE_REGISTRY) {
  registry = new Registry(REGISTRY ?? undefined);
  for (const line of registry.versions()) {
    try {
      const library = registry.for(line);
      if (library.abiRevision >= 6) rev6.push(library);
    } catch (err) {
      console.warn(`[chtypes] line ${line} not exercised: ${(err as Error).message}`);
    }
  }
}
if (rev6.length === 0) {
  console.warn(
    '[chtypes] error-codes artifact tests SKIPPED: no ABI revision-6 artifact on the search path — ' +
      'fetch one with scripts/fetch.sh (docs/guides/fetch.md).',
  );
}

afterAll(() => {
  registry?.close();
});

describe.skipIf(rev6.length === 0)('the error-code table from a loaded revision-6 library', () => {
  it('is ascending, round-trips, names 252, and is kept', () => {
    for (const library of rev6) {
      const table = library.errorCodes();
      const all = table.all();
      expect(all.length, library.minor).toBeGreaterThan(0);
      for (let i = 1; i < all.length; i++) expect(all[i - 1]!.code).toBeLessThan(all[i]!.code);
      for (const e of all) {
        expect(table.name(e.code)).toBe(e.name);
        expect(table.code(e.name)).toBe(e.code);
      }
      expect(table.name(252), library.minor).toBe('TOO_MANY_PARTS');
      expect(table.name(-1)).toBeUndefined();
      expect(table.name(-2)).toBeUndefined();
      expect(library.errorCodes(), `${library.minor}: the table was not kept`).toBe(table);
    }
  });

  it('answers 903 per line, as the header documents — absent on 25.10', () => {
    // The producer's measurement of every served line. `null` is ABSENT (the
    // table answers `undefined`), never a synthesized name. A line not named
    // here (26.10 and later) has no expectation and is not checked.
    const want = new Map<string, string | null>([
      ['25.3', 'LICENSE_EXPIRED'],
      ['25.8', 'LICENSE_EXPIRED'],
      ['25.10', null],
      ...[2, 3, 4, 5, 6, 7, 8, 9].map((m) => [`26.${m}`, 'DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN'] as [string, string]),
    ]);
    let checked = 0;
    for (const library of rev6) {
      if (!want.has(library.minor)) continue;
      const expected = want.get(library.minor) ?? undefined;
      expect(library.errorCodes().name(903), library.minor).toBe(expected);
      checked++;
    }
    if (checked === 0) console.warn('[chtypes] no loaded revision-6 line has a documented expectation for code 903');
  });
});
