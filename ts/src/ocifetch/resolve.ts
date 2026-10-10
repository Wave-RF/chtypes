/**
 * `chtypes resolve` (`docs/guides/fetch-v1.md` §3, "What a request resolves
 * to"; public issue #493): what a line, a patch or an exact version resolves
 * to on every platform the registry offers, each verified exactly as a fetch
 * verifies it (§4), and never a layer downloaded. Offline it is the cache's
 * answer instead, the lookup an open makes.
 */

import { noteIgnoredOverrides, offlineMode } from './channel.js';
import { PLATFORMS } from './constants.gen.js';
import { missingNotes, requestOptionsFor, resolveBases, resolveInstalled, verifyManifestTrust, withNotes } from './ensure.js';
import { ArtifactMissingError, ArtifactUnpublishedError } from './errors.js';
import { unwritable } from './faults.js';
import { cacheRoot } from './layout.js';
import { checkSpelling, type Descriptor, fetchManifestBytesByDigest, parseManifest, resolveIndex, selectPlatformDescriptor } from './oci.js';
import type { FetchV1Options, PlatformKey } from './types.js';

/** What a request resolves to on one platform. */
export interface Resolution {
  readonly platform: PlatformKey;
  /** The signed `clickhouse_version`. */
  readonly version: string;
  /** The signed build id. */
  readonly build: string;
  /** The platform manifest's digest, `sha256:<hex>`. */
  readonly manifest: string;
}

/** `resolveBuilds`' answer: one resolution per platform, in the constants' platform order, and allow-unsigned's warnings. */
export interface ResolveBuildsResult {
  readonly resolutions: readonly Resolution[];
  readonly warnings: readonly string[];
}

/**
 * What `spelling` resolves to, one `Resolution` per platform, in the
 * constants' platform order. Online it resolves the index (through the dev
 * channel's alias first, §3), and for each platform the index offers it
 * fetches the manifest by digest and verifies its signed statement against the
 * request (§4), exactly as `ensure` does; no layer is requested and nothing is
 * written. A platform the index does not offer is left out, and an index that
 * offers none of them is `ArtifactUnpublishedError`. Offline it answers from
 * the cache: each platform's `resolveInstalled`, and `ArtifactMissingError`
 * when no platform has an installed build. A refused spelling is refused
 * first, before anything is read (`SpellingRefusedError`).
 */
export async function resolveBuilds(spelling: string, options: FetchV1Options = {}): Promise<ResolveBuildsResult> {
  noteIgnoredOverrides(options);
  try {
    return await resolveIn(spelling, options);
  } catch (err) {
    // An offline lookup may verify a pre-seeded entry into the cache: a write
    // that failed there is one class in every mode, as for `ensure`.
    throw await unwritable(err, cacheRoot(options.cacheDir));
  }
}

async function resolveIn(spelling: string, options: FetchV1Options): Promise<ResolveBuildsResult> {
  checkSpelling(spelling);
  if (offlineMode(options.offline)) {
    const resolutions: Resolution[] = [];
    for (const p of PLATFORMS) {
      const hit = await resolveInstalled(spelling, p.key, options);
      if (hit !== undefined) {
        resolutions.push({ platform: p.key, version: hit.version, build: hit.build, manifest: hit.digests.manifest });
      }
    }
    if (resolutions.length === 0) {
      throw new ArtifactMissingError(
        withNotes(`chtypes: no installed artifact for ${spelling} on any platform, and --offline forbids a network fetch`, await missingNotes(options)),
      );
    }
    return { resolutions, warnings: [] };
  }

  const bases = resolveBases(options);
  const baseReqOptions = requestOptionsFor(options, bases);
  const reqOptions = { ...baseReqOptions, maxBytes: 0 };
  const index = await resolveIndex(bases, spelling, reqOptions);
  const resolutions: Resolution[] = [];
  const warnings: string[] = [];
  for (const p of PLATFORMS) {
    let descriptor: Descriptor;
    try {
      descriptor = selectPlatformDescriptor(index, spelling, p.key);
    } catch (err) {
      if (err instanceof ArtifactUnpublishedError) continue; // the index does not offer this platform
      throw err;
    }
    const manifest = parseManifest(descriptor.digest, await fetchManifestBytesByDigest(bases, descriptor, reqOptions));
    // Under allow-unsigned with no verified statement, the version and build
    // are the config blob's, as an unsigned install records them.
    const verified = await verifyManifestTrust(index.repositoryRoot, manifest, index.aliasAbsent, p.key, spelling, options, baseReqOptions);
    warnings.push(...verified.warnings);
    resolutions.push({
      platform: p.key,
      version: stringMember(verified.predicate.clickhouse_version),
      build: stringMember(verified.predicate.build),
      manifest: manifest.digest,
    });
  }
  if (resolutions.length === 0) {
    throw new ArtifactUnpublishedError(`chtypes: the index for ${spelling} offers no platform this SDK knows`);
  }
  return { resolutions, warnings };
}

/** A predicate member read as the signed statement holds it: its text, or `''` for anything that is not a string. */
function stringMember(v: unknown): string {
  return typeof v === 'string' ? v : '';
}
