/**
 * Fetching, verifying and installing artifacts — docs/fetch.md, the contract
 * every SDK implements identically: the same source (§2), the same
 * verification chain (§3), the same signature policy (§4), the same lock
 * file (§5), the same function (§6) and the same codes (§7).
 *
 * Nothing here is a verdict but the chain. No exit code, no
 * `Content-Length`, no "download finished" ever is:
 *
 *   0. `SHA256SUMS.sig` — ed25519 over the exact bytes of `SHA256SUMS`, with a
 *      trusted key. Failure stops everything; nothing is downloaded around it.
 *   1. `index.json` names the asset for the line/platform and its sha256.
 *   2. `SHA256SUMS` — now known-authentic — must list the same file with the
 *      same sha256; a disagreement is a broken release, reported, not repaired.
 *   3. The tarball is hashed BEFORE it is unpacked.
 *   4. `manifest.json` inside names the library and its sha256; after the
 *      move into `<registry>/<minor>/`, the installed library is hashed again
 *      in place.
 *
 * The install is atomic — unpack into a temporary sibling, rename into place
 * — and idempotent: an installed line that hashes what the release says is
 * reported as installed and nothing is downloaded.
 *
 * Steady state is offline: once files are in a registry directory the loader
 * trusts the directory, exactly as a runtime trusts `node_modules`.
 * Verification is a fetch-time policy, not a load-time gate.
 */

import { createHash, createPublicKey, randomBytes, verify as verifyRaw } from 'node:crypto';
import { createReadStream, createWriteStream, existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import type { ReadableStream as WebReadableStream } from 'node:stream/web';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import {
  ArtifactCorruptError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  ChtypesError,
  SourceUnreachableError,
} from './errors.js';
import { fetchDestination, hostPlatform, isPlatformKey } from './paths.js';
import { extractTarGz } from './tar.js';

// ------------------------------------------------------------- constants

/** The public artifacts host (docs/fetch.md §2); `CHTYPES_ARTIFACTS_URL` overrides it. */
export const DEFAULT_ARTIFACTS_URL = 'https://artifacts.wavehouse.dev';
/** The rolling release every fetch reads by default; `tag` picks a frozen one. */
export const DEFAULT_RELEASE_TAG = 'artifacts';
/**
 * The release signing key(s) — raw 32-byte ed25519 public keys, hex
 * (docs/fetch.md §4). `CHTYPES_TRUSTED_KEYS` or the `trustedKeys` option
 * REPLACES this list; it is never extended silently.
 */
export const RELEASE_PUBLIC_KEYS: readonly string[] = [
  'fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc',
];
/** The lock-file schema this SDK writes and reads (docs/fetch.md §5). */
export const LOCK_SCHEMA = 1;
/** The lock file `frozen` reads when no `lock` names one (docs/fetch.md, Decisions); relative, so the working directory's. */
export const DEFAULT_LOCK_FILE = 'chtypes.lock';
const INDEX_SCHEMA = 1;

// ------------------------------------------------------------------ types

/** One row of a release's `index.json` (docs/artifacts.md §2). */
export interface IndexArtifact {
  readonly os: string;
  readonly arch: string;
  readonly file: string;
  /** sha256 of the tarball. */
  readonly sha256: string;
  /** size of the tarball. */
  readonly bytes: number;
  readonly clickhouse_version: string;
  readonly clickhouse_minor: string;
  readonly library: string;
  readonly library_sha256: string;
  /**
   * The wrapper build for this ClickHouse version. A rebuild of the same
   * version is a NEW row beside the old one, never a swap, so `build` is what
   * separates them. Absent from an old row, and from a file name with no
   * `-b<N>` suffix: both mean build 0.
   */
  readonly build: number;
  /** The core commit the wrapper was built from; `''` on an old row. */
  readonly core_commit: string;
}

/** A release's `index.json`, schema 1. */
export interface ReleaseIndex {
  readonly schema: number;
  readonly generated_at?: string;
  readonly release_tag?: string;
  readonly license?: string;
  readonly license_url?: string;
  readonly artifacts: readonly IndexArtifact[];
}

/** One pinned line in a lock file: the asset that was installed and its sha256. */
export interface LockEntry {
  readonly file: string;
  readonly sha256: string;
}

/** `chtypes.lock` (docs/fetch.md §5): `{"schema": 1, "artifacts": {"<os>-<arch>/<minor>": {file, sha256}}}`. */
export interface LockFile {
  readonly schema: number;
  readonly artifacts: Record<string, LockEntry>;
}

/** Progress, for a caller that wants to show it (the CLI prints these on stderr). */
export type FetchEvent =
  | { readonly type: 'status'; readonly message: string }
  | { readonly type: 'download'; readonly file: string; readonly received: number; readonly total: number };

/** Options for `ensure`, `ensureAll`, `listArtifacts` — the §6 flags, spelled as properties. */
export interface EnsureOptions {
  /** Where to install (`--dest`); default: the §1 write target (`CHTYPES_REGISTRY`, else the per-user cache). */
  readonly dest?: string | undefined;
  /** `<os>-<arch>` (`--platform`); default: this host (`CHTYPES_TARGET` overrides, as `scripts/fetch.sh`). */
  readonly platform?: string | undefined;
  /** A release tag on the artifacts host (`--tag`); default the rolling `artifacts`. Exclusive with `url`. */
  readonly tag?: string | undefined;
  /** Any other base (`--url`): `https://…`, `file://…`, or a plain directory. Exclusive with `tag`. */
  readonly url?: string | undefined;
  /** A lock file to record into (`--lock`), and to enforce with `frozen`. */
  readonly lock?: string | undefined;
  /** Refuse anything the lock does not pin, with `CHTYPES_ARTIFACT_PINNED` (`--frozen`). */
  readonly frozen?: boolean | undefined;
  /** Re-download even when the line is installed and verified (`--force`). */
  readonly force?: boolean | undefined;
  /** Never touch the source: installed-and-verified is fine, anything else is `CHTYPES_SOURCE_UNREACHABLE` (`--offline`). */
  readonly offline?: boolean | undefined;
  /** Raw ed25519 public keys, hex; REPLACES the embedded release key (as `CHTYPES_TRUSTED_KEYS` does). */
  readonly trustedKeys?: readonly string[] | undefined;
  /** Skip the signature (step 0) with one loud warning; never the default (as `CHTYPES_ALLOW_UNSIGNED=1`). */
  readonly allowUnsigned?: boolean | undefined;
  /** Progress events; silent when absent. */
  readonly onProgress?: ((event: FetchEvent) => void) | undefined;
  /** Aborts an in-progress download. */
  readonly signal?: AbortSignal | undefined;
}

/** What `ensure` did: where the line is, what it is, and whether bytes moved. */
export interface EnsureResult {
  /** The installed line directory, `<registry>/<minor>` — what `fetch` prints. */
  readonly dir: string;
  /** The registry directory the line lives in. */
  readonly registry: string;
  readonly line: string;
  readonly version: string;
  readonly platform: string;
  /** The asset name and its sha256 — what the lock records; empty on the offline path. */
  readonly file: string;
  readonly sha256: string;
  readonly library: string;
  readonly librarySha256: string;
  /** true when the tarball was downloaded and installed by this call; false when the line was already installed and verified. */
  readonly installed: boolean;
  /** The source that was consulted, or `offline`. */
  readonly source: string;
  /** Whether `SHA256SUMS` carried a signature a trusted key verified, and that key's id. */
  readonly signed: boolean;
  readonly keyId: string | null;
}

/** One installed line, as `verify` and `list` report it. */
export interface InstalledArtifact {
  readonly line: string;
  readonly dir: string;
  readonly version: string;
  readonly library: string;
  /** The manifest's sha256; what the file hashes (only `verifyInstalled` fills it in); the verdict. */
  readonly expected: string;
  readonly actual: string | null;
  readonly ok: boolean;
  readonly problem: string | null;
}

/** What `list` shows: what is installed, and what the release offers for the platform. */
export interface ListResult {
  readonly registry: string;
  readonly platform: string;
  readonly installed: readonly InstalledArtifact[];
  /** null when offline. */
  readonly offered: readonly IndexArtifact[] | null;
  readonly source: string | null;
}

// -------------------------------------------------------------- versions

/** How a version was asked for: the line, and the exact patch if one was typed. */
export interface VersionRequest {
  readonly spelling: string;
  /** e.g. `25.8`. */
  readonly line: string;
  /** `25.8.28.1-lts` when a patch was typed (a hard requirement), else null. */
  readonly exact: string | null;
  /** The exact patch without its channel suffix, when one was typed. */
  readonly bare: string | null;
}

const CHANNEL = /-(lts|stable|prestable|testing)$/;

/**
 * `25.8` is a line; `25.8.28.1-lts`, `v25.8.28.1-lts` and `25.8.28.1` are an
 * exact patch and a hard requirement (a release that publishes another patch
 * of the line refuses it — ask for the line to take what was published).
 *
 * @throws {ChtypesError} when the spelling is not a ClickHouse version at all.
 */
export function parseVersionSpelling(spelling: string): VersionRequest {
  const s = spelling.trim().replace(/^v/, '');
  const bare = s.replace(CHANNEL, '');
  const parts = bare.split('.');
  if (parts.length < 2 || !parts.every((p) => /^\d+$/.test(p))) {
    throw new ChtypesError(`chtypes: cannot make a ClickHouse version out of ${JSON.stringify(spelling)}`);
  }
  const line = `${parts[0]}.${parts[1]}`;
  return parts.length >= 4 ? { spelling, line, exact: s, bare } : { spelling, line, exact: null, bare: null };
}

/** Numeric order over dotted versions, channel suffix ignored: 25.10 > 25.8, 25.8.30.16 > 25.8.28.1. */
export function compareVersions(a: string, b: string): number {
  const pa = a.replace(CHANNEL, '').split('.').map((p) => Number.parseInt(p, 10));
  const pb = b.replace(CHANNEL, '').split('.').map((p) => Number.parseInt(p, 10));
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? -1;
    const y = pb[i] ?? -1;
    if (Number.isNaN(x) || Number.isNaN(y)) return a < b ? -1 : a > b ? 1 : 0;
    if (x !== y) return x - y;
  }
  return 0;
}

