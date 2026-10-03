/**
 * Resolve (`docs/guides/fetch-v1.md` §3): the version spelling check, the
 * multi-base tag GET returning an OCI image index, platform selection, and
 * by-digest fetches (manifests and blobs) with their own integrity check
 * (size + sha256 against the descriptor that named them — independent of,
 * and prior to, the trust check in `dsse.ts`).
 *
 * Every by-digest fetch tries every configured base in order (guide §7 A5):
 * a 404 moves to the next base, and on the **last** base a 404 is retried
 * within the normal budget before `CHTYPES_SOURCE_UNREACHABLE` ("a listed
 * digest that 404s is a host fault", never `UNPUBLISHED` — that verdict is
 * for the tag alone).
 */

import { createHash } from 'node:crypto';
import { asString, field, items, type Json, parseJsonValue } from '../json.js';
import { MANIFEST_MAX_BYTES, MAX_UNPACKED_BYTES, MEDIA_TYPE_INDEX, MEDIA_TYPE_MANIFEST, SPELLING_REFUSE_HINT_REGEX } from './constants.gen.js';
import { ArtifactCorruptError, ArtifactUnpublishedError, SourceIncompatibleError, SourceUnreachableError } from './errors.js';
import { type RequestOptions, readFileUrl, requestBuffered, requestToSink } from './http.js';
import { digestOfHex, endpointUrl, hexOfDigest, MANIFEST_ACCEPT_HEADER, type PlatformKey, platformInfo } from './types.js';

/**
 * Refuses the two documented spelling mistakes (a `v` prefix, a
 * `-lts`/`-stable` channel suffix) before any network call (guide §3). A
 * spelling that is simply not numeric is NOT refused here: the tag lookup
 * answers for it (`CHTYPES_ARTIFACT_UNPUBLISHED` when nothing matches), the
 * same path any arbitrary OCI tag takes — and the conformance trees publish
 * symbolic tags such as `t-untrusted-key`. `SPELLING_REGEX` is a positive
 * pattern used elsewhere (`versionWithinRequest`) to decide whether a
 * resolved predicate's version is compared against the request at all.
 */
export function checkSpelling(spelling: string): void {
  if (new RegExp(SPELLING_REFUSE_HINT_REGEX).test(spelling)) {
    throw new Error(
      `chtypes: ${JSON.stringify(spelling)} is not a v1 version spelling (no "v" prefix, no "-lts"/"-stable" suffix); ` +
        'use the bare version, e.g. "26.8" or "26.8.15.10"',
    );
  }
}

/** A descriptor's `size` is absent when it comes from a lock pin (schema 3 stores digests only, never sizes). */
export interface Descriptor {
  readonly mediaType: string;
  readonly digest: string;
  readonly size?: number;
}

export interface ManifestInfo {
  readonly digest: string;
  readonly bytes: Buffer;
  readonly layer: Descriptor;
  readonly config: Descriptor | undefined;
}

/** A `GET …/manifests/<ref>` (the tag resolve) — always carries the manifest `Accept` header (delivery-side review: our host ignores it, a mirror may need it). */
async function getJsonAt(
  url: string,
  maxBytes: number,
  options: RequestOptions,
): Promise<{ status: number; body: Buffer; json: Json | null }> {
  const res = url.startsWith('file:')
    ? await readFileUrl(url, maxBytes)
    : await requestBuffered(url, { ...options, maxBytes, headers: { ...options.headers, accept: MANIFEST_ACCEPT_HEADER } });
  if (res.status !== 200) return { status: res.status, body: res.body, json: null };
  const json = parseJsonValue(res.body);
  return { status: res.status, body: res.body, json };
}

function descriptorField(json: Json, key: string): Descriptor | undefined {
  const d = field(json, key);
  if (d === undefined) return undefined;
  const digest = asString(field(d, 'digest'));
  if (digest === '') return undefined;
  return { mediaType: asString(field(d, 'mediaType')), digest, size: numberField(d, 'size') };
}

function numberField(json: Json, key: string): number {
  const v = field(json, key);
  if (v === undefined || v.kind !== 'number') return 0;
  const n = Number(v.raw.toString('utf8'));
  return Number.isFinite(n) ? n : 0;
}

function sha256Hex(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex');
}

/**
 * Parses an OCI image manifest's layer (exactly one, required) and config
 * descriptors. `CHTYPES_SOURCE_INCOMPATIBLE` is reserved for a recognized-but-wrong
 * media type (guide §3); unparseable JSON or a manifest that disagrees with
 * its own shape (not exactly one layer) is `CORRUPT` — "a release that
 * disagrees with itself", the same bucket a hash mismatch falls into.
 */
