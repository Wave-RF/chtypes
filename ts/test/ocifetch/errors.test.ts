/**
 * The error hierarchy (public issue #487): every fetch error is an
 * `ArtifactError` and every `ArtifactError` is a `ChtypesError`, so a caller
 * that catches `ChtypesError` to handle every SDK error catches the fetch
 * errors too. Codes and messages are unchanged.
 */

import { describe, expect, it } from 'vitest';
import { ArtifactError, ChtypesError, corruptRefusal } from '../../src/abi2/index.js';
import * as publicApi from '../../src/index.js';
import { ERROR_EXIT_CODES } from '../../src/ocifetch/constants.gen.js';
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
      expect(err).toBeInstanceOf(ArtifactError);
    }
  });
});

describe('the artifact error carries a loader refusal\'s fields, as in Go and Python (public issue #500)', () => {
  it('leaves them undefined on a fetch error, and sets them on a refusal and a cache fault', () => {
    const fetchErr = new ArtifactCorruptError('x');
    expect([fetchErr.reason, fetchErr.path, fetchErr.want, fetchErr.got]).toEqual([undefined, undefined, undefined, undefined]);
    const refusal = corruptRefusal({ reason: 'build_info_malformed', path: '/p', got: 'not ASCII' });
    expect(refusal).toBeInstanceOf(ArtifactCorruptError);
    expect(refusal.name).toBe('ArtifactCorruptError');
    expect([refusal.reason, refusal.path, refusal.want, refusal.got]).toEqual(['build_info_malformed', '/p', undefined, 'not ASCII']);
    const fault = new CacheUnusableError('x', { path: '/c', reason: 'unwritable', osError: 'EACCES' });
    expect([fault.reason, fault.path, fault.osError]).toEqual(['unwritable', '/c', 'EACCES']);
  });

  it('exports one generated constant per code, and no exit status and no isChtypesError', () => {
    const codes = Object.entries(publicApi)
      .filter(([name]) => name.startsWith('CODE_'))
      .map(([, value]) => value)
      .sort();
    expect(codes).toEqual(Object.keys(ERROR_EXIT_CODES).sort());
    expect('exitStatus' in new ArtifactMissingError('x')).toBe(false);
    expect('isChtypesError' in publicApi).toBe(false);
    expect('LoaderCorruptError' in publicApi).toBe(false);
  });
});