// ------------------------------------------------------------ signature

/** The 12-byte SubjectPublicKeyInfo header for an Ed25519 key (RFC 8410): SEQUENCE { AlgorithmIdentifier(1.3.101.112), BIT STRING }. */
const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');
const RAW_KEY = /^[0-9a-fA-F]{64}$/;

/** The key id docs/fetch.md §4 defines: the first 16 hex characters of sha256 over the raw 32-byte public key. */
export function keyId(rawKeyHex: string): string {
  return createHash('sha256').update(Buffer.from(rawKeyHex, 'hex')).digest('hex').slice(0, 16);
}

/**
 * Verify an ed25519 signature over `message` with a raw public key, through
 * `node:crypto` — the key wrapped as SPKI DER, the 64-byte signature as is.
 * False, never a throw, for a malformed key or signature.
 */
export function verifyEd25519(message: Uint8Array, signature: Uint8Array, rawKeyHex: string): boolean {
  if (!RAW_KEY.test(rawKeyHex) || signature.byteLength !== 64) return false;
  try {
    const key = createPublicKey({
      key: Buffer.concat([ED25519_SPKI_PREFIX, Buffer.from(rawKeyHex, 'hex')]),
      format: 'der',
      type: 'spki',
    });
    return verifyRaw(null, message, key, signature);
  } catch {
    return false;
  }
}

/**
 * `SHA256SUMS.sig`: an `untrusted comment:` line, then the base64 of the
 * 64-byte signature. Null when the file is not in that shape.
 */
export function parseSignatureFile(bytes: Uint8Array): { comment: string; signature: Buffer } | null {
  const lines = Buffer.from(bytes).toString('utf8').split(/\r?\n/);
  let comment = '';
  for (const raw of lines) {
    const line = raw.trim();
    if (line === '') continue;
    if (line.startsWith('untrusted comment:')) {
      comment = line.slice('untrusted comment:'.length).trim();
      continue;
    }
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(line)) return null;
    const signature = Buffer.from(line, 'base64');
    return signature.length === 64 ? { comment, signature } : null;
  }
  return null;
}

/**
 * The keys a fetch trusts: the `trustedKeys` option, else `CHTYPES_TRUSTED_KEYS`
 * (`<hex>[,<hex>…]`), else the embedded release key. Each REPLACES the one
 * below it.
 *
 * @throws {ChtypesError} when an entry is not 64 hex characters.
 */
