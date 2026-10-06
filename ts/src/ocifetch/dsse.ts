/**
 * Trust (`docs/guides/fetch-v1.md` §4): verifying one Sigstore bundle v0.3 by
 * hand — never a Sigstore client library, which cannot verify a key-only
 * bundle at all (`layout-v2.md` §4.3). `node:crypto`'s `verify(null, …)` is
 * ed25519 (no digest algorithm to name), matching Go `crypto/ed25519`,
 * Python's own RFC 8032 verifier and Rust's `ed25519-dalek` — the primitive
 * every binding already ships for v0.
 *
 * The split this module enforces (guide §4): a bundle whose SIGNATURE does
 * not verify under any trusted key is simply "not this one" — a caller tries
 * the next referrer and only reports `UNTRUSTED` once every candidate has
 * failed this way. A bundle whose signature DOES verify but whose STATEMENT
 * fails a content check (wrong predicateType, wrong subject, wrong platform,
 * wrong version, a duplicate JSON key) is `CORRUPT` **immediately** — an
 * authentically-signed statement that asserts the wrong thing is a real
 * inconsistency, never something to paper over by trying another referrer.
 */

import { createHash, createPublicKey, verify as cryptoVerify } from 'node:crypto';
import { items, type Json, parseJsonValue } from '../json.js';
import { activeChannel } from './channel.js';
import { DSSE_MAX_SIGNATURES, DSSE_PAYLOAD_TYPE, SPELLING_REGEX } from './constants.gen.js';
import { ArtifactCorruptError } from './errors.js';
import { fetchBlobBytesByDigest, fetchManifestByDigest, type ManifestInfo } from './oci.js';
import type { RequestOptions } from './http.js';
import { discoverSignatureCandidates } from './referrers.js';
import type { ArtifactPredicate, Statement, TrustedKey } from './types.js';
import { hexOfDigest } from './types.js';

// -------------------------------------------------------------- strict JSON

/**
 * `bytes` as a plain JS value, refusing a duplicate key **anywhere** in the
 * document (guide §4's "a duplicate key anywhere in the statement's JSON is
 * CORRUPT") — `../json.ts`'s parser keeps every key/value pair, duplicates
 * included, specifically so a caller can detect this; `JSON.parse` would
 * silently keep only the last and could never see it.
 */
export function parseJsonNoDuplicates(bytes: Buffer): unknown {
  const parsed = parseJsonValue(bytes);
  if (parsed === null) throw new ArtifactCorruptError('chtypes: not a single valid JSON value');
  return toPlain(parsed);
}

function toPlain(json: Json): unknown {
  switch (json.kind) {
    case 'null':
      return null;
    case 'bool':
      return json.raw[0] === 0x74;
    case 'number': {
      const n = Number(json.raw.toString('utf8'));
      return Number.isFinite(n) ? n : json.raw.toString('utf8');
    }
    case 'string':
      return json.bytes.toString('utf8');
    case 'array':
      return items(json).map(toPlain);
    case 'object': {
      const seen = new Set<string>();
      const obj: Record<string, unknown> = {};
      for (let i = 0; i < json.keys.length; i++) {
        const key = json.keys[i]!.toString('utf8');
        if (seen.has(key)) throw new ArtifactCorruptError(`chtypes: duplicate JSON key ${JSON.stringify(key)}`);
        seen.add(key);
        obj[key] = toPlain(json.items[i]!);
      }
      return obj;
    }
    default:
      return null;
  }
}

// ------------------------------------------------------------------- ed25519

/** DSSE's pre-authentication encoding (PAE): `DSSEv1 LEN(type) SP type LEN(body) SP body`. */
/** A key's id: `sha256-first16hex`, the first 16 hex characters of sha256 over the raw 32-byte public key (`spec/fetch-v1/constants.json`, `trust.keyid_algorithm`). */
export function keyIdOfRawKey(publicKeyHex: string): string {
  return createHash('sha256').update(Buffer.from(publicKeyHex, 'hex')).digest('hex').slice(0, 16);
}

export function dssePAE(payloadType: string, payload: Buffer): Buffer {
  const typeBytes = Buffer.from(payloadType, 'utf8');
  return Buffer.concat([
    Buffer.from('DSSEv1 ', 'ascii'),
    Buffer.from(String(typeBytes.length), 'ascii'),
    Buffer.from(' ', 'ascii'),
    typeBytes,
    Buffer.from(' ', 'ascii'),
    Buffer.from(String(payload.length), 'ascii'),
    Buffer.from(' ', 'ascii'),
    payload,
  ]);
}

