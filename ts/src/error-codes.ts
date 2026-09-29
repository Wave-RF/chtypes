/**
 * The error-code table (revision 6): one loaded build's own code -> name map.
 *
 * There is no table in this package, and there must never be one. The table is
 * a property of the BUILD: codes join and leave between ClickHouse lines, and
 * one number can name two different errors on two lines (903 is
 * LICENSE_EXPIRED on 25.3/25.8 and DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN from
 * 26.2). Every answer therefore comes from `Library#errorCodes()`, i.e. from
 * `chs_error_codes` of the library being asked, and
 * `scripts/check-no-error-code-table.py` fails the build if a literal code ->
 * name table appears in any binding.
 */

import { ChtypesError } from './errors.js';

/**
 * One row of a build's error-code table: a ClickHouse error code and the name
 * THAT BUILD gives it.
 */
export interface ErrorCodeEntry {
  readonly code: number;
  readonly name: string;
}

/**
 * One loaded library's own error-code table, from `chs_error_codes` — obtained
 * from `Library#errorCodes()` and valid for that library's ClickHouse line
 * only. Immutable once built.
 *
 * Lookups answer only what the build's own table holds: an unknown code, a
 * negative code (the ABI's -1 and -2 sentinels included — they are not
 * ClickHouse codes) or an unknown name is `undefined`, never a synthesized
 * spelling. Names match exactly and case-sensitively, as the server prints
 * them.
 */
export class ErrorCodeTable {
  private readonly entries: readonly ErrorCodeEntry[];
  private readonly byCode: ReadonlyMap<number, string>;
  private readonly byName: ReadonlyMap<string, number>;

  /** @internal — obtained from `Library#errorCodes()`. */
  constructor(entries: readonly ErrorCodeEntry[]) {
    const byCode = new Map<number, string>();
    const byName = new Map<string, number>();
    const kept: ErrorCodeEntry[] = [];
    for (const e of entries) {
      // Not a name, not a ClickHouse code, or a repeat: the first entry for a
      // code or a name wins, and nothing else is invented.
      if (e.name === '' || e.code < 0 || byCode.has(e.code) || byName.has(e.name)) continue;
      byCode.set(e.code, e.name);
      byName.set(e.name, e.code);
      kept.push(Object.freeze({ code: e.code, name: e.name }));
    }
    kept.sort((a, b) => a.code - b.code);
    this.entries = Object.freeze(kept);
    this.byCode = byCode;
    this.byName = byName;
  }

  /**
   * The name this build gives `code`.
   *
   * @returns the name, or `undefined` for an unknown or negative code.
   */
  name(code: number): string | undefined {
    if (code < 0) return undefined;
    return this.byCode.get(code);
  }

  /**
   * The code this build gives `name` — an exact, case-sensitive match.
   *
   * @returns the code, or `undefined` for an unknown name.
   */
  code(name: string): number | undefined {
    return this.byName.get(name);
  }

  /** Every entry, in ascending code order. */
  all(): readonly ErrorCodeEntry[] {
    return this.entries;
  }

  /** Iterates the entries in ascending code order, as `all()` returns them. */
  [Symbol.iterator](): Iterator<ErrorCodeEntry> {
    return this.entries[Symbol.iterator]();
  }
}

function bad(why: string): ChtypesError {
  return new ChtypesError(`chtypes: bad chs_error_codes document: ${why}`);
}

/**
 * Build a table from a `chs_error_codes` document,
 * `{"error_codes":[{"code":N,"name":"…"}, …]}`. Unknown keys are ignored and
 * an absent (or null) key is its default, like every document this ABI hands
 * back; a value of the wrong type is a bad document, never a guess.
 *
 * Not re-exported from the package: it is the parser `Library#errorCodes()`
 * runs, exported for this binding's own tests.
 */
export function parseErrorCodes(doc: Buffer | string): ErrorCodeTable {
  let parsed: unknown;
  try {
    parsed = JSON.parse(typeof doc === 'string' ? doc : doc.toString('utf8'));
  } catch (err) {
    throw bad((err as Error).message);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw bad('not a JSON object');
  const rows = (parsed as Record<string, unknown>)['error_codes'] ?? [];
  if (!Array.isArray(rows)) throw bad('error_codes is not an array');
  const entries: ErrorCodeEntry[] = [];
  for (const row of rows as unknown[]) {
    if (row === null || typeof row !== 'object' || Array.isArray(row)) throw bad('an entry is not an object');
    const code = (row as Record<string, unknown>)['code'] ?? 0;
    const name = (row as Record<string, unknown>)['name'] ?? '';
    if (typeof code !== 'number' || !Number.isInteger(code) || typeof name !== 'string') {
      throw bad(`entry ${JSON.stringify(row)}`);
    }
    entries.push({ code, name });
  }
  return new ErrorCodeTable(entries);
}

/**
 * One library's table once it has been built, and only then. A `null` answer
 * from `chs_error_codes` is a guarded exception inside the library — transient
 * by definition — so it throws `ChtypesError` and is NOT remembered: the next
 * call asks again. A missing symbol throws the decline type and is not
 * remembered either.
 *
 * Not re-exported from the package: `Library` holds one, and this binding's
 * own tests drive it directly.
 */
export class ErrorCodeCache {
  private table: ErrorCodeTable | undefined;

  get(fetch: () => Buffer | null): ErrorCodeTable {
    if (this.table !== undefined) return this.table;
    const doc = fetch();
    if (doc === null) {
      throw new ChtypesError(
        'chtypes: chs_error_codes returned no document (a guarded exception inside the library); ' +
          'nothing was cached, so the next call asks again',
      );
    }
    const table = parseErrorCodes(doc);
    this.table = table;
    return table;
  }
}