export function parseManifest(digest: string, bytes: Buffer): ManifestInfo {
  const json = parseJsonValue(bytes);
  if (json === null) throw new ArtifactCorruptError(`chtypes: manifest ${digest} is not valid JSON`);
  const mediaType = asString(field(json, 'mediaType'));
  if (mediaType !== '' && mediaType !== MEDIA_TYPE_MANIFEST) {
    throw new SourceIncompatibleError(`chtypes: manifest ${digest} has mediaType ${JSON.stringify(mediaType)}, not ${MEDIA_TYPE_MANIFEST}`);
  }
  const layers = items(field(json, 'layers'));
  if (layers.length !== 1) {
    throw new ArtifactCorruptError(`chtypes: manifest ${digest} has ${layers.length} layers, expected exactly 1`);
  }
  const layerJson = layers[0]!;
  const layerDigest = asString(field(layerJson, 'digest'));
  if (layerDigest === '') throw new ArtifactCorruptError(`chtypes: manifest ${digest}'s layer has no digest`);
  const layer: Descriptor = {
    mediaType: asString(field(layerJson, 'mediaType')),
    digest: layerDigest,
    size: numberField(layerJson, 'size'),
  };
  const config = descriptorField(json, 'config');
  return { digest, bytes, layer, config };
}

/**
 * Verifies fetched bytes against the descriptor that named them (OCI content
 * addressing — independent of signature trust, and checked **before** any
 * decompression or JSON parsing of untrusted content). `size` is checked
 * only when the descriptor carries one — a lock pin (schema 3) has a digest
 * only, never a size. A mismatch is `CORRUPT`: "any hash mismatch along the
 * chain" (the same verdict v0's `ArtifactCorruptError` names), never
 * `SOURCE_INCOMPATIBLE`, which is reserved for a wrong media type.
 */
export function verifyDescriptor(descriptor: Descriptor, bytes: Buffer, what: string): void {
  verifyDigestAndSize(descriptor, sha256Hex(bytes), bytes.length, what);
}

/**
 * The same check as `verifyDescriptor`, for a caller that streamed the bytes
 * through a hasher rather than holding them in one `Buffer` — `unpack.ts`'s
 * layer download, which is never buffered whole in memory.
 */
export function verifyDigestAndSize(descriptor: Descriptor, actualSha256Hex: string, actualSize: number, what: string): void {
  if (descriptor.size !== undefined && actualSize !== descriptor.size) {
    throw new ArtifactCorruptError(`chtypes: ${what} is ${actualSize} bytes, descriptor named ${descriptor.size}`);
  }
  if (actualSha256Hex !== hexOfDigest(descriptor.digest)) {
    throw new ArtifactCorruptError(`chtypes: ${what}'s sha256 does not match its own descriptor`);
  }
}

async function fetchByDigestAcrossBases(
  bases: readonly string[],
  pathFor: (digest: string) => string,
  descriptor: Descriptor,
  what: string,
  defaultMaxBytes: number,
  options: RequestOptions,
  extraHeaders?: Readonly<Record<string, string>>,
): Promise<Buffer> {
  if (bases.length === 0) throw new Error('chtypes: no base URLs configured');
  let lastUnreachable: SourceUnreachableError | undefined;
  for (let i = 0; i < bases.length; i++) {
    const base = bases[i]!;
    const isLast = i === bases.length - 1;
    const url = endpointUrl(base, pathFor(descriptor.digest));
    const maxBytes = descriptor.size ?? defaultMaxBytes;
    let res: { status: number; body: Buffer };
    try {
      res = url.startsWith('file:')
        ? await readFileUrl(url, maxBytes)
        : await requestBuffered(url, {
            ...options,
            maxBytes,
            retryOn404: isLast,
            headers: { ...options.headers, ...extraHeaders },
          });
    } catch (err) {
      // A temporary error (guide §2: a timeout, a 5xx exhausted, a dead
      // host) moves to the next base — never a verification failure, which
      // propagates immediately (`mirror-no-failover-on-verify-fail`).
      if (err instanceof SourceUnreachableError && !isLast) {
        lastUnreachable = err;
        continue;
      }
      throw err;
    }
    if (res.status === 404) continue;
    if (res.status !== 200) throw new SourceUnreachableError(`chtypes: ${url} returned status ${res.status}`);
    verifyDescriptor(descriptor, res.body, what);
    return res.body;
  }
  if (lastUnreachable !== undefined) throw lastUnreachable;
  // Unreachable through an ordinary persistent 404 on the last base: that
  // already throws `SourceUnreachableError` from inside `requestBuffered`
  // (via `retryOn404`) before control returns here. This covers only an
  // empty `bases` list.
  throw new SourceUnreachableError(`chtypes: ${what} (${descriptor.digest}) was not found on any configured base`);
}

