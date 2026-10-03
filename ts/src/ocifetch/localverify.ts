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
 * with the signature bundle's own `artifactType` is a *candidate* referrer
 * of the platform manifest, tried the same way `dsse.ts`'s
 * `verifyAnyReferrerBundle` tries referrers fetched over the network —
 * stopping at the first whose signature verifies AND whose statement names
 * this exact platform manifest's layer as its subject. `oras copy -r
 * --to-oci-layout` is what populates these blobs when pulling from a real
 * registry (guide §1); `index.json` itself carries no referrers listing (no
 * such thing exists in the OCI image-layout spec), which is exactly why
 * this is a scan rather than a lookup.
 */

import { rm } from 'node:fs/promises';
import path from 'node:path';
import { MEDIA_TYPE_BUNDLE, PREDICATE_TYPE_ARTIFACT } from './constants.gen.js';
import { checkArtifactStatement, parseStatement, verifyBundleSignature } from './dsse.js';
import { ArtifactCorruptError } from './errors.js';
import {
  commitStaging,
  freshStagingDir,
  listLocalBlobHexes,
  readLocalBlob,
  readVerifiedRecord,
  unpackedDir,
  type VerifiedRecord,
  writeVerifiedRecord,
} from './layout.js';
import { type Descriptor, parseManifest, verifyDescriptor } from './oci.js';
import type { ArtifactPredicate, PlatformKey, Statement, TrustedKey } from './types.js';
import { digestOfHex, hexOfDigest, platformInfo } from './types.js';
import { verifyAndUnpackLayerBytes, verifyInstalledLibrary } from './unpack.js';

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
    const found = await findLocalSignature(blobsSourceDir, manifest.layer, trustedKeys);
    if (found === undefined) return undefined;
    const predicate: ArtifactPredicate = checkArtifactStatement(found.statement, PREDICATE_TYPE_ARTIFACT, manifest.layer.digest, {
      os: info.os,
      arch: info.architecture,
      // No `requestedSpelling`: this entry is verified on its own terms,
      // not against one request — see this function's own doc comment.
    });

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
      const record: VerifiedRecord = {
        schema: 1,
        manifestDigest,
        layerDigest: manifest.layer.digest,
        bundleDigest: found.bundleDigest,
        signedBy: found.signedBy,
        predicate,
        library: predicate.library,
      };
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
}

/**
 * Scans every local blob for one that is an OCI manifest of the signature
 * bundle's `artifactType`, tries its own layer (the bundle JSON) against
 * `trustedKeys`, and accepts the first whose verified statement names
 * `layer`'s digest as its subject. Order is the filesystem's own
 * `readdir` order — deterministic per run, immaterial to correctness since
 * every candidate is checked.
 */
async function findLocalSignature(
  blobsSourceDir: string,
  layer: Descriptor,
  trustedKeys: readonly TrustedKey[],
): Promise<LocalSignature | undefined> {
  const hexes = await listLocalBlobHexes(blobsSourceDir);
  for (const hex of hexes) {
    const bytes = await readLocalBlob(blobsSourceDir, hex);
    if (bytes === undefined) continue;
    const artifactType = readArtifactType(bytes);
    if (artifactType !== MEDIA_TYPE_BUNDLE) continue;
    let candidateManifest: ReturnType<typeof parseManifest>;
    try {
      candidateManifest = parseManifest(digestOfHex(hex), bytes);
    } catch {
      continue; // Names the bundle artifactType but is not a readable manifest — not a candidate.
    }
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
    return { statement, signedBy: verified.signedBy, bundleDigest: digestOfHex(hex) };
  }
  return undefined;
}

function readArtifactType(manifestBytes: Buffer): string | undefined {
  try {
    const parsed: unknown = JSON.parse(manifestBytes.toString('utf8'));
    if (parsed === null || typeof parsed !== 'object') return undefined;
    const v = (parsed as Record<string, unknown>)['artifactType'];
    return typeof v === 'string' ? v : undefined;
  } catch {
    return undefined;
  }
}
