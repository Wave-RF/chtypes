/**
 * Referrer discovery (`docs/guides/fetch-v1.md` §4): the OCI referrers API,
 * **and** the tag-schema fallback (`sha256-<hex>`) — both implemented always,
 * never only one, because which a host serves is a property of that host,
 * not of this fetcher.
 *
 * **Both are tried, every time** — never only when the referrers API is
 * absent. A referrers index can read back empty (200, no matching entries)
 * for a manifest that does have a signature, on our own host too, not only a
 * mirror — so "the API answered 200" is not the same claim as "the API's
 * answer is complete". This module always gathers candidates from both
 * paths and lets the caller try every one, stopping at the first that
 * verifies (`dsse.ts` decides "verifies"; this module only gathers
 * descriptors to try).
 */

import { asString, field, items, type Json, parseJsonValue } from '../json.js';
import { MANIFEST_MAX_BYTES, MAX_REFERRERS } from './constants.gen.js';
import { ArtifactCorruptError } from './errors.js';
import { type RequestOptions, readFileUrl, requestBuffered } from './http.js';
import { endpointUrl, MANIFEST_ACCEPT_HEADER } from './types.js';

export interface ReferrerDescriptor {
  readonly digest: string;
  readonly mediaType: string;
  readonly artifactType: string | undefined;
  readonly size: number;
}

/** The fallback tag schema name for a digest, e.g. `sha256:ab12..` -> `sha256-ab12..`. */
export function fallbackTagFor(digest: string): string {
  const [algo, hex] = digest.split(':', 2);
  if (algo !== 'sha256' || hex === undefined) {
    throw new ArtifactCorruptError(`chtypes: cannot derive a fallback tag from digest ${JSON.stringify(digest)}`);
  }
  return `sha256-${hex}`;
}

async function getJson(
  url: string,
  maxBytes: number,
  options: RequestOptions,
  extraHeaders?: Readonly<Record<string, string>>,
): Promise<{ status: number; json: Json | null }> {
  const res = url.startsWith('file:')
    ? await readFileUrl(url, maxBytes)
    : await requestBuffered(url, {
        ...options,
        maxBytes,
        headers: { ...options.headers, ...extraHeaders },
      });
  if (res.status !== 200) return { status: res.status, json: null };
  const json = parseJsonValue(res.body);
  if (json === null) throw new ArtifactCorruptError(`chtypes: ${url} did not return valid JSON`);
  return { status: res.status, json };
}

function descriptorsFromIndex(json: Json): ReferrerDescriptor[] {
  // A host may omit the index's own `mediaType`; the `manifests` array is
  // read either way, so this is not itself a shape check (`oci.ts` checks
  // media types where their presence is load-bearing).
  const manifests = items(field(json, 'manifests'));
  const out: ReferrerDescriptor[] = [];
  for (const m of manifests.slice(0, MAX_REFERRERS)) {
    const digest = asString(field(m, 'digest'));
    const descMediaType = asString(field(m, 'mediaType'));
    const artifactTypeField = field(m, 'artifactType');
    const size = numberField(m, 'size');
    if (digest === '') continue;
    out.push({
      digest,
      mediaType: descMediaType,
      artifactType: artifactTypeField === undefined ? undefined : asString(artifactTypeField),
      size,
    });
  }
  return out;
}

function numberField(json: Json, key: string): number {
  const v = field(json, key);
  if (v === undefined || v.kind !== 'number') return 0;
  const n = Number(v.raw.toString('utf8'));
  return Number.isFinite(n) ? n : 0;
}

/**
 * Every referrer of `artifactType` for `digest` within `repositoryRoot`,
 * from the referrers API alone (`[]` when the API is absent, errors, or
 * matches nothing — all three read the same to a caller, since
 * `discoverSignatureCandidates` always checks the fallback tag too
 * regardless of which of the three this was).
 */
export async function discoverReferrers(
  repositoryRoot: string,
  digest: string,
  artifactType: string,
  options: RequestOptions,
): Promise<ReferrerDescriptor[]> {
  const url = `${endpointUrl(repositoryRoot, `/referrers/${digest}`)}?artifactType=${encodeURIComponent(artifactType)}`;
  const fromApi: ReferrerDescriptor[] = [];
  try {
    const { status, json } = await getJson(url, MANIFEST_MAX_BYTES, options);
    if (status === 200 && json !== null) {
      for (const d of descriptorsFromIndex(json)) {
        if (d.artifactType === artifactType) fromApi.push(d);
      }
    }
  } catch {
    // The referrers API is not guaranteed on every host (mirrors) — fall
    // through to the tag schema below exactly as if it had answered empty.
  }
  return fromApi;
}

/**
 * The fallback tag's own referrer list for `digest` (the tag-schema index,
 * `GET manifests/<tag>` — always with the manifest `Accept` header), filtered
 * the same way as the referrers API.
 */
export async function discoverFallbackTag(
  repositoryRoot: string,
  digest: string,
  artifactType: string,
  options: RequestOptions,
): Promise<ReferrerDescriptor[]> {
  const tag = fallbackTagFor(digest);
  const url = endpointUrl(repositoryRoot, `/manifests/${tag}`);
  const { status, json } = await getJson(url, MANIFEST_MAX_BYTES, options, { accept: MANIFEST_ACCEPT_HEADER });
  if (status !== 200 || json === null) return [];
  return descriptorsFromIndex(json).filter((d) => d.artifactType === artifactType);
}

/**
 * The combined, ordered candidate list a caller should try verifying in
 * order, stopping at the first success: referrers-API descriptors, then (not
 * instead of — see this module's header) the fallback tag's.
 */
export async function discoverSignatureCandidates(
  repositoryRoot: string,
  digest: string,
  artifactType: string,
  options: RequestOptions,
): Promise<ReferrerDescriptor[]> {
  const fromApi = await discoverReferrers(repositoryRoot, digest, artifactType, options);
  const fromFallback = await discoverFallbackTag(repositoryRoot, digest, artifactType, options);
  const seen = new Set(fromApi.map((d) => d.digest));
  const merged = [...fromApi];
  for (const d of fromFallback) {
    if (!seen.has(d.digest)) {
      merged.push(d);
      seen.add(d.digest);
    }
  }
  return merged;
}
