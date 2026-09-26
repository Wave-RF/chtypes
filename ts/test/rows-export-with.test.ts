/**
 * Unit-level coverage for issue #54 (`Schema#rows(..., { rowFilter })`) that
 * needs no loaded artifact: the pure document-decoding path
 * (`batchResultOf` against a hand-built chs_rows-with-attached-filter
 * document, shaped exactly as the C ABI contract §Rows describes it) and
 * the TypeScript-level cross-library refusal `Schema#rows` performs before
 * any C call.
 *
 * What this file deliberately does NOT cover: a real `Filter` compiled and
 * evaluated through `chs_rows` against a live artifact. No revision-5
 * artifact exists yet (issue #54's own blocker), so that path is wired
 * (`Schema#rows`'s `options.rowFilter` branch and `NativeLibrary#rows`'s
 * `filterHandle` argument) but not run here.
 */

import { describe, expect, it } from 'vitest';
import { ChtypesError } from '../src/errors.js';
import type { FilterHandle, NativeLibrary, SchemaHandle } from '../src/ffi.js';
import { parseDocument } from '../src/json.js';
import { batchResultOf, isAnswer, type Verdict } from '../src/results.js';
import { Filter, Schema } from '../src/schema.js';

// A stand-in for NativeLibrary: just enough surface (`columns`) for
// `Schema`'s constructor, with no real dlopen behind it.
function fakeNative(): NativeLibrary {
  return { columns: () => [] } as unknown as NativeLibrary;
}

describe('rows(..., { rowFilter }) verdict decoding', () => {
  it('decodes all four verdict characters plus rows_passed/rows_cut from a hand-built document', () => {
    // Four rows exercising all four verdict characters, one of them ('d')
    // on a row whose own parse outcome is not accepted, plus rows_passed/
    // rows_cut at the batch level. This is the acceptance-bar test for
    // property (3) — bytes only for 't', e/d never collapsed into f — at
    // the decoding layer.
    const raw = `{
      "outcome":"accepted","code":0,"err":"","rows_read":4,"rows_skipped":0,
      "rows_passed":1,"rows_cut":3,
      "rows":[
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"t"},
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"f"},
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"e","verdict_code":386,"verdict_err":"no common type"},
        {"outcome":"skipped","code":117,"err":"bad row","cols":[],"verdict":"d","verdict_code":117,"verdict_err":"bad row"}
      ],
      "row_spans":[{"off":0,"len":5},{"off":0,"len":0},{"off":0,"len":0},{"off":0,"len":0}]
    }`;
    const res = batchResultOf(parseDocument(Buffer.from(raw, 'utf8')));

    expect(res.rowsPassed).toBe(1);
    expect(res.rowsCut).toBe(3);
    expect(res.rowsPassed + res.rowsCut).toBe(4);

    const want: Verdict[] = ['t', 'f', 'e', 'd'];
    expect(res.rows.map((r) => r.verdict)).toEqual(want);

    // Property (3), directly: neither non-answer decoded as 'f'.
    expect(res.rows[2]!.verdict).not.toBe('f');
    expect(res.rows[3]!.verdict).not.toBe('f');
    expect(isAnswer(res.rows[2]!.verdict!)).toBe(false);
    expect(isAnswer(res.rows[3]!.verdict!)).toBe(false);

    // Row 3's own outcome is not accepted, and it still carries
    // verdict_code / verdict_err beside verdict, per the contract.
    expect(res.rows[3]!.outcome).toBe('skipped');
    expect(res.rows[3]!.verdictCode).toBe(117);
    expect(res.rows[3]!.verdictErr).toBe('bad row');
    expect(res.rows[2]!.verdictCode).toBe(386);
    expect(res.rows[2]!.verdictErr).toBe('no common type');
  });

  it('leaves verdict undefined and rowsPassed/rowsCut at 0 for an ordinary document', () => {
    const raw = `{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_skipped":0,
      "rows":[{"outcome":"accepted","code":0,"err":"","cols":[]}]}`;
    const res = batchResultOf(parseDocument(Buffer.from(raw, 'utf8')));
    expect(res.rowsPassed).toBe(0);
    expect(res.rowsCut).toBe(0);
    expect(res.rows[0]!.verdict).toBeUndefined();
  });
});

describe('Schema#rows cross-library filter refusal', () => {
  it('throws before any C call when the filter comes from a different loaded library', () => {
    // Property (8)'s TypeScript-level half: a filter from a DIFFERENT
    // loaded library is refused before any C call — no handle crosses a
    // dlopen'd image boundary, the same rule Filter#eval enforces for a
    // (filter, block) pair. Needs no dlopen'd artifact: the refusal fires
    // on the NativeLibrary identity check, before `this.live()` or any
    // handle is touched.
    //
    // The SAME-library-different-schema half of (8) (code 1002) is the
    // server's own answer and cannot be exercised without a loaded
    // artifact; it is wired (Schema#rows passes the filter handle straight
    // through when the libraries match) but not run here.
    const schema = new Schema(fakeNative(), {} as unknown as SchemaHandle, 'x UInt8');
    const otherNative = fakeNative();
    const otherSchema = new Schema(otherNative, {} as unknown as SchemaHandle, 'x UInt8');
    const filter = new Filter(otherNative, otherSchema, {} as unknown as FilterHandle, '1');

    expect(() => schema.rows(0, new Uint8Array(), undefined, { rowFilter: filter })).toThrow(ChtypesError);
  });
});