/**
 * `GET manifests/<digest>`, tried across every base (guide §6: `--frozen`'s
 * manifest fetch; also `fetchSigned`'s referrer manifest hop), always with
 * the manifest `Accept` header (delivery-side review).
 */
export async function fetchManifestBytesByDigest(
  bases: readonly string[],
  descriptor: Descriptor,
  options: RequestOptions,
): Promise<Buffer> {
  if (descriptor.size !== undefined && descriptor.size > MANIFEST_MAX_BYTES) {
    throw new ArtifactCorruptError(
      `chtypes: manifest ${descriptor.digest}'s descriptor names ${descriptor.size} bytes, over the ${MANIFEST_MAX_BYTES}-byte manifest cap`,
    );
  }
  return fetchByDigestAcrossBases(bases, (d) => `/manifests/${d}`, descriptor, `manifest ${descriptor.digest}`, MANIFEST_MAX_BYTES, options, {
    accept: MANIFEST_ACCEPT_HEADER,
  });
}

/** `GET blobs/<digest>`, tried across every base, fully buffered — a bundle, never the (potentially huge) library layer. */
export async function fetchBlobBytesByDigest(
  bases: readonly string[],
  descriptor: Descriptor,
  options: RequestOptions,
): Promise<Buffer> {
  return fetchByDigestAcrossBases(bases, (d) => `/blobs/${d}`, descriptor, `blob ${descriptor.digest}`, MAX_UNPACKED_BYTES, options);
}

export interface ByteSink {
  reset(): void;
  write(chunk: Buffer): void;
}

/**
 * `GET blobs/<digest>` straight into `sink` (`unpack.ts`'s hashing temp
 * file) — the library layer, which is never buffered whole in memory. Tries
 * every base in order, same as every other by-digest fetch in this module. A
 * `file://` base has no streaming primitive of its own, so it is read whole
 * and written to the sink in one call — acceptable because fixture layers
 * are small test artifacts, never the multi-hundred-megabyte real ones.
 */
export async function fetchBlobToSink(bases: readonly string[], descriptor: Descriptor, options: RequestOptions, sink: ByteSink): Promise<void> {
  if (bases.length === 0) throw new Error('chtypes: no base URLs configured');
  const maxBytes = descriptor.size ?? MAX_UNPACKED_BYTES;
  let lastUnreachable: SourceUnreachableError | undefined;
  for (let i = 0; i < bases.length; i++) {
    const base = bases[i]!;
    const isLast = i === bases.length - 1;
    const url = endpointUrl(base, `/blobs/${descriptor.digest}`);
    try {
      if (url.startsWith('file:')) {
        const res = await readFileUrl(url, maxBytes);
        if (res.status === 404) continue;
        if (res.status !== 200) throw new SourceUnreachableError(`chtypes: ${url} returned status ${res.status}`);
        sink.reset();
        sink.write(res.body);
        return;
      }
      const res = await requestToSink(url, { ...options, maxBytes, retryOn404: isLast }, sink);
      if (res.status === 404) continue;
      if (res.status !== 200 && res.status !== 206) throw new SourceUnreachableError(`chtypes: ${url} returned status ${res.status}`);
      return;
    } catch (err) {
      if (err instanceof SourceUnreachableError && !isLast) {
        lastUnreachable = err;
        continue;
      }
      throw err;
    }
  }
  if (lastUnreachable !== undefined) throw lastUnreachable;
  throw new SourceUnreachableError(`chtypes: blob ${descriptor.digest} was not found on any configured base`);
}

/** `fetchManifestBytesByDigest` plus parsing — `--frozen`'s and `fetchSigned`'s "give me this manifest" primitive. */
export async function fetchManifestByDigest(bases: readonly string[], digest: string, options: RequestOptions): Promise<ManifestInfo> {
  const bytes = await fetchManifestBytesByDigest(bases, { mediaType: MEDIA_TYPE_MANIFEST, digest }, options);
  return parseManifest(digest, bytes);
}

export interface IndexResolveResult {
  readonly repositoryRoot: string;
  readonly indexDigest: string;
  readonly indexBytes: Buffer;
  readonly manifest: ManifestInfo;
}