export function trustedKeys(explicit?: readonly string[]): string[] {
  let list: readonly string[];
  if (explicit !== undefined) list = explicit;
  else {
    const env = process.env['CHTYPES_TRUSTED_KEYS'];
    const fromEnv = env === undefined ? [] : env.split(',').map((k) => k.trim()).filter((k) => k !== '');
    list = fromEnv.length > 0 ? fromEnv : RELEASE_PUBLIC_KEYS;
  }
  return list.map((k) => {
    if (!RAW_KEY.test(k)) {
      throw new ChtypesError(
        `chtypes: ${JSON.stringify(k)} is not a raw ed25519 public key (64 hex characters) — check CHTYPES_TRUSTED_KEYS`,
      );
    }
    return k.toLowerCase();
  });
}

// -------------------------------------------------------------- sources

interface Source {
  readonly description: string;
  /** A small release file, whole; null when the source does not have it. */
  get(name: string): Promise<Buffer | null>;
  /** Stream an asset to `to`; false when the source does not have it. */
  download(name: string, to: string, onReceived: (received: number) => void): Promise<boolean>;
}

class DirSource implements Source {
  readonly description: string;
  constructor(private readonly dir: string) {
    this.description = dir;
  }

  async get(name: string): Promise<Buffer | null> {
    try {
      return await readFile(path.join(this.dir, name));
    } catch (err) {
      if (isNoEntry(err)) return null;
      throw new SourceUnreachableError(`chtypes: cannot read ${path.join(this.dir, name)}: ${errorText(err)}`, {
        cause: err,
      });
    }
  }

  async download(name: string, to: string, onReceived: (received: number) => void): Promise<boolean> {
    const from = path.join(this.dir, name);
    if (!existsSync(from)) return false;
    try {
      await pipeline(createReadStream(from), progressTap(onReceived), createWriteStream(to));
    } catch (err) {
      throw new SourceUnreachableError(`chtypes: cannot read ${from}: ${errorText(err)}`, { cause: err });
    }
    return true;
  }
}

class HttpSource implements Source {
  readonly description: string;
  constructor(
    private readonly base: string,
    private readonly signal: AbortSignal | undefined,
  ) {
    this.description = base;
  }

  private async request(name: string): Promise<Response | null> {
    const url = `${this.base}/${name}`;
    const headers: Record<string, string> = {};
    const token = process.env['CHTYPES_DOWNLOAD_TOKEN'];
    if (token !== undefined && token !== '') headers['Authorization'] = `Bearer ${token}`;
    let last: unknown;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await fetch(url, { headers, redirect: 'follow', ...(this.signal ? { signal: this.signal } : {}) });
        if (res.status === 404) {
          await res.body?.cancel();
          return null;
        }
        if (res.ok) return res;
        await res.body?.cancel();
        last = new Error(`HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''}`);
        // Client errors do not get better on retry; server errors and throttling might.
        if (res.status < 500 && res.status !== 408 && res.status !== 429) break;
      } catch (err) {
        if (this.signal?.aborted) throw err;
        last = err;
      }
      if (attempt < 3) await sleep(500 * attempt);
    }
    throw new SourceUnreachableError(`chtypes: cannot fetch ${url}: ${errorText(last)}`, { cause: last });
  }

  async get(name: string): Promise<Buffer | null> {
    const res = await this.request(name);
    if (res === null) return null;
    return Buffer.from(await res.arrayBuffer());
  }

  async download(name: string, to: string, onReceived: (received: number) => void): Promise<boolean> {
    const res = await this.request(name);
    if (res === null) return false;
    if (res.body === null) {
      throw new SourceUnreachableError(`chtypes: ${this.base}/${name} returned no body`);
    }
    try {
      await pipeline(
        Readable.fromWeb(res.body as unknown as WebReadableStream<Uint8Array>),
        progressTap(onReceived),
        createWriteStream(to),
      );
    } catch (err) {
      throw new SourceUnreachableError(`chtypes: download of ${this.base}/${name} failed: ${errorText(err)}`, {
        cause: err,
      });
    }
    return true;
  }
}

