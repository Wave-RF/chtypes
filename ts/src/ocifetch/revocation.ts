/**
 * Build-level revocation (`docs/guides/fetch-v1.md`, "Build-level revocation";
 * public issue #625). A revocation is a signed referrer of a platform manifest,
 * read from the listing its signature lookup reads; one that verifies refuses the
 * build with `ArtifactRevokedError` and is stored in the build's cache entry, and
 * one that does not is ignored with one warning. A 410 whose body's
 * `errors[0].detail.revoked` is an object is the same code (`retired.ts`). A
 * status that cannot be read fails open with one warning, or in strict mode is the
 * transport's error. Generation 2 only (`Channel.revocation`): the v1 contract has
 * none of it.
 *
 * Four paths check, each with its own budget, exactly as Go's `revocation.go`:
 *
 *   - an install reads its revocation candidates from the trust listing it already
 *     made, under the retry table;
 *   - `--frozen` reads one listing, warm or cold, under the retry table;
 *   - a build already installed and used without a download (a registry open,
 *     preload, an ensure that finds it installed) is checked at most once per
 *     process (the memo below), one attempt per base, no backoff, and one deadline
 *     for the whole check;
 *   - `--lock` and `update` refuse a revoked build on every platform they pin.
 */

import { randomBytes } from 'node:crypto';
import { readFile, rename, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { activeChannel, testOnly } from './channel.js';
import {
  BUNDLE_MAX_BYTES,
  CACHE_REVOCATION_RECORD,
  CODE_SOURCE_FORBIDDEN,
  CODE_SOURCE_UNAUTHORIZED,
  CODE_SOURCE_UNREACHABLE,
  PREDICATE_TYPE_REVOCATION,
  REVOCATION_CHECK_S,
  STATEMENT_TYPE,
} from './constants.gen.js';
import { parseStatement, verifyBundleSignature } from './dsse.js';
import {
  type Advisory,
  ArtifactCorruptError,
  ArtifactRevokedError,
  type FetchV1ErrorCode,
  SourceForbiddenError,
  SourceUnauthorizedError,
  SourceUnreachableError,
  type Revocation,
} from './errors.js';
import type { RequestOptions } from './http.js';
import { fetchBlobBytesByDigest, fetchManifestByDigest } from './oci.js';
import { type ReferrerDescriptor, readListing } from './referrers.js';
import { isGone, sanitizeRetiredText } from './retired.js';
import type { Resolved, Statement, TrustedKey } from './types.js';

// ------------------------------------------------------------------ the context

/** What one revocation check reads from the call's options, derived once (`ensure.ts`'s `revocationContext`). */
export interface RevocationContext {
  /** The configured bases, in order. */
  readonly bases: readonly string[];
  /** The trust list the build itself is checked under. */
  readonly trustedKeys: readonly TrustedKey[];
  /** The request options every fetch over `bases` sends (`requestOptionsFor`). */
  readonly request: Omit<RequestOptions, 'maxBytes'>;
  /** The cache root a writable entry lies under. */
  readonly cacheRoot: string;
  readonly offline: boolean;
  /** Per call, and never with a lock or an update (`revocationContext` drops it). */
  readonly allowRevoked: boolean;
  /** A revocation status that cannot be read is the transport's error, not a warning. */
  readonly requireRevocationCheck: boolean;
}

/** Whether the active contract has revocation at all. */
export function revocationOn(): boolean {
  return activeChannel().revocation;
}

// ------------------------------------------------------------------- the subject

/**
 * The build a check concerns: its own digests and signed predicate, and its
 * installed entry (`dir`) when it has one, writable when that entry is in the
 * user cache.
 */
export interface RevocationSubject {
  readonly manifest: string;
  readonly layer: string;
  readonly predicate: Readonly<Record<string, unknown>>;
  readonly version: string;
  readonly platform: string;
  readonly build: string;
  readonly request: string;
  readonly dir: string;
  readonly writable: boolean;
}

function underDir(root: string, dir: string): boolean {
  const rel = path.relative(root, dir);
  return rel !== '' && rel !== '.' && !rel.startsWith('..') && !path.isAbsolute(rel);
}

/** The subject an installed build's `Resolved` names. */
export function subjectOf(ctx: RevocationContext, res: Resolved): RevocationSubject {
  return {
    manifest: res.digests.manifest,
    layer: res.digests.layer,
    predicate: res.predicate as unknown as Readonly<Record<string, unknown>>,
    version: res.version,
    platform: res.platform,
    build: res.build,
    request: res.request,
    dir: res.dir,
    writable: res.dir !== '' && underDir(ctx.cacheRoot, res.dir),
  };
}

function stringPredicate(pred: Readonly<Record<string, unknown>>, key: string): string {
  const v = pred[key];
  return typeof v === 'string' ? v : '';
}

/** The subject of a build not yet installed: the signed statement verified for it. */
export function subjectOfStatement(statement: Statement, manifest: string, layer: string, platform: string, request: string): RevocationSubject {
  const pred = statement.predicate;
  return {
    manifest,
    layer,
    predicate: pred,
    version: stringPredicate(pred, 'clickhouse_version'),
    platform,
    build: stringPredicate(pred, 'build'),
    request,
    dir: '',
    writable: false,
  };
}

// ---------------------------------------------------------------- verification

function isInt(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v);
}

