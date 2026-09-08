/**
 * The 2026-08-26 SDK-fix cycle's regression tests: outcome degradation, the
 * decline TYPE on the rows() path, the introspection trio, discovery
 * duplicate handling, and release-order sorting.
 */

import { describe, expect, it } from 'vitest';
import {
  Registry,
  UnsupportedError,
  looksLikeRegistry,
  parseChangedSettingsResult,
  parseDocument,
  resolveRegistryDir,
  Format,
} from '../src/index.js';
import { rowResultOf } from '../src/results.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

const buf = (s: string): Buffer => Buffer.from(s, 'utf8');

describe('outcome degradation', () => {
  // spec/bindings.md §RowResult: an unrecognised outcome string maps to
  // `unsupported`, never `rejected` — a future artifact's new verdict must
  // land on the arm that is never scored as agreement, not manufacture an
  // over-reject out of vocabulary drift.
  it('maps an unknown outcome string to unsupported, never rejected', () => {
    const doc = parseDocument(buf('{"outcome":"verdict_from_the_future","code":0,"err":"","cols":[]}'));
    expect(rowResultOf(doc).outcome).toBe('unsupported');
  });

  it('maps the four known spellings exactly', () => {
    for (const s of ['accepted', 'rejected', 'accepted_poisoned', 'unsupported'] as const) {
      const doc = parseDocument(buf(`{"outcome":"${s}","code":0,"err":"","cols":[]}`));
      expect(rowResultOf(doc).outcome).toBe(s);
    }
  });
});

describe('discovery duplicate names', () => {
  // spec/bindings.md §Discovery: LAST write wins, in every SDK, so one server
  // answer can never discover two different profiles.
  it('resolves a duplicated name last-write-wins', () => {
    const body = buf(
      '{"name":"flatten_nested","value":"1"}\n' +
        '{"name":"max_block_size","value":"65409"}\n' +
        '{"name":"flatten_nested","value":"0"}\n',
    );
    expect(parseChangedSettingsResult(body)).toEqual({
      flatten_nested: '0',
      max_block_size: '65409',
    });
  });
});

describe.skipIf(!HAVE_REGISTRY)('against a real registry', () => {
  it('libraries() comes back in release order, not directory order', () => {
    const registry = new Registry(REGISTRY ?? undefined);
    const minors = registry.libraries().map((l) => l.minor);
    const numeric = (m: string): number => {
      const [a = '0', b = '0'] = m.split('.');
      return Number.parseInt(a, 10) * 1000 + Number.parseInt(b, 10);
    };
    for (let i = 1; i < minors.length; i++) {
      expect(numeric(minors[i]!)).toBeGreaterThan(numeric(minors[i - 1]!));
    }
    // The canonical trap: lexical order puts 25.10 before 25.8.
    if (minors.includes('25.8') && minors.includes('25.10')) {
      expect(minors.indexOf('25.8')).toBeLessThan(minors.indexOf('25.10'));
    }
    // versions() and libraries() agree on the order.
    expect(registry.versions()).toEqual(minors);
  });

  it('exposes the introspection trio (spec/bindings.md §Introspection)', () => {
    const registry = new Registry(REGISTRY ?? undefined);
    const lib = registry.libraries()[registry.libraries().length - 1]!;
    const families = lib.registeredFamilies();
    expect(families).toContain('String');
    expect(families.length).toBeGreaterThan(100);
    const flags = lib.functionFlags();
    expect(flags).toContain('now\t');
    const lines = flags.split('\n').filter((l) => l !== '');
    expect(lines.length).toBeGreaterThan(500);
    expect(lines[0]!.split('\t')).toHaveLength(6);
    // The trio's third member: reference_type, already part of the surface.
    expect(lib.referenceType('UInt8')).toBe('Int256');
  });

  it('rows() degrades a missing chs_rows to UnsupportedError, exactly as row() does', () => {
    const registry = new Registry(REGISTRY ?? undefined);
    const lib = registry.libraries()[0]!;
    const schema = lib.compileDdl('x UInt8');
    try {
      // Reach into the interned NativeLibrary and make ffi-rs's
      // missing-symbol throw happen for chs_rows / chs_row. This is the shape
      // an artifact built before the symbol produces; no artifact in dist/out
      // lacks either, so the path is simulated rather than found.
      const native = (lib as unknown as { native: { fns: Record<string, unknown> } }).native;
      const missing = (name: string) => () => {
        throw new Error(`Cannot find "${name}" function in shared library`);
      };
      const origRows = native.fns['chs_rows'];
      const origRow = native.fns['chs_row'];
      native.fns['chs_rows'] = missing('chs_rows');
      native.fns['chs_row'] = missing('chs_row');
      try {
        expect(() => schema.rows(Format.JSONEachRow, buf('{"x":1}\n'))).toThrowError(UnsupportedError);
        expect(() => schema.rows(Format.JSONEachRow, buf('{"x":1}\n'))).toThrowError(
          /\[-2\].*predates chs_rows/,
        );
        // The symmetry this fix restored: row() already degraded this way.
        expect(() => schema.row(Format.JSONEachRow, buf('{"x":1}'))).toThrowError(UnsupportedError);
        expect(() => schema.row(Format.JSONEachRow, buf('{"x":1}'))).toThrowError(
          /\[-2\].*predates chs_row/,
        );
      } finally {
        native.fns['chs_rows'] = origRows;
        native.fns['chs_row'] = origRow;
      }
      // Restored: the real path answers again.
      expect(schema.rows(Format.JSONEachRow, buf('{"x":256}\n')).rows[0]!.outcome).toBe('accepted');
    } finally {
      schema.close();
    }
  });
});
