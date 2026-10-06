/**
 * Goldens revision selection's pure half (`src/ocifetch/goldens.ts`, guide §9):
 * reading the integer `revision` from a statement's signed bytes, and choosing
 * among verified candidates. The registry-facing half is the conformance
 * suite's `goldens-revision-*` cases.
 */

import { describe, expect, it } from 'vitest';
import { ArtifactCorruptError } from '../../src/ocifetch/errors.js';
import { type GoldensCandidate, selectGoldens, statementRevision } from '../../src/ocifetch/goldens.js';

function stmt(revisionJson: string | undefined): Buffer {
  const body = revisionJson === undefined ? '"schema":1' : `"revision":${revisionJson}`;
  return Buffer.from(`{"predicate":{${body}}}`);
}

describe('statementRevision', () => {
  it.each([
    ['0', 0],
    ['1', 1],
    ['9007199254740991', 9007199254740991],
  ])('accepts the integer %s', (literal, want) => {
    expect(statementRevision(stmt(literal))).toBe(want);
  });

  it.each([
    undefined,
    'true',
    'false',
    '"2"',
    'null',
    '1.0',
    '1e0',
    '-1',
    '-0',
    '{}',
    '[]',
    '9007199254740992',
  ])('refuses %s as CORRUPT', (literal) => {
    expect(() => statementRevision(stmt(literal))).toThrow(ArtifactCorruptError);
  });
});

function cand(manifest: string, blob: string, revision: number, bundle = 'b'): GoldensCandidate {
  return { manifest, blob, revision, bytes: Buffer.alloc(0), predicate: {}, signedBy: 'k', bundle };
}

describe('selectGoldens', () => {
  it('picks the highest revision wherever it is listed', () => {
    const chosen = selectGoldens([cand('m1', 'a', 1), cand('m2', 'b', 2), cand('m3', 'a', 0)]);
    expect([chosen.revision, chosen.blob]).toEqual([2, 'b']);
  });

  it('refuses a tie of different documents, naming both digests and the revision', () => {
    expect(() => selectGoldens([cand('m1', 'a', 3), cand('m2', 'b', 3)])).toThrow(/revision 3.*a.*b/);
    expect(() => selectGoldens([cand('m1', 'a', 3), cand('m2', 'b', 3)])).toThrow(ArtifactCorruptError);
  });

  it('treats the same document under the same revision as one document', () => {
    const chosen = selectGoldens([cand('m1', 'a', 2, 'b1'), cand('m1', 'a', 2, 'b2'), cand('m2', 'b', 1)]);
    expect(chosen.bundle).toBe('b1');
  });

  it('ignores a tie below the highest revision', () => {
    expect(selectGoldens([cand('m1', 'a', 1), cand('m2', 'b', 1), cand('m3', 'a', 4)]).revision).toBe(4);
  });
});
