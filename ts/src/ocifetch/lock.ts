/**
 * Lock schema 3 (`docs/guides/fetch-v1.md` §6, `spec/fetch-v1/schema/lock3.schema.json`):
 * per declared platform, the request, the exact version and build, and the
 * platform manifest/layer/bundle digests — never a hostname. Schema 1 and 2
 * (v0) are refused outright, never silently reinterpreted, exactly as v0
 * refuses a lock newer than it understands.
 */

import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { LOCK_DEFAULT_FILE, LOCK_SCHEMA } from './constants.gen.js';
import { ArtifactPinnedError } from './errors.js';
import { isPlatformKey, type PlatformKey } from './types.js';

export interface LockPin {
  readonly version: string;
  readonly build: string;
  readonly manifest: string;
  readonly layer: string;
  readonly bundle: string;
  readonly index?: string;
}

export interface LockFile {
  readonly schema: 3;
  readonly abi: 1;
  readonly platforms: readonly PlatformKey[];
  /** Keyed by the originally-requested spelling, then by platform key. */
  readonly requests: Readonly<Record<string, Readonly<Record<string, LockPin>>>>;
}

const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

export function emptyLock(): LockFile {
  return { schema: LOCK_SCHEMA, abi: 1, platforms: [], requests: {} };
}

export function defaultLockPath(cwd: string = process.cwd()): string {
  return path.join(cwd, LOCK_DEFAULT_FILE);
}

function pinName(digest: string, field: string): void {
  if (!DIGEST_RE.test(digest)) {
    throw new ArtifactPinnedError(`chtypes: lock pin's ${field} ${JSON.stringify(digest)} is not a sha256 digest`);
  }
}

/**
 * Validates and narrows a parsed JSON value into a `LockFile`. Any v0 lock
 * (`schema: 1` or `schema: 2` in v0's own numbering) and any other
 * malformed shape is `ArtifactPinnedError`, naming re-lock — `--frozen`
 * reads this the same way a missing pin does (guide §6).
 */
export function validateLock(parsed: unknown, path_: string): LockFile {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new ArtifactPinnedError(`chtypes: ${path_} is not a lock file object; re-lock with \`fetch --lock\``);
  }
  const doc = parsed as Record<string, unknown>;
  const schema = doc['schema'];
  if (schema !== LOCK_SCHEMA) {
    throw new ArtifactPinnedError(
      `chtypes: ${path_} is schema ${JSON.stringify(schema)}, not v1's schema ${LOCK_SCHEMA}` +
        (schema === 1 || schema === 2 ? ' (a v0 lock is never reinterpreted)' : '') +
        '; re-lock with `fetch --lock`',
    );
  }
  if (doc['abi'] !== 1) {
    throw new ArtifactPinnedError(`chtypes: ${path_}'s abi is ${JSON.stringify(doc['abi'])}, not 1; re-lock with \`fetch --lock\``);
  }
  const platformsRaw = doc['platforms'];
  if (!Array.isArray(platformsRaw) || platformsRaw.length === 0) {
    throw new ArtifactPinnedError(`chtypes: ${path_} names no platforms; re-lock with \`fetch --lock\``);
  }
  const platforms: PlatformKey[] = [];
  for (const p of platformsRaw) {
    if (typeof p !== 'string' || !isPlatformKey(p)) {
      throw new ArtifactPinnedError(`chtypes: ${path_} names an unknown platform ${JSON.stringify(p)}`);
    }
    platforms.push(p);
  }
  const requestsRaw = doc['requests'];
  if (requestsRaw === null || typeof requestsRaw !== 'object' || Array.isArray(requestsRaw)) {
    throw new ArtifactPinnedError(`chtypes: ${path_} has no readable requests table`);
  }
  const requests: Record<string, Record<string, LockPin>> = {};
  for (const [spelling, byPlatform] of Object.entries(requestsRaw as Record<string, unknown>)) {
    if (byPlatform === null || typeof byPlatform !== 'object' || Array.isArray(byPlatform)) {
      throw new ArtifactPinnedError(`chtypes: ${path_}'s request ${JSON.stringify(spelling)} is malformed`);
    }
    const row: Record<string, LockPin> = {};
    for (const [platform, pinRaw] of Object.entries(byPlatform as Record<string, unknown>)) {
      if (!isPlatformKey(platform) || !platforms.includes(platform)) {
        throw new ArtifactPinnedError(`chtypes: ${path_}'s request ${JSON.stringify(spelling)} names platform ${JSON.stringify(platform)}, not in its own platforms list`);
      }
      if (pinRaw === null || typeof pinRaw !== 'object' || Array.isArray(pinRaw)) {
        throw new ArtifactPinnedError(`chtypes: ${path_}'s pin for ${spelling}/${platform} is malformed`);
      }
      const p = pinRaw as Record<string, unknown>;
      const version = p['version'];
      const build = p['build'];
      const manifest = p['manifest'];
      const layer = p['layer'];
      const bundle = p['bundle'];
      const index = p['index'];
      if (typeof version !== 'string' || typeof build !== 'string') {
        throw new ArtifactPinnedError(`chtypes: ${path_}'s pin for ${spelling}/${platform} is missing version or build`);
      }
      if (typeof manifest !== 'string' || typeof layer !== 'string' || typeof bundle !== 'string') {
        throw new ArtifactPinnedError(`chtypes: ${path_}'s pin for ${spelling}/${platform} is missing a digest`);
      }
      pinName(manifest, 'manifest');
      pinName(layer, 'layer');
      pinName(bundle, 'bundle');
      if (index !== undefined) {
        if (typeof index !== 'string') throw new ArtifactPinnedError(`chtypes: ${path_}'s pin for ${spelling}/${platform} has a non-string index`);
        pinName(index, 'index');
      }
      row[platform] = { version, build, manifest, layer, bundle, ...(index !== undefined ? { index } : {}) };
    }
    requests[spelling] = row;
  }
  return { schema: LOCK_SCHEMA, abi: 1, platforms, requests };
}

/** `undefined` when no lock file exists at `lockPath`. */
export async function readLock(lockPath: string): Promise<LockFile | undefined> {
  let raw: string;
  try {
    raw = await readFile(lockPath, 'utf8');
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw err;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new ArtifactPinnedError(`chtypes: ${lockPath} is not valid JSON: ${(err as Error).message}`);
  }
  return validateLock(parsed, lockPath);
}

export async function writeLock(lockPath: string, lock: LockFile): Promise<void> {
  const serializable = { schema: LOCK_SCHEMA, abi: 1, platforms: lock.platforms, requests: lock.requests };
  await mkdir(path.dirname(lockPath), { recursive: true });
  const tmp = `${lockPath}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
  await writeFile(tmp, `${JSON.stringify(serializable, null, 2)}\n`);
  await rename(tmp, lockPath);
}

export function getPin(lock: LockFile, spelling: string, platform: PlatformKey): LockPin | undefined {
  return lock.requests[spelling]?.[platform];
}

/** Returns a new lock with `pin` set for `spelling`/`platform`, adding `platform` to the declared list if new. */
export function withPin(lock: LockFile, spelling: string, platform: PlatformKey, pin: LockPin): LockFile {
  const platforms = lock.platforms.includes(platform) ? lock.platforms : [...lock.platforms, platform];
  const row = { ...(lock.requests[spelling] ?? {}), [platform]: pin };
  return { ...lock, platforms, requests: { ...lock.requests, [spelling]: row } };
}
