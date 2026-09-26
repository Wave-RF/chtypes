/**
 * Issue #90: `rowResultOf`'s `values` excludes a column reported with
 * `Source.Skipped` (MATERIALIZED / ALIAS / EPHEMERAL, never read from an
 * input row) — the rule `Source.EphemeralInput`'s exclusion (#97, see
 * transform-ephemeral.test.ts) was modeled on. That exclusion is real and has been
 * correct all along; it simply had no test of its own, like the
 * ephemeral_input exclusion before #97's test. This test is expected to PASS
 * immediately — it is coverage for existing behavior, not a regression fix.
 */

import { describe, expect, it } from 'vitest';
import { parseDocument } from '../src/json.js';
import { rowResultOf, Source } from '../src/results.js';

const DOC_JSON = `{
  "outcome": "accepted",
  "cols": [
    {
      "name": "mat_col", "type": "UInt8", "base": "UInt8",
      "src": "skipped", "nullable": false
    },
    {
      "name": "in_col", "type": "UInt8", "base": "UInt8",
      "src": "input", "input": "5", "stored": 5, "nullable": false
    }
  ]
}`;

describe('skipped excluded from values, keeps input', () => {
  it('excludes skipped from values, keeps an ordinary input column', () => {
    expect(Source.Skipped).toBe('skipped');
    const res = rowResultOf(parseDocument(Buffer.from(DOC_JSON, 'utf8')));
    const columns = new Set(res.values.map((v) => v.column));
    expect(columns.has('mat_col')).toBe(false);
    expect(columns.has('in_col')).toBe(true);
  });
});
