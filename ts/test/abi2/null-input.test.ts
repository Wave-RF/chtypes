/**
 * The null_input decode cells, through the public API, over the generated v2
 * stub (variant `null-input`). The stub answers
 * `scripts/abi-v1/emit/_stubshared.py`'s NULL_INPUT_DOCS: for a JSON null and
 * a TSV `\N` into each of six non-Nullable types, the entry carries
 * `null_input` true; two controls carry it false and absent. Each entry's
 * expected value is read off its name (a `control.` name expects false), and
 * the cell count is the generator's NULL_INPUT_CELL_COUNT, pinned here as 12
 * (the Python suite fails first if the generator changes it), so a document
 * that lost its cells fails. Without `$CHTYPES_ABI2_STUBS` this SKIPS LOUDLY
 * by name.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { Format } from '../../src/abi2/index.js';
import type { Value } from '../../src/documents.js';
import { type Library, libraryOf } from '../../src/library.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;
const CELL_COUNT = 12; // _stubshared.NULL_INPUT_CELL_COUNT: 2 formats x 6 types

function openStub(variant: string): Library {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`stubs.json has no ${variant} variant`);
  const image: LoadedImage = openAbi2({
    libraryPath: path.join(STUBS_DIR as string, `${variant}.so`),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  return libraryOf(image, undefined);
}

function check(where: string, cols: readonly Value[]): number {
  let cells = 0;
  for (const c of cols) {
    const name = c.column.toString('utf8');
    const want = !name.startsWith('control.');
    if (want) cells++;
    expect({ where, name, nullInput: c.nullInput }).toEqual({ where, name, nullInput: want });
    // A NULL into a non-Nullable Enum8 stores 0, outside the enum: poisoned.
    const wantPoison = want && name.endsWith('.enum8');
    expect({ where, name, poison: c.poison }).toEqual({ where, name, poison: wantPoison });
  }
  return cells;
}

if (!stubsAvailable) {
  console.warn('null_input cells over the stub: SKIPPED. Set CHTYPES_ABI2_STUBS (scripts/abi-v1/build-stubs.sh --out DIR) to run them.');
}

describe.skipIf(!stubsAvailable)('null_input: a NULL literal into a non-Nullable column (stub null-input)', () => {
  it('every row and batch entry exposes nullInput, false where the member is false or absent', () => {
    const lib = openStub('null-input');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const r = schema.row(Format.JSONEachRow, Buffer.from('{"x":null}', 'utf8'));
    expect(check('row', r.columns)).toBe(CELL_COUNT);
    const b = schema.rows(Format.TSV, Buffer.from('\\N\n', 'utf8'));
    let cells = 0;
    b.rows.forEach((row, i) => {
      cells += check(`batch.rows[${i}]`, row.columns);
    });
    expect(cells).toBe(CELL_COUNT);
  });
});

/**
 * The poison and dup_dropped cells, variant `value-flags`
 * (`_stubshared.VALUE_FLAGS_DOCS`): a JSON null or TSV \N into a non-Nullable
 * Enum8 (poison, in JSONEachRow and TSV) and one canned entry that carries
 * dup_dropped, plus four controls that hold each member false and absent. A
 * `poison.` entry expects poison true and dupDropped false, a `dup.` entry the
 * reverse, a `control.` entry both false, so a
 * binding that swaps the members fails. The counts are pinned as 2 and 1 (the
 * Python suite fails first if the generator changes them).
 */
const POISON_CELLS = 2;
const DUP_CELLS = 1;

function checkFlags(where: string, cols: readonly Value[]): { poison: number; dup: number } {
  let poison = 0;
  let dup = 0;
  for (const c of cols) {
    const name = c.column.toString('utf8');
    const wantP = name.startsWith('poison.');
    const wantD = name.startsWith('dup.');
    if (!wantP && !wantD) expect({ where, name }).toEqual({ where, name: expect.stringMatching(/^control\./) });
    if (wantP) poison++;
    if (wantD) {
      dup++;
      // The producing input: a repeated top-level JSONEachRow key after every column was read; the first value is stored.
      expect({ where, name, text: c.text.toString('utf8') }).toEqual({
        where,
        name: 'dup.JSONEachRow.repeated_top_key',
        text: '1',
      });
    }
    expect({ where, name, poison: c.poison, dupDropped: c.dupDropped }).toEqual({
      where,
      name,
      poison: wantP,
      dupDropped: wantD,
    });
  }
  return { poison, dup };
}

describe.skipIf(!stubsAvailable)('poison and dup_dropped: an Enum8 NULL read, a canned dup_dropped entry (stub value-flags)', () => {
  it('every row and batch entry exposes both members, each false where it is false or absent', () => {
    const lib = openStub('value-flags');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const r = schema.row(Format.JSONEachRow, Buffer.from('{"a":1,"a":2}', 'utf8'));
    expect(checkFlags('row', r.columns)).toEqual({ poison: POISON_CELLS, dup: DUP_CELLS });
    const dupTransforms = r.transformed.filter(
      (t) => t.reason === 'duplicate_key_dropped' && t.column.toString('utf8') === 'dup.JSONEachRow.repeated_top_key',
    );
    expect(dupTransforms).toHaveLength(1);
    const b = schema.rows(Format.TSV, Buffer.from('1\n', 'utf8'));
    let poison = 0;
    let dup = 0;
    b.rows.forEach((row, i) => {
      const c = checkFlags(`batch.rows[${i}]`, row.columns);
      poison += c.poison;
      dup += c.dup;
    });
    expect({ poison, dup }).toEqual({ poison: POISON_CELLS, dup: DUP_CELLS });
  });
});
