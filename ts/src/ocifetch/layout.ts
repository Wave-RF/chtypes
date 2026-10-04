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
import { CACHE_UNPACKED_DIR, CACHE_VERIFIED_RECORD, ENV_CACHE_NAME, PLATFORMS, SYSTEM_CACHE_DIRS } from './constants.gen.js';
import { ArtifactCorruptError } from './errors.js';
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

export function blobsDir(root: string): string {
  return path.join(root, 'blobs', 'sha256');
}

/** Reads a local blob by its hex digest, `undefined` when it is not there — never throws on ENOENT. */
export async function readLocalBlob(root: string, hex: string): Promise<Buffer | undefined> {
  try {
    return await readFile(path.join(blobsDir(root), hex));
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw err;
  }
}

/** Every blob hex name present under `blobs/sha256/` — `[]` when the directory does not exist. */
export async function listLocalBlobHexes(root: string): Promise<readonly string[]> {
  try {
    return await readdir(blobsDir(root));
  } catch {
    return [];
  }
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

export interface IndexDescriptor {
  readonly mediaType: string;
  readonly digest: string;
  readonly size: number;
  readonly artifactType?: string;
  readonly platform?: { readonly os: string; readonly architecture: string };
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

/**
 * One `unpacked/sha256/<hex>/verified.json`, in memory. Its on-disk form is
 * the one canonical record every binding reads and writes
 * (`spec/fetch-v1/schema/verified.schema.json`, guide §1): see
 * `encodeRecord` and `decodeRecord`. Optional members are `null` here and
 * on disk.
 */
export interface VerifiedRecord {
  readonly platform: string;
  readonly version: string;
  readonly channel: string | null;
  readonly build: string;
  /** The library's file name, relative to the unpacked directory. */
  readonly library: string;
  readonly librarySha256: string;
  readonly libraryBytes: number;
  readonly indexDigest: string | null;
  readonly manifestDigest: string;
  readonly layerDigest: string;
  readonly bundleDigest: string | null;
  readonly bundleManifestDigest: string | null;
  /** `null` only under allow-unsigned. */
  readonly signedBy: string | null;
  readonly predicate: ArtifactPredicate;
}

export interface RecordSource {
  readonly platform: string;
  readonly predicate: ArtifactPredicate;
  /** The unpacked library as measured on disk (`measureLibrary`), never copied from an unsigned config. */
  readonly library: { readonly sha256: string; readonly bytes: number };
  readonly indexDigest: string | null;
  readonly manifestDigest: string;
  readonly layerDigest: string;
  readonly bundleDigest: string | null;
  readonly bundleManifestDigest: string | null;
  readonly signedBy: string | null;
}

/** Builds the canonical record for one verified install from what the install itself established. */
export function buildRecord(src: RecordSource): VerifiedRecord {
  const channel = src.predicate.channel;
  return {
    platform: src.platform,
    version: src.predicate.clickhouse_version,
    channel: typeof channel === 'string' && channel !== '' ? channel : null,
    build: src.predicate.build,
    library: src.predicate.library,
    librarySha256: src.library.sha256,
    libraryBytes: src.library.bytes,
    indexDigest: src.indexDigest,
    manifestDigest: src.manifestDigest,
    layerDigest: src.layerDigest,
    bundleDigest: src.bundleDigest,
    bundleManifestDigest: src.bundleManifestDigest,
    signedBy: src.signedBy,
    predicate: src.predicate,
  };
}

const HEX64 = /^[0-9a-f]{64}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const VERSION4 = /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/;

/** The canonical `verified.json` text: every member present, `null` where the schema allows it. */
export function encodeRecord(record: VerifiedRecord): string {
  return JSON.stringify({
    schema: 1,
    platform: record.platform,
    version: record.version,
    channel: record.channel,
    build: record.build,
    library: record.library,
    library_sha256: record.librarySha256,
    library_bytes: record.libraryBytes,
    digests: {
      index: record.indexDigest,
      manifest: record.manifestDigest,
      layer: record.layerDigest,
      bundle: record.bundleDigest,
      bundle_manifest: record.bundleManifestDigest,
    },
    signed_by: record.signedBy,
    predicate: record.predicate,
  });
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function nullableString(v: unknown): string | null | undefined {
  if (v === null) return null;
  return typeof v === 'string' ? v : undefined;
}

/**
 * Accepts exactly the canonical schema-1 record. Anything else (not JSON,
 * another schema, a missing member, a member of the wrong type, a broken
 * rule) is `undefined`, which every caller treats as an ABSENT record —
 * never fatal by itself and never trusted.
 */
export function decodeRecord(raw: string): VerifiedRecord | undefined {
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (!isObject(doc)) return undefined;
  for (const key of ['schema', 'platform', 'version', 'channel', 'build', 'library', 'library_sha256', 'library_bytes', 'digests', 'signed_by', 'predicate']) {
    if (!(key in doc)) return undefined;
  }
  const digests = doc['digests'];
  if (!isObject(digests)) return undefined;
  for (const key of ['index', 'manifest', 'layer', 'bundle', 'bundle_manifest']) {
    if (!(key in digests)) return undefined;
  }
  if (doc['schema'] !== 1) return undefined;
  const platform = doc['platform'];
  const version = doc['version'];
  const build = doc['build'];
  const library = doc['library'];
  const sha = doc['library_sha256'];
  const bytes = doc['library_bytes'];
  if (typeof platform !== 'string' || !PLATFORMS.some((p) => p.key === platform)) return undefined;
  if (typeof version !== 'string' || !VERSION4.test(version)) return undefined;
  if (typeof build !== 'string' || build === '') return undefined;
  if (typeof library !== 'string' || library === '' || library === '.' || library === '..' || library.includes('/') || library.includes('\\')) return undefined;
  if (typeof sha !== 'string' || !HEX64.test(sha)) return undefined;
  if (typeof bytes !== 'number' || !Number.isInteger(bytes) || bytes < 0) return undefined;
  if (!isObject(doc['predicate'])) return undefined;
  const channel = nullableString(doc['channel']);
  const signedBy = nullableString(doc['signed_by']);
  const index = nullableString(digests['index']);
  const manifest = digests['manifest'];
  const layer = digests['layer'];
  const bundle = nullableString(digests['bundle']);
  const bundleManifest = nullableString(digests['bundle_manifest']);
  if (channel === undefined || signedBy === undefined || index === undefined || bundle === undefined || bundleManifest === undefined) return undefined;
  if (typeof manifest !== 'string' || !DIGEST.test(manifest) || typeof layer !== 'string' || !DIGEST.test(layer)) return undefined;
  for (const d of [index, bundle, bundleManifest]) {
    if (d !== null && !DIGEST.test(d)) return undefined;
  }
  return {
    platform,
    version,
    channel,
    build,
    library,
    librarySha256: sha,
    libraryBytes: bytes,
    indexDigest: index,
    manifestDigest: manifest,
    layerDigest: layer,
    bundleDigest: bundle,
    bundleManifestDigest: bundleManifest,
    signedBy,
    predicate: doc['predicate'] as unknown as ArtifactPredicate,
  };
}

/**
 * Writes the directory's own durable proof of verification — **the source
 * of truth for `--offline` and `resolve_installed`** (guide §6), never
 * `index.json`. Written once, by temp-then-rename, immediately after a
 * successful unpack; a directory with no acceptable `verified.json` is
 * treated as unverified regardless of what `index.json` claims about it.
 * A record that would not read back (it breaks the schema) is refused here
 * rather than left behind as a directory no reader will trust.
 */
export async function writeVerifiedRecord(dir: string, record: VerifiedRecord): Promise<void> {
  const text = encodeRecord(record);
  if (decodeRecord(text) === undefined) {
    throw new ArtifactCorruptError(`chtypes: refusing to write a verified.json that breaks its own schema (version ${JSON.stringify(record.version)}, build ${JSON.stringify(record.build)})`);
  }
  const dest = path.join(dir, CACHE_VERIFIED_RECORD);
  const tmp = await tempPathNear(dest);
  await writeFile(tmp, text);
  await rename(tmp, dest);
}

/** `undefined` when the directory has no acceptable `verified.json` (missing, unparsable, another schema, or a broken rule) — never partially trusted. */
export async function readVerifiedRecord(dir: string): Promise<VerifiedRecord | undefined> {
  try {
    return decodeRecord(await readFile(path.join(dir, CACHE_VERIFIED_RECORD), 'utf8'));
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
