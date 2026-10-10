/**
 * The error hierarchy (public issue #487): every fetch error is an
 * `ArtifactError` and every `ArtifactError` is a `ChtypesError`, so a caller
 * that catches `ChtypesError` to handle every SDK error catches the fetch
 * errors too. Codes and messages are unchanged.
 */

import { describe, expect, it } from 'vitest';
import { ArtifactError, ChtypesError, isChtypesError } from '../../src/abi2/index.js';
import {
  ArtifactCorruptError,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  CacheUnusableError,
  SourceForbiddenError,
  SourceIncompatibleError,
  SourceRetiredError,
  SourceUnauthorizedError,
  SourceUnreachableError,
} from '../../src/ocifetch/index.js';

describe('the fetch errors are ChtypesErrors', () => {
  it('makes a missing artifact an ArtifactError and a ChtypesError, keeping its code and message', () => {
    const err = new ArtifactMissingError('x');
    expect(err).toBeInstanceOf(ArtifactError);
    expect(err).toBeInstanceOf(ChtypesError);
    expect(err).toBeInstanceOf(Error);
    expect(isChtypesError(err)).toBe(true);
    expect(err.code).toBe('CHTYPES_ARTIFACT_MISSING');
    expect(err.message).toBe('x');
    expect(err.name).toBe('ArtifactMissingError');
  });

  it('covers every class of the fetch family', () => {
    const errors = [
      new ArtifactCorruptError('x'),
      new ArtifactMissingError('x'),
      new ArtifactPinnedError('x'),
      new ArtifactUnpublishedError('x'),
      new ArtifactUntrustedError('x'),
      new SourceForbiddenError('x'),
      new SourceIncompatibleError('x'),
      new SourceRetiredError('x'),
      new SourceUnauthorizedError('x'),
      new SourceUnreachableError('x'),
      new CacheUnusableError('x', { path: '/p', reason: 'r', osError: undefined }),
    ];
    for (const err of errors) {
      expect(err).toBeInstanceOf(ChtypesError);
      expect(isChtypesError(err)).toBe(true);
    }
  });
});