function show(v: unknown): string {
  return typeof v === 'string' ? JSON.stringify(v) : String(v);
}

/**
 * One revocation bundle against `sub` (the guide's five rules): the signature
 * under `keys`, the statement and predicate types, the subject and manifest, the
 * build's identity, and the advisory's shape. The revocation, or why it does not
 * verify. A signed payload that is not a statement is ignored, never CORRUPT.
 */
export function verifyRevocation(body: Buffer, keys: readonly TrustedKey[], sub: RevocationSubject): { readonly revocation: Revocation } | { readonly reason: string } {
  const signed = verifyBundleSignature(body, keys);
  if (signed === undefined) return { reason: 'no signature verifies under a trusted key' };
  let st: Statement;
  try {
    st = parseStatement(signed.payload);
  } catch (err) {
    if (err instanceof ArtifactCorruptError) return { reason: err.message };
    throw err;
  }
  if (st._type !== STATEMENT_TYPE) return { reason: `statement _type ${JSON.stringify(st._type)}, expected ${JSON.stringify(STATEMENT_TYPE)}` };
  if (st.predicateType !== PREDICATE_TYPE_REVOCATION) {
    return { reason: `predicateType ${JSON.stringify(st.predicateType)}, expected ${JSON.stringify(PREDICATE_TYPE_REVOCATION)}` };
  }
  const layerHex = sub.layer.startsWith('sha256:') ? sub.layer.slice('sha256:'.length) : sub.layer;
  if (!st.subject.some((s) => s.digest.sha256 === layerHex)) return { reason: `no subject digest is the build's layer ${sub.layer}` };
  const pred = st.predicate;
  const m = pred['manifest'];
  if (m !== sub.manifest) return { reason: `predicate manifest ${show(m)} is not the build's ${sub.manifest}` };
  const got = pred['abi'];
  const want = sub.predicate['abi'];
  if (!isInt(got) || !isInt(want) || got !== want) return { reason: `predicate abi ${show(got)} is not the build's ${show(want)}` };
  for (const f of ['abi_fingerprint', 'os', 'arch', 'clickhouse_version', 'build']) {
    const g = pred[f];
    const w = sub.predicate[f];
    if (typeof g !== 'string' || typeof w !== 'string' || g !== w) return { reason: `predicate ${f} ${show(g)} is not the build's ${show(w)}` };
  }
  const adv = pred['advisory'];
  const advObj = adv !== null && typeof adv === 'object' && !Array.isArray(adv) ? (adv as Record<string, unknown>) : {};
  const id = advObj['id'];
  const url = advObj['url'];
  if (typeof id !== 'string' || id === '' || typeof url !== 'string' || url === '') {
    return { reason: 'predicate advisory does not name an id and a URL' };
  }
  const strs: Record<string, string> = {};
  for (const f of ['class', 'summary', 'superseded_by', 'revoked_at']) {
    const v = pred[f];
    if (typeof v !== 'string') return { reason: `predicate ${f} is not a string` };
    strs[f] = v;
  }
  const advisory: Advisory = {
    id: sanitizeRetiredText(id),
    url: sanitizeRetiredText(url),
    class: sanitizeRetiredText(strs['class']!),
    summary: sanitizeRetiredText(strs['summary']!),
    build: sub.build,
    supersededBy: sanitizeRetiredText(strs['superseded_by']!),
    revokedAt: sanitizeRetiredText(strs['revoked_at']!),
  };
  return { revocation: { signed: true, manifest: sub.manifest, advisories: [advisory] } };
}

