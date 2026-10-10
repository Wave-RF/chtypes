/**
 * The seam (`docs/guides/fetch-v1.md` §9): `ensure`, `resolveInstalled`,
 * `listInstalled`, `verifyInstalled`, `fetchSigned`. This is the only file
 * that wires resolve (`oci.ts`), trust (`dsse.ts`), bytes (`unpack.ts`), the
 * cache (`layout.ts`) and the lock (`lock.ts`) together; nothing outside
 * `index.ts` calls into those modules directly. `resolve.ts` (`chtypes
 * resolve`) takes the same resolve and trust steps, through
 * `verifyManifestTrust`, and `prune.ts` (`chtypes prune`) reads and removes
 * cache entries under the in-use hold (`hold.ts`).
 */

import { randomBytes } from 'node:crypto';
import { unlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { asString, field, items, parseJsonValue } from '../json.js';
import { activeChannel, aheadOfRegistry, allowUnsigned, noteIgnoredOverrides, offlineMode, refusePinning, visibleToChannel, warnIgnored } from './channel.js';
import {
  BASE_SEPARATOR,
  CACHE_ANNOTATION_PREFIX,
  ENV_BASES_NAME,
  GOLDENS_ARTIFACT_TYPE,
  MEDIA_TYPE_BUNDLE,
  MEDIA_TYPE_MANIFEST,
  PREDICATE_TYPE_ARTIFACT,
  PREDICATE_TYPE_GOLDENS,
  SPELLING_REGEX,
  TAGS_LIST_MAX_BYTES,
} from './constants.gen.js';
import { checkArtifactStatement, checkGenericStatement, parseStatement, type TrustResult, verifyAnyReferrerBundle, verifyBundleSignature } from './dsse.js';
import {
  ArtifactCorruptError,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  FetchV1Error,
  SourceRetiredError,
  SourceUnreachableError,
} from './errors.js';
import { readFileUrl, type RequestOptions, requestBuffered } from './http.js';
import {
  buildRecord,
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
  zeroXHint,
} from './layout.js';
import { type GoldensCandidate, selectGoldens, statementRevision } from './goldens.js';
import { discoverSignatureCandidates } from './referrers.js';
import { probeRoots, strictMode, unwritable } from './faults.js';
import { verifyAndInstallFromLocalBlobs } from './localverify.js';
import { type Descriptor, fetchBlobBytesByDigest, fetchManifestByDigest, type ManifestInfo, resolveTag } from './oci.js';
import { emptyLock, getPin, type LockFile, type LockPin, readLock, withPin, writeLock } from './lock.js';
import { digestOfHex, endpointUrl, hexOfDigest, platformInfo, realClock, resolvePlatformOption } from './types.js';
import type { ArtifactPredicate, FetchV1Options, PlatformKey, Resolved, TrustedKey, VerifyResult } from './types.js';
import { fetchVerifyAndUnpackLayer, measureLibrary, verifyInstalledLibrary } from './unpack.js';

/** The trust list: the caller's when the active contract honors one, else the contract's own (the staging key alone on the dev channel, rule r6). */
function defaultTrustedKeys(options: FetchV1Options): readonly TrustedKey[] {
  const channel = activeChannel();
  if (channel.overridable && options.trustedKeys !== undefined) return options.trustedKeys;
  return channel.keys;
}

/** The bases: the caller's or `CHTYPES_ARTIFACTS_URL` when the active contract honors them, else the contract's own (the staging dev channel alone, rule r6). */
export function resolveBases(options: FetchV1Options): readonly string[] {
  const channel = activeChannel();
  if (!channel.overridable) return channel.bases;
  if (options.bases !== undefined && options.bases.length > 0) return options.bases;
  const env = process.env[ENV_BASES_NAME];
  if (env !== undefined && env !== '') {
    return env
      .split(BASE_SEPARATOR)
      .map((b) => b.trim())
      .filter((b) => b !== '');
  }
  return channel.bases;
}

/**
 * The bases, the trust list and the unsigned decision a call with `options`
 * reads under the active contract: exactly what `ensure`, `fetchSigned` and
 * `listTags` use. Not part of the package's entry point; the dev channel's
 * rule r6 tests read it, so they test the derivation rather than restate it.
 */
export function effectiveFetchOptions(options: FetchV1Options): {
  readonly bases: readonly string[];
  readonly trustedKeys: readonly TrustedKey[];
  readonly allowUnsigned: boolean;
} {
  return { bases: resolveBases(options), trustedKeys: defaultTrustedKeys(options), allowUnsigned: allowUnsigned(options) };
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

/** The request options every fetch over `bases` sends: the clock, and the token for the configured base hosts only. */
export function requestOptionsFor(options: FetchV1Options, bases: readonly string[]): Omit<RequestOptions, 'maxBytes'> {
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
    abiGeneration: activeChannel().abi,
    platform,
    request,
    version: record.version,
    ...(record.channel !== null ? { channel: record.channel } : {}),
    build: record.build,
    libraryPath: path.join(dir, record.library),
    dir,
    digests: {
      ...(record.indexDigest !== null ? { index: record.indexDigest } : {}),
      manifest: record.manifestDigest,
      layer: record.layerDigest,
      bundle: record.bundleDigest ?? '',
      ...(record.bundleManifestDigest !== null ? { bundleManifest: record.bundleManifestDigest } : {}),
    },
    predicate: record.predicate,
    signedBy: record.signedBy ?? '',
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
  noteIgnoredOverrides(options);
  const root = cacheRoot(options.cacheDir);
  const warnings = await probeRoots(
    searchRoots(options).map((r) => r.root),
    strictMode(options.strictCache),
  );
  const trustedKeys = defaultTrustedKeys(options);
  await verifyPreseededEntries(root, root, platform, trustedKeys);
  for (const sysDir of systemDirs(options.systemDirs)) {
    await verifyPreseededEntries(sysDir, root, platform, trustedKeys);
  }

  // One root order (guide §1; public issue #486): the cache, then every
  // system directory in order, read the same way. The newest (version,
  // build) among every record that answers the request wins, and a tie goes
  // to the earlier root.
  const candidates: { record: VerifiedRecord; dir: string; source: string; rank: number }[] = [];
  for (const [rank, r] of searchRoots(options).entries()) {
    for (const { dir, record } of await listVerified(r.root)) {
      if (record.platform !== platform) continue;
      if (!withinRequest(record.version, request)) continue;
      // Under the dev channel, only its own fingerprint's builds (`visibleToChannel`).
      if (!visibleToChannel(record.predicate)) continue;
      candidates.push({ record, dir, source: r.source, rank });
    }
  }
  if (candidates.length === 0) return undefined;
  candidates.sort((a, b) => compareRecords(b.record, a.record) || a.rank - b.rank);
  const best = candidates[0]!;
  return resolvedFromRecord(best.record, platform, request, best.dir, best.source, true, warnings);
}

/**
 * The build id of the newest cached record of this SDK's OWN fingerprint that
 * answers the request (`resolveInstalled`'s filter), `''` when it names none,
 * or `undefined` when the cache holds no such record (public issue #581).
 */
async function cachedOwnBuild(options: FetchV1Options, platform: PlatformKey, request: string): Promise<string | undefined> {
  let best: VerifiedRecord | undefined;
  for (const r of searchRoots(options)) {
    for (const { record } of await listVerified(r.root)) {
      if (record.platform !== platform) continue;
      if (!withinRequest(record.version, request)) continue;
      if (!visibleToChannel(record.predicate)) continue;
      if (best === undefined || compareRecords(record, best) > 0) best = record;
    }
  }
  return best === undefined ? undefined : best.build;
}

/** The cache, then every system directory in order: the one search order every lookup uses. */
function searchRoots(options: FetchV1Options): readonly { readonly root: string; readonly source: string }[] {
  return [{ root: cacheRoot(options.cacheDir), source: 'cache' }, ...systemDirs(options.systemDirs).map((d) => ({ root: d, source: `system:${d}` }))];
}

/** The directories every lookup reads, in order: the cache root, then each system directory (public issue #530). It reads and creates nothing. */
export function searchDirs(options: FetchV1Options = {}): readonly string[] {
  return searchRoots(options).map((r) => r.root);
}

/** Two records by (version, build): the version numerically, part by part, then the fixed-width build. */
function compareRecords(a: VerifiedRecord, b: VerifiedRecord): number {
  const av = a.version.split('.').map(Number);
  const bv = b.version.split('.').map(Number);
  for (let i = 0; i < Math.max(av.length, bv.length); i++) {
    const diff = (av[i] ?? 0) - (bv[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return a.build < b.build ? -1 : a.build > b.build ? 1 : 0;
}

/**
 * What a `CHTYPES_ARTIFACT_MISSING` answer from the cache `options` names adds
 * to its message, each a complete sentence: the 0.x hint when the cache is a
 * 0.x registry directory (guide, "Upgrading from 0.x"; public issue #486). The
 * registry's own MISSING adds the same notes as the offline fetch's.
 */
export async function missingNotes(options: FetchV1Options = {}): Promise<readonly string[]> {
  const notes = await probeRoots(
    searchRoots(options).map((r) => r.root),
    false,
  );
  const hint = await zeroXHint(cacheRoot(options.cacheDir));
  return hint === undefined ? notes : [...notes, hint];
}

/** Checks the cache and its system dirs by the mode `options` asks for: the default mode's warnings, or strict mode's `CacheUnusableError`. `chtypes where --strict` is this check. */
export async function probeCache(options: FetchV1Options = {}): Promise<readonly string[]> {
  return probeRoots(
    searchRoots(options).map((r) => r.root),
    strictMode(options.strictCache),
  );
}

/** `message` with `notes`, each a complete sentence, appended. */
export function withNotes(message: string, notes: readonly string[]): string {
  return notes.length === 0 ? message : `${message}. ${notes.join(' ')}`;
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
    await verifyAndInstallFromLocalBlobs(sourceDir, writeRoot, entry.digest, platform, trustedKeys, true);
  }
}

/**
 * Whether a library whose own `clickhouse_version` is `version` answers
 * `request`: equal to an exact (four-part) request, or within a floating
 * one. A request that is not a version spelling (an arbitrary tag) names no
 * version, so it constrains nothing and this returns true. The registry
 * uses it to refuse a library outside its request, whatever the cache did
 * (guide §9; public issue #481).
 */
export function satisfiesRequest(request: string, version: string): boolean {
  if (!new RegExp(SPELLING_REGEX).test(request)) return true;
  return withinRequest(version, request);
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
 * `monotonic-warning`: the registry offering a version/build **lower** than
 * one already installed WITHIN THE REQUEST is not an error (a mirror or a
 * rollback can legitimately offer an older build), but the newer install is
 * kept, with a warning, rather than replaced by an older one it was not
 * asked to roll back to. Returns the newest such install.
 *
 * Scoped to the request (guide §9; public issue #481): an install of
 * another line never answers a line request, and an exact request is
 * answered only by that exact version, so for it only a newer BUILD of the
 * same version counts. A request that is not a version spelling (an
 * arbitrary tag) names no range, so nothing is kept for it.
 *
 * Under the dev channel only an install of its own fingerprint counts
 * (`visibleToChannel`): a newer build of another fingerprint, installed by
 * another dev SDK, is never kept over this one's own.
 */
async function checkMonotonic(
  root: string,
  platform: PlatformKey,
  request: string,
  incoming: ArtifactPredicate,
): Promise<{ readonly warning: string; readonly existing: VerifiedRecord; readonly dir: string } | undefined> {
  if (!new RegExp(SPELLING_REGEX).test(request)) return undefined;
  let best: { record: VerifiedRecord; dir: string } | undefined;
  for (const { dir, record } of await listVerified(root)) {
    if (record.platform !== platform) continue;
    if (!withinRequest(record.version, request)) continue;
    if (!visibleToChannel(record.predicate)) continue;
    if (compareVersionThenBuild(incoming, record.predicate) >= 0) continue;
    if (best === undefined || compareVersionThenBuild(best.record.predicate, record.predicate) < 0) best = { record, dir };
  }
  if (best === undefined) return undefined;
  return {
    warning: `chtypes: the registry offered ${incoming.clickhouse_version} build ${incoming.build}, older than ${best.record.version} build ${best.record.build} already installed within ${request} (monotonic warning); keeping the newer install`,
    existing: best.record,
    dir: best.dir,
  };
}

// ------------------------------------------------------------------ listInstalled

export async function listInstalled(options: FetchV1Options = {}): Promise<readonly Resolved[]> {
  noteIgnoredOverrides(options);
  const warnings = await probeCache(options);
  const out: Resolved[] = [];
  for (const r of searchRoots(options)) {
    for (const { dir, record } of await listVerified(r.root)) {
      const platform = record.platform as PlatformKey;
      out.push(resolvedFromRecord(record, platform, record.version, dir, r.source, true, warnings));
    }
  }
  return out;
}

// ----------------------------------------------------------------- verifyInstalled

/** Re-checks every installed library's bytes against its own `verified.json` record (guide §9). */
export async function verifyInstalled(options: FetchV1Options = {}): Promise<readonly VerifyResult[]> {
  noteIgnoredOverrides(options);
  await probeCache(options);
  const out: VerifyResult[] = [];
  for (const r of searchRoots(options)) {
    for (const { dir, record } of await listVerified(r.root)) {
      const platform = record.platform as PlatformKey;
      try {
        await verifyInstalledLibrary(path.join(dir, record.library), record.librarySha256, record.libraryBytes);
        out.push({ platform, dir, ok: true, detail: '' });
      } catch (err) {
        out.push({ platform, dir, ok: false, detail: err instanceof Error ? err.message : String(err) });
      }
    }
  }
  return out;
}

// ------------------------------------------------------------------------ ensure

/**
 * `ensure(request, options)`: resolve unless `frozen` or `offline`, verify
 * trust, download and unpack bytes, install into the cache, optionally write
 * a lock entry. See `docs/guides/fetch-v1.md` §9 for the full contract. On the
 * dev channel (`./channel.ts`) every pinning option is refused first, before
 * any request, and every base, trust or unsigned override is ignored with one
 * warning (spec/abi-v2/docs.md, rule r6).
 */
export async function ensure(request: string, options: FetchV1Options = {}): Promise<Resolved> {
  refusePinning(options);
  noteIgnoredOverrides(options);
  try {
    return await ensureIn(request, options);
  } catch (err) {
    // A write the fetch layer needed that failed, under the cache: one class
    // in every mode, never a raw Node error (public issue #486).
    throw await unwritable(err, cacheRoot(options.cacheDir));
  }
}

async function ensureIn(request: string, options: FetchV1Options): Promise<Resolved> {
  const platform = resolvePlatformOption(options.platform, os.platform(), os.arch());
  const root = cacheRoot(options.cacheDir);

  // An offline lookup is read-only: it creates nothing, so a read-only mount
  // reads cleanly (guide §6; public issue #486). Only a fetch makes the layout.
  if (offlineMode(options.offline)) {
    if (options.frozen === true) return resolveFrozenOffline(request, platform, options);
    const hit = await resolveInstalled(request, platform, options);
    if (hit === undefined) {
      throw new ArtifactMissingError(
        withNotes(`chtypes: ${request} (${platform}) is not installed, and --offline forbids fetching it`, await missingNotes(options)),
      );
    }
    return hit;
  }
  if (strictMode(options.strictCache)) {
    // Strict: the cache is checked before anything is fetched into it.
    await probeCache(options);
  }
  await ensureLayout(root);

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
    // An install of another fingerprint's build, reached through the tag
    // because the registry has none at this SDK's own, is never reported
    // installed (public issue #578): refused before the cache is touched.
    await aheadOfRegistry(resolveResult.aliasAbsent, existing.predicate, () => cachedOwnBuild(options, platform, request));
    record = existing;
    alreadyInstalled = true;
    // A NEWER build within the request, installed beside it, is still the
    // answer (`monotonic-warning`, guide §9).
    const monotonic = await checkMonotonic(root, platform, request, {
      ...existing.predicate,
      clickhouse_version: existing.version,
      build: existing.build,
    });
    if (monotonic !== undefined) {
      record = monotonic.existing;
      recordDir = monotonic.dir;
      warnings = [...warnings, monotonic.warning];
    }
  } else {
    const verified = await verifyManifestTrust(
      resolveResult.repositoryRoot,
      resolveResult.manifest,
      resolveResult.aliasAbsent,
      platform,
      request,
      options,
      baseReqOptions,
    );
    const { trust, predicate, signedBy } = verified;
    warnings = [...warnings, ...verified.warnings];

    const monotonic = await checkMonotonic(root, platform, request, predicate);
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
      let lostRace = false;
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
        const newRecord = buildRecord({
          platform,
          predicate,
          library: await measureLibrary(path.join(staging, predicate.library)),
          indexDigest: resolveResult.indexDigest,
          manifestDigest: resolveResult.manifest.digest,
          layerDigest: resolveResult.manifest.layer.digest,
          bundleDigest: trust === undefined ? null : trust.bundleDigest,
          bundleManifestDigest: trust === undefined ? null : trust.bundleManifestDigest,
          signedBy: trust === undefined ? null : signedBy,
        });
        await writeVerifiedRecord(staging, newRecord);
        // True when another process installed this build first: its entry is kept.
        lostRace = await commitStaging(staging, finalDir);
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
      alreadyInstalled = lostRace;
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

/** What `verifyManifestTrust` established for one platform manifest. */
export interface ManifestTrust {
  /** The verified signature, or `undefined` when the call proceeds unsigned (allow-unsigned). */
  readonly trust: TrustResult | undefined;
  /** The signed predicate, or under allow-unsigned the config blob's (`unsignedPredicateFallback`). */
  readonly predicate: ArtifactPredicate;
  /** The key id that verified the signature, `''` when unsigned. */
  readonly signedBy: string;
  readonly warnings: readonly string[];
}

/**
 * The trust step of a fetch for one platform manifest (guide §4), exactly as
 * `ensure` takes it before anything is downloaded: a signature referrer of the
 * manifest verified under the trust list, its statement checked against the
 * manifest's layer, the platform and `request`; no signature that verifies is
 * `ArtifactUntrustedError` unless allow-unsigned is honored, when the config
 * blob's metadata stands in with a warning. A signed build is then checked by
 * the dev channel's ahead-of-registry rule (public issue #578: the alias for
 * its own fingerprint answered 404 on every base, and the tag's build is
 * signed for another), refused from the signed statement, before any layer is
 * requested. `chtypes resolve` takes the same step for every platform.
 */
export async function verifyManifestTrust(
  repositoryRoot: string,
  manifest: ManifestInfo,
  aliasAbsent: boolean,
  platform: PlatformKey,
  request: string,
  options: FetchV1Options,
  baseReqOptions: Omit<RequestOptions, 'maxBytes'>,
): Promise<ManifestTrust> {
  const info = platformInfo(platform);
  const trust = await verifyAnyReferrerBundle(
    repositoryRoot,
    manifest.digest,
    MEDIA_TYPE_BUNDLE,
    defaultTrustedKeys(options),
    (statement) =>
      checkArtifactStatement(statement, PREDICATE_TYPE_ARTIFACT, manifest.layer.digest, {
        os: info.os,
        arch: info.architecture,
        requestedSpelling: request,
      }),
    { ...baseReqOptions, maxBytes: 0 },
  );
  if (trust === undefined) {
    if (!allowUnsigned(options)) {
      throw new ArtifactUntrustedError(
        activeChannel().overridable
          ? `chtypes: no referrer of ${manifest.digest} verified under a trusted key (CHTYPES_ALLOW_UNSIGNED=1 to proceed anyway)`
          : `chtypes: no referrer of ${manifest.digest} verified under the staging key ${activeChannel().keys.map((k) => k.keyid).join(', ')}`,
      );
    }
    const predicate = await unsignedPredicateFallback(repositoryRoot, manifest, request, info, baseReqOptions);
    return { trust, predicate, signedBy: '', warnings: ['chtypes: proceeding with an unsigned artifact (CHTYPES_ALLOW_UNSIGNED)'] };
  }
  const predicate = trust.statement.predicate as unknown as ArtifactPredicate;
  await aheadOfRegistry(aliasAbsent, predicate, () => cachedOwnBuild(options, platform, request));
  return { trust, predicate, signedBy: trust.signedBy, warnings: [] };
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
    abi: activeChannel().abi,
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
  } catch (err) {
    // A retired repository (a 410, guide §2) is the answer, never a fallback.
    if (err instanceof SourceRetiredError) throw err;
    return fallback;
  }
}

/**
 * The lock's pin for `request`/`platform`: no lock file, or a lock with no
 * entry for them, is `CHTYPES_ARTIFACT_PINNED`.
 */
async function lockedPin(request: string, platform: PlatformKey, options: FetchV1Options): Promise<LockPin> {
  const lockPath = options.lockPath ?? path.join(process.cwd(), 'chtypes.lock');
  const lock = await readLock(lockPath);
  if (lock === undefined) {
    throw new ArtifactPinnedError(`chtypes: --frozen with no lock file at ${lockPath}; run \`fetch --lock\` first`);
  }
  const pin = getPin(lock, request, platform);
  if (pin === undefined) {
    throw new ArtifactPinnedError(`chtypes: ${lockPath} names no entry for ${request}/${platform}; re-lock with \`fetch --lock\``);
  }
  return pin;
}

/**
 * The installed build a lock pin names, with no network: an unpacked
 * directory named by the pinned manifest digest, in the cache or a system
 * directory, whose `verified.json` is for this platform and records exactly
 * the pinned manifest, layer and bundle digests (public issues #414, #487).
 * `undefined` when no installed build matches.
 */
async function installedByPin(request: string, platform: PlatformKey, options: FetchV1Options, pin: LockPin): Promise<Resolved | undefined> {
  return (await lookupByPin(request, platform, options, pin)).hit;
}

/** `installedByPin`, also saying whether a record exists under the pinned manifest but disagrees with the pin. */
async function lookupByPin(
  request: string,
  platform: PlatformKey,
  options: FetchV1Options,
  pin: LockPin,
): Promise<{ readonly hit: Resolved | undefined; readonly mismatch: boolean }> {
  let mismatch = false;
  const warnings = await probeRoots(
    searchRoots(options).map((r) => r.root),
    strictMode(options.strictCache),
  );
  for (const r of searchRoots(options)) {
    const dir = unpackedDir(r.root, hexOfDigest(pin.manifest));
    const record = await readVerifiedRecord(dir);
    if (record === undefined) continue;
    if (
      record.platform === platform &&
      record.manifestDigest === pin.manifest &&
      record.layerDigest === pin.layer &&
      record.bundleDigest === pin.bundle
    ) {
      return { hit: resolvedFromRecord(record, platform, request, dir, r.source, true, warnings), mismatch: false };
    }
    mismatch = true;
  }
  return { hit: undefined, mismatch };
}

/** `--frozen --offline`: verify the installed build against the lock, with zero network. No lock entry, or an installed record that does not match the pin, is `CHTYPES_ARTIFACT_PINNED`; a pinned build that is not installed is `CHTYPES_ARTIFACT_MISSING`. */
async function resolveFrozenOffline(request: string, platform: PlatformKey, options: FetchV1Options): Promise<Resolved> {
  const pin = await lockedPin(request, platform, options);
  const { hit, mismatch } = await lookupByPin(request, platform, options, pin);
  if (hit === undefined) {
    if (mismatch) {
      throw new ArtifactPinnedError(`chtypes: --frozen --offline: the installed build for ${request} (${platform}) does not match the lock's pin ${pin.manifest}`);
    }
    throw new ArtifactMissingError(
      withNotes(
        `chtypes: --frozen --offline: the build the lock pins (${pin.manifest}) is not installed for ${request} (${platform})`,
        await missingNotes(options),
      ),
    );
  }
  return hit;
}

async function ensureFrozen(request: string, platform: PlatformKey, options: FetchV1Options): Promise<Resolved> {
  const root = cacheRoot(options.cacheDir);
  const pin = await lockedPin(request, platform, options);
  // A build already installed under exactly the pinned digests is used as it
  // is: zero requests (guide §6; public issues #414 and #487).
  const installed = await installedByPin(request, platform, options, pin);
  if (installed !== undefined) return installed;
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
    if (!allowUnsigned(options)) {
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
    const record = buildRecord({
      platform,
      predicate,
      library: await measureLibrary(path.join(staging, predicate.library)),
      indexDigest: null,
      manifestDigest: manifest.digest,
      layerDigest: manifest.layer.digest,
      bundleDigest: verified === undefined ? null : pin.bundle,
      bundleManifestDigest: null,
      signedBy: verified === undefined ? null : signedBy,
    });
    await writeVerifiedRecord(staging, record);
    const lostRace = await commitStaging(staging, finalDir);
    return resolvedFromRecord(record, platform, request, finalDir, bases[0]!, lostRace, warnings);
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
    version: record.version,
    build: record.build,
    manifest: record.manifestDigest,
    layer: record.layerDigest,
    bundle: record.bundleDigest ?? '',
  });

  if (options.lockAllPlatforms !== false) {
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
      } catch (err) {
        // A retired repository (a 410, guide §2) fails the write: it is
        // permanent, never a platform to leave out.
        if (err instanceof SourceRetiredError) throw err;
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
  callerRepository: string,
  ref: string,
  predicateType: string,
  options: FetchV1Options = {},
): Promise<FetchSignedResult> {
  noteIgnoredOverrides(options);
  const repository = channelRepository(callerRepository);
  const root = cacheRoot(options.cacheDir);
  await ensureLayout(root);
  const baseReqOptions = requestOptionsFor(options, [repository]);
  const trustedKeys = defaultTrustedKeys(options);

  if (predicateType === PREDICATE_TYPE_GOLDENS) {
    return fetchGoldens(repository, ref, root, trustedKeys, allowUnsigned(options), { ...baseReqOptions, maxBytes: 0 });
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
  if (trust === undefined && !allowUnsigned(options)) {
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
 * The repository `fetchSigned` reads: the caller's when the active contract
 * honors a base, else the contract's own, with one warning when the caller
 * named another (the dev channel's goldens are its own release's referrers,
 * signed with the staging key; rule r6).
 */
function channelRepository(callerRepository: string): string {
  const channel = activeChannel();
  if (channel.overridable) return callerRepository;
  const own = channel.bases[0] as string;
  if (callerRepository !== '' && callerRepository !== own) warnIgnored("fetchSigned's repository argument");
  return own;
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
      // A retired repository (a 410, guide §2) is the answer, never a
      // candidate to pass over.
      if (!(err instanceof FetchV1Error) || err instanceof SourceRetiredError) throw err;
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
      } catch (err) {
        if (err instanceof SourceRetiredError) throw err;
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

/** The published lines: exactly two numeric parts, no leading zeros (Go's `lineTag`). */
const LINE_SPELLING = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;

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
 * The lines a repository publishes: `GET tags/list` from the first base that
 * has the repository, every tag that is not a two-part line (three- and
 * four-part versions, aliases, the `sha256-<hex>` referrers fallback tags,
 * symbolic tags) dropped, the rest in ascending numeric order. A tag names a
 * line, not a platform: whether a platform is offered is that tag's index,
 * which only a resolve reads. A next page is followed through the `Link`
 * header.
 */
export async function listTags(options: FetchV1Options = {}): Promise<readonly string[]> {
  noteIgnoredOverrides(options);
  const bases = resolveBases(options);
  const reqOptions = { ...requestOptionsFor(options, bases), maxBytes: TAGS_LIST_MAX_BYTES };
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
        if (LINE_SPELLING.test(tag)) tags.add(tag);
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
