/**
 * Issue #97: `classify()`'s early-return list named `'skipped'`,
 * `'default_expr_unsupported'`, `'default_volatile_unresolved'` and
 * `'default_pending'` but not `Source.EphemeralInput`. A listed EPHEMERAL
 * column's value is read (it is in scope for other columns' DEFAULT
 * expressions) but never stored, so it fell through into the value-comparison
 * detectors and could be reported as a transform for a column that was never
 * stored. `Source.MaterializedInput` must NOT gain the same early return:
 * under `insert_allow_materialized_columns=1` that value IS stored and must
 * keep classifying.
 *
 * Both tests below parse a row document with this binding's own parser
 * (`parseDocument`, `columnDocOf`) rather than hand-constructing a
 * `ColumnDoc` or `Transform` object.
 */

import { describe, expect, it } from 'vitest';
import { field, items, parseDocument } from '../src/json.js';
import { columnDocOf, rowResultOf, type ColumnDoc } from '../src/results.js';
import { classify, Reason } from '../src/transform.js';

// eph_col: src 'ephemeral_input', no 'stored' key at all — an EPHEMERAL
// column is never written. mat_col: src 'materialized_input', 'stored': 0 —
// the supplied value DID land, overflowing. Both carry the identical
// UInt8-overflow reference shape (input 256, ref_type Int256, ref 256) so
// that the reference-type detector (not gated on `src`) would flag both if
// classify() did not stop the ephemeral one first.
const DOC_JSON = `{
  "outcome": "accepted",
  "cols": [
    {
      "name": "eph_col", "type": "UInt8", "base": "UInt8",
      "src": "ephemeral_input", "input": "256",
      "ref_type": "Int256", "ref": 256, "nullable": false
    },
    {
      "name": "mat_col", "type": "UInt8", "base": "UInt8",
      "src": "materialized_input", "input": "256", "stored": 0,
      "ref_type": "Int256", "ref": 256, "nullable": false
    }
  ]
}`;

function parsedCols(): Map<string, ColumnDoc> {
  const doc = parseDocument(Buffer.from(DOC_JSON, 'utf8'));
  const out = new Map<string, ColumnDoc>();
  for (const rawCol of items(field(doc, 'cols'))) {
    const c = columnDocOf(rawCol);
    out.set(c.name, c);
  }
  return out;
}

describe('#97: ephemeral_input vs materialized_input', () => {
  it('excludes ephemeral_input from values, keeps materialized_input', () => {
    // rowResultOf's own column loop already special-cases Source.EphemeralInput
    // the same way it special-cases 'skipped' (and that loop `continue`s
    // before it ever reaches its `classify(c)` call, so it cannot observe the
    // classify() bug below). This was already correct before #97's fix; it
    // simply had no test. Expect PASS both before and after the classify()
    // fix — this is not what that fix changes.
    const res = rowResultOf(parseDocument(Buffer.from(DOC_JSON, 'utf8')));
    const columns = new Set(res.values.map((v) => v.column));
    expect(columns.has('eph_col')).toBe(false);
    expect(columns.has('mat_col')).toBe(true);
  });

  it('classify() excludes ephemeral_input, still classifies materialized_input', () => {
    // rowResultOf's loop already `continue`s past Source.EphemeralInput
    // before it ever calls classify(), so driving this through rowResultOf
    // (as the test above does) would pass before the fix for the wrong
    // reason: it would never run the code under test. classify() must be
    // correct standing alone — it is the piece #53 deliberately left as "a
    // behavior judgement" per the issue, and nothing stops a future caller
    // (or a refactor of that loop) from invoking it on an ephemeral_input
    // column without the same guard.
    //
    // Before the fix, eph_col's early-return switch does not include
    // 'ephemeral_input', so classify() falls through to the reference-type
    // detector, sees stored ('') disagree with the reference-widened value
    // ('256'), and reports a phantom overflow_wrap transform for a column
    // that was never stored. This assertion FAILS before the fix, PASSES
    // after.
    //
    // mat_col carries the identical overflow shape but src
    // 'materialized_input', which must keep classifying either way — the
    // guard against folding the two early-return lists together (the
    // issue's explicit warning).
    const cols = parsedCols();

    const ephTransforms = classify(cols.get('eph_col')!);
    expect(
      ephTransforms,
      `classify() reported a transform for eph_col (src ephemeral_input), want none: ${JSON.stringify(ephTransforms)}`,
    ).toHaveLength(0);

    const matTransforms = classify(cols.get('mat_col')!);
    expect(matTransforms).toHaveLength(1);
    expect(matTransforms[0]?.reason).toBe(Reason.OverflowWrap);
    expect(matTransforms[0]?.column).toBe('mat_col');
  });
});