/**
 * The guide's message for a verified revocation: `<version> <platform> build
 * <build> (manifest <digest>) is revoked: <id> <url> (<class>); superseded by
 * build <superseded_by>: <summary>`, a part whose field is empty left out.
 */
export function revokedText(sub: RevocationSubject, rev: Revocation): string {
  const a = rev.advisories[0]!;
  let msg = `${sub.version} ${sub.platform} build ${sub.build} (manifest ${sub.manifest}) is revoked: ${a.id} ${a.url}`;
  if (a.class !== '') msg += ` (${a.class})`;
  if (a.supersededBy !== '') msg += `; superseded by build ${a.supersededBy}`;
  if (a.summary !== '') msg += `: ${a.summary}`;
  return msg;
}

// ------------------------------------------------------------------- the outcome

/** One verified revocation: the bundle, for the stored record, and what it says. */
export interface FoundRevocation {
  readonly bundle: Buffer;
  readonly revocation: Revocation;
}

/** Why a status could not be read: the text, and the code the transport's error carries in strict mode. */
export interface Unread {
  readonly text: string;
  readonly code: FetchV1ErrorCode;
}

/**
 * What one check learned: a verified revocation, the warnings for the candidates
 * it ignored, and, when it could not read the status, why.
 */
export interface RevocationOutcome {
  verified: FoundRevocation | undefined;
  readonly warnings: string[];
  unread: Unread | undefined;
}

function codeOf(err: unknown): FetchV1ErrorCode {
  if (err instanceof SourceUnauthorizedError) return CODE_SOURCE_UNAUTHORIZED;
  if (err instanceof SourceForbiddenError) return CODE_SOURCE_FORBIDDEN;
  return CODE_SOURCE_UNREACHABLE;
}

function requestFor(ctx: RevocationContext, single: boolean, signal: AbortSignal | undefined): RequestOptions {
  return { ...ctx.request, maxBytes: 0, ...(single ? { single: true } : {}), ...(signal !== undefined ? { signal } : {}) };
}

/** One referrer's bundle: its manifest by digest, then its one layer, at most `BUNDLE_MAX_BYTES`. */
async function fetchReferrerBundle(bases: readonly string[], cand: ReferrerDescriptor, options: RequestOptions): Promise<Buffer> {
  const manifest = await fetchManifestByDigest(bases, cand.digest, options);
  if (manifest.layer.size !== undefined && manifest.layer.size > BUNDLE_MAX_BYTES) {
    throw new ArtifactCorruptError(`chtypes: referrer ${cand.digest}'s layer names ${manifest.layer.size} bytes, over the ${BUNDLE_MAX_BYTES}-byte bundle cap`);
  }
  return fetchBlobBytesByDigest(bases, manifest.layer, options);
}

/**
 * Fetches and verifies each revocation candidate in turn; the first that
 * verifies is the answer. A candidate whose bytes cannot be fetched leaves the
 * status unread; one fetched but corrupt or not verifying is ignored with a
 * warning. Its only error is a 410's.
 */
