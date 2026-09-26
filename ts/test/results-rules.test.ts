/**
 * Issue #122: five more results-group rules the four bindings agree on by
 * construction, declared nowhere in `tests/parity/manifest.json` until this
 * change.
 *
 * Every document below is parsed through the same production path
 * `rowResultOf`/`filterResultOf` already use, rather than constructing a
 * result object by hand.
 */

import { describe, expect, it } from 'vitest';
import { parseDocument } from '../src/json.js';
import { FilterOutcome, filterResultOf, Outcome, rowResultOf, Source, Verdict } from '../src/results.js';

function row(js: string) {
  return rowResultOf(parseDocument(Buffer.from(js, 'utf8')));
}
function filter(js: string) {
  return filterResultOf(parseDocument(Buffer.from(js, 'utf8')));
}

describe('results.unsupported-settings-forces-unsupported', () => {
  it.each([
    ['accepted', Outcome.Unsupported],
    ['rejected', Outcome.Rejected],
  ] as const)('outcome %s with unsupported_settings -> %s', (wireOutcome, want) => {
    const res = row(
      `{"outcome": "${wireOutcome}", "unsupported_settings": ["some_setting"], "cols": []}`,
    );
    expect(res.outcome, `unsupported_settings ${JSON.stringify(res.unsupportedSettings)}`).toBe(want);
  });
});

describe('results.unknown-outcome-degrades-to-unsupported', () => {
  it('RowResult.outcome degrades to Unsupported, never Rejected', () => {
    const res = row('{"outcome": "totally-unknown-future-outcome", "cols": []}');
    expect(res.outcome).toBe(Outcome.Unsupported);
  });
  it('FilterResult.outcome degrades to Unsupported, never Rejected', () => {
    const res = filter('{"outcome": "totally-unknown-future-outcome", "verdicts": "", "errors": []}');
    expect(res.outcome).toBe(FilterOutcome.Unsupported);
  });
});

describe('results.unknown-verdict-degrades-to-decline', () => {
  it('row-level verdict degrades to Decline, never an invented answer', () => {
    const res = row('{"outcome": "accepted", "cols": [], "verdict": "z"}');
    expect(res.verdict).toBe(Verdict.Decline);
  });
  it('filter verdicts string degrades to Decline, never an invented answer', () => {
    const res = filter('{"outcome": "ok", "verdicts": "z", "errors": []}');
    expect(res.verdicts).toEqual([Verdict.Decline]);
  });
});

describe('results.null-false-when-poisoned', () => {
  it.each([
    ['false', true],
    ['true', false],
  ] as const)('poison=%s -> isNull=%s', (poison, want) => {
    const res = row(
      `{"outcome": "accepted", "cols": [{"name": "c", "type": "UInt8", "base": "UInt8", ` +
        `"src": "input", "input": "", "stored": null, "poison": ${poison}, "nullable": true}]}`,
    );
    expect(res.values).toHaveLength(1);
    expect(res.values[0]?.isNull, `poison=${poison}`).toBe(want);
  });
});

describe('results.default-substituted-populates-substituted', () => {
  it('default_substituted populates substituted, and only that src does', () => {
    expect(Source.DefaultSubstituted).toBe('default_substituted');
    const res = row(`{
      "outcome": "accepted",
      "cols": [
        {
          "name": "ts", "type": "DateTime", "base": "DateTime",
          "src": "default_substituted", "input": "now()",
          "stored": "2026-09-21 00:00:00", "nullable": false
        },
        {
          "name": "in_col", "type": "UInt8", "base": "UInt8",
          "src": "input", "input": "5", "stored": 5, "nullable": false
        }
      ]
    }`);
    expect(res.substituted).toHaveLength(1);
    expect(res.substituted[0]?.column).toBe('ts');
    expect(res.substituted[0]?.expr).toBe('now()');
    expect(res.substituted[0]?.text).toBe('"2026-09-21 00:00:00"');
  });
});

describe('results.non-ok-filter-result-forces-empty', () => {
  it('a non-OK FilterResult forces verdicts and errors empty', () => {
    const res = filter(
      '{"outcome": "rejected", "code": 115, "err": "unknown setting", ' +
        '"verdicts": "tfed", "errors": [{"row": 0, "code": 27, "err": "boom"}]}',
    );
    expect(res.outcome).toBe(FilterOutcome.Rejected);
    expect(res.verdicts).toEqual([]);
    expect(res.errors).toEqual([]);
  });
});
