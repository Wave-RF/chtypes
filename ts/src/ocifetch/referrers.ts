/**
 * Referrer discovery (`docs/guides/fetch-v1.md` §4): the OCI referrers API,
 * **and** the tag-schema fallback (`sha256-<hex>`) — both implemented, never
 * only one, because which a host serves is a property of that host, not of
 * this fetcher.
 *
 * **The fallback tag is consulted only when the referrers API's own answer
 * has no bundle of the trusted signature artifactType** — an empty list, a
 * list holding only other artifactTypes (goldens, say), or the API itself
 * unsupported (errors, or any non-200). When the referrers API already
 * names a candidate of that artifactType, the fallback tag is never fetched
 * — this keeps the request count the same shape as Go, Python and Rust's
 * lanes, which conformance cases assert on (a case can require "no fallback
 * request" for a referrers-API-only host). A referrers-API 404 reads the
 * same as "unsupported": fall back at once, with no retry — this is a
 * discovery probe, not a promise that a specific digest exists (unlike a
 * by-digest manifest/blob fetch, which does retry a 404 on the last base;
 * `oci.ts`'s `retryOn404`).
 */

import { asString, field, items, type Json, parseJsonValue } from '../json.js';
import { MANIFEST_MAX_BYTES, MAX_REFERRERS } from './constants.gen.js';
import { ArtifactCorruptError, SourceRetiredError } from './errors.js';
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
 * from the referrers API alone. `[]` whenever the API's own answer has no
 * bundle of this `artifactType` — an empty list, a list of only other
 * artifactTypes, a 404 (treated as "unsupported here", not retried — see
 * this module's header), or any other error — and in every one of those
 * cases `discoverSignatureCandidates` then also tries the fallback tag.
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
  } catch (err) {
    // A retired repository (a 410, guide §2) is permanent: never read as "no
    // candidates here", and never a reason to try the fallback tag.
    if (err instanceof SourceRetiredError) throw err;
    // The referrers API is not guaranteed on every host (mirrors) — read as
    // "no candidates here", exactly as an empty or goldens-only answer would.
  }
  return fromApi;
}

/**
 * The fallback tag's own referrer list for `digest` (the tag-schema index,
 * `GET manifests/<tag>` — always with the manifest `Accept` header), filtered
 * the same way as the referrers API. Only ever called by
 * `discoverSignatureCandidates` when the referrers API found nothing of the
 * trusted artifactType.
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
 * The candidate list a caller should try verifying in order, stopping at
 * the first success. Queries the referrers API first; the fallback tag is
 * fetched **only** when the referrers API named no candidate of
 * `artifactType` at all (see this module's header) — never unconditionally,
 * so a referrers-API-only host sees exactly one discovery request, matching
 * Go, Python and Rust.
 */
export async function discoverSignatureCandidates(
  repositoryRoot: string,
  digest: string,
  artifactType: string,
  options: RequestOptions,
): Promise<ReferrerDescriptor[]> {
  const fromApi = await discoverReferrers(repositoryRoot, digest, artifactType, options);
  if (fromApi.length > 0) return fromApi;
  return discoverFallbackTag(repositoryRoot, digest, artifactType, options);
}