/** Verifies one ed25519 signature over `message` against a raw 32-byte public key given as hex. */
export function verifyEd25519(publicKeyHex: string, message: Buffer, signature: Buffer): boolean {
  const keyBytes = Buffer.from(publicKeyHex, 'hex');
  if (keyBytes.length !== 32) return false;
  let keyObject: ReturnType<typeof createPublicKey>;
  try {
    keyObject = createPublicKey({
      key: { kty: 'OKP', crv: 'Ed25519', x: keyBytes.toString('base64url') },
      format: 'jwk',
    });
  } catch {
    return false;
  }
  try {
    return cryptoVerify(null, message, keyObject, signature);
  } catch {
    return false;
  }
}

// -------------------------------------------------------------------- bundle

interface ParsedEnvelope {
  readonly payloadType: string;
  readonly payload: Buffer;
  readonly signatures: readonly { readonly sig: Buffer; readonly keyid: string | undefined }[];
}

/**
 * Parses a Sigstore bundle v0.3's DSSE envelope. Lenient on purpose: a
 * malformed or unexpectedly-shaped bundle among several referrer candidates
 * is "not usable", not a hard failure — `null` lets the caller move on to
 * the next candidate instead of raising `CORRUPT` for bytes nothing has
 * authenticated yet.
 */
function parseEnvelope(bundleBytes: Buffer): ParsedEnvelope | null {
  let bundle: unknown;
  try {
    bundle = JSON.parse(bundleBytes.toString('utf8'));
  } catch {
    return null;
  }
  if (bundle === null || typeof bundle !== 'object') return null;
  const envelope = (bundle as Record<string, unknown>)['dsseEnvelope'];
  if (envelope === null || typeof envelope !== 'object') return null;
  const e = envelope as Record<string, unknown>;
  const payloadType = e['payloadType'];
  const payloadB64 = e['payload'];
  const signaturesRaw = e['signatures'];
  if (typeof payloadType !== 'string' || typeof payloadB64 !== 'string' || !Array.isArray(signaturesRaw)) return null;
  let payload: Buffer;
  try {
    payload = Buffer.from(payloadB64, 'base64');
  } catch {
    return null;
  }
  const signatures: { sig: Buffer; keyid: string | undefined }[] = [];
  for (const row of signaturesRaw.slice(0, DSSE_MAX_SIGNATURES)) {
    if (row === null || typeof row !== 'object') continue;
    const sigB64 = (row as Record<string, unknown>)['sig'];
    const keyid = (row as Record<string, unknown>)['keyid'];
    if (typeof sigB64 !== 'string') continue;
    try {
      signatures.push({ sig: Buffer.from(sigB64, 'base64'), keyid: typeof keyid === 'string' ? keyid : undefined });
    } catch {
      // Not valid base64 — skip this one signature, not the whole bundle.
    }
  }
  return { payloadType, payload, signatures };
}

/**
 * Attempts to verify one bundle's DSSE signature against `trustedKeys`.
 * Returns the trusted key id that verified it, or `undefined` when none did
 * (including an unparseable bundle) — callers try the next candidate.
 *
 * This checks the SIGNATURE only. The caller parses and checks the
 * statement's content afterward, using the authenticated `payload` bytes.
 */
export function verifyBundleSignature(
  bundleBytes: Buffer,
  trustedKeys: readonly TrustedKey[],
): { readonly signedBy: string; readonly payload: Buffer } | undefined {
  const envelope = parseEnvelope(bundleBytes);
  if (envelope === null) return undefined;
  if (envelope.payloadType !== DSSE_PAYLOAD_TYPE) return undefined;
  const pae = dssePAE(envelope.payloadType, envelope.payload);
  for (const sig of envelope.signatures) {
    for (const key of trustedKeys) {
      if (verifyEd25519(key.ed25519Hex, pae, sig.sig)) {
        return { signedBy: key.keyid, payload: envelope.payload };
      }
    }
  }
  return undefined;
}

/**
 * Parses an already-authenticated DSSE payload into a `Statement`, refusing
 * a duplicate key anywhere (guide §4). Call this only after
 * `verifyBundleSignature` has returned a `signedBy` for these exact bytes —
 * parsing content checks are meaningless, and must never run, on bytes no
 * signature has covered.
 */
