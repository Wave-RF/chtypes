/**
 * The cache (`docs/guides/fetch-v1.md` §1): a standard OCI image layout
 * rooted at `CHTYPES_CACHE` or `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/`,
 * `unpacked/sha256/<manifest-hex>/` beside it, each carrying its own
 * `verified.json` — the durable, **immutable once written** proof a
 * directory was verified, which `resolve_installed`/`--offline` trust
 * instead of `index.json` (guide §6). Read-only system directories are
 * searched after the cache, never written to.
 *
 * `index.json` is written by temp-file-then-atomic-rename and **never
 * edited in place**; a concurrent writer's entry must survive (the
 * `index-race-reapply` case), so every write re-reads the file immediately
 * before renaming and, if it changed since this write started, discards its
 * temp file and retries from the fresh content — an optimistic-concurrency
 * loop, not a lock, because nothing here may block on another process.
 */

import { randomBytes } from 'node:crypto';
import { mkdir, readdir, readFile, rename, rm, unlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { CACHE_UNPACKED_DIR, CACHE_VERIFIED_RECORD, ENV_CACHE_NAME, SYSTEM_CACHE_DIRS } from './constants.gen.js';
import type { ArtifactPredicate } from './types.js';

/** `CHTYPES_CACHE`, else `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1` — the layout root itself, not a parent of it. */
export function cacheRoot(explicitCacheDir?: string): string {
  if (explicitCacheDir !== undefined && explicitCacheDir !== '') return path.resolve(explicitCacheDir);
  const env = process.env[ENV_CACHE_NAME];
  if (env !== undefined && env !== '') return path.resolve(env);
  const xdg = process.env['XDG_CACHE_HOME'];
  const base = xdg !== undefined && xdg !== '' ? xdg : path.join(os.homedir(), '.cache');
  return path.join(base, 'chtypes', 'v1');
}

export function systemDirs(override?: readonly string[]): readonly string[] {
  return override ?? SYSTEM_CACHE_DIRS;
}

function blobsDir(root: string): string {
  return path.join(root, 'blobs', 'sha256');
}

export function unpackedDir(root: string, manifestDigestHex: string): string {
  return path.join(root, CACHE_UNPACKED_DIR, manifestDigestHex);
}

function indexJsonPath(root: string): string {
  return path.join(root, 'index.json');
}

async function tempPathNear(target: string): Promise<string> {
  const rand = randomBytes(6).toString('hex');
  return `${target}.tmp-${process.pid}-${rand}`;
}

/** Creates the layout root (and `oci-layout`) if it does not exist yet. Safe to call every time — idempotent. */
export async function ensureLayout(root: string): Promise<void> {
  await mkdir(root, { recursive: true });
  await mkdir(blobsDir(root), { recursive: true });
  await mkdir(path.join(root, CACHE_UNPACKED_DIR), { recursive: true });
  const marker = path.join(root, 'oci-layout');
  try {
    await readFile(marker);
  } catch {
    const tmp = await tempPathNear(marker);
    await writeFile(tmp, `${JSON.stringify({ imageLayoutVersion: '1.0.0' })}\n`);
    await rename(tmp, marker);
  }
}

/** Installs one verified blob into `blobs/sha256/<hex>` by temp-then-rename. A no-op if it is already there (content-addressed). */
export async function installBlob(root: string, digestHex: string, bytes: Buffer): Promise<string> {
  const dest = path.join(blobsDir(root), digestHex);
  try {
    await readFile(dest);
    return dest;
  } catch {
    // Not present yet — fall through and install it.
  }
  const tmp = await tempPathNear(dest);
  await writeFile(tmp, bytes);
  try {
    await rename(tmp, dest);
  } catch (err) {
    await unlink(tmp).catch(() => {});
    throw err;
  }
  return dest;
}

// ------------------------------------------------------------- index.json

interface IndexDescriptor {
  readonly mediaType: string;
  readonly digest: string;
  readonly size: number;
  readonly annotations?: Readonly<Record<string, string>>;
}

interface IndexJson {
  readonly schemaVersion: 2;
  readonly manifests: readonly IndexDescriptor[];
}

const EMPTY_INDEX: IndexJson = { schemaVersion: 2, manifests: [] };

async function readRawIndex(root: string): Promise<string | undefined> {
  try {
    return await readFile(indexJsonPath(root), 'utf8');
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw err;
  }
}

/**
 * `index.json` is our own durable bookkeeping (plus whatever `oras copy
 * --to-oci-layout` pre-seeded) — parsed with plain `JSON.parse` here rather
 * than `../json.ts`'s duplicate-key-aware reader, which is reserved for
 * untrusted, attacker-reachable bytes (DSSE statements, referrer listings).
 * This file is local and either ours or a trusted tool's.
 */
function parseIndex(raw: string | undefined): IndexJson {
  if (raw === undefined) return EMPTY_INDEX;
  try {
    const parsed = JSON.parse(raw) as Partial<IndexJson>;
    if (!Array.isArray(parsed.manifests)) return EMPTY_INDEX;
    return { schemaVersion: 2, manifests: parsed.manifests as IndexDescriptor[] };
  } catch {
    return EMPTY_INDEX;
  }
}

/**
 * Upserts one descriptor (by digest) into `index.json`, surviving a
 * concurrent writer: re-reads the file immediately before the atomic rename
 * and, if it changed since this call started, re-merges onto the fresh
 * content and tries again (bounded — a layout should never see sustained
 * contention). `beforeRename` is a test-only hook (`index-race-reapply`)
 * that runs after the temp file is written and before the race check.
 */
export async function upsertIndexEntry(
  root: string,
  descriptor: IndexDescriptor,
  beforeRename?: () => Promise<void> | void,
): Promise<void> {
  const indexPath = indexJsonPath(root);
  for (let attempt = 0; attempt < 8; attempt++) {
    const before = await readRawIndex(root);
    const current = parseIndex(before);
    const withoutExisting = current.manifests.filter((m) => m.digest !== descriptor.digest);
    const merged: IndexJson = { schemaVersion: 2, manifests: [...withoutExisting, descriptor] };
    const tmp = await tempPathNear(indexPath);
    await writeFile(tmp, JSON.stringify(merged));
    if (beforeRename) await beforeRename();
    const after = await readRawIndex(root);
    if (after !== before) {
      // Lost the race: someone else wrote in between. Discard this attempt
      // and re-merge onto what is actually there now.
      await unlink(tmp).catch(() => {});
      continue;
    }
    await rename(tmp, indexPath);
    return;
  }
  throw new Error(`chtypes: index.json at ${indexPath} would not settle after 8 attempts (sustained concurrent writers)`);
}

/** Every descriptor `index.json` currently lists, `[]` if the file is absent or unreadable. */
export async function readIndexEntries(root: string): Promise<readonly IndexDescriptor[]> {
  return parseIndex(await readRawIndex(root)).manifests;
}

// ------------------------------------------------------------- verified.json

export interface VerifiedRecord {
  readonly schema: 1;
  readonly manifestDigest: string;
  readonly layerDigest: string;
  readonly bundleDigest: string;
  readonly signedBy: string;
  readonly predicate: ArtifactPredicate;
  /** The library's path relative to the unpacked directory (matches `predicate.library`). */
  readonly library: string;
}

/**
 * Writes the directory's own durable proof of verification — **the source
 * of truth for `--offline` and `resolve_installed`** (guide §6), never
 * `index.json`. Written once, by temp-then-rename, immediately after a
 * successful unpack; a directory with no `verified.json` is treated as
 * unverified regardless of what `index.json` claims about it.
 */
export async function writeVerifiedRecord(dir: string, record: VerifiedRecord): Promise<void> {
  const dest = path.join(dir, CACHE_VERIFIED_RECORD);
  const tmp = await tempPathNear(dest);
  await writeFile(tmp, JSON.stringify(record));
  await rename(tmp, dest);
}

/** `undefined` when the directory has no (or an unreadable) `verified.json` — never partially trusted. */
export async function readVerifiedRecord(dir: string): Promise<VerifiedRecord | undefined> {
  try {
    const raw = await readFile(path.join(dir, CACHE_VERIFIED_RECORD), 'utf8');
    const parsed = JSON.parse(raw) as VerifiedRecord;
    if (parsed.schema !== 1 || typeof parsed.manifestDigest !== 'string') return undefined;
    return parsed;
  } catch {
    return undefined;
  }
}

/** Every `unpacked/sha256/*` directory that carries a `verified.json` — `list_installed`'s and monotonicity's source. */
export async function listVerified(root: string): Promise<readonly { readonly dir: string; readonly record: VerifiedRecord }[]> {
  const base = path.join(root, CACHE_UNPACKED_DIR);
  let names: string[];
  try {
    names = await readdir(base);
  } catch {
    return [];
  }
  const out: { dir: string; record: VerifiedRecord }[] = [];
  for (const name of names) {
    const dir = path.join(base, name);
    const record = await readVerifiedRecord(dir);
    if (record !== undefined) out.push({ dir, record });
  }
  return out;
}

/** A fresh, empty staging directory beside `unpacked/`, for temp-then-rename of a whole unpacked tree. */
export async function freshStagingDir(root: string): Promise<string> {
  const base = path.join(root, CACHE_UNPACKED_DIR);
  await mkdir(base, { recursive: true });
  const dir = path.join(base, `.staging-${process.pid}-${randomBytes(6).toString('hex')}`);
  await mkdir(dir, { recursive: true });
  return dir;
}

/** Renames a staged, fully-verified unpack into its final content-addressed home; removes the staging dir on any failure. */
export async function commitStaging(stagingDir: string, finalDir: string): Promise<void> {
  try {
    await rm(finalDir, { recursive: true, force: true });
    await rename(stagingDir, finalDir);
  } catch (err) {
    await rm(stagingDir, { recursive: true, force: true }).catch(() => {});
    throw err;
  }
}

export async function removeStaging(stagingDir: string): Promise<void> {
  await rm(stagingDir, { recursive: true, force: true }).catch(() => {});
}