/** §2: `url` is a full base (http(s), `file://`, or a directory); otherwise `<CHTYPES_ARTIFACTS_URL>/<tag>`. */
function openSource(options: EnsureOptions): Source {
  if (options.url !== undefined && options.url !== '' && options.tag !== undefined && options.tag !== '') {
    throw new ChtypesError('chtypes: url names a full base; tag selects a release on the artifacts host — pass one');
  }
  let base: string;
  if (options.url !== undefined && options.url !== '') base = options.url;
  else {
    const host = process.env['CHTYPES_ARTIFACTS_URL'];
    const root = host !== undefined && host !== '' ? host : DEFAULT_ARTIFACTS_URL;
    const tag = options.tag !== undefined && options.tag !== '' ? options.tag : DEFAULT_RELEASE_TAG;
    base = `${root.replace(/\/+$/, '')}/${tag}`;
  }
  if (/^https?:\/\//i.test(base)) return new HttpSource(base.replace(/\/+$/, ''), options.signal);
  const dir = base.startsWith('file://') ? fileURLToPath(base) : path.resolve(base);
  return new DirSource(dir);
}

// ------------------------------------------------------------ the chain

interface Release {
  readonly source: Source;
  readonly index: ReleaseIndex;
  readonly sums: ReadonlyMap<string, string>;
  readonly signed: boolean;
  readonly keyId: string | null;
}

/** Steps 0–2's inputs, verified in that order: the signature over SHA256SUMS, then the index. */
async function loadReleaseOnce(source: Source, options: EnsureOptions, emit: (e: FetchEvent) => void): Promise<Release> {
  const sumsBytes = await source.get('SHA256SUMS');
  if (sumsBytes === null) throw new SourceUnreachableError(`chtypes: no SHA256SUMS at ${source.description}`);

  let signed = false;
  let signer: string | null = null;
  if (options.allowUnsigned ?? envFlag('CHTYPES_ALLOW_UNSIGNED')) {
    warn(
      `chtypes: WARNING — the signature of ${source.description}/SHA256SUMS is NOT being verified ` +
        '(CHTYPES_ALLOW_UNSIGNED). Anything it lists will be installed on its hashes alone.',
    );
  } else {
    const sigBytes = await source.get('SHA256SUMS.sig');
    if (sigBytes === null) {
      throw new ArtifactUntrustedError(
        `chtypes: the release at ${source.description} is unsigned (no SHA256SUMS.sig); nothing was downloaded. ` +
          'CHTYPES_ALLOW_UNSIGNED=1 installs it anyway, loudly.',
      );
    }
    const sig = parseSignatureFile(sigBytes);
    if (sig === null) {
      throw new ArtifactUntrustedError(
        `chtypes: ${source.description}/SHA256SUMS.sig is not an ed25519 signature file; nothing was downloaded.`,
      );
    }
    const keys = trustedKeys(options.trustedKeys);
    const hit = keys.find((k) => verifyEd25519(sumsBytes, sig.signature, k));
    if (hit === undefined) {
      throw new ArtifactUntrustedError(
        `chtypes: ${source.description}/SHA256SUMS.sig does not verify with any trusted key ` +
          `(${keys.map(keyId).join(', ')}); the release is not trusted and nothing was downloaded.`,
      );
    }
    signed = true;
    signer = keyId(hit);
    emit({ type: 'status', message: `SHA256SUMS signature verified (key ${signer})` });
  }

  const indexBytes = await source.get('index.json');
  if (indexBytes === null) throw new SourceUnreachableError(`chtypes: no index.json at ${source.description}`);
  const index = parseIndex(indexBytes, source.description);
  if (index.license !== undefined && index.license !== '') {
    emit({
      type: 'status',
      message: `artifacts are licensed under ${index.license}${index.license_url ? ` (${index.license_url})` : ''} — LICENSE and NOTICE ship beside them`,
    });
  }
  const release: Release = { source, index, sums: parseSums(sumsBytes), signed, keyId: signer };
  // Step 2 for the whole release, not just the asset being installed. installOne
  // still checks its own asset; this runs here so a disagreement is seen while
  // loadRelease can still fix it by reading all three files again.
  for (const art of index.artifacts) {
    const listed = release.sums.get(art.file);
    if (listed !== undefined && listed !== art.sha256) {
      throw new ArtifactCorruptError(
        `chtypes: index.json says ${art.file} is ${art.sha256} but SHA256SUMS says ${listed} — the release disagrees with itself; not installing it`,
      );
    }
  }
  return release;
}

/** How many times {@link loadRelease} reads an HTTP release before giving up. */
const RELEASE_LOAD_ATTEMPTS = 3;
/** The wait between those reads: three attempts span about ten seconds. */
const RELEASE_RETRY_DELAY_MS = 4_000;

/**
 * {@link loadReleaseOnce}, retried through a publish window.
 *
 * A publish into the rolling release is three objects — `SHA256SUMS`,
 * `SHA256SUMS.sig`, `index.json` — and object storage cannot swap them
 * atomically. They go up in that order, so an old index read against new sums
 * still cross-checks; the unsafe window is between the sums and the signature
 * that covers them, one small object wide and seconds long.
 *
 * The two symptoms of reading inside it — a signature under no trusted key and
 * an index that disagrees with the sums — are retried. Nothing else is, and
 * neither are these once the attempts run out: the same error surfaces, with the
 * same code, as it did before. A tarball whose hash is wrong is never retried;
 * that is the release lying about a byte, not a half-finished upload.
 *
 * Only an HTTP source can be mid-publish, so a `file://` or directory source is
 * read exactly once and refuses on the first look.
 */
async function loadRelease(source: Source, options: EnsureOptions, emit: (e: FetchEvent) => void): Promise<Release> {
  const attempts = source instanceof HttpSource ? RELEASE_LOAD_ATTEMPTS : 1;
  for (let attempt = 1; ; attempt++) {
    try {
      return await loadReleaseOnce(source, options, emit);
    } catch (err) {
      const retryable = err instanceof ArtifactUntrustedError || err instanceof ArtifactCorruptError;
      if (!retryable || attempt >= attempts) throw err;
      emit({
        type: 'status',
        message:
          `${String(err)} (attempt ${attempt}/${attempts}) — this is what a release being ` +
          `published looks like from outside; retrying in ${RELEASE_RETRY_DELAY_MS / 1000}s`,
      });
      await new Promise((resolve) => setTimeout(resolve, RELEASE_RETRY_DELAY_MS));
    }
  }
}

function parseIndex(bytes: Buffer, where: string): ReleaseIndex {
  let doc: unknown;
  try {
    doc = JSON.parse(bytes.toString('utf8'));
  } catch (err) {
    throw new ArtifactCorruptError(`chtypes: index.json at ${where} is not JSON: ${errorText(err)}`, { cause: err });
  }
  if (typeof doc !== 'object' || doc === null) throw new ArtifactCorruptError(`chtypes: index.json at ${where} is not an object`);
  const d = doc as Record<string, unknown>;
  if (d['schema'] !== INDEX_SCHEMA) {
    throw new ArtifactCorruptError(`chtypes: index.json schema ${JSON.stringify(d['schema'])} is not ${INDEX_SCHEMA} — this SDK cannot read it`);
  }
  if (!Array.isArray(d['artifacts'])) throw new ArtifactCorruptError(`chtypes: index.json at ${where} has no artifacts list`);
  const artifacts = (d['artifacts'] as unknown[]).map((row, i) => indexArtifact(row, i));
  return {
    schema: INDEX_SCHEMA,
    ...(typeof d['generated_at'] === 'string' ? { generated_at: d['generated_at'] } : {}),
    ...(typeof d['release_tag'] === 'string' ? { release_tag: d['release_tag'] } : {}),
    ...(typeof d['license'] === 'string' ? { license: d['license'] } : {}),
    ...(typeof d['license_url'] === 'string' ? { license_url: d['license_url'] } : {}),
    artifacts,
  };
}

function indexArtifact(row: unknown, i: number): IndexArtifact {
  if (typeof row !== 'object' || row === null) throw new ArtifactCorruptError(`chtypes: index.json artifacts[${i}] is not an object`);
  const r = row as Record<string, unknown>;
  const str = (k: string): string => {
    const v = r[k];
    if (typeof v !== 'string' || v === '') throw new ArtifactCorruptError(`chtypes: index.json entry for ${String(r['file'] ?? i)} is missing ${k}`);
    return v;
  };
  const bytes = r['bytes'];
  if (typeof bytes !== 'number' || !Number.isSafeInteger(bytes) || bytes <= 0) {
    throw new ArtifactCorruptError(`chtypes: index.json entry for ${String(r['file'] ?? i)} is missing bytes`);
  }
  const file = str('file');
  const version = str('clickhouse_version');
  const minor = typeof r['clickhouse_minor'] === 'string' && r['clickhouse_minor'] !== '' ? r['clickhouse_minor'] : minorOfVersion(version);
  return {
    os: str('os'),
    arch: str('arch'),
    file,
    sha256: str('sha256').toLowerCase(),
    bytes,
    clickhouse_version: version,
    clickhouse_minor: minor,
    library: str('library'),
    library_sha256: str('library_sha256').toLowerCase(),
    build: buildOf(r['build'], file),
    core_commit: typeof r['core_commit'] === 'string' ? r['core_commit'] : '',
  };
}

/**
 * The wrapper build number: the row's own `build` when it has one, else the
 * `-b<N>` suffix of the file name, else 0 — an old row, published before builds
 * existed, which is build 0 by definition.
 */
function buildOf(field: unknown, file: string): number {
  if (typeof field === 'number' && Number.isSafeInteger(field) && field >= 0) return field;
  const m = /-b(\d+)\.tar\.gz$/.exec(file);
  return m ? Number(m[1]) : 0;
}

/** `sha256sum` format: `<hex>  <file>` (or `<hex> *<file>`), one per line. */
function parseSums(bytes: Buffer): Map<string, string> {
  const out = new Map<string, string>();
  for (const raw of bytes.toString('utf8').split(/\r?\n/)) {
    const m = /^([0-9a-fA-F]{64})\s+\*?(\S.*)$/.exec(raw.trim());
    if (m) out.set(m[2]!, m[1]!.toLowerCase());
  }
  return out;
}

function minorOfVersion(version: string): string {
  const parts = version.split('.');
  return parts.length < 2 ? version : `${parts[0]}.${parts[1]}`;
}

/** The one row for a request, or `CHTYPES_ARTIFACT_UNPUBLISHED` naming what the release does have. */
export function selectArtifact(index: ReleaseIndex, platform: string, req: VersionRequest): IndexArtifact {
  const rows = forPlatform(index, platform);
  let hit: IndexArtifact[];
  if (req.exact !== null) {
    const typedChannel = CHANNEL.test(req.exact);
    hit = rows.filter(
      (a) => a.clickhouse_version === req.exact || (!typedChannel && a.clickhouse_version.replace(CHANNEL, '') === req.bare),
    );
    if (hit.length === 0) {
      throw new ArtifactUnpublishedError(
        `chtypes: you asked for exactly ClickHouse ${req.exact} on ${platform} and this release does not publish it ` +
          `(it has: ${rows.map((a) => a.clickhouse_version).join(', ')}). Ask for the line (${req.line}) to take what was published.`,
      );
    }
  } else {
    hit = rows.filter((a) => a.clickhouse_minor === req.line);
    if (hit.length === 0) {
      throw new ArtifactUnpublishedError(
        `chtypes: no artifact for ClickHouse line ${req.line} on ${platform} (it has: ${rows.map((a) => a.clickhouse_version).join(', ')})`,
      );
    }
  }
  // A release should not carry two patches of one line; if it does, the newer one.
  return hit.sort((a, b) => compareVersions(a.clickhouse_version, b.clickhouse_version))[hit.length - 1]!;
}

/**
 * Is `a` the row to prefer over `b`? Newest ClickHouse version first, and among
 * rows of the SAME version the highest wrapper build — a rebuild is a new row
 * beside the old one, so without the build tie-break the older build could win
 * on nothing but its position in the index.
 */
function newerRow(a: IndexArtifact, b: IndexArtifact): boolean {
  const byVersion = compareVersions(a.clickhouse_version, b.clickhouse_version);
  return byVersion !== 0 ? byVersion > 0 : a.build > b.build;
}

/** Every line the release publishes for the platform, newest patch per line, oldest line first. */
export function selectAll(index: ReleaseIndex, platform: string): IndexArtifact[] {
  const best = new Map<string, IndexArtifact>();
  for (const a of forPlatform(index, platform)) {
    const cur = best.get(a.clickhouse_minor);
    if (cur === undefined || newerRow(a, cur)) best.set(a.clickhouse_minor, a);
  }
  return [...best.values()].sort((a, b) => compareVersions(a.clickhouse_minor, b.clickhouse_minor));
}

function forPlatform(index: ReleaseIndex, platform: string): IndexArtifact[] {
  const [os, arch] = platform.split('-');
  const rows = index.artifacts.filter((a) => a.os === os && a.arch === arch);
  if (rows.length === 0) {
    const have = [...new Set(index.artifacts.map((a) => `${a.os}-${a.arch}`))].sort();
    throw new ArtifactUnpublishedError(
      `chtypes: the release has nothing for ${platform} (it has: ${have.length > 0 ? have.join(', ') : 'nothing'})`,
    );
  }
  return rows;
}

// -------------------------------------------------------------- ensure

/** Concurrent `ensure`s of one line in one process share one fetch (docs/fetch.md §6). */
const inFlight = new Map<string, Promise<EnsureResult>>();

/**
 * Make one ClickHouse line available in a registry directory, through the
 * verification chain, and say where it is. Idempotent: installed and
 * verified is a no-op that downloads nothing (`force` re-downloads).
 *
 * @param spelling - a line (`"25.8"`) or an exact patch (`"25.8.28.1-lts"`, a hard requirement).
 * @param options - see `EnsureOptions`; every §6 flag has a property here.
 * @returns where the line is and what it is — `dir` is `<registry>/<minor>`.
 * @throws {ArtifactUntrustedError} `CHTYPES_ARTIFACT_UNTRUSTED` — no signature, or none a trusted key verifies.
 * @throws {ArtifactCorruptError} `CHTYPES_ARTIFACT_CORRUPT` — any hash mismatch, a release that disagrees with itself.
 * @throws {ArtifactPinnedError} `CHTYPES_ARTIFACT_PINNED` — `frozen` and the lock says otherwise.
 * @throws {ArtifactUnpublishedError} `CHTYPES_ARTIFACT_UNPUBLISHED` — nothing for this platform/line/patch.
 * @throws {SourceUnreachableError} `CHTYPES_SOURCE_UNREACHABLE` — the source cannot be reached, or `offline` and not installed.
 * @throws {ChtypesError} for a spelling that is not a version, a bad platform key, or conflicting options.
 */
export async function ensure(spelling: string, options: EnsureOptions = {}): Promise<EnsureResult> {
  const req = parseVersionSpelling(spelling);
  const platform = resolvePlatform(options.platform);
  const dest = fetchDestination(options.dest, platform);
  const key = [dest, platform, req.exact ?? req.line, options.force ? 'force' : ''].join(' ');
  const running = inFlight.get(key);
  if (running !== undefined) return running;
  const p = ensureUncached(req, platform, dest, options).finally(() => {
    inFlight.delete(key);
  });
  inFlight.set(key, p);
  return p;
}

/** `fetch --all`: every line the release publishes for the platform, each through `ensure`'s chain. */
export async function ensureAll(options: EnsureOptions = {}): Promise<EnsureResult[]> {
  const platform = resolvePlatform(options.platform);
  const dest = fetchDestination(options.dest, platform);
  const emit = options.onProgress ?? noop;
  if (options.offline) throw new ChtypesError('chtypes: --all needs the release listing; it cannot run offline');
  const ctx = installContext(platform, dest, options);
  const source = openSource(options);
  emit({ type: 'status', message: `every published ClickHouse line, ${platform} -> ${dest}` });
  emit({ type: 'status', message: `source ${source.description}` });
  const release = await loadRelease(source, options, emit);
  const out: EnsureResult[] = [];
  for (const art of selectAll(release.index, platform)) out.push(await installOne(release, art, ctx));
  await installGoldens(release, ctx);
  return out;
}

/**
 * The served golden set: a release-level file like `index.json`, and a row in
 * the signed `SHA256SUMS` like a tarball, so it verifies through the same chain
 * and installs beside the artifacts as `<registry>/sdk-goldens.json` — where
 * every binding's golden test reads it offline.
 */
const GOLDENS_ASSET = 'sdk-goldens.json';

/**
 * Install the served golden set, if this release publishes one.
 *
 * Never throws. A release with no such row simply predates the served set, and a
 * set that cannot be written leaves the golden tests skipping loudly, which is
 * their job when there is nothing to read. What it will not do is install bytes
 * the signed `SHA256SUMS` does not describe.
 */
async function installGoldens(release: Release, ctx: InstallContext): Promise<void> {
  const want = release.sums.get(GOLDENS_ASSET);
  if (want === undefined) {
    ctx.emit({ type: 'status', message: `this release does not publish ${GOLDENS_ASSET} (the SDKs' golden tests will skip until it does)` });
    return;
  }
  const blob = await release.source.get(GOLDENS_ASSET);
  if (blob === null) {
    ctx.emit({ type: 'status', message: `SHA256SUMS lists ${GOLDENS_ASSET} but ${release.source.description} does not serve it — NOT installing it` });
    return;
  }
  const got = createHash('sha256').update(blob).digest('hex');
  if (got !== want) {
    ctx.emit({ type: 'status', message: `NOT installing ${GOLDENS_ASSET}: it hashes to ${got} but the signed SHA256SUMS says ${want}` });
    return;
  }
  const out = path.join(ctx.dest, GOLDENS_ASSET);
  try {
    await mkdir(ctx.dest, { recursive: true });
    await writeFile(out, blob);
  } catch (err) {
    ctx.emit({ type: 'status', message: `could not write ${GOLDENS_ASSET}: ${String(err)} — the golden tests will skip` });
    return;
  }
  ctx.emit({ type: 'status', message: `golden set verified and installed: ${out}` });
}

interface InstallContext {
  readonly platform: string;
  readonly dest: string;
  readonly lockPath: string | undefined;
  readonly lock: LockFile | null;
  readonly frozen: boolean;
  readonly force: boolean;
  readonly emit: (e: FetchEvent) => void;
}

function installContext(platform: string, dest: string, options: EnsureOptions): InstallContext {
  const frozen = options.frozen ?? false;
  // frozen without a lock path reads ./chtypes.lock (docs/fetch.md, Decisions).
  const lockPath =
    options.lock !== undefined && options.lock !== '' ? path.resolve(options.lock) : frozen ? path.resolve(DEFAULT_LOCK_FILE) : undefined;
  const lock = lockPath !== undefined ? readLock(lockPath) : null;
  if (frozen && lock === null) {
    throw new ArtifactPinnedError(`chtypes: ${lockPath} does not exist, and frozen refuses anything it does not pin`);
  }
  if (platform !== hostPlatform()) {
    warn(
      `chtypes: note — fetching ${platform} artifacts on a ${hostPlatform()} host. They are for a ${platform} process ` +
        `(a container, usually), not this one.`,
    );
  }
  return { platform, dest, lockPath, lock, frozen, force: options.force ?? false, emit: options.onProgress ?? noop };
}

async function ensureUncached(req: VersionRequest, platform: string, dest: string, options: EnsureOptions): Promise<EnsureResult> {
  const ctx = installContext(platform, dest, options);
  const install = path.join(dest, req.line);

  if (options.offline) {
    // Never touch the source: installed and hashing what its own manifest says is the whole test.
    const have = await installedLine(install);
    const pin = ctx.lock?.artifacts[`${platform}/${req.line}`];
    if (have !== null && have.ok && (!ctx.frozen || pin !== undefined)) {
      ctx.emit({ type: 'status', message: `offline — already installed and verified: ${install}` });
      return {
        dir: install,
        registry: dest,
        line: req.line,
        version: have.version,
        platform,
        file: pin?.file ?? '',
        sha256: pin?.sha256 ?? '',
        library: have.library,
        librarySha256: have.expected,
        installed: false,
        source: 'offline',
        signed: false,
        keyId: null,
      };
    }
    if (have !== null && have.ok && ctx.frozen) {
      throw new ArtifactPinnedError(`chtypes: ${ctx.lockPath} pins nothing for ${platform}/${req.line}, and frozen refuses anything it does not pin`);
    }
    throw new SourceUnreachableError(
      `chtypes: offline — ClickHouse ${req.line} (${platform}) is not installed and verified in ${dest}` +
        (have !== null && !have.ok ? ` (${have.problem})` : '') +
        ', and offline forbids fetching it',
    );
  }

  const source = openSource(options);
  ctx.emit({
    type: 'status',
    message: `ClickHouse ${req.spelling} -> line ${req.line}${req.exact !== null ? ` (exact ${req.exact})` : ''}, ${platform}`,
  });
  ctx.emit({ type: 'status', message: `source ${source.description}` });
  const release = await loadRelease(source, options, ctx.emit);
  const art = selectArtifact(release.index, platform, req);
  const installed = await installOne(release, art, ctx);
  await installGoldens(release, ctx);
  return installed;
}

/** One index row, from the release into `<dest>/<minor>/` — steps 2 through 4. */
async function installOne(release: Release, art: IndexArtifact, ctx: InstallContext): Promise<EnsureResult> {
  const { dest, platform, emit } = ctx;
  const minor = art.clickhouse_minor;
  const install = path.join(dest, minor);
  const lockKey = `${platform}/${minor}`;
  emit({ type: 'status', message: `${art.file}  (${art.bytes} bytes, ClickHouse ${art.clickhouse_version}, library ${art.library})` });

  // Step 2: the authentic SHA256SUMS must agree with index.json, byte for byte.
  const listed = release.sums.get(art.file);
  if (listed === undefined) throw new ArtifactCorruptError(`chtypes: SHA256SUMS has no line for ${art.file}`);
  if (listed !== art.sha256) {
    throw new ArtifactCorruptError(
      `chtypes: index.json says ${art.file} is ${art.sha256} but SHA256SUMS says ${listed} — the release disagrees with itself; not installing it`,
    );
  }

  // §5: a frozen lock refuses anything but what it pins.
  if (ctx.frozen && ctx.lock !== null) {
    const pin = ctx.lock.artifacts[lockKey];
    if (pin === undefined) {
      throw new ArtifactPinnedError(`chtypes: ${ctx.lockPath} pins nothing for ${lockKey}, and frozen refuses anything it does not pin`);
    }
    if (pin.file !== art.file || pin.sha256.toLowerCase() !== art.sha256) {
      throw new ArtifactPinnedError(
        `chtypes: ${ctx.lockPath} pins ${lockKey} to ${pin.file} (${pin.sha256}) but the release offers ${art.file} (${art.sha256}); frozen refuses it`,
      );
    }
  }

  const result = (installed: boolean): EnsureResult => ({
    dir: install,
    registry: dest,
    line: minor,
    version: art.clickhouse_version,
    platform,
    file: art.file,
    sha256: art.sha256,
    library: art.library,
    librarySha256: art.library_sha256,
    installed,
    source: release.source.description,
    signed: release.signed,
    keyId: release.keyId,
  });

  // Already installed and intact? The same hash the install path ends with,
  // so "already there" is a verified claim, not an inference from a directory.
  if (!ctx.force) {
    const libPath = path.join(install, art.library);
    if (existsSync(path.join(install, 'manifest.json')) && existsSync(libPath)) {
      const have = await sha256File(libPath);
      if (have === art.library_sha256) {
        emit({ type: 'status', message: `already installed and verified: ${libPath}` });
        if (ctx.lockPath !== undefined) writeLockEntry(ctx.lockPath, lockKey, { file: art.file, sha256: art.sha256 });
        return result(false);
      }
      emit({ type: 'status', message: `${libPath} is present but hashes ${have} (want ${art.library_sha256}) — replacing` });
    }
  }

  await mkdir(dest, { recursive: true });
  const tag = `${process.pid}.${randomBytes(4).toString('hex')}`;
  // Hidden siblings: the loader skips dot-directories, so a half-written
  // install is never scanned as a version.
  const tarball = path.join(dest, `.${minor}.download.${tag}`);
  const incoming = path.join(dest, `.${minor}.incoming.${tag}`);
  try {
    emit({ type: 'status', message: `downloading ${art.file}` });
    const got = await release.source.download(art.file, tarball, (received) =>
      emit({ type: 'download', file: art.file, received, total: art.bytes }),
    );
    if (!got) throw new SourceUnreachableError(`chtypes: could not download ${art.file} from ${release.source.description}`);

    // Step 3: size, then hash, before a byte is unpacked.
    const size = statSync(tarball).size;
    if (size !== art.bytes) throw new ArtifactCorruptError(`chtypes: ${art.file} is ${size} bytes, index.json says ${art.bytes}`);
    const sha = await sha256File(tarball);
    if (sha !== art.sha256) {
      throw new ArtifactCorruptError(`chtypes: ${art.file} FAILED its sha256: got ${sha}, want ${art.sha256} — not unpacking it`);
    }
    emit({ type: 'status', message: `sha256 verified before unpacking: ${sha}` });

    await mkdir(incoming);
    await extractTarGz(tarball, incoming);
    const manifest = readInstalledManifest(incoming);
    if (manifest === null) throw new ArtifactCorruptError(`chtypes: ${art.file} contains no usable manifest.json at its root`);
    // The index is a convenience; the manifest is the artifact's own claim
    // about itself. They must agree, or the index was built from a different artifact.
    if (
      manifest.library !== art.library ||
      manifest.version !== art.clickhouse_version ||
      manifest.minor !== minor ||
      manifest.librarySha256 !== art.library_sha256
    ) {
      throw new ArtifactCorruptError(
        `chtypes: manifest.json inside ${art.file} disagrees with index.json ` +
          `(${manifest.library}/${manifest.version}/${manifest.minor}/${manifest.librarySha256} vs ` +
          `${art.library}/${art.clickhouse_version}/${minor}/${art.library_sha256})`,
      );
    }
    if (!existsSync(path.join(incoming, manifest.library))) {
      throw new ArtifactCorruptError(`chtypes: ${art.file} names library ${manifest.library} but does not contain it`);
    }

    // Into place through a sibling: an interrupted install never leaves a
    // half-populated <minor>/ for the loader to dlopen.
    const replaced = path.join(dest, `.${minor}.replaced.${tag}`);
    if (existsSync(install)) await rename(install, replaced);
    await rename(incoming, install);
    await rm(replaced, { recursive: true, force: true });

    // Step 4: re-hash the installed file, in place. Everything up to here
    // proved the bytes were right somewhere else.
    const installedLib = path.join(install, manifest.library);
    const finalSha = await sha256File(installedLib);
    if (finalSha !== manifest.librarySha256) {
      const aside = path.join(dest, `.${minor}.corrupt.${tag}`);
      await rename(install, aside);
      throw new ArtifactCorruptError(
        `chtypes: installed ${installedLib} hashes ${finalSha}, manifest says ${manifest.librarySha256} — the install is bad; moved aside to ${aside}`,
      );
    }
    emit({ type: 'status', message: `installed and verified: ${install} (${manifest.library} sha256 ${finalSha})` });
    if (ctx.lockPath !== undefined) writeLockEntry(ctx.lockPath, lockKey, { file: art.file, sha256: art.sha256 });
    return result(true);
  } finally {
    await rm(tarball, { force: true });
    await rm(incoming, { recursive: true, force: true });
  }
}

// ------------------------------------------------------ verify and list

/**
 * `chtypes verify`: re-hash every installed line in a registry directory
 * against its own manifest. A line whose manifest cannot be read is not a
 * line (a scratch directory) and is skipped, as the loader skips it.
 */
export async function verifyInstalled(dest?: string, platform?: string): Promise<InstalledArtifact[]> {
  const dir = fetchDestination(dest, resolvePlatform(platform));
  const out: InstalledArtifact[] = [];
  for (const line of installedLines(dir)) {
    const state = await installedLine(path.join(dir, line));
    if (state !== null) out.push(state);
  }
  return out;
}

/** `chtypes list`: what is installed in the registry directory, and what the release offers for the platform. */
export async function listArtifacts(options: EnsureOptions = {}): Promise<ListResult> {
  const platform = resolvePlatform(options.platform);
  const dir = fetchDestination(options.dest, platform);
  const installed: InstalledArtifact[] = [];
  for (const line of installedLines(dir)) {
    const m = readInstalledManifest(path.join(dir, line));
    if (m === null) continue;
    const present = existsSync(path.join(dir, line, m.library));
    installed.push({
      line,
      dir: path.join(dir, line),
      version: m.version,
      library: m.library,
      expected: m.librarySha256,
      actual: null,
      ok: present,
      problem: present ? null : `${m.library} is missing`,
    });
  }
  if (options.offline) return { registry: dir, platform, installed, offered: null, source: null };
  const source = openSource(options);
  const release = await loadRelease(source, options, options.onProgress ?? noop);
  let offered: IndexArtifact[];
  try {
    offered = selectAll(release.index, platform);
  } catch (err) {
    if (!(err instanceof ArtifactUnpublishedError)) throw err;
    offered = [];
  }
  return { registry: dir, platform, installed, offered, source: source.description };
}

// ------------------------------------------------------------ the lock

/** Read a lock file; null when it does not exist. */
export function readLock(file: string): LockFile | null {
  let text: string;
  try {
    text = readFileSync(file, 'utf8');
  } catch (err) {
    if (isNoEntry(err)) return null;
    throw new ChtypesError(`chtypes: cannot read lock file ${file}: ${errorText(err)}`, { cause: err });
  }
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    throw new ChtypesError(`chtypes: lock file ${file} is not JSON: ${errorText(err)}`, { cause: err });
  }
  if (typeof doc !== 'object' || doc === null || (doc as Record<string, unknown>)['schema'] !== LOCK_SCHEMA) {
    throw new ChtypesError(`chtypes: lock file ${file} is not schema ${LOCK_SCHEMA}`);
  }
  const raw = (doc as Record<string, unknown>)['artifacts'];
  const artifacts: Record<string, LockEntry> = {};
  if (typeof raw === 'object' && raw !== null) {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof v !== 'object' || v === null) continue;
      const e = v as Record<string, unknown>;
      if (typeof e['file'] === 'string' && typeof e['sha256'] === 'string') artifacts[k] = { file: e['file'], sha256: e['sha256'] };
    }
  }
  return { schema: LOCK_SCHEMA, artifacts };
}