export function parseStatement(payload: Buffer): Statement {
  const value = parseJsonNoDuplicates(payload);
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new ArtifactCorruptError('chtypes: the DSSE payload is not a JSON object');
  }
  const v = value as Record<string, unknown>;
  const type = v['_type'];
  const predicateType = v['predicateType'];
  const predicate = v['predicate'];
  const subject = v['subject'];
  if (typeof type !== 'string' || typeof predicateType !== 'string') {
    throw new ArtifactCorruptError('chtypes: the statement is missing _type or predicateType');
  }
  if (predicate === null || typeof predicate !== 'object' || Array.isArray(predicate)) {
    throw new ArtifactCorruptError('chtypes: the statement predicate is not an object');
  }
  if (!Array.isArray(subject) || subject.length === 0) {
    throw new ArtifactCorruptError('chtypes: the statement names no subject');
  }
  const parsedSubjects = subject.map((s) => {
    if (s === null || typeof s !== 'object') throw new ArtifactCorruptError('chtypes: a statement subject is not an object');
    const row = s as Record<string, unknown>;
    const digest = row['digest'];
    const name = row['name'];
    if (digest === null || typeof digest !== 'object') throw new ArtifactCorruptError('chtypes: a subject has no digest');
    const sha256 = (digest as Record<string, unknown>)['sha256'];
    if (typeof sha256 !== 'string') throw new ArtifactCorruptError('chtypes: a subject digest has no sha256');
    return { name: typeof name === 'string' ? name : '', digest: { sha256 } };
  });
  return {
    _type: type,
    predicateType,
    predicate: predicate as Record<string, unknown>,
    subject: parsedSubjects,
  };
}

// --------------------------------------------------------- content checking

/** `clickhouse_version`'s relationship to the original request spelling (guide §4). */
export function versionWithinRequest(actualVersion: string, requestedSpelling: string): boolean {
  const actual = actualVersion.split('.');
  const requested = requestedSpelling.split('.');
  if (requested.length > actual.length) return false;
  for (let i = 0; i < requested.length; i++) {
    if (requested[i] !== actual[i]) return false;
  }
  return true;
}

export interface ArtifactStatementCheck {
  readonly os: string;
  readonly arch: string;
  /**
   * Omit only when there is no one request to check against yet —
   * `localverify.ts`'s local-blob scan verifies a pre-seeded entry on its
   * own terms first, and the caller checks the resulting predicate's own
   * `clickhouse_version` against whatever request prompted the scan
   * afterward. Every other caller always supplies this.
   */
  readonly requestedSpelling?: string;
}

/**
 * The full statement check for a **platform artifact** manifest (guide §4):
 * predicateType, subject digest, `abi` (never `abi_revision`), os/arch and
 * — when `check.requestedSpelling` is given — the version-within-request
 * rule. Throws `ArtifactCorruptError` naming the first failing check.
 */
export function checkArtifactStatement(
  statement: Statement,
  expectedPredicateType: string,
  expectedLayerDigest: string,
  check: ArtifactStatementCheck,
): ArtifactPredicate {
  if (statement.predicateType !== expectedPredicateType) {
    throw new ArtifactCorruptError(
      `chtypes: statement predicateType ${JSON.stringify(statement.predicateType)} != expected ${JSON.stringify(expectedPredicateType)}`,
    );
  }
  const expectedHex = hexOfDigest(expectedLayerDigest);
  if (statement.subject[0]?.digest.sha256 !== expectedHex) {
    throw new ArtifactCorruptError(
      `chtypes: statement subject digest does not match the manifest's own layer digest`,
    );
  }
  const p = statement.predicate;
  // The active contract's generation: 2 on the ABI v2 dev channel (rule r6),
  // so a 1.x build's abi-1 predicate is refused there.
  const abi = activeChannel().abi;
  if (p['abi'] !== abi) {
    throw new ArtifactCorruptError(`chtypes: statement predicate.abi is ${JSON.stringify(p['abi'])}, not ${abi}`);
  }
  if (p['os'] !== check.os || p['arch'] !== check.arch) {
    throw new ArtifactCorruptError(
      `chtypes: statement predicate is for ${JSON.stringify(p['os'])}-${JSON.stringify(p['arch'])}, not ${check.os}-${check.arch}`,
    );
  }
  const version = p['clickhouse_version'];
  if (typeof version !== 'string') {
    throw new ArtifactCorruptError(`chtypes: statement predicate.clickhouse_version ${JSON.stringify(version)} is not a string`);
  }
  if (check.requestedSpelling !== undefined && new RegExp(SPELLING_REGEX).test(check.requestedSpelling) && !versionWithinRequest(version, check.requestedSpelling)) {
    throw new ArtifactCorruptError(
      `chtypes: statement predicate.clickhouse_version ${JSON.stringify(version)} does not lie within requested ${JSON.stringify(check.requestedSpelling)}`,
    );
  }
  return p as unknown as ArtifactPredicate;
}

