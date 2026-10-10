/**
 * `chtypes prune` (`docs/guides/fetch-v1.md` §1, "Pruning"; public issue
 * #494): remove the installed builds of a line that newer installed builds of
 * the same line and platform supersede, keeping the newest N, and never one a
 * live process holds (`./hold.ts`). It reads and writes the cache only, never
 * a system directory, and never another fingerprint's records (§3: the dev
 * channel's own-fingerprint rule).
 */

import { randomBytes } from 'node:crypto';
import { mkdirSync, renameSync, rmdirSync } from 'node:fs';
import { lstat, readdir, readFile, rename, rm, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { noteIgnoredOverrides, visibleToChannel } from './channel.js';
import { CACHE_UNPACKED_DIR, MANIFEST_MAX_BYTES, PLATFORMS } from './constants.gen.js';
import { probeRoots, strictMode, unwritable } from './faults.js';
import { claim } from './hold.js';
import { blobsDir, cacheRoot, listVerified, type VerifiedRecord } from './layout.js';
import { type FetchV1Options, hexOfDigest, type PlatformKey } from './types.js';

/** A two-part line spelling, `26.8`. */
const LINE = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const HEX64 = /^[0-9a-f]{64}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;

/** How many times a fresh aside directory, or an `index.json` rewrite, is tried before the failure stands. */
const ATTEMPTS = 8;

/** Whether `s` is a two-part line spelling, such as `26.8`. */
export function isLine(s: string): boolean {
  return LINE.test(s);
}

/** The two-part line of a version: `26.8` for `26.8.15.10`, or `''` for anything shorter. */
export function lineOf(version: string): string {
  const parts = version.split('.');
  return parts.length < 2 ? '' : parts.slice(0, 2).join('.');
}

/** What a prune removes. */
export interface PruneOptions {
  /** A two-part line (`26.8`); unset or `''` is every line. */
  readonly line?: string | undefined;
  /** How many of each line's newest builds stay, per platform: at least 1. */
  readonly keep: number;
  /** Decide and report exactly what would go, and remove nothing. */
  readonly dryRun?: boolean | undefined;
}

/** One installed build a prune found superseded: removed (or, in a dry run, to be removed), or kept because it is in use. */
export interface Superseded {
  readonly platform: PlatformKey;
  readonly version: string;
  readonly build: string;
  /** The entry's own name, `sha256:<hex>` of `unpacked/sha256/<hex>`. */
  readonly manifest: string;
  readonly dir: string;
  /** The build was kept: a live process holds it (or its record cannot be locked at all, so nothing can tell). */
  readonly inUse: boolean;
}

/** One installed entry of the cache. */
interface Entry {
  readonly dir: string;
  readonly record: VerifiedRecord;
  /** Its directory's name, as a digest. */
  readonly manifest: string;
}

/**
 * Removes, from the cache, every installed build that at least `keep` newer
 * installed builds of its own line and platform supersede, oldest first.
 * Builds are ordered by (version, build), then the manifest digest, and the
 * report by platform (the constants' order), then oldest first. A superseded
 * build a live process holds is reported `inUse` and kept. For each build it
 * removes the unpacked entry with its record, then its `index.json` entries,
 * then its blobs (the manifest, its config and layer, and every referrer
 * manifest of it the layout holds, with that referrer's layers), never a blob
 * a remaining build names. A `keep` below 1 or a `line` that is not a two-part
 * line is the caller's misuse. A write that fails under the cache is a
 * `CacheUnusableError` (reason `unwritable`).
 */
export async function prune(options: FetchV1Options, request: PruneOptions): Promise<readonly Superseded[]> {
  if (!Number.isInteger(request.keep) || request.keep < 1) {
    throw new RangeError(`chtypes: --keep is at least 1 (the newest build of a line is never superseded), not ${request.keep}`);
  }
  const line = request.line ?? '';
  if (line !== '' && !isLine(line)) {
    throw new RangeError(`chtypes: --line takes a two-part line such as 26.8, not ${JSON.stringify(line)}`);
  }
  noteIgnoredOverrides(options);
  const root = cacheRoot(options.cacheDir);
  try {
    return await pruneIn(root, options, line, request.keep, request.dryRun === true);
  } catch (err) {
    throw await unwritable(err, root);
  }
}

async function pruneIn(root: string, options: FetchV1Options, line: string, keep: number, dryRun: boolean): Promise<readonly Superseded[]> {
  // Only the cache is pruned: a system directory is read-only. An unreadable
  // cache holds nothing this call can prune; the default mode's warning names
  // it (`missingNotes`), and strict mode's `CacheUnusableError` is thrown here.
  await probeRoots([root], strictMode(options.strictCache));
  const groups = new Map<string, Entry[]>();
  for (const { dir, record } of await listVerified(root)) {
    // Another fingerprint's build: its own SDK keeps or prunes it.
    if (!visibleToChannel(record.predicate)) continue;
    const entryLine = lineOf(record.version);
    if (line !== '' && entryLine !== line) continue;
    const key = `${record.platform} ${entryLine}`;
    const entry: Entry = { dir, record, manifest: entryManifest(dir) };
    const group = groups.get(key);
    if (group === undefined) groups.set(key, [entry]);
    else group.push(entry);
  }
  const superseded: Entry[] = [];
  for (const members of groups.values()) {
    members.sort((a, b) => compareAge(b, a)); // newest first
    superseded.push(...members.slice(keep));
  }
  superseded.sort((a, b) => platformRank(a.record.platform) - platformRank(b.record.platform) || compareAge(a, b));

  const unpackedRoot = path.join(root, CACHE_UNPACKED_DIR);
  const out: Superseded[] = [];
  const removed: { readonly entry: Entry; readonly aside: string }[] = [];
  for (const e of superseded) {
    const s = {
      platform: e.record.platform as PlatformKey,
      version: e.record.version,
      build: e.record.build,
      manifest: e.manifest,
      dir: e.dir,
    };
    const claimed = claim(e.dir);
    if (claimed.state === 'gone') continue; // another prune removed it, or an install replaced it
    if (claimed.state === 'in-use') {
      out.push({ ...s, inUse: true });
      continue;
    }
    if (dryRun) {
      claimed.release();
      out.push({ ...s, inUse: false });
      continue;
    }
    // Moved aside while the exclusive lock is held, synchronously, so nothing
    // else in this process runs meanwhile: once the lock drops, no lookup
    // lists the entry, and a hold taken on it finds it gone.
    const aside = releasing(claimed.release, () => moveAside(unpackedRoot, e.dir));
    removed.push({ entry: e, aside });
    out.push({ ...s, inUse: false });
  }
  if (removed.length === 0) return out;

  const blobs = await blobsOf(root, new Set(removed.map((r) => r.entry.manifest)));
  for (const { entry } of removed) {
    for (const d of [entry.record.layerDigest, entry.record.bundleDigest, entry.record.bundleManifestDigest]) {
      if (d !== null && DIGEST.test(d)) blobs.add(d);
    }
  }
  // Never a blob a remaining build names, whatever its fingerprint.
  for (const { dir, record } of await listVerified(root)) {
    const named = [...(await ownBlobs(root, entryManifest(dir))), record.manifestDigest, record.layerDigest, record.bundleDigest, record.bundleManifestDigest];
    for (const d of named) if (d !== null) blobs.delete(d);
  }
  await removeIndexEntries(root, blobs);
  for (const d of blobs) {
    try {
      await unlink(path.join(blobsDir(root), hexOfDigest(d)));
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
    }
  }
  for (const { aside } of removed) await rm(aside, { recursive: true, force: true });
  return out;
}

/** `f()`, then `release()` however `f` ended. */
function releasing<T>(release: () => void, f: () => T): T {
  try {
    return f();
  } finally {
    release();
  }
}

/** Renames the entry `dir` into a fresh `unpacked/sha256/.prune-<random>` directory, as `entry`, and returns that directory. */
function moveAside(unpackedRoot: string, dir: string): string {
  const aside = freshPruneDir(unpackedRoot);
  try {
    renameSync(dir, path.join(aside, 'entry'));
  } catch (err) {
    try {
      rmdirSync(aside);
    } catch {
      // Left empty beside the entries; no lookup reads a `.prune-` name.
    }
    throw err;
  }
  return aside;
}

/** A fresh, empty `.prune-<random>` directory under `unpackedRoot` (mode 0777 less the umask), its random bits this process's own. */
function freshPruneDir(unpackedRoot: string): string {
  for (let attempt = 1; ; attempt++) {
    const dir = path.join(unpackedRoot, `.prune-${process.pid}-${randomBytes(6).toString('hex')}`);
    try {
      mkdirSync(dir, { mode: 0o777 });
      return dir;
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST' || attempt >= ATTEMPTS) throw err;
    }
  }
}

/** An installed entry's own manifest digest: its directory's name. */
function entryManifest(dir: string): string {
  return `sha256:${path.basename(dir)}`;
}

/** Orders installed builds by age: (version, numerically, part by part), then the build, then the manifest digest, so the order is total. */
function compareAge(a: Entry, b: Entry): number {
  if (a.record.version !== b.record.version) {
    const av = a.record.version.split('.');
    const bv = b.record.version.split('.');
    for (let i = 0; i < Math.min(av.length, bv.length); i++) {
      if (av[i] !== bv[i]) return Number(av[i]) - Number(bv[i]);
    }
    return av.length - bv.length;
  }
  if (a.record.build !== b.record.build) return a.record.build < b.record.build ? -1 : 1;
  if (a.manifest !== b.manifest) return a.manifest < b.manifest ? -1 : 1;
  return 0;
}

/** A platform key's place in the constants' platform list. */
function platformRank(key: string): number {
  const i = PLATFORMS.findIndex((p) => p.key === key);
  return i < 0 ? PLATFORMS.length : i;
}

/** What a prune reads of a manifest blob: its config's and layers' digests (each only when it is a digest), and its subject's. */
interface ManifestDoc {
  readonly config: string | undefined;
  readonly layers: readonly string[];
  readonly subject: string | undefined;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** A descriptor member's digest: `''` when absent or null, `undefined` when the member is not a descriptor. */
function descriptorDigest(v: unknown): string | undefined {
  if (v === undefined || v === null) return '';
  if (!isObject(v)) return undefined;
  const digest = v['digest'];
  if (digest === undefined || digest === null) return '';
  return typeof digest === 'string' ? digest : undefined;
}

/** A manifest blob's descriptors, or `undefined` for bytes that are not one (not JSON, not an object, or a descriptor member of the wrong shape). */
function manifestDoc(bytes: Buffer): ManifestDoc | undefined {
  let doc: unknown;
  try {
    doc = JSON.parse(bytes.toString('utf8'));
  } catch {
    return undefined;
  }
  if (!isObject(doc)) return undefined;
  const config = descriptorDigest(doc['config']);
  const subject = descriptorDigest(doc['subject']);
  if (config === undefined || subject === undefined) return undefined;
  const rawLayers = doc['layers'];
  const layers: string[] = [];
  if (rawLayers !== undefined && rawLayers !== null) {
    if (!Array.isArray(rawLayers)) return undefined;
    for (const layer of rawLayers) {
      const digest = descriptorDigest(layer);
      if (digest === undefined) return undefined;
      if (DIGEST.test(digest)) layers.push(digest);
    }
  }
  return {
    config: DIGEST.test(config) ? config : undefined,
    layers,
    subject: subject === '' ? undefined : subject,
  };
}

/** The blobs a manifest names itself: the manifest, its config and its layers (only the manifest when its blob is absent or unreadable). */
async function ownBlobs(root: string, manifest: string): Promise<string[]> {
  const out = [manifest];
  let bytes: Buffer;
  try {
    bytes = await readFile(path.join(blobsDir(root), hexOfDigest(manifest)));
  } catch {
    return out;
  }
  const doc = manifestDoc(bytes);
  if (doc === undefined) return out;
  if (doc.config !== undefined) out.push(doc.config);
  out.push(...doc.layers);
  return out;
}

/**
 * The blobs a prune removes with `manifests`: each one's own (`ownBlobs`), and
 * every referrer manifest the layout holds whose subject is one of them (a
 * signature, a goldens document), with that referrer's layers. A referrer's
 * config, the empty `{}` every referrer shares, is never among them.
 */
async function blobsOf(root: string, manifests: ReadonlySet<string>): Promise<Set<string>> {
  const out = new Set<string>();
  for (const d of manifests) for (const x of await ownBlobs(root, d)) out.add(x);
  const dir = blobsDir(root);
  const names = await readdir(dir, { withFileTypes: true }).catch(() => undefined);
  if (names === undefined) return out;
  for (const e of names) {
    if (e.isDirectory() || !HEX64.test(e.name)) continue;
    const file = path.join(dir, e.name);
    let bytes: Buffer;
    try {
      if ((await lstat(file)).size > MANIFEST_MAX_BYTES) continue;
      bytes = await readFile(file);
    } catch {
      continue;
    }
    const doc = manifestDoc(bytes);
    if (doc === undefined || doc.subject === undefined || !manifests.has(doc.subject)) continue;
    out.add(`sha256:${e.name}`);
    for (const layer of doc.layers) out.add(layer);
  }
  return out;
}

/**
 * Drops every `index.json` entry whose digest is in `drop`, by the same
 * read, write-temp, re-read, rename loop `upsertIndexEntry` uses, so an entry
 * another process adds meanwhile survives; the other entries keep their order.
 * An absent or unparsable `index.json` is left as it is.
 */
async function removeIndexEntries(root: string, drop: ReadonlySet<string>): Promise<void> {
  const indexPath = path.join(root, 'index.json');
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    let before: string;
    try {
      before = await readFile(indexPath, 'utf8');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return;
      throw err;
    }
    let doc: unknown;
    try {
      doc = JSON.parse(before);
    } catch {
      return;
    }
    if (!isObject(doc)) return;
    const manifests = doc['manifests'];
    if (!Array.isArray(manifests)) return;
    const kept = manifests.filter((m: unknown) => {
      if (!isObject(m)) return true;
      const digest = m['digest'];
      return typeof digest !== 'string' || !drop.has(digest);
    });
    if (kept.length === manifests.length) return;
    const tmp = `${indexPath}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
    await writeFile(tmp, JSON.stringify({ ...doc, manifests: kept }));
    let current: string | undefined;
    try {
      current = await readFile(indexPath, 'utf8');
    } catch {
      current = undefined;
    }
    if (current !== before) {
      // Another process wrote it meanwhile: drop from theirs.
      await unlink(tmp).catch(() => {});
      continue;
    }
    try {
      await rename(tmp, indexPath);
    } catch (err) {
      await unlink(tmp).catch(() => {});
      throw err;
    }
    return;
  }
  throw new Error(`chtypes: index.json at ${indexPath} would not settle after ${ATTEMPTS} attempts (sustained concurrent writers)`);
}
