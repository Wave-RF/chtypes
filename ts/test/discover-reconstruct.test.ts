/**
 * Unit coverage for `reconstructDdlWith` (`../src/discover.js`) that needs no
 * loaded artifact — `Library#reconstructDdl` wraps it to supply its own
 * `quoteIdentifier`.
 *
 * Reconstruction moved from a free function to a method on a loaded library
 * in issue #52, because spelling a column name now goes through the
 * artifact. That took this no-artifact coverage away; it is restored here
 * against `reconstructDdlWith` directly, with a marker quoter standing in
 * for the library's real one.
 *
 * The marker quoter, `marker()` below, returns something deliberately
 * un-ClickHouse-like (`<name>`, never a back-quoted spelling) so these tests
 * pin this module's own logic — the column kinds, the expressions, and the
 * refusals — and never the rule for how a name is quoted. That rule now
 * lives behind an artifact (issue #119); pinning any spelling of it here
 * would re-assert what #52 deleted. Mirrors `rust/src/discover.rs`'s
 * `marker` test helper and the two tests built on it.
 */

import { describe, expect, it } from 'vitest';
import { type DiscoveredColumn, reconstructDdlWith } from '../src/discover.js';
import { ChtypesError } from '../src/errors.js';

/** Stand-in for the library's own `quoteIdentifier`: deliberately NOT a
 * ClickHouse spelling, so these cases pin only what this module decides —
 * the kinds, the expressions, the refusals. */
function marker(name: string): string {
  return '<' + name + '>';
}

/** Fills in the fields a test case does not care about, so each case names
 * only what it is pinning. */
function col(
  partial: Partial<DiscoveredColumn> & Pick<DiscoveredColumn, 'name' | 'type'>,
): DiscoveredColumn {
  return { defaultKind: '', defaultExpression: '', position: 0, ...partial };
}

describe('reconstructDdlWith: kinds, expressions, and refusals (no artifact)', () => {
  it('spells kinds and leaves the name to the quoter', () => {
    const cols: DiscoveredColumn[] = [
      col({ name: 'id', type: 'UInt64' }),
      col({ name: 'ts', type: 'DateTime', defaultKind: 'DEFAULT', defaultExpression: 'now()' }),
      col({ name: 'n.a', type: 'Array(Int64)' }),
      col({ name: 'e', type: 'UInt8', defaultKind: 'EPHEMERAL' }),
      col({ name: 'm', type: 'UInt64', defaultKind: 'MATERIALIZED', defaultExpression: 'id + 1' }),
    ];
    const ddl = reconstructDdlWith(cols, marker);
    // Every name is whatever the quoter answered, verbatim: this function no
    // longer decides which names are spelled how.
    expect(ddl).toContain('<n.a> Array(Int64)');
    expect(ddl).toContain('<ts> DateTime DEFAULT now()');
    expect(ddl).toContain('<e> UInt8 EPHEMERAL');
    expect(ddl).toContain('<m> UInt64 MATERIALIZED id + 1');
  });

  it('surfaces refusals for missing expressions, unknown kinds, and no columns', () => {
    // DEFAULT/MATERIALIZED/ALIAS require an expression.
    for (const kind of ['DEFAULT', 'MATERIALIZED', 'ALIAS'] as const) {
      expect(() =>
        reconstructDdlWith([col({ name: 'x', type: 'UInt8', defaultKind: kind })], marker),
      ).toThrow(ChtypesError);
    }

    // An unknown kind errors rather than passing through.
    expect(() =>
      reconstructDdlWith([col({ name: 'x', type: 'UInt8', defaultKind: 'WEIRD' })], marker),
    ).toThrow(ChtypesError);

    // An expression with no kind errors: it would silently drop semantics.
    expect(() =>
      reconstructDdlWith([col({ name: 'x', type: 'UInt8', defaultExpression: '1' })], marker),
    ).toThrow(ChtypesError);

    // No columns is a wrong table, not an empty DDL.
    expect(() => reconstructDdlWith([], marker)).toThrow(ChtypesError);
  });

  it('lets EPHEMERAL omit its expression, and carry one', () => {
    const ddl = reconstructDdlWith(
      [col({ name: 'e', type: 'UInt8', defaultKind: 'EPHEMERAL', defaultExpression: '7' })],
      marker,
    );
    expect(ddl).toBe('<e> UInt8 EPHEMERAL 7');
  });
});