function writeLockEntry(file: string, key: string, entry: LockEntry): void {
  const current = readLock(file) ?? { schema: LOCK_SCHEMA, artifacts: {} };
  const merged: Record<string, LockEntry> = { ...current.artifacts, [key]: entry };
  const sorted = Object.fromEntries(Object.keys(merged).sort().map((k) => [k, merged[k]!]));
  writeFileSync(file, `${JSON.stringify({ schema: LOCK_SCHEMA, artifacts: sorted }, null, 2)}\n`);
}

// -------------------------------------------------------------- helpers

/** The platform a fetch is for: the option, else `CHTYPES_TARGET`, else this host. */
export function resolvePlatform(explicit?: string): string {
  const env = process.env['CHTYPES_TARGET'];
  const key = explicit !== undefined && explicit !== '' ? explicit : env !== undefined && env !== '' ? env : hostPlatform();
  if (!isPlatformKey(key)) throw new ChtypesError(`chtypes: not a known platform key: ${key} ((linux|darwin)-(arm64|amd64))`);
  return key;
}

interface InstalledManifest {
  readonly library: string;
  readonly version: string;
  readonly minor: string;
  readonly librarySha256: string;
}

function readInstalledManifest(dir: string): InstalledManifest | null {
  try {
    const m = JSON.parse(readFileSync(path.join(dir, 'manifest.json'), 'utf8')) as Record<string, unknown>;
    if (typeof m['library'] !== 'string' || m['library'] === '') return null;
    if (typeof m['library_sha256'] !== 'string' || m['library_sha256'] === '') return null;
    const version = typeof m['clickhouse_version'] === 'string' ? m['clickhouse_version'] : '';
    // clickhouse_minor is absent from the first Linux artifacts; deriving it
    // from clickhouse_version is what the loader does, so both agree.
    const minor = typeof m['clickhouse_minor'] === 'string' && m['clickhouse_minor'] !== '' ? m['clickhouse_minor'] : minorOfVersion(version);
    return { library: m['library'], version, minor, librarySha256: m['library_sha256'].toLowerCase() };
  } catch {
    return null;
  }
}