export async function checkCandidates(
  ctx: RevocationContext,
  bases: readonly string[],
  cands: readonly ReferrerDescriptor[],
  sub: RevocationSubject,
  single: boolean,
  signal?: AbortSignal,
): Promise<RevocationOutcome> {
  const out: RevocationOutcome = { verified: undefined, warnings: [], unread: undefined };
  for (const cand of cands) {
    let body: Buffer | undefined;
    let reason = '';
    try {
      body = await fetchReferrerBundle(bases, cand, requestFor(ctx, single, signal));
    } catch (err) {
      if (isGone(err)) throw err;
      if (!(err instanceof ArtifactCorruptError)) {
        out.unread = { text: `revocation ${cand.digest} could not be read: ${err instanceof Error ? err.message : String(err)}`, code: codeOf(err) };
        continue;
      }
      reason = err.message;
    }
    if (body !== undefined) {
      const v = verifyRevocation(body, ctx.trustedKeys, sub);
      if ('revocation' in v) {
        out.verified = { bundle: body, revocation: v.revocation };
        return out;
      }
      reason = v.reason;
    }
    out.warnings.push(`chtypes: revocation ${cand.digest} for manifest ${sub.manifest} does not verify and is ignored: ${reason}`);
  }
  return out;
}

/**
 * One network check of `sub`: its listing, then its candidates. A listing that
 * names no signature referrer is an unread status.
 */
export async function checkListing(ctx: RevocationContext, sub: RevocationSubject, single: boolean, signal?: AbortSignal): Promise<RevocationOutcome> {
  const l = await readListing(ctx.bases, sub.manifest, requestFor(ctx, single, signal));
  if (!l.found) {
    const cause = l.failure ?? new Error('no listing names a signature for this manifest');
    return { verified: undefined, warnings: [], unread: { text: cause.message, code: codeOf(l.failure) } };
  }
  return checkCandidates(ctx, ctx.bases, l.revocations, sub, single, signal);
}

/**
 * The guide's fail-open warning, without the `chtypes: ` every warning starts
 * with: `revocation status not checked for manifest <digest> (bases: <base>,
 * <base>): <cause>`. A base's password is redacted.
 */
function notCheckedText(sub: RevocationSubject, bases: readonly string[], cause: string): string {
  const shown = bases.map((b) => {
    try {
      const u = new URL(b);
      if (u.password !== '') {
        u.password = 'xxxxx';
        return u.toString();
      }
    } catch {
      // Not a URL: shown as given.
    }
    return b;
  });
  return `revocation status not checked for manifest ${sub.manifest} (bases: ${shown.join(', ')}): ${cause}`;
}

function strictError(unread: Unread, text: string): Error {
  const message = `chtypes: ${text}`;
  if (unread.code === CODE_SOURCE_UNAUTHORIZED) return new SourceUnauthorizedError(message);
  if (unread.code === CODE_SOURCE_FORBIDDEN) return new SourceForbiddenError(message);
  return new SourceUnreachableError(message);
}

/**
 * Turns an outcome into the call's answer, its warnings pushed on `warn`: a
 * verified revocation is stored in an installed, writable entry (when `store`)
 * and refuses the build, or with the override becomes one warning; an unread
 * status is one warning, or in strict mode the transport's error.
 */
export async function settle(ctx: RevocationContext, sub: RevocationSubject, out: RevocationOutcome, store: boolean, warn: string[]): Promise<void> {
  warn.push(...out.warnings);
  if (out.verified !== undefined) {
    if (store && sub.writable) {
      // A read-only cache still refuses, for this call and process.
      await writeRevocationRecord(sub.dir, out.verified.bundle).catch(() => {});
    }
    const text = revokedText(sub, out.verified.revocation);
    if (ctx.allowRevoked) {
      warn.push(`chtypes: allowing a revoked build: ${text}`);
      return;
    }
    throw new ArtifactRevokedError(`chtypes: ${text}`, out.verified.revocation);
  }
  if (out.unread !== undefined) {
    const text = notCheckedText(sub, ctx.bases, out.unread.text);
    if (ctx.requireRevocationCheck) throw strictError(out.unread, text);
    warn.push(`chtypes: ${text}`);
  }
}

