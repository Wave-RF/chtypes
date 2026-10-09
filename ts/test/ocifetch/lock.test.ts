/**
 * Lock schema 3 (`docs/guides/fetch-v1.md` §6): a v0 lock (schema 1 or 2) is
 * refused outright, never reinterpreted, and a schema-3 lock round-trips
 * through `validateLock`/`writeLock`/`readLock` unchanged.
 */

import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { ArtifactPinnedError } from '../../src/ocifetch/errors.js';
import { emptyLock, getPin, readLock, validateLock, withPin, writeLock } from '../../src/ocifetch/lock.js';
import { useFetchV1ForTests } from '../../src/ocifetch/channel.js';

// This file tests the v1 lock contract (docs/guides/fetch-v1.md §6): its locks
// carry abi 1. A lock's abi is the active channel's generation, so it selects
// the v1 channel; the production-v2 locks run through the conformance cases.
useFetchV1ForTests();

const GOOD_PIN = {
  version: '26.8.15.10',
  build: '20261001.183455',
  manifest: `sha256:${'a'.repeat(64)}`,
  layer: `sha256:${'b'.repeat(64)}`,
  bundle: `sha256:${'c'.repeat(64)}`,
};

describe('validateLock', () => {
  it('accepts a well-formed schema-3 lock', () => {
    const lock = validateLock(
      { schema: 3, abi: 1, platforms: ['linux-arm64'], requests: { '26.8': { 'linux-arm64': GOOD_PIN } } },
      'chtypes.lock',
    );
    expect(getPin(lock, '26.8', 'linux-arm64')).toEqual(GOOD_PIN);
  });

  it('refuses a v0 schema-1 lock, never reinterpreting it', () => {
    expect(() => validateLock({ schema: 1, artifacts: {} }, 'chtypes.lock')).toThrow(ArtifactPinnedError);
  });

  it('refuses a v0 schema-2 lock', () => {
    expect(() => validateLock({ schema: 2, artifacts: {} }, 'chtypes.lock')).toThrow(ArtifactPinnedError);
  });

  it('refuses an abi other than 1', () => {
    expect(() =>
      validateLock({ schema: 3, abi: 2, platforms: ['linux-arm64'], requests: {} }, 'chtypes.lock'),
    ).toThrow(ArtifactPinnedError);
  });

  it('refuses an unknown platform key', () => {
    expect(() =>
      validateLock({ schema: 3, abi: 1, platforms: ['darwin-amd64'], requests: {} }, 'chtypes.lock'),
    ).toThrow(ArtifactPinnedError);
  });

  it('refuses a pin whose digest is not a sha256 digest', () => {
    expect(() =>
      validateLock(
        {
          schema: 3,
          abi: 1,
          platforms: ['linux-arm64'],
          requests: { '26.8': { 'linux-arm64': { ...GOOD_PIN, manifest: 'not-a-digest' } } },
        },
        'chtypes.lock',
      ),
    ).toThrow(ArtifactPinnedError);
  });

  it('refuses a request naming a platform not in its own platforms list', () => {
    expect(() =>
      validateLock(
        { schema: 3, abi: 1, platforms: ['linux-arm64'], requests: { '26.8': { 'linux-amd64': GOOD_PIN } } },
        'chtypes.lock',
      ),
    ).toThrow(ArtifactPinnedError);
  });

  it('refuses a non-object', () => {
    expect(() => validateLock('not an object', 'chtypes.lock')).toThrow(ArtifactPinnedError);
    expect(() => validateLock(null, 'chtypes.lock')).toThrow(ArtifactPinnedError);
  });
});

describe('withPin / getPin', () => {
  it('adds a platform to the declared list the first time it is pinned', () => {
    const lock = withPin(emptyLock(), '26.8', 'linux-arm64', GOOD_PIN);
    expect(lock.platforms).toEqual(['linux-arm64']);
    expect(getPin(lock, '26.8', 'linux-arm64')).toEqual(GOOD_PIN);
  });

  it('does not duplicate an already-declared platform', () => {
    const once = withPin(emptyLock(), '26.8', 'linux-arm64', GOOD_PIN);
    const twice = withPin(once, '26.8.15', 'linux-arm64', GOOD_PIN);
    expect(twice.platforms).toEqual(['linux-arm64']);
  });
});

describe('readLock / writeLock round-trip', () => {
  let dir: string | undefined;

  afterEach(async () => {
    if (dir !== undefined) await rm(dir, { recursive: true, force: true });
    dir = undefined;
  });

  it('round-trips a lock unchanged', async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'ocifetch-v1-lock-'));
    const lockPath = path.join(dir, 'chtypes.lock');
    const lock = withPin(emptyLock(), '26.8', 'linux-arm64', GOOD_PIN);
    await writeLock(lockPath, lock);
    const read = await readLock(lockPath);
    expect(read).toEqual(lock);
  });

  it('readLock returns undefined for a missing file', async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'ocifetch-v1-lock-'));
    const read = await readLock(path.join(dir, 'does-not-exist.lock'));
    expect(read).toBeUndefined();
  });
});