/** The state of one installed line directory: null when it is not one (no usable manifest). */
async function installedLine(dir: string): Promise<InstalledArtifact | null> {
  const m = readInstalledManifest(dir);
  if (m === null) return null;
  const line = path.basename(dir);
  const libPath = path.join(dir, m.library);
  if (!existsSync(libPath)) {
    return { line, dir, version: m.version, library: m.library, expected: m.librarySha256, actual: null, ok: false, problem: `${m.library} is missing` };
  }
  const actual = await sha256File(libPath);
  const ok = actual === m.librarySha256;
  return {
    line,
    dir,
    version: m.version,
    library: m.library,
    expected: m.librarySha256,
    actual,
    ok,
    problem: ok ? null : `${m.library} hashes ${actual}, manifest says ${m.librarySha256}`,
  };
}

function installedLines(dir: string): string[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return [];
  }
  return entries
    .filter((e) => !e.startsWith('.') && existsSync(path.join(dir, e, 'manifest.json')))
    .sort(compareVersions);
}

/** sha256 of a file, streamed — a 300 MB library never sits in memory whole. */
export async function sha256File(file: string): Promise<string> {
  const hash = createHash('sha256');
  await pipeline(createReadStream(file), async function* (chunks) {
    for await (const chunk of chunks) hash.update(chunk as Buffer);
  });
  return hash.digest('hex');
}

function progressTap(onReceived: (received: number) => void): Transform {
  let received = 0;
  return new Transform({
    transform(chunk: Buffer, _enc, cb) {
      received += chunk.length;
      onReceived(received);
      cb(null, chunk);
    },
  });
}

function envFlag(name: string): boolean {
  const v = process.env[name];
  return v !== undefined && ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
}

function isNoEntry(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: unknown }).code === 'ENOENT';
}

function errorText(err: unknown): string {
  if (!(err instanceof Error)) return String(err);
  const cause = (err as { cause?: unknown }).cause;
  return cause instanceof Error && cause.message !== err.message ? `${err.message}: ${cause.message}` : err.message;
}

function warn(message: string): void {
  process.stderr.write(`${message}\n`);
}

const noop = (): void => {};
