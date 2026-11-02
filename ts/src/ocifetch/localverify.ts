/**
 * Verifying a pre-seeded cache entry from local blobs alone, zero network
 * (`docs/guides/fetch-v1.md` §1, §6): "a pre-seeded layout has entries in
 * `index.json` with no corresponding `unpacked/` directory yet; the first
 * request for one verifies it against its signature exactly as a freshly
 * downloaded layer would, then unpacks it." `--offline`/`resolve_installed`
 * are the only callers — never `ensure`'s normal online path, which always
 * has a live referrers API to ask.
 *
 * There is no referrers API to query here, so trust discovery is a local
 * scan: every blob under `blobs/sha256/` that parses as an OCI manifest
 * with the signature bundle's own `artifactType` and names the platform
 * manifest as its `subject` is a *candidate* referrer of it, tried the same
 * way `dsse.ts`'s `verifyAnyReferrerBundle` tries referrers fetched over the
 * network — stopping at the first whose signature verifies AND whose
 * statement names this exact platform manifest's layer as its subject. The
 * scan runs once per lookup, never once per entry, and never reads a layer
 * (`localSignatureReferrers`; public issue #690). `oras copy -r
 * --to-oci-layout` is what populates these blobs when pulling from a real
 * registry (guide §1); `index.json` itself carries no referrers listing (no
 * such thing exists in the OCI image-layout spec), which is exactly why
 * this is a scan rather than a lookup.
 */

import { rm } from 'node:fs/promises';
import path from 'node:path';
import { MANIFEST_MAX_BYTES, MEDIA_TYPE_BUNDLE, PREDICATE_TYPE_ARTIFACT } from './constants.gen.js';
import { visibleToChannel } from './channel.js';
import { checkArtifactStatement, parseStatement, verifyBundleSignature } from './dsse.js';
import { ArtifactCorruptError } from './errors.js';
import {
  buildRecord,
  commitStaging,
  freshStagingDir,
  listLocalBlobHexes,
  localBlobSize,
  readLocalBlob,
  readVerifiedRecord,
  unpackedDir,
  type VerifiedRecord,
  writeVerifiedRecord,
} from './layout.js';
import { type Descriptor, parseManifest, verifyDescriptor } from './oci.js';
import type { ArtifactPredicate, PlatformKey, Statement, TrustedKey } from './types.js';
import { digestOfHex, hexOfDigest, platformInfo } from './types.js';
import { measureLibrary, verifyAndUnpackLayerBytes, verifyInstalledLibrary } from './unpack.js';

/**
 * Verifies and installs the platform manifest named by `manifestDigest`,
 * reading every blob it needs from `blobsSourceDir`'s `blobs/sha256/` but
 * always **writing** the result (the unpacked library and its
 * `verified.json`) under `writeRoot` — the two differ for a read-only
 * system directory (guide §1: "never written to"), where `blobsSourceDir`
 * is that system dir and `writeRoot` is the user's own cache.
 *
 * Idempotent and cheap on a repeat call: if `writeRoot` already has a
 * `verified.json` for this digest, returns it without touching
 * `blobsSourceDir` again. Returns `undefined` (never throws) when this
 * specific entry cannot be locally verified — a bad or incomplete
 * pre-seeded blob is "not usable", not a hard failure for whatever broader
 * scan is trying several candidates. The caller decides whether the
 * resulting predicate's own `clickhouse_version` fits whatever request
 * prompted the scan — this function verifies the entry on its own terms,
 * not against any one request.
 */
export async function verifyAndInstallFromLocalBlobs(
  blobsSourceDir: string,
  writeRoot: string,
  manifestDigest: string,
  platform: PlatformKey,
  trustedKeys: readonly TrustedKey[],
  /** A lookup's own install: another fingerprint's build is never installed (`visibleToChannel`). */
  visibleOnly = false,
  /** The layout's signature referrers, when the caller has already scanned for them (`localSignatureReferrers`). */
  referrers?: LocalReferrers,
): Promise<VerifiedRecord | undefined> {
  const manifestHex = hexOfDigest(manifestDigest);
  const existing = await readVerifiedRecord(unpackedDir(writeRoot, manifestHex));
  if (existing !== undefined) return existing;

  try {
    const manifestBytes = await readLocalBlob(blobsSourceDir, manifestHex);
    if (manifestBytes === undefined) return undefined;
    verifyDescriptor({ mediaType: '', digest: manifestDigest }, manifestBytes, `manifest ${manifestDigest}`);
    const manifest = parseManifest(manifestDigest, manifestBytes);

    const info = platformInfo(platform);
    const refs = referrers ?? (await localSignatureReferrers(blobsSourceDir, new Set([hexOfDigest(manifest.layer.digest)])));
    const found = await findLocalSignature(blobsSourceDir, refs.get(manifestDigest) ?? [], manifest.layer, trustedKeys);
    if (found === undefined) return undefined;
    const predicate: ArtifactPredicate = checkArtifactStatement(found.statement, PREDICATE_TYPE_ARTIFACT, manifest.layer.digest, {
      os: info.os,
      arch: info.architecture,
      // No `requestedSpelling`: this entry is verified on its own terms,
      // not against one request — see this function's own doc comment.
    });
    if (visibleOnly && !visibleToChannel(predicate)) return undefined;

    const layerBytes = await readLocalBlob(blobsSourceDir, hexOfDigest(manifest.layer.digest));
    if (layerBytes === undefined) return undefined;

    const staging = await freshStagingDir(writeRoot);
    try {
      const entries = await verifyAndUnpackLayerBytes(layerBytes, manifest.layer, staging);
      if (!entries.some((e) => e.name === predicate.library)) {
        throw new ArtifactCorruptError(
          `chtypes: the local layer does not contain its own predicate's library ${JSON.stringify(predicate.library)}`,
        );
      }
      await verifyInstalledLibrary(path.join(staging, predicate.library), predicate.library_sha256, predicate.library_bytes);
      const record = buildRecord({
        platform,
        predicate,
        library: await measureLibrary(path.join(staging, predicate.library)),
        indexDigest: null,
        manifestDigest,
        layerDigest: manifest.layer.digest,
        bundleDigest: found.bundleDigest,
        bundleManifestDigest: found.bundleManifestDigest,
        signedBy: found.signedBy,
      });
      await writeVerifiedRecord(staging, record);
      await commitStaging(staging, unpackedDir(writeRoot, manifestHex));
      return record;
    } catch {
      await rm(staging, { recursive: true, force: true }).catch(() => {});
      return undefined;
    }
  } catch {
    // Any failure anywhere in this local-only verification (malformed
    // blob, bad signature, wrong subject) just means this candidate is not
    // usable — never a hard error, since a scan may have other candidates
    // to try, and `--offline` would otherwise crash on one bad bystander
    // entry in an otherwise-healthy cache.
    return undefined;
  }
}

