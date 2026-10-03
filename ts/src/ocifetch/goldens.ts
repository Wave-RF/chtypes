/**
 * Goldens revision selection (`docs/guides/fetch-v1.md` §9) — the pure half.
 *
 * The registry is append-only, so a corrected goldens set for the same build
 * is published as a SECOND goldens referrer of the same platform manifest;
 * the first can never be deleted. The artifact producer therefore puts an
 * integer `revision` in the goldens predicate and never reuses one for a
 * different document. `fetchSigned`, called with the goldens predicate type
 * and a platform manifest digest, verifies every goldens referrer and returns
 * the one with the highest revision (`ensure.ts`); this module reads the
 * revision and chooses, with no registry in sight, so it is unit-testable.
 */

import { field, type Json, parseJsonValue } from '../json.js';
import { ArtifactCorruptError } from './errors.js';

/** The largest revision every binding accepts: `Number.MAX_SAFE_INTEGER` (2^53 - 1). */
export const MAX_GOLDENS_REVISION = Number.MAX_SAFE_INTEGER;

const CANONICAL_INTEGER = /^(0|[1-9][0-9]*)$/;

/** One goldens referrer whose own signature verified. */
export interface GoldensCandidate {
  /** The goldens referrer manifest's digest. */
  readonly manifest: string;
  /** Its layer digest: the signed goldens document. */
  readonly blob: string;
  readonly revision: number;
  readonly bytes: Buffer;
  readonly predicate: unknown;
  readonly signedBy: string;
  /** The bundle blob's digest. */
  readonly bundle: string;
}

/**
 * The integer `revision` of a verified statement's predicate, read from the
 * signed bytes (`JSON.parse` would make `1.0` and `1` the same number). It
 * must be a JSON integer written without a fraction, an exponent, a sign or a
 * leading zero: a missing key, a boolean, a string, null, a float such as 1.0
 * and anything above `MAX_GOLDENS_REVISION` throw `ArtifactCorruptError`.
 */
export function statementRevision(payload: Buffer): number {
  const root: Json | null = parseJsonValue(payload);
  const revision = field(field(root ?? undefined, 'predicate'), 'revision');
  if (revision === undefined) throw new ArtifactCorruptError('chtypes: a goldens predicate carries no "revision"');
  const text = revision.raw.toString('utf8').trim();
  if (revision.kind !== 'number' || !CANONICAL_INTEGER.test(text)) {
    throw new ArtifactCorruptError(`chtypes: a goldens predicate "revision" is ${text}, not a non-negative JSON integer`);
  }
  const n = Number(text);
  if (!Number.isSafeInteger(n)) {
    throw new ArtifactCorruptError(`chtypes: a goldens predicate "revision" ${text} is larger than ${MAX_GOLDENS_REVISION}`);
  }
  return n;
}

/**
 * The verified candidate with the highest revision. Two or more at that
 * revision with DIFFERENT blob digests are a tie, refused as
 * `ArtifactCorruptError` naming both digests and the revision; the same blob
 * digest under the same revision (two bundles, say) is one document, not a
 * tie. The earliest-listed candidate of the winning document is returned.
 */
export function selectGoldens(candidates: readonly GoldensCandidate[]): GoldensCandidate {
  let best = candidates[0];
  if (best === undefined) throw new Error('selectGoldens needs at least one candidate');
  for (const c of candidates) {
    if (c.revision > best.revision) best = c;
  }
  for (const c of candidates) {
    if (c.revision === best.revision && c.blob !== best.blob) {
      throw new ArtifactCorruptError(`chtypes: goldens revision ${best.revision} is carried by two different documents, ${best.blob} and ${c.blob}`);
    }
  }
  return best;
}