// ------------------------------------------------------------------ the stored record

/** Stores a verified revocation's bundle, byte for byte, in an installed build's entry: a temporary file, then a rename. */
export async function writeRevocationRecord(dir: string, bundle: Buffer): Promise<void> {
  const dest = path.join(dir, CACHE_REVOCATION_RECORD);
  const existing = await readFile(dest).catch(() => undefined);
  if (existing !== undefined && existing.equals(bundle)) return;
  const tmp = path.join(dir, `.${CACHE_REVOCATION_RECORD}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`);
  try {
    await writeFile(tmp, bundle);
    await rename(tmp, dest);
  } catch (err) {
    await unlink(tmp).catch(() => {});
    throw err;
  }
}

/** The revocation an install let through under the override, stored in the entry it installed (a failed write is ignored). */
export async function storePending(ctx: RevocationContext, dir: string, pending: FoundRevocation | undefined): Promise<void> {
  if (pending !== undefined && dir !== '' && underDir(ctx.cacheRoot, dir)) {
    await writeRevocationRecord(dir, pending.bundle).catch(() => {});
  }
}

/**
 * Reads `sub`'s stored record, if its entry holds one: verified, it decides the
 * call (returns `true`; a refusal is thrown); one that does not verify is a
 * warning, and the call goes on.
 */
export async function checkStored(ctx: RevocationContext, sub: RevocationSubject, warn: string[]): Promise<boolean> {
  if (sub.dir === '') return false;
  const file = path.join(sub.dir, CACHE_REVOCATION_RECORD);
  let body: Buffer;
  try {
    body = await readFile(file);
  } catch {
    return false;
  }
  const v = verifyRevocation(body, ctx.trustedKeys, sub);
  if ('reason' in v) {
    warn.push(`chtypes: revocation record ${file} for manifest ${sub.manifest} does not verify and is ignored: ${v.reason}`);
    return false;
  }
  await settle(ctx, sub, { verified: { bundle: body, revocation: v.revocation }, warnings: [], unread: undefined }, false, warn);
  return true;
}

/** The revocation `res`'s stored record holds, when it verifies, and its message (`chtypes verify`'s). */
export async function storedRevocation(ctx: RevocationContext, res: Resolved): Promise<{ readonly revocation: Revocation; readonly text: string } | undefined> {
  const sub = subjectOf(ctx, res);
  let body: Buffer;
  try {
    body = await readFile(path.join(sub.dir, CACHE_REVOCATION_RECORD));
  } catch {
    return undefined;
  }
  const v = verifyRevocation(body, ctx.trustedKeys, sub);
  return 'revocation' in v ? { revocation: v.revocation, text: revokedText(sub, v.revocation) } : undefined;
}

/** The check every call that answers from the cache with no request makes (`--offline`, `--frozen --offline`): the stored record. */
export async function checkInstalledStored(ctx: RevocationContext, res: Resolved): Promise<readonly string[]> {
  if (!revocationOn()) return [];
  const warn: string[] = [];
  await checkStored(ctx, subjectOf(ctx, res), warn);
  return warn;
}

/** `--frozen`'s check of the build installed under its pin: the stored record, then one listing under the retry table. */
export async function checkFrozenInstalled(ctx: RevocationContext, res: Resolved): Promise<readonly string[]> {
  if (!revocationOn()) return [];
  const warn: string[] = [];
  const sub = subjectOf(ctx, res);
  if (await checkStored(ctx, sub, warn)) return warn;
  const out = await checkListing(ctx, sub, false);
  await settle(ctx, sub, out, true, warn);
  return warn;
}

// ------------------------------------------------------------------------ the memo

type MemoState = 'not-revoked' | 'revoked' | 'not-checked';

interface MemoEntry {
  readonly done: Promise<void>;
  readonly finish: () => void;
  state: MemoState;
  found: FoundRevocation | undefined;
}

