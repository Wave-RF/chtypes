/**
 * Resolve's pure checks (`docs/guides/fetch-v1.md` §3): the version spelling
 * regex and its refuse-hint, and descriptor integrity (size + sha256)
 * independent of any network transport.
 */

import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { checkSpelling, verifyDescriptor, verifyDigestAndSize } from '../../src/ocifetch/oci.js';
import { ArtifactCorruptError } from '../../src/ocifetch/errors.js';

function digestOf(bytes: Buffer): string {
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
}

describe('checkSpelling', () => {
  it.each(['26.8', '26.8.15', '26.8.15.10'])('accepts %s', (spelling) => {
    expect(() => checkSpelling(spelling)).not.toThrow();
  });

  it.each(['v26.8', '26.8.15.10-lts', '26.8.15.10-stable'])('refuses %s with a hint', (spelling) => {
    expect(() => checkSpelling(spelling)).toThrow(/v1 version spelling/);
  });

  it('leaves a non-numeric spelling to the tag lookup (UNPUBLISHED), like any arbitrary OCI tag', () => {
    expect(() => checkSpelling('26.x')).not.toThrow();
    expect(() => checkSpelling('t-untrusted-key')).not.toThrow();
  });
});

describe('verifyDescriptor', () => {
  it('accepts bytes matching their own descriptor', () => {
    const bytes = Buffer.from('hello world');
    const descriptor = { mediaType: 'application/octet-stream', digest: digestOf(bytes), size: bytes.length };
    expect(() => verifyDescriptor(descriptor, bytes, 'test blob')).not.toThrow();
  });

  it('refuses a size mismatch (CORRUPT)', () => {
    const bytes = Buffer.from('hello world');
    const descriptor = { mediaType: 'application/octet-stream', digest: digestOf(bytes), size: bytes.length + 1 };
    expect(() => verifyDescriptor(descriptor, bytes, 'test blob')).toThrow(ArtifactCorruptError);
  });

  it('refuses a digest mismatch (CORRUPT) even when size matches', () => {
    const bytes = Buffer.from('hello world');
    const tampered = Buffer.from('HELLO WORLD'); // same length, different bytes
    const descriptor = { mediaType: 'application/octet-stream', digest: digestOf(bytes), size: bytes.length };
    expect(() => verifyDescriptor(descriptor, tampered, 'test blob')).toThrow(ArtifactCorruptError);
  });

  it('skips the size check when the descriptor carries none (a lock pin)', () => {
    const bytes = Buffer.from('hello world');
    const descriptor = { mediaType: 'application/octet-stream', digest: digestOf(bytes) };
    expect(() => verifyDescriptor(descriptor, bytes, 'test blob')).not.toThrow();
  });
});

describe('verifyDigestAndSize', () => {
  it('is the byte-free equivalent of verifyDescriptor, for a streamed hash', () => {
    const bytes = Buffer.from('hello world');
    const descriptor = { mediaType: 'application/octet-stream', digest: digestOf(bytes), size: bytes.length };
    const actualHash = createHash('sha256').update(bytes).digest('hex');
    expect(() => verifyDigestAndSize(descriptor, actualHash, bytes.length, 'layer')).not.toThrow();
    expect(() => verifyDigestAndSize(descriptor, actualHash, bytes.length + 1, 'layer')).toThrow(ArtifactCorruptError);
  });
});