/**
 * The reduced statement check `fetchSigned` uses for goldens and fixtures
 * (guide §9): predicateType and subject digest only. These objects' own
 * predicate shapes are not yet specified (blocked on the producer, plan
 * §1.2), so `fetchSigned`'s caller reads `statement.predicate` itself.
 */
export function checkGenericStatement(statement: Statement, expectedPredicateType: string, expectedLayerDigest: string): void {
  if (statement.predicateType !== expectedPredicateType) {
    throw new ArtifactCorruptError(
      `chtypes: statement predicateType ${JSON.stringify(statement.predicateType)} != expected ${JSON.stringify(expectedPredicateType)}`,
    );
  }
  const expectedHex = hexOfDigest(expectedLayerDigest);
  if (statement.subject[0]?.digest.sha256 !== expectedHex) {
    throw new ArtifactCorruptError("chtypes: statement subject digest does not match the fetched object's own layer digest");
  }
}

// --------------------------------------------------------- end-to-end trust

export interface TrustResult {
  readonly statement: Statement;
  /** The trusted key id that verified it, or the fixture test key's id under `trust: "test"`. */
  readonly signedBy: string;
  /** The digest of the verified bundle blob itself (the referrer manifest's layer), as a lock pin's `bundle` records it. */
  readonly bundleDigest: string;
  /** The digest of the referrer manifest that carried the bundle. */
  readonly bundleManifestDigest: string;
}

/**
 * Finds, fetches and verifies a signature for `subjectDigest` within
 * `repositoryRoot`: every referrer-API-or-fallback-tag candidate of
 * `bundleArtifactType` (`referrers.ts`), each one's own manifest-then-layer
 * hop (a referrer is itself a small OCI artifact; its layer blob holds the
 * actual bundle JSON), tried in order until one verifies.
 *
 * `checkContent` is the caller's statement check (`checkArtifactStatement`
 * for the main artifact, `checkGenericStatement` for `fetchSigned`) — it
 * throws `ArtifactCorruptError` on a content mismatch, and that throw
 * propagates immediately, out of this function, without trying further
 * candidates (this module's header explains why).
 *
 * A candidate whose manifest or layer cannot even be fetched is skipped
 * (treated the same as a signature that failed to verify) rather than
 * aborting the whole search — a dangling or unreachable referrer on one
 * mirror should not prevent trying another.
 *
 * Returns `undefined` when no candidate's signature verified under any
 * trusted key at all — the caller raises `ArtifactUntrustedError` (or
 * honors allow-unsigned).
 */
export async function verifyAnyReferrerBundle(
  repositoryRoot: string,
  subjectDigest: string,
  bundleArtifactType: string,
  trustedKeys: readonly TrustedKey[],
  checkContent: (statement: Statement) => void,
  options: RequestOptions,
): Promise<TrustResult | undefined> {
  const candidates = await discoverSignatureCandidates(repositoryRoot, subjectDigest, bundleArtifactType, options);
  for (const candidate of candidates) {
    let bundleBytes: Buffer;
    let referrerManifest: ManifestInfo;
    try {
      referrerManifest = await fetchManifestByDigest([repositoryRoot], candidate.digest, options);
      bundleBytes = await fetchBlobBytesByDigest([repositoryRoot], referrerManifest.layer, options);
    } catch {
      continue;
    }
    const verified = verifyBundleSignature(bundleBytes, trustedKeys);
    if (verified === undefined) continue;
    const statement = parseStatement(verified.payload);
    checkContent(statement);
    return { statement, signedBy: verified.signedBy, bundleDigest: referrerManifest.layer.digest, bundleManifestDigest: referrerManifest.digest };
  }
  return undefined;
}