/** The installed-build check's in-process outcomes, by platform manifest digest: at most one check per build per process. */
const memo = new Map<string, MemoEntry>();

let deadlineSeconds = REVOCATION_CHECK_S as number;

/** Forgets every kept outcome, as a new process would. A conformance runner calls it before every case. Test-only. */
export function resetRevocationMemoForTests(): void {
  testOnly('resetRevocationMemoForTests');
  memo.clear();
}

/** Shortens (or, with `undefined`, restores) the installed-build check's one deadline. Test-only. */
export function setRevocationCheckDeadlineForTests(seconds: number | undefined): void {
  testOnly('setRevocationCheckDeadlineForTests');
  deadlineSeconds = seconds ?? REVOCATION_CHECK_S;
}

function newEntry(): MemoEntry {
  let finish!: () => void;
  const done = new Promise<void>((resolve) => {
    finish = resolve;
  });
  return { done, finish, state: 'not-revoked', found: undefined };
}

/** One network check under the installed-build check's budget. Passing the deadline is an unread status. */
async function checkWithDeadline(ctx: RevocationContext, sub: RevocationSubject): Promise<RevocationOutcome> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), deadlineSeconds * 1000);
  try {
    const out = await checkListing(ctx, sub, true, controller.signal);
    if (out.verified === undefined && out.unread !== undefined && controller.signal.aborted) {
      out.unread = { ...out.unread, text: `the check passed its ${deadlineSeconds}s deadline: ${out.unread.text}` };
    }
    return out;
  } finally {
    clearTimeout(timer);
  }
}

function keep(manifest: string, out: RevocationOutcome): void {
  const e = memo.get(manifest);
  if (e === undefined) return;
  if (out.verified !== undefined) {
    e.state = 'revoked';
    e.found = out.verified;
  } else {
    e.state = 'not-revoked';
  }
}

/**
 * The installed-build check: the stored record, then, unless offline, the kept
 * outcome or one check of its own, with one attempt per base, no backoff and one
 * deadline. Concurrent checks of one build share one; a kept "not checked"
 * answers only a call without strict mode, and a check that ends in an error
 * keeps nothing. Warnings go on `warn`; a refusal is thrown.
 */
export async function checkInstalledOnce(ctx: RevocationContext, res: Resolved, warn: string[]): Promise<void> {
  if (!revocationOn()) return;
  const sub = subjectOf(ctx, res);
  if ((await checkStored(ctx, sub, warn)) || ctx.offline) return;
  for (;;) {
    let e = memo.get(sub.manifest);
    const lead = e === undefined;
    if (e === undefined) {
      e = newEntry();
      memo.set(sub.manifest, e);
    }

    if (!lead) {
      await e.done;
      const current = memo.get(sub.manifest) === e;
      const { state, found } = e;
      if (!current) continue; // that check kept nothing: check again
      if (state === 'revoked') {
        await settle(ctx, sub, { verified: found, warnings: [], unread: undefined }, true, warn);
      } else if (state === 'not-checked' && ctx.requireRevocationCheck) {
        // A strict call never takes a kept "not checked": it checks for itself,
        // and keeps a better answer when it gets one.
        const out = await checkWithDeadline(ctx, sub);
        if (out.unread === undefined) keep(sub.manifest, out);
        await settle(ctx, sub, out, true, warn);
      }
      return; // not revoked, or not checked and already warned
    }

    let out: RevocationOutcome | undefined;
    try {
      out = await checkWithDeadline(ctx, sub);
      await settle(ctx, sub, out, true, warn);
      if (out.verified !== undefined) {
        e.state = 'revoked';
        e.found = out.verified;
      } else if (out.unread !== undefined) {
        e.state = 'not-checked';
      } else {
        e.state = 'not-revoked';
      }
    } catch (err) {
      if (out?.verified !== undefined) {
        e.state = 'revoked';
        e.found = out.verified;
      } else {
        memo.delete(sub.manifest); // a 410 or a strict failure: keep nothing
      }
      throw err;
    } finally {
      e.finish();
    }
    return;
  }
}
