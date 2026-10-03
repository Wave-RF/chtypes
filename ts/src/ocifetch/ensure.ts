/**
 * The seam (`docs/guides/fetch-v1.md` §9): `ensure`, `resolveInstalled`,
 * `listInstalled`, `verifyInstalled`, `fetchSigned`. This is the only file
 * that wires resolve (`oci.ts`), trust (`dsse.ts`), bytes (`unpack.ts`), the
 * cache (`layout.ts`) and the lock (`lock.ts`) together; nothing outside
 * `index.ts` calls into those modules directly.
 */

import { randomBytes } from 'node:crypto';
import { unlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { asString, field, items, parseJsonValue } from '../json.js';
import {
  ABI_GENERATION,
  BASE_SEPARATOR,
  CACHE_ANNOTATION_PREFIX,
  DEFAULT_BASES,
  ENV_BASES_NAME,
  GOLDENS_ARTIFACT_TYPE,
  MEDIA_TYPE_BUNDLE,
  MEDIA_TYPE_MANIFEST,
  PREDICATE_TYPE_ARTIFACT,
  PREDICATE_TYPE_GOLDENS,
  RELEASE_KEYS,
  SPELLING_REGEX,
  TAGS_LIST_MAX_BYTES,
} from './constants.gen.js';
import { checkArtifactStatement, checkGenericStatement, parseStatement, verifyAnyReferrerBundle, verifyBundleSignature } from './dsse.js';
import {
  ArtifactCorruptError,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  FetchV1Error,
  SourceUnreachableError,
} from './errors.js';
import { readFileUrl, type RequestOptions, requestBuffered } from './http.js';
import {
  cacheRoot,
  commitStaging,
  ensureLayout,
  freshStagingDir,
  type IndexDescriptor,
  installBlob,
  listVerified,
  readIndexEntries,
  readVerifiedRecord,
  removeStaging,
  systemDirs,
  unpackedDir,
  upsertIndexEntry,
  type VerifiedRecord,
  writeVerifiedRecord,
} from './layout.js';
import { type GoldensCandidate, selectGoldens, statementRevision } from './goldens.js';
import { discoverSignatureCandidates } from './referrers.js';
import { verifyAndInstallFromLocalBlobs } from './localverify.js';
import { type Descriptor, fetchBlobBytesByDigest, fetchManifestByDigest, resolveTag } from './oci.js';
import { emptyLock, getPin, type LockFile, readLock, withPin, writeLock } from './lock.js';
import { digestOfHex, endpointUrl, hexOfDigest, platformInfo, realClock, resolvePlatformOption } from './types.js';
import type { ArtifactPredicate, FetchV1Options, PlatformKey, Resolved, TrustedKey, VerifyResult } from './types.js';
import { fetchVerifyAndUnpackLayer, verifyInstalledLibrary } from './unpack.js';

function defaultTrustedKeys(options: FetchV1Options): readonly TrustedKey[] {
  if (options.trustedKeys !== undefined) return options.trustedKeys;
  return RELEASE_KEYS.map((k) => ({ keyid: k.keyid, ed25519Hex: k.ed25519Hex }));
}

function resolveBases(options: FetchV1Options): readonly string[] {
  if (options.bases !== undefined && options.bases.length > 0) return options.bases;
  const env = process.env[ENV_BASES_NAME];
  if (env !== undefined && env !== '') {
    return env
      .split(BASE_SEPARATOR)
      .map((b) => b.trim())
      .filter((b) => b !== '');
  }
  return DEFAULT_BASES;
}

function tokenHostsFor(bases: readonly string[]): readonly string[] {
  const hosts: string[] = [];
  for (const b of bases) {
    if (b.startsWith('file:')) continue;
    try {
      hosts.push(new URL(b).host);
    } catch {
      // Not a URL this binding can parse as a host — never a token target.
    }
  }
  return hosts;
}

function requestOptionsFor(options: FetchV1Options, bases: readonly string[]): Omit<RequestOptions, 'maxBytes'> {
  return {
    clock: options.clock ?? realClock(),
    ...(options.token !== undefined ? { token: options.token } : {}),
    tokenHosts: tokenHostsFor(bases),
  };
}

/** Builds the `Resolved` the seam promises, from a `verified.json` record plus where it came from. */
function resolvedFromRecord(
  record: VerifiedRecord,
  platform: PlatformKey,
  request: string,
  dir: string,
  source: string,
  alreadyInstalled: boolean,
  warnings: readonly string[],
): Resolved {
  return {
    abiGeneration: ABI_GENERATION,
    platform,
    request,
    version: record.predicate.clickhouse_version,
    channel: record.predicate.channel,
    build: record.predicate.build,
    libraryPath: path.join(dir, record.library),
    dir,
    digests: { manifest: record.manifestDigest, layer: record.layerDigest, bundle: record.bundleDigest },
    predicate: record.predicate,
    signedBy: record.signedBy,
    source,
    alreadyInstalled,
    warnings,
  };
}

// ------------------------------------------------------------- resolveInstalled

/**
 * Never touches the network (guide §9). The source of truth is the
 * immutable `unpacked/sha256/*\/verified.json` records (guide §6) — never
 * `index.json` directly, though a pre-seeded `index.json` entry with no
 * `verified.json` of its own yet is verified and unpacked from local blobs
 * first (guide §1), exactly once, so every call after the first reads the
 * same way `verified.json`-backed entries always have. Picks the newest
 * match (version, then build, both compared as fixed-width strings per the
 * guide's monotonicity rule) among records whose platform matches and whose
 * `clickhouse_version` lies within `request`.
 */
export async function resolveInstalled(
  request: string,
  platform: PlatformKey,
  options: FetchV1Options = {},
): Promise<Resolved | undefined> {
  const root = cacheRoot(options.cacheDir);
  const trustedKeys = defaultTrustedKeys(options);
  await verifyPreseededEntries(root, root, platform, trustedKeys);
  for (const sysDir of systemDirs(options.systemDirs)) {
    await verifyPreseededEntries(sysDir, root, platform, trustedKeys);
  }

  const candidates: { record: VerifiedRecord; dir: string; source: string }[] = [];
  for (const { dir, record } of await listVerified(root)) {
    if (record.predicate.os !== platformInfo(platform).os || record.predicate.arch !== platformInfo(platform).architecture) continue;
    if (!withinRequest(record.predicate.clickhouse_version, request)) continue;
    candidates.push({ record, dir, source: 'cache' });
  }
  if (candidates.length === 0) return undefined;
  candidates.sort((a, b) => compareVersionThenBuild(b.record.predicate, a.record.predicate));
  const best = candidates[0]!;
  return resolvedFromRecord(best.record, platform, request, best.dir, best.source, true, []);
}

/**
 * For every `index.json` entry in `sourceDir` matching `platform` that has
 * no `verified.json` under `writeRoot` yet, attempts a local-blob
 * verify-and-install (`localverify.ts`), reading from `sourceDir` and
 * writing into `writeRoot` — the same directory for the user's own cache,
 * always `writeRoot` (never `sourceDir`) for a read-only system directory
 * (`system-dir-readonly`: "the system dir is never written"). Every
 * platform-matching entry is verified unconditionally (not pre-filtered by
 * the index's own unverified `org.opencontainers.image.ref.name`
 * annotation) — a real cache's pre-seeded set is small, and verifying one
 * extra irrelevant entry is cheap next to the alternative of trusting
 * unverified metadata to decide what is even worth checking.
 */
async function verifyPreseededEntries(
  sourceDir: string,
  writeRoot: string,
  platform: PlatformKey,
  trustedKeys: readonly TrustedKey[],
): Promise<void> {
  const info = platformInfo(platform);
  let entries: readonly IndexDescriptor[];
  try {
    entries = await readIndexEntries(sourceDir);
  } catch {
    return;
  }
  for (const entry of entries) {
    if (entry.platform !== undefined && (entry.platform.os !== info.os || entry.platform.architecture !== info.architecture)) continue;
    const already = await readVerifiedRecord(unpackedDir(writeRoot, hexOfDigest(entry.digest)));
    if (already !== undefined) continue;
    await verifyAndInstallFromLocalBlobs(sourceDir, writeRoot, entry.digest, platform, trustedKeys);
  }
}

function withinRequest(actualVersion: string, requestedSpelling: string): boolean {
  const actual = actualVersion.split('.');
  const requested = requestedSpelling.split('.');
  if (requested.length > actual.length) return false;
  for (let i = 0; i < requested.length; i++) if (requested[i] !== actual[i]) return false;
  return true;
}

/** Version first, then build — both as fixed-width strings, per the guide's monotonicity rule. */
function compareVersionThenBuild(a: ArtifactPredicate, b: ArtifactPredicate): number {
  const av = a.clickhouse_version.split('.').map(Number);
  const bv = b.clickhouse_version.split('.').map(Number);
  for (let i = 0; i < Math.max(av.length, bv.length); i++) {
    const diff = (av[i] ?? 0) - (bv[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return a.build.localeCompare(b.build);
}

/**
 * `monotonic-warning`: installing a version/build **lower** than one
 * already installed for the same platform is not an error (requests float
 * to a specific registry answer, and a mirror or a rollback can legitimately
 * offer an older build) — but it is surprising enough to warn about loudly,
 * once, naming nothing more specific than that it happened.
 */
async function checkMonotonic(
  root: string,
  platform: PlatformKey,
  incoming: ArtifactPredicate,
): Promise<{ readonly warning: string; readonly existing: VerifiedRecord; readonly dir: string } | undefined> {
  const info = platformInfo(platform);
  for (const { dir, record } of await listVerified(root)) {
    if (record.predicate.os !== info.os || record.predicate.arch !== info.architecture) continue;
    if (compareVersionThenBuild(incoming, record.predicate) < 0) {
      return {
        warning: 'chtypes: the registry offered a version/build older than one already installed for this platform (monotonic warning); keeping the newer install',
        existing: record,
        dir,
      };
    }
  }
  return undefined;
}

// ------------------------------------------------------------------ listInstalled

export async function listInstalled(options: FetchV1Options = {}): Promise<readonly Resolved[]> {
  const root = cacheRoot(options.cacheDir);
  const out: Resolved[] = [];
  for (const { dir, record } of await listVerified(root)) {
    const platform = `${record.predicate.os}-${record.predicate.arch}` as PlatformKey;
    out.push(resolvedFromRecord(record, platform, record.predicate.clickhouse_version, dir, 'cache', true, []));
  }
  return out;
}

// ----------------------------------------------------------------- verifyInstalled

/** Re-checks every installed library's bytes against its own `verified.json` record (guide §9). */
export async function verifyInstalled(options: FetchV1Options = {}): Promise<readonly VerifyResult[]> {
  const root = cacheRoot(options.cacheDir);
  const out: VerifyResult[] = [];
  for (const { dir, record } of await listVerified(root)) {
    const platform = `${record.predicate.os}-${record.predicate.arch}` as PlatformKey;
    try {
      await verifyInstalledLibrary(path.join(dir, record.library), record.predicate.library_sha256, record.predicate.library_bytes);
      out.push({ platform, dir, ok: true, detail: '' });
    } catch (err) {
      out.push({ platform, dir, ok: false, detail: err instanceof Error ? err.message : String(err) });
    }
  }
  return out;
}

// ------------------------------------------------------------------------ ensure

/**
 * `ensure(request, options)`: resolve unless `frozen` or `offline`, verify
 * trust, download and unpack bytes, install into the cache, optionally write
 * a lock entry. See `docs/guides/fetch-v1.md` §9 for the full contract.
 */
export async function ensure(request: string, options: FetchV1Options = {}): Promise<Resolved> {
  const platform = resolvePlatformOption(options.platform, os.platform(), os.arch());
  const root = cacheRoot(options.cacheDir);
  await ensureLayout(root);

  if (options.offline === true) {
    const hit = await resolveInstalled(request, platform, options);
    if (hit === undefined) {
      throw new ArtifactMissingError(`chtypes: ${request} (${platform}) is not installed, and --offline forbids fetching it`);
    }
    return hit;
  }

  if (options.frozen === true) {
    return ensureFrozen(request, platform, options);
  }

  const bases = resolveBases(options);
  const baseReqOptions = requestOptionsFor(options, bases);
  const trustedKeys = defaultTrustedKeys(options);

  const resolveResult = await resolveTag(bases, request, platform, { ...baseReqOptions, maxBytes: 0 });
  const manifestHex = hexOfDigest(resolveResult.manifest.digest);
  const finalDir = unpackedDir(root, manifestHex);

  const existing = await readVerifiedRecord(finalDir);
  let record: VerifiedRecord;
  let recordDir = finalDir;
  let warnings: string[] = [];
  let alreadyInstalled: boolean;

  if (existing !== undefined) {
    record = existing;
    alreadyInstalled = true;
  } else {
    const info = platformInfo(platform);
    const trust = await verifyAnyReferrerBundle(
      resolveResult.repositoryRoot,
      resolveResult.manifest.digest,
      MEDIA_TYPE_BUNDLE,
      trustedKeys,
      (statement) =>
        checkArtifactStatement(statement, PREDICATE_TYPE_ARTIFACT, resolveResult.manifest.layer.digest, {
          os: info.os,
          arch: info.architecture,
          requestedSpelling: request,
        }),
      { ...baseReqOptions, maxBytes: 0 },
    );

    let predicate: ArtifactPredicate;
    let signedBy: string;
    if (trust === undefined) {
      if (options.allowUnsigned !== true) {
        throw new ArtifactUntrustedError(
          `chtypes: no referrer of ${resolveResult.manifest.digest} verified under a trusted key (CHTYPES_ALLOW_UNSIGNED=1 to proceed anyway)`,
        );
      }
      predicate = await unsignedPredicateFallback(resolveResult.repositoryRoot, resolveResult.manifest, request, info, baseReqOptions);
      signedBy = '';
      warnings = [...warnings, 'chtypes: proceeding with an unsigned artifact (CHTYPES_ALLOW_UNSIGNED)'];
    } else {
      predicate = trust.statement.predicate as unknown as ArtifactPredicate;
      signedBy = trust.signedBy;
    }

    const monotonic = await checkMonotonic(root, platform, predicate);
    if (monotonic !== undefined) {
      // The registry is offering something OLDER than what is already
      // installed for this platform (`monotonic-warning`): warn loudly, but
      // never replace a newer install with an older one it was not asked to
      // roll back to — hand back the existing, newer record instead of
      // downloading and installing the one just resolved.
      record = monotonic.existing;
      recordDir = monotonic.dir;
      alreadyInstalled = true;
      warnings = [...warnings, monotonic.warning];
    } else {
      const staging = await freshStagingDir(root);
      const tempLayerPath = path.join(root, `.tmp-layer-${process.pid}-${randomBytes(6).toString('hex')}`);
      try {
        const entries = await fetchVerifyAndUnpackLayer(
          [resolveResult.repositoryRoot],
          resolveResult.manifest.layer,
          tempLayerPath,
          staging,
          { ...baseReqOptions, maxBytes: 0 },
        );
        if (!entries.some((e) => e.name === predicate.library)) {
          throw new ArtifactUnpublishedError(`chtypes: the layer does not contain its own predicate's library ${JSON.stringify(predicate.library)}`);
        }
        // Nothing signed names the library's hash or size on the unsigned
        // path (`CHTYPES_ALLOW_UNSIGNED`), so there is nothing to compare to.
        if (trust !== undefined) {
          await verifyInstalledLibrary(path.join(staging, predicate.library), predicate.library_sha256, predicate.library_bytes);
        }
        const newRecord: VerifiedRecord = {
          schema: 1,
          manifestDigest: resolveResult.manifest.digest,
          layerDigest: resolveResult.manifest.layer.digest,
          bundleDigest: trust === undefined ? '' : trust.bundleDigest,
          signedBy,
          predicate,
          library: predicate.library,
        };
        await writeVerifiedRecord(staging, newRecord);
        await commitStaging(staging, finalDir);
        record = newRecord;
      } catch (err) {
        await removeStaging(staging);
        throw err;
      } finally {
        await unlink(tempLayerPath).catch(() => {});
      }

      const manifestDescriptor: Descriptor = { mediaType: MEDIA_TYPE_MANIFEST, digest: resolveResult.manifest.digest };
      await upsertIndexEntry(
        root,
        { ...manifestDescriptor, size: resolveResult.manifest.bytes.length, annotations: { [`${CACHE_ANNOTATION_PREFIX}request`]: request } },
        options.beforeIndexRename,
      );
      alreadyInstalled = false;
    }
  }

  if (options.lockWrite === true) {
    await writeLockEntry(request, platform, record, options, resolveResult.repositoryRoot, resolveResult.indexBytes, baseReqOptions);
  }
  if (options.update === true) {
    // `update` re-resolves every locked request and rewrites the lock from
    // scratch (guide §6: it never merges a stale entry with a fresh one), in
    // addition to resolving the request above.
    await updateLock(options, bases, baseReqOptions, trustedKeys);
  }

  return resolvedFromRecord(record, platform, request, recordDir, resolveResult.repositoryRoot, alreadyInstalled, warnings);
}

/**
 * `CHTYPES_ALLOW_UNSIGNED`'s fallback metadata source: the tarball's own
 * `manifest.json`, which is also the OCI **config** blob and (per
 * `layout-v2.md` §4.1) carries the same fields as the predicate, value-equal
 * — read here unverified, since there is nothing to verify it against once
 * signing itself is being skipped. Missing or unreadable: the fields fall
 * back to the request itself, and `library` to `libchtypes.so`/`.dylib` by
 * platform, which is what every fixture library is actually named.
 */
async function unsignedPredicateFallback(
  repositoryRoot: string,
  manifest: { readonly config: Descriptor | undefined },
  request: string,
  info: { readonly os: string; readonly architecture: string },
  baseReqOptions: Omit<RequestOptions, 'maxBytes'>,
): Promise<ArtifactPredicate> {
  const fallbackLibrary = info.os === 'darwin' ? 'libchtypes.dylib' : 'libchtypes.so';
  const fallback: ArtifactPredicate = {
    abi: ABI_GENERATION,
    abi_fingerprint: '',
    clickhouse_version: request,
    channel: '',
    clickhouse_minor: '',
    clickhouse_commit: '',
    os: info.os,
    arch: info.architecture,
    build: '',
    core_commit: '',
    inputs_sha256: '',
    library: fallbackLibrary,
    library_sha256: '',
    library_bytes: 0,
  };
  if (manifest.config === undefined) return fallback;
  try {
    const bytes = await fetchBlobBytesByDigest([repositoryRoot], manifest.config, { ...baseReqOptions, maxBytes: 0 });
    const json = parseJsonValue(bytes);
    if (json === null) return fallback;
    return {
      ...fallback,
      clickhouse_version: asString(field(json, 'clickhouse_version')) || fallback.clickhouse_version,
      channel: asString(field(json, 'channel')),
      build: asString(field(json, 'build')),
      library: asString(field(json, 'library')) || fallback.library,
      library_sha256: asString(field(json, 'library_sha256')),
      library_bytes: Number(asString(field(json, 'library_bytes'))) || 0,
    };
  } catch {
    return fallback;
  }
}

async function ensureFrozen(request: string, platform: PlatformKey, options: FetchV1Options): Promise<Resolved> {
  const root = cacheRoot(options.cacheDir);
  const lockPath = options.lockPath ?? path.join(process.cwd(), 'chtypes.lock');
  const lock = await readLock(lockPath);
  if (lock === undefined) {
    throw new ArtifactPinnedError(`chtypes: --frozen with no lock file at ${lockPath}; run \`fetch --lock\` first`);
  }
  const pin = getPin(lock, request, platform);
  if (pin === undefined) {
    throw new ArtifactPinnedError(`chtypes: ${lockPath} names no entry for ${request}/${platform}; re-lock with \`fetch --lock\``);
  }
  const bases = resolveBases(options);
  const baseReqOptions = requestOptionsFor(options, bases);
  const trustedKeys = defaultTrustedKeys(options);
  const info = platformInfo(platform);

  // `--frozen` skips resolution and discovery entirely (guide §6): every
  // request is by digest — the manifest, the layer and the bundle blob all
  // come from the lock's own pin, from any configured base.
  const manifest = await fetchManifestByDigest(bases, pin.manifest, { ...baseReqOptions, maxBytes: 0 });
  if (manifest.layer.digest !== pin.layer) {
    throw new ArtifactCorruptError(`chtypes: manifest ${manifest.digest}'s layer does not match the locked digest ${pin.layer}`);
  }
  const manifestHex = hexOfDigest(manifest.digest);
  const finalDir = unpackedDir(root, manifestHex);
  const existing = await readVerifiedRecord(finalDir);
  if (existing !== undefined) {
    return resolvedFromRecord(existing, platform, request, finalDir, 'cache', true, []);
  }

  const bundleBytes = await fetchBlobBytesByDigest(bases, { mediaType: MEDIA_TYPE_BUNDLE, digest: pin.bundle }, { ...baseReqOptions, maxBytes: 0 });
  const verified = verifyBundleSignature(bundleBytes, trustedKeys);
  let predicate: ArtifactPredicate;
  let signedBy: string;
  let warnings: string[] = [];
  if (verified === undefined) {
    if (options.allowUnsigned !== true) {
      throw new ArtifactUntrustedError(`chtypes: the locked bundle ${pin.bundle} does not verify under a trusted key`);
    }
    predicate = await unsignedPredicateFallback(bases[0]!, manifest, request, info, baseReqOptions);
    signedBy = '';
    warnings = ['chtypes: proceeding with an unsigned artifact (CHTYPES_ALLOW_UNSIGNED)'];
  } else {
    predicate = checkArtifactStatement(parseStatement(verified.payload), PREDICATE_TYPE_ARTIFACT, manifest.layer.digest, {
      os: info.os,
      arch: info.architecture,
      requestedSpelling: request,
    });
    signedBy = verified.signedBy;
  }

  const staging = await freshStagingDir(root);
  const tempLayerPath = path.join(root, `.tmp-layer-${process.pid}-${randomBytes(6).toString('hex')}`);
  try {
    const entries = await fetchVerifyAndUnpackLayer(bases, manifest.layer, tempLayerPath, staging, { ...baseReqOptions, maxBytes: 0 });
    if (!entries.some((e) => e.name === predicate.library)) {
      throw new ArtifactUnpublishedError(`chtypes: the layer does not contain its own predicate's library ${JSON.stringify(predicate.library)}`);
    }
    if (verified !== undefined) {
      await verifyInstalledLibrary(path.join(staging, predicate.library), predicate.library_sha256, predicate.library_bytes);
    }
    const record: VerifiedRecord = {
      schema: 1,
      manifestDigest: manifest.digest,
      layerDigest: manifest.layer.digest,
      bundleDigest: verified === undefined ? '' : pin.bundle,
      signedBy,
      predicate,
      library: predicate.library,
    };
    await writeVerifiedRecord(staging, record);
    await commitStaging(staging, finalDir);
    return resolvedFromRecord(record, platform, request, finalDir, bases[0]!, false, warnings);
  } catch (err) {
    await removeStaging(staging);
    throw err;
  } finally {
    await unlink(tempLayerPath).catch(() => {});
  }
}

/**
 * `update`: re-resolve every (request, platform) pair the existing lock
 * names against the current index and rewrite the lock from that fresh set
 * alone — never a merge with a stale entry (guide §6). A request with no
 * platform that verifies is dropped rather than carried over stale.
 */
async function updateLock(
  options: FetchV1Options,
  bases: readonly string[],
  baseReqOptions: Omit<RequestOptions, 'maxBytes'>,
  trustedKeys: readonly TrustedKey[],
): Promise<void> {
  const lockPath = options.lockPath ?? path.join(process.cwd(), 'chtypes.lock');
  const existing = await readLock(lockPath);
  if (existing === undefined) throw new ArtifactPinnedError(`chtypes: update with no lock file at ${lockPath}`);
  let fresh: LockFile = emptyLock();
  for (const [spelling, byPlatform] of Object.entries(existing.requests)) {
    for (const key of Object.keys(byPlatform)) {
      const platformKey = key as PlatformKey;
      const info = platformInfo(platformKey);
      const resolved = await resolveTag(bases, spelling, platformKey, { ...baseReqOptions, maxBytes: 0 });
      const trust = await verifyAnyReferrerBundle(
        resolved.repositoryRoot,
        resolved.manifest.digest,
        MEDIA_TYPE_BUNDLE,
        trustedKeys,
        (statement) =>
          checkArtifactStatement(statement, PREDICATE_TYPE_ARTIFACT, resolved.manifest.layer.digest, {
            os: info.os,
            arch: info.architecture,
            requestedSpelling: spelling,
          }),
        { ...baseReqOptions, maxBytes: 0 },
      );
      if (trust === undefined) continue;
      const predicate = trust.statement.predicate as unknown as ArtifactPredicate;
      fresh = withPin(fresh, spelling, platformKey, {
        version: predicate.clickhouse_version,
        build: predicate.build,
        manifest: resolved.manifest.digest,
        layer: resolved.manifest.layer.digest,
        bundle: trust.bundleDigest,
      });
    }
  }
  await writeLock(lockPath, fresh);
}

async function writeLockEntry(
  request: string,
  platform: PlatformKey,
  record: VerifiedRecord,
  options: FetchV1Options,
  repositoryRoot: string,
  indexBytes: Buffer,
  baseReqOptions: Omit<RequestOptions, 'maxBytes'>,
): Promise<void> {
  const lockPath = options.lockPath ?? path.join(process.cwd(), 'chtypes.lock');
  let lock: LockFile = (await readLock(lockPath)) ?? emptyLock();
  lock = withPin(lock, request, platform, {
    version: record.predicate.clickhouse_version,
    build: record.predicate.build,
    manifest: record.manifestDigest,
    layer: record.layerDigest,
    bundle: record.bundleDigest,
  });

  if (options.lockAllPlatforms === true) {
    const indexJson = parseJsonValue(indexBytes);
    const manifests = items(field(indexJson ?? undefined, 'manifests'));
    const trustedKeys = defaultTrustedKeys(options);
    for (const m of manifests) {
      const p = field(m, 'platform');
      const key = `${asString(field(p, 'os'))}-${asString(field(p, 'architecture'))}`;
      if (key === platform) continue; // already pinned above, with its layer downloaded
      if (!isLikelyPlatformKey(key)) continue;
      const digest = asString(field(m, 'digest'));
      if (digest === '') continue;
      try {
        const otherManifest = await fetchManifestByDigest([repositoryRoot], digest, { ...baseReqOptions, maxBytes: 0 });
        const otherInfo = platformInfo(key as PlatformKey);
        const trust = await verifyAnyReferrerBundle(
          repositoryRoot,
          otherManifest.digest,
          MEDIA_TYPE_BUNDLE,
          trustedKeys,
          (statement) =>
            checkArtifactStatement(statement, PREDICATE_TYPE_ARTIFACT, otherManifest.layer.digest, {
              os: otherInfo.os,
              arch: otherInfo.architecture,
              requestedSpelling: request,
            }),
          { ...baseReqOptions, maxBytes: 0 },
        );
        if (trust === undefined) continue;
        const otherPredicate = trust.statement.predicate as unknown as ArtifactPredicate;
        lock = withPin(lock, request, key as PlatformKey, {
          version: otherPredicate.clickhouse_version,
          build: otherPredicate.build,
          manifest: otherManifest.digest,
          layer: otherManifest.layer.digest,
          bundle: trust.bundleDigest,
        });
      } catch {
        // Best-effort: a platform whose bundle cannot be fetched/verified is
        // left out of the lock rather than failing the whole write.
      }
    }
  }

  await writeLock(lockPath, lock);
}

function isLikelyPlatformKey(key: string): boolean {
  return key === 'linux-amd64' || key === 'linux-arm64' || key === 'darwin-arm64';
}

// ------------------------------------------------------------------ fetchSigned

export interface FetchSignedResult {
  readonly path: string;
  readonly statement: unknown;
  readonly digests: { readonly manifest: string; readonly layer: string };
}

/**
 * The generic signed-artifact fetch (guide §9): `repository` is a full
 * repository root (like a base — see `types.ts`'s `endpointUrl`), `ref` a
 * tag or digest. Resolves the manifest, downloads and verifies its single
 * layer, and verifies a referrer bundle of `predicateType` against it —
 * `predicateType` and subject digest only (`checkGenericStatement`): these
 * objects' predicate shapes are not yet specified (blocked on the producer,
 * plan §1.2), so the caller reads `statement.predicate` itself. Returns the
 * verified blob's path on disk (written into this layout's `blobs/`, not
 * unpacked — unpacking is this fetch's caller's job, since goldens and
 * fixtures are not necessarily tarballs).
 *
 * For `predicateType` = `PREDICATE_TYPE_GOLDENS`, `ref` is instead a
 * PLATFORM manifest digest: the registry is append-only, so a corrected
 * goldens set is a second goldens referrer of it, and the result is the
 * highest-`revision` verified one (`goldens.ts`, guide §9).
 */
export async function fetchSigned(
  repository: string,
  ref: string,
  predicateType: string,
  options: FetchV1Options = {},
): Promise<FetchSignedResult> {
  const root = cacheRoot(options.cacheDir);
  await ensureLayout(root);
  const baseReqOptions = requestOptionsFor(options, [repository]);
  const trustedKeys = defaultTrustedKeys(options);

  if (predicateType === PREDICATE_TYPE_GOLDENS) {
    return fetchGoldens(repository, ref, root, trustedKeys, options.allowUnsigned === true, { ...baseReqOptions, maxBytes: 0 });
  }

  const manifest = await fetchManifestByDigest([repository], ref, { ...baseReqOptions, maxBytes: 0 });
  const layerBytes = await fetchBlobBytesByDigest([repository], manifest.layer, { ...baseReqOptions, maxBytes: 0 });

  const trust = await verifyAnyReferrerBundle(
    repository,
    manifest.digest,
    MEDIA_TYPE_BUNDLE,
    trustedKeys,
    (statement) => checkGenericStatement(statement, predicateType, manifest.layer.digest),
    { ...baseReqOptions, maxBytes: 0 },
  );
  if (trust === undefined && options.allowUnsigned !== true) {
    throw new ArtifactUntrustedError(`chtypes: no referrer of ${manifest.digest} verified under a trusted key`);
  }

  const dest = await installBlob(root, hexOfDigest(manifest.layer.digest), layerBytes);
  return {
    path: dest,
    statement: trust?.statement,
    digests: { manifest: manifest.digest, layer: manifest.layer.digest },
  };
}

/**
 * `fetchSigned` for the goldens predicate type: the highest-revision verified
 * goldens referrer of the platform manifest `subject`.
 *
 * Every candidate is verified on its own (its blob against its descriptor,
 * its own signature referrer, the statement's subject against the blob
 * digest, the predicateType). A candidate that fails is skipped: never
 * chosen, never a tie. If none verifies, the first failure is thrown. A
 * VERIFIED candidate whose `revision` is unusable is not skipped: it could be
 * the newest document, and quietly choosing an older set is the stale pick
 * this rule exists to prevent.
 */
async function fetchGoldens(
  repository: string,
  subject: string,
  root: string,
  trustedKeys: readonly TrustedKey[],
  allowUnsigned: boolean,
  reqOptions: RequestOptions,
): Promise<FetchSignedResult> {
  if (!/^sha256:[0-9a-f]{64}$/.test(subject)) {
    throw new ArtifactCorruptError(`chtypes: a goldens fetch names a platform manifest by digest, not ${JSON.stringify(subject)}`);
  }
  const listed = await discoverSignatureCandidates(repository, subject, GOLDENS_ARTIFACT_TYPE, reqOptions);
  if (listed.length === 0) throw new ArtifactUnpublishedError(`chtypes: no goldens referrer for ${subject}`);

  const verified: GoldensCandidate[] = [];
  const unsigned: { manifest: string; blob: string; bytes: Buffer }[] = [];
  let firstFailure: unknown;
  const note = (err: unknown): void => {
    firstFailure ??= err;
  };
  const seen = new Set<string>();

  for (const referrer of listed) {
    if (seen.has(referrer.digest)) continue;
    seen.add(referrer.digest);
    let blob: Buffer;
    let blobDigest: string;
    try {
      const manifest = await fetchManifestByDigest([repository], referrer.digest, reqOptions);
      blobDigest = manifest.layer.digest;
      blob = await fetchBlobBytesByDigest([repository], manifest.layer, reqOptions);
    } catch (err) {
      if (!(err instanceof FetchV1Error)) throw err;
      note(err);
      continue;
    }
    unsigned.push({ manifest: referrer.digest, blob: blobDigest, bytes: blob });

    let found: GoldensCandidate | undefined;
    const bundles = await discoverSignatureCandidates(repository, referrer.digest, MEDIA_TYPE_BUNDLE, reqOptions);
    for (const bundleRef of bundles) {
      let bundleBytes: Buffer;
      let bundleDigest: string;
      try {
        const bundleManifest = await fetchManifestByDigest([repository], bundleRef.digest, reqOptions);
        bundleDigest = bundleManifest.layer.digest;
        bundleBytes = await fetchBlobBytesByDigest([repository], bundleManifest.layer, reqOptions);
      } catch {
        continue;
      }
      const signed = verifyBundleSignature(bundleBytes, trustedKeys);
      if (signed === undefined) {
        note(new ArtifactUntrustedError(`chtypes: no goldens signature of ${referrer.digest} verifies under a trusted key`));
        continue;
      }
      let statement: ReturnType<typeof parseStatement>;
      try {
        statement = parseStatement(signed.payload);
        checkGenericStatement(statement, PREDICATE_TYPE_GOLDENS, blobDigest);
      } catch (err) {
        if (!(err instanceof ArtifactCorruptError)) throw err;
        note(err);
        continue;
      }
      found = {
        manifest: referrer.digest,
        blob: blobDigest,
        revision: statementRevision(signed.payload),
        bytes: blob,
        predicate: statement.predicate,
        signedBy: signed.signedBy,
        bundle: bundleDigest,
      };
      break;
    }
    if (found !== undefined) verified.push(found);
  }

  let chosen: { manifest: string; blob: string; bytes: Buffer; predicate: unknown };
  if (verified.length > 0) {
    chosen = selectGoldens(verified);
  } else if (allowUnsigned && unsigned.length === 1) {
    chosen = { ...unsigned[0]!, predicate: undefined };
  } else if (allowUnsigned && unsigned.length > 1) {
    throw new ArtifactCorruptError(
      `chtypes: ${unsigned.length} unsigned goldens documents of ${subject}: no signature to read a revision from, so none can be chosen`,
    );
  } else if (firstFailure !== undefined) {
    throw firstFailure;
  } else {
    throw new ArtifactUntrustedError(`chtypes: no goldens referrer of ${subject} verifies under a trusted key`);
  }

  const dest = await installBlob(root, hexOfDigest(chosen.blob), chosen.bytes);
  return { path: dest, statement: chosen.predicate, digests: { manifest: chosen.manifest, layer: chosen.blob } };
}

// ---------------------------------------------------------------------- listTags

/** Compares two version spellings component by component, numerically. */
function compareSpellings(a: string, b: string): number {
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? -1) - (pb[i] ?? -1);
    if (d !== 0) return d;
  }
  return 0;
}

/**
 * The version spellings a repository publishes: `GET tags/list` from the first
 * base that has the repository, every tag that is not a version spelling
 * (`SPELLING_REGEX`: the `sha256-<hex>` referrers fallback tags, symbolic tags)
 * dropped, the rest in ascending numeric order. A tag names a version, not a
 * platform: whether a platform is offered is that tag's index, which only a
 * resolve reads. A next page is followed through the `Link` header.
 */
export async function listTags(options: FetchV1Options = {}): Promise<readonly string[]> {
  const bases = resolveBases(options);
  const reqOptions = { ...requestOptionsFor(options, bases), maxBytes: TAGS_LIST_MAX_BYTES };
  const spelling = new RegExp(SPELLING_REGEX);
  let lastUnreachable: SourceUnreachableError | undefined;
  for (let i = 0; i < bases.length; i++) {
    const base = bases[i]!;
    const isLast = i === bases.length - 1;
    const tags = new Set<string>();
    let url: string | undefined = endpointUrl(base, '/tags/list');
    let found = false;
    for (let page = 0; url !== undefined && page < 64; page++) {
      let res: Awaited<ReturnType<typeof requestBuffered>>;
      try {
        res = url.startsWith('file:') ? await readFileUrl(url, TAGS_LIST_MAX_BYTES) : await requestBuffered(url, reqOptions);
      } catch (err) {
        if (err instanceof SourceUnreachableError && !isLast) {
          lastUnreachable = err;
          url = undefined;
          break;
        }
        throw err;
      }
      if (res.status === 404) {
        url = undefined;
        break;
      }
      if (res.status !== 200) throw new SourceUnreachableError(`chtypes: ${url} returned status ${res.status}`);
      const json = parseJsonValue(res.body);
      if (json === null) throw new ArtifactCorruptError(`chtypes: ${url} did not return valid JSON`);
      found = true;
      for (const t of items(field(json, 'tags'))) {
        const tag = asString(t);
        if (spelling.test(tag)) tags.add(tag);
      }
      const link = res.headers['link'];
      const next = link === undefined ? undefined : /<([^>]+)>\s*;\s*rel="?next"?/.exec(link)?.[1];
      url = next === undefined ? undefined : new URL(next, url).toString();
    }
    if (found) return [...tags].sort(compareSpellings);
  }
  if (lastUnreachable !== undefined) throw lastUnreachable;
  throw new ArtifactUnpublishedError(`chtypes: no base serves a tag list (${bases.join(', ')})`);
}