/** One verified candidate found by scanning local blobs for a signature referrer of `layer`. */
interface LocalSignature {
  readonly statement: Statement;
  readonly signedBy: string;
  readonly bundleDigest: string;
  readonly bundleManifestDigest: string;
}

/** One signature referrer manifest found among a layout's blobs: its blob hex name and its parsed manifest. */
interface LocalReferrer {
  readonly hex: string;
  readonly manifest: ReturnType<typeof parseManifest>;
}

/** Each subject manifest digest's signature referrers among a layout's blobs, in `readdir` order. */
export type LocalReferrers = ReadonlyMap<string, readonly LocalReferrer[]>;

/**
 * Every blob under `blobsSourceDir`'s `blobs/sha256/` that is an OCI manifest
 * of the signature bundle's `artifactType` naming a `subject`, keyed by that
 * subject's digest. A referrer need not be listed in `index.json`, so every
 * blob that could be one is read, once per lookup rather than once per
 * entry. A blob named in `skip` (the caller's candidate layers) and a blob
 * larger than `limits.manifest_bytes`, which cannot be a manifest, are never
 * read, so no layer is read here; a blob in `known` (bytes the caller already
 * read) is not read again (public issue #690).
 */
export async function localSignatureReferrers(
  blobsSourceDir: string,
  skip: ReadonlySet<string>,
  known: ReadonlyMap<string, Buffer> = new Map(),
): Promise<LocalReferrers> {
  const out = new Map<string, LocalReferrer[]>();
  for (const hex of await listLocalBlobHexes(blobsSourceDir)) {
    if (skip.has(hex)) continue;
    let bytes = known.get(hex);
    if (bytes === undefined) {
      const size = await localBlobSize(blobsSourceDir, hex);
      if (size === undefined || size > MANIFEST_MAX_BYTES) continue;
      bytes = await readLocalBlob(blobsSourceDir, hex);
    }
    if (bytes === undefined) continue;
    const parsed = readReferrerFields(bytes);
    if (parsed?.artifactType !== MEDIA_TYPE_BUNDLE || parsed.subject === undefined) continue;
    let manifest: ReturnType<typeof parseManifest>;
    try {
      manifest = parseManifest(digestOfHex(hex), bytes);
    } catch {
      continue; // Names the bundle artifactType but is not a readable manifest — not a candidate.
    }
    const list = out.get(parsed.subject) ?? [];
    list.push({ hex, manifest });
    out.set(parsed.subject, list);
  }
  return out;
}

/**
 * Tries each of `refs` (the signature referrers of one platform manifest)
 * in order: its own layer (the bundle JSON) against `trustedKeys`, accepting
 * the first whose verified statement names `layer`'s digest as its subject.
 */
async function findLocalSignature(
  blobsSourceDir: string,
  refs: readonly LocalReferrer[],
  layer: Descriptor,
  trustedKeys: readonly TrustedKey[],
): Promise<LocalSignature | undefined> {
  for (const { hex, manifest: candidateManifest } of refs) {
    const bundleBytes = await readLocalBlob(blobsSourceDir, hexOfDigest(candidateManifest.layer.digest));
    if (bundleBytes === undefined) continue;
    const verified = verifyBundleSignature(bundleBytes, trustedKeys);
    if (verified === undefined) continue;
    let statement: Statement;
    try {
      statement = parseStatement(verified.payload);
    } catch {
      continue;
    }
    if (statement.subject[0]?.digest.sha256 !== hexOfDigest(layer.digest)) continue;
    return { statement, signedBy: verified.signedBy, bundleDigest: candidateManifest.layer.digest, bundleManifestDigest: digestOfHex(hex) };
  }
  return undefined;
}

/** A manifest's `artifactType` and `subject.digest`, or `undefined` when it is not a JSON object. */
function readReferrerFields(manifestBytes: Buffer): { artifactType: string | undefined; subject: string | undefined } | undefined {
  try {
    const parsed: unknown = JSON.parse(manifestBytes.toString('utf8'));
    if (parsed === null || typeof parsed !== 'object') return undefined;
    const rec = parsed as Record<string, unknown>;
    const artifactType = typeof rec['artifactType'] === 'string' ? rec['artifactType'] : undefined;
    const subjectField = rec['subject'];
    const subjectDigest =
      subjectField !== null && typeof subjectField === 'object' ? (subjectField as Record<string, unknown>)['digest'] : undefined;
    return { artifactType, subject: typeof subjectDigest === 'string' ? subjectDigest : undefined };
  } catch {
    return undefined;
  }
}