/**
 * `GET manifests/<spelling>` across `bases` in order (guide §3, §7 A5's tag
 * rule: a tag 404 moves to the next base, and every base 404ing is
 * `UNPUBLISHED`), then the platform's manifest by digest across every base
 * (mirrors may carry the same tag pointing at the same digest, so a base
 * that served the index is not the only one asked for its blobs).
 */
export async function resolveTag(
  bases: readonly string[],
  spelling: string,
  platform: PlatformKey,
  options: RequestOptions,
): Promise<IndexResolveResult> {
  checkSpelling(spelling);
  if (bases.length === 0) throw new Error('chtypes: no base URLs configured');
  const info = platformInfo(platform);
  const triedBases: string[] = [];
  for (let baseIndex = 0; baseIndex < bases.length; baseIndex++) {
    const base = bases[baseIndex]!;
    const isLastBase = baseIndex === bases.length - 1;
    const url = endpointUrl(base, `/manifests/${spelling}`);
    let status: number;
    let body: Buffer;
    let json: Json | null;
    try {
      ({ status, body, json } = await getJsonAt(url, MANIFEST_MAX_BYTES, options));
    } catch (err) {
      // A temporary error (guide §2: a timeout, a 5xx exhausted, a dead
      // host) moves to the next base, same as a tag 404 — never a
      // verification failure, which propagates immediately.
      if (err instanceof SourceUnreachableError && !isLastBase) {
        triedBases.push(base);
        continue;
      }
      throw err;
    }
    if (status === 404) {
      triedBases.push(base);
      continue;
    }
    if (status !== 200) {
      // A persistent retryable status on the last base already threw from
      // inside `requestBuffered`; anything reaching here (400, 418, …) is an
      // anomaly this fetcher does not have a specific verdict for.
      throw new SourceUnreachableError(`chtypes: ${url} returned status ${status}`);
    }
    if (json === null) {
      throw new ArtifactCorruptError(`chtypes: ${url} did not return valid JSON`);
    }
    const mediaType = asString(field(json, 'mediaType'));
    if (mediaType !== '' && mediaType !== MEDIA_TYPE_INDEX) {
      throw new SourceIncompatibleError(`chtypes: ${url} has mediaType ${JSON.stringify(mediaType)}, not ${MEDIA_TYPE_INDEX}`);
    }
    const manifests = items(field(json, 'manifests'));
    const matches = manifests.filter((m) => {
      const p = field(m, 'platform');
      return asString(field(p, 'os')) === info.os && asString(field(p, 'architecture')) === info.architecture;
    });
    if (matches.length === 0) {
      const offered = manifests
        .map((m) => {
          const p = field(m, 'platform');
          return `${asString(field(p, 'os'))}-${asString(field(p, 'architecture'))}`;
        })
        .filter((k) => k !== '-');
      throw new ArtifactUnpublishedError(
        `chtypes: ${spelling} offers no manifest for ${platform}; the index offers: ${offered.join(', ') || '(none)'}`,
      );
    }
    if (matches.length > 1) {
      // `index-duplicate-platform`: the index disagrees with itself about
      // which manifest is this platform's — `CORRUPT`, the plan's own
      // annotation for this case.
      throw new ArtifactCorruptError(`chtypes: ${spelling}'s index names ${platform} more than once`);
    }
    const d = matches[0]!;
    const manifestDigest = asString(field(d, 'digest'));
    if (manifestDigest === '') throw new ArtifactCorruptError(`chtypes: ${spelling}'s ${platform} descriptor has no digest`);
    const manifestDescriptor: Descriptor = {
      mediaType: asString(field(d, 'mediaType')),
      digest: manifestDigest,
      size: numberField(d, 'size'),
    };
    if (manifestDescriptor.mediaType !== MEDIA_TYPE_MANIFEST) {
      throw new SourceIncompatibleError(
        `chtypes: ${spelling}'s ${platform} descriptor has mediaType ${JSON.stringify(manifestDescriptor.mediaType)}, not ${MEDIA_TYPE_MANIFEST}`,
      );
    }
    const manifestBytes = await fetchManifestBytesByDigest(bases, manifestDescriptor, options);
    const manifest = parseManifest(manifestDigest, manifestBytes);
    return { repositoryRoot: base, indexDigest: digestOfHex(sha256Hex(body)), indexBytes: body, manifest };
  }
  throw new ArtifactUnpublishedError(`chtypes: ${spelling} is unpublished: every base 404d the tag (${triedBases.join(', ')})`);
}
