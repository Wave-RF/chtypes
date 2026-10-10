/**
 * The v1 HTTP stack (`docs/guides/fetch-v1.md` §2, §7): one retry table, one
 * redirect policy, the anonymous bearer-token flow (for mirrors), the system
 * CA store, and `HTTPS_PROXY`/`NO_PROXY`.
 *
 * Deliberately **not** global `fetch`: Node's built-in `fetch` (undici) does
 * not honor `HTTPS_PROXY` without a process-wide flag. `https.Agent({
 * proxyEnv })` and `tls.getCACertificates` are both real APIs on the v1 Node
 * floor (>=22.21) — measured on macOS (Node 22.23.2) during this lane's own
 * work: `tls.getCACertificates('system')` returns the Keychain roots,
 * `tls.getCACertificates('default')` returns Node's bundled Mozilla set, and
 * an `https.Agent({ proxyEnv: process.env })` request to an `https:` origin
 * issues a `CONNECT` to the host named by `HTTPS_PROXY` before anything else.
 * The Linux leg of that measurement is `v1-network`'s to make (not yet wired
 * — lane 0B).
 */

import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import * as http from 'node:http';
import * as https from 'node:https';
import * as tls from 'node:tls';
import { fileURLToPath } from 'node:url';
import {
  CONNECT_TIMEOUT_S,
  IDLE_READ_TIMEOUT_S,
  MAX_REDIRECTS,
  RETIRED_BODY_MAX_BYTES,
  RETRY_AFTER_STATUSES,
  RETRY_ATTEMPTS,
  RETRY_FIRST_WAIT_S,
  RETRY_MULTIPLIER,
  RETRY_STATUSES,
} from './constants.gen.js';
import { ArtifactCorruptError, SourceForbiddenError, SourceRetiredError, SourceUnauthorizedError, SourceUnreachableError } from './errors.js';
import { isRetiredStatus, readRetiredBody, retiredText } from './retired.js';
import type { Clock } from './types.js';

/**
 * The ceiling past which a `Retry-After` is refused rather than shortened
 * (`docs/guides/fetch-v1.md` §7, A6). Not yet pinned by a fixture (lane 0B);
 * `IDLE_READ_TIMEOUT_S` is this lane's own reasoned choice — the fetch layer
 * declines to sit idle waiting longer than it would already time out a read
 * for. **Flag for the merge/integration lane**: adjust this to match
 * `retry-after-over-budget`'s actual fixture value once it exists.
 */
export const RETRY_AFTER_BUDGET_S = IDLE_READ_TIMEOUT_S;

export interface HttpResult {
  readonly status: number;
  readonly headers: Readonly<Record<string, string>>;
  readonly url: string;
}

export interface BufferedResult extends HttpResult {
  readonly body: Buffer;
}

export interface RequestOptions {
  readonly method?: 'GET' | 'HEAD';
  readonly headers?: Readonly<Record<string, string>>;
  readonly maxBytes: number;
  readonly clock: Clock;
  /** Sent as `Authorization: Bearer <token>` — only to a host in this exact list. */
  readonly token?: string;
  readonly tokenHosts?: readonly string[];
  readonly onRequest?: (url: string) => void;
  /**
   * `docs/guides/fetch-v1.md` §7's digest rule: "a listed digest that 404s is
   * a host fault" — on the **last** configured base only, `oci.ts` retries a
   * by-digest 404 within the normal budget instead of accepting it as
   * terminal, so this option makes 404 itself a retryable status for one
   * call.
   */
  readonly retryOn404?: boolean;
}

/** One GET/HEAD attempt's outcome, before the retry loop decides what to do with it. */
interface AttemptOutcome {
  readonly ok: boolean;
  readonly retryable: boolean;
  readonly status: number | undefined;
  readonly retryAfterSeconds: number | undefined;
  readonly message: string;
}

/**
 * Runs `attempt` under the shared retry table (`docs/guides/fetch-v1.md`
 * §7): 5 attempts, waits doubling from 4s, honoring `Retry-After` on 429/503
 * and refusing one past `RETRY_AFTER_BUDGET_S`. `attempt` returns `{ok:
 * true}` with its own value already produced as a side effect (buffering a
 * body, or writing a file) — this loop only decides whether to retry.
 * `attempt` throwing directly (rather than returning `{ok: false, ...}`)
 * skips the retry loop entirely — used for a failure that is never
 * transient, such as an oversize response (`CORRUPT`, never retried).
 */
export async function withRetry<T>(
  clock: Clock,
  attempt: (attemptNumber: number) => Promise<{ outcome: AttemptOutcome; value?: T }>,
): Promise<T> {
  let last: AttemptOutcome | undefined;
  for (let n = 1; n <= RETRY_ATTEMPTS; n++) {
    const { outcome, value } = await attempt(n);
    if (outcome.ok) return value as T;
    last = outcome;
    const isLast = n === RETRY_ATTEMPTS;
    if (!outcome.retryable || isLast) break;
    let waitSeconds = RETRY_FIRST_WAIT_S * RETRY_MULTIPLIER ** (n - 1);
    if (outcome.retryAfterSeconds !== undefined) {
      if (outcome.retryAfterSeconds > RETRY_AFTER_BUDGET_S) {
        throw new SourceUnreachableError(
          `chtypes: Retry-After named ${outcome.retryAfterSeconds}s, past the ${RETRY_AFTER_BUDGET_S}s retry budget; refusing rather than sleeping it`,
        );
      }
      waitSeconds = Math.max(0, outcome.retryAfterSeconds);
    }
    await clock.sleep(waitSeconds);
  }
  const statusPart = last?.status !== undefined ? `, last status ${last.status}` : '';
  throw new SourceUnreachableError(
    `chtypes: ${last?.message ?? 'request failed'} (after ${RETRY_ATTEMPTS} attempts${statusPart})`,
  );
}

/** Is this status one the retry table retries on 408/429/500/502/503/504? */
function isRetryableStatus(status: number): boolean {
  return (RETRY_STATUSES as readonly number[]).includes(status);
}

/**
 * A connection-level failure meaning "nothing is answering here at all" —
 * refused, no route, or DNS failed to resolve a name — as opposed to a
 * timeout waiting for a slow or stalled peer. The retry table's backoff
 * schedule is for a peer that IS there but answering badly (a status code,
 * or a stall); a dead host gets **zero** sleep and fails this base
 * immediately, so the caller's multi-base loop moves on at once
 * (`connection-refused-then-next-base`: sleeps `[]`, not `[4]`). A timeout
 * (this server's own `connect timeout` on a stalled socket, `ETIMEDOUT`,
 * `ECONNRESET` mid-stream) stays in the normal backoff-and-retry path
 * (`stall-timeout-retried`: sleeps `[4]`).
 */
function isDeadHostError(err: unknown): boolean {
  const code = (err as NodeJS.ErrnoException | undefined)?.code;
  return code === 'ECONNREFUSED' || code === 'ENOTFOUND' || code === 'EAI_AGAIN' || code === 'EHOSTUNREACH' || code === 'ENETUNREACH';
}

/**
 * `Retry-After`, in both forms (`docs/guides/fetch-v1.md` §7): a delta in
 * seconds, or an HTTP-date, for which the delay is the date minus the
 * response's own `Date` header (or `clock.now()` when absent).
 */
export function parseRetryAfterSeconds(
  value: string,
  responseDateHeader: string | undefined,
  clock: Clock,
): number | undefined {
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10);
  const target = Date.parse(trimmed);
  if (Number.isNaN(target)) return undefined;
  const base = responseDateHeader !== undefined ? Date.parse(responseDateHeader) : Number.NaN;
  const from = Number.isNaN(base) ? clock.now() : base;
  return Math.max(0, (target - from) / 1000);
}

function flattenHeaders(raw: http.IncomingHttpHeaders): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (v === undefined) continue;
    out[k.toLowerCase()] = Array.isArray(v) ? v.join(', ') : v;
  }
  return out;
}

/** The system + bundled CA roots, per `docs/guides/fetch-v1.md` §2. */
function systemAndDefaultCertificates(): string[] {
  const out: string[] = [];
  try {
    out.push(...tls.getCACertificates('default'));
  } catch {
    // Older runtime without this half of the API: fall back to the platform default.
  }
  try {
    out.push(...tls.getCACertificates('system'));
  } catch {
    // No system store reachable (e.g. a minimal container); default-only is still correct.
  }
  return out;
}

let cachedAgent: https.Agent | undefined;
function httpsAgent(): https.Agent {
  cachedAgent ??= new https.Agent({
    // A real, measured Node >=22.21 API — see this file's header comment.
    proxyEnv: process.env,
    ca: systemAndDefaultCertificates(),
  } as https.AgentOptions);
  return cachedAgent;
}

let cachedPlainAgent: http.Agent | undefined;
function httpAgent(): http.Agent {
  cachedPlainAgent ??= new http.Agent({ proxyEnv: process.env } as http.AgentOptions);
  return cachedPlainAgent;
}

function isSameOrigin(a: URL, b: URL): boolean {
  return a.protocol === b.protocol && a.hostname === b.hostname && (a.port || defaultPort(a)) === (b.port || defaultPort(b));
}

function defaultPort(u: URL): string {
  return u.protocol === 'https:' ? '443' : '80';
}

const USER_AGENT_DEV_VERSION = '0.0.0-dev';

/** The package's own version, from its `package.json` (one level above `src/` and `dist/` alike). */
function readPackageVersion(): string {
  try {
    const raw: unknown = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8'));
    const v = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>)['version'] : undefined;
    return typeof v === 'string' && /^[0-9A-Za-z.+-]+$/.test(v) ? v : USER_AGENT_DEV_VERSION;
  } catch {
    return USER_AGENT_DEV_VERSION;
  }
}

/**
 * `chtypes-ts/<version>`, the User-Agent on every request this module makes
 * (`docs/guides/fetch-v1.md` §2): delivery hosts may refuse a generic library
 * agent. Never omitted; `0.0.0-dev` when the version cannot be read.
 */
export const USER_AGENT = `chtypes-ts/${readPackageVersion()}`;

interface RawResponse {
  readonly status: number;
  readonly headers: Record<string, string>;
  readonly stream: http.IncomingMessage;
}

/** One connection attempt with no retry and no redirect following — the unit `requestBuffered`/`requestToSink` build on. */
function rawRequest(url: URL, method: 'GET' | 'HEAD', callerHeaders: Record<string, string>): Promise<RawResponse> {
  // The one place a request is made: manifests, blobs, referrers, tag lists,
  // token exchanges and every redirect hop come through here, so the agent is
  // set here and nowhere else. It replaces any caller-supplied one.
  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(callerHeaders)) if (k.toLowerCase() !== 'user-agent') headers[k] = v;
  headers['user-agent'] = USER_AGENT;
  return new Promise((resolve, reject) => {
    const mod = url.protocol === 'https:' ? https : http;
    const agent = url.protocol === 'https:' ? httpsAgent() : httpAgent();
    const req = mod.request(
      url,
      {
        method,
        agent,
        headers,
        timeout: CONNECT_TIMEOUT_S * 1000,
      },
      (res) => {
        resolve({ status: res.statusCode ?? 0, headers: flattenHeaders(res.headers), stream: res });
      },
    );
    req.once('timeout', () => req.destroy(new Error('connect timeout')));
    req.once('error', reject);
    req.end();
  });
}

/**
 * `anonBearerToken` (freshly obtained for this exact host via the mirror
 * token flow) is always applied when present. `token` (the static
 * `CHTYPES_DOWNLOAD_TOKEN`) is restricted to `tokenHosts` — an exact-host
 * allowlist of the configured bases, never "whatever host we happen to be
 * calling" (`docs/guides/fetch-v1.md` §2).
 */
function buildHeaders(
  url: URL,
  base: Readonly<Record<string, string>> | undefined,
  token: string | undefined,
  tokenHosts: readonly string[] | undefined,
  anonBearerToken: string | undefined,
): Record<string, string> {
  const headers: Record<string, string> = { ...base };
  if (anonBearerToken !== undefined) {
    headers['authorization'] = `Bearer ${anonBearerToken}`;
  } else if (token !== undefined && tokenHosts !== undefined && tokenHosts.includes(url.host)) {
    headers['authorization'] = `Bearer ${token}`;
  }
  return headers;
}

// ------------------------------------------------------- anonymous token flow
//
// `docs/guides/fetch-v1.md` §2 / the plan's §1.1: mirrors may front the
// distribution API with a token endpoint (`WWW-Authenticate: Bearer
// realm="…",service="…",scope="…"` on 401). Our own host never 401s a
// public pull (A8), so this only ever fires against a mirror.

interface BearerChallenge {
  readonly realm: string;
  readonly service: string | undefined;
  readonly scope: string | undefined;
}

function parseBearerChallenge(headerValue: string | undefined): BearerChallenge | undefined {
  if (headerValue === undefined) return undefined;
  const m = /^\s*Bearer\s+(.*)$/i.exec(headerValue);
  if (m?.[1] === undefined) return undefined;
  const params: Record<string, string> = {};
  const re = /(\w+)="([^"]*)"/g;
  let mm: RegExpExecArray | null = re.exec(m[1]);
  while (mm !== null) {
    if (mm[1] !== undefined && mm[2] !== undefined) params[mm[1]] = mm[2];
    mm = re.exec(m[1]);
  }
  if (params['realm'] === undefined) return undefined;
  return { realm: params['realm'], service: params['service'], scope: params['scope'] };
}

/** Fetches an anonymous bearer token from a mirror's realm. `undefined` on any failure — the caller falls back to a plain 401. */
async function fetchAnonymousToken(challenge: BearerChallenge, clock: Clock): Promise<string | undefined> {
  try {
    const realm = new URL(challenge.realm);
    if (challenge.service !== undefined) realm.searchParams.set('service', challenge.service);
    if (challenge.scope !== undefined) realm.searchParams.set('scope', challenge.scope);
    const res = await rawRequest(realm, 'GET', {});
    const body = await drain(res.stream, 1 << 20).catch(() => undefined);
    void clock;
    if (body === undefined || res.status !== 200) return undefined;
    const parsed: unknown = JSON.parse(body.toString('utf8'));
    if (parsed === null || typeof parsed !== 'object') return undefined;
    const token = (parsed as Record<string, unknown>)['token'] ?? (parsed as Record<string, unknown>)['access_token'];
    return typeof token === 'string' ? token : undefined;
  } catch {
    return undefined;
  }
}

/** Drains a stream into a `Buffer`. Rejects with `ArtifactCorruptError` (never retryable) past `maxBytes`, or the stream's own error otherwise. */
function drain(stream: http.IncomingMessage, maxBytes: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let total = 0;
    stream.on('data', (chunk: Buffer) => {
      total += chunk.length;
      if (total > maxBytes) {
        stream.destroy();
        reject(new ArtifactCorruptError(`chtypes: response exceeds the ${maxBytes}-byte limit`));
        return;
      }
      chunks.push(chunk);
    });
    stream.on('end', () => resolve(Buffer.concat(chunks)));
    stream.on('error', reject);
  });
}

/**
 * GET/HEAD a small, fully-buffered object (a manifest, a referrers listing,
 * `tags/list`, a bundle) — redirects, retries, the anonymous token flow, the
 * system CA store and the proxy all handled. Throws
 * `SourceUnauthorizedError`/`SourceForbiddenError` on a 401/403 that survives
 * (neither code is retryable), `SourceRetiredError` on a 410 (a retired
 * repository, never retried) and `ArtifactCorruptError` on an oversize body
 * (never retryable — an oversize response is a content problem, not a
 * transient one). Any other non-2xx status is returned to the caller to
 * interpret (404 in particular means different things to a tag and a digest
 * GET, decided in `oci.ts`).
 */
export async function requestBuffered(startUrl: string, options: RequestOptions): Promise<BufferedResult> {
  const method = options.method ?? 'GET';
  return withRetry(options.clock, async () => {
    let url = new URL(startUrl);
    const firstOrigin = url;
    let redirects = 0;
    let bearerToken: string | undefined;
    let triedAnonToken = false;
    let authDropped = false;
    options.onRequest?.(url.toString());
    for (;;) {
      let res: RawResponse;
      try {
        const headers = buildHeaders(
          url,
          options.headers,
          authDropped ? undefined : options.token,
          options.tokenHosts,
          bearerToken,
        );
        res = await rawRequest(url, method, headers);
      } catch (err) {
        return {
          outcome: {
            ok: false,
            retryable: !isDeadHostError(err),
            status: undefined,
            retryAfterSeconds: undefined,
            message: `${method} ${url.toString()} failed: ${(err as Error).message}`,
          },
        };
      }
      if (res.status >= 300 && res.status < 400 && res.headers['location'] !== undefined) {
        res.stream.resume();
        if (redirects >= MAX_REDIRECTS) {
          return {
            outcome: {
              ok: false,
              retryable: false,
              status: res.status,
              retryAfterSeconds: undefined,
              message: `too many redirects (> ${MAX_REDIRECTS}) fetching ${startUrl}`,
            },
          };
        }
        const next = new URL(res.headers['location'], url);
        redirects += 1;
        const crossOrigin = !isSameOrigin(firstOrigin, next) || !isSameOrigin(url, next);
        url = next;
        if (crossOrigin) {
          // Authorization (and our own Bearer token) never survive a cross-origin hop.
          authDropped = true;
          bearerToken = undefined;
        }
        continue;
      }
      if (isRetiredStatus(res.status)) {
        // A retired repository (guide §2): permanent, so thrown rather than
        // returned, which skips the retry loop and every base loop above it.
        const body = await readRetiredBody(res.stream, RETIRED_BODY_MAX_BYTES);
        throw new SourceRetiredError(`chtypes: ${retiredText(url.toString(), res.status, body)}`);
      }
      if (res.status === 401) {
        const challenge = parseBearerChallenge(res.headers['www-authenticate']);
        res.stream.resume();
        if (!triedAnonToken && challenge !== undefined) {
          triedAnonToken = true;
          const token = await fetchAnonymousToken(challenge, options.clock);
          if (token !== undefined) {
            bearerToken = token;
            continue;
          }
        }
        throw new SourceUnauthorizedError(`chtypes: ${method} ${startUrl}: 401 from ${url.host}`);
      }
      if (res.status === 403) {
        res.stream.resume();
        throw new SourceForbiddenError(`chtypes: ${method} ${startUrl}: 403 from ${url.host}`);
      }
      if (isRetryableStatus(res.status) || (res.status === 404 && options.retryOn404 === true)) {
        let retryAfterSeconds: number | undefined;
        if ((RETRY_AFTER_STATUSES as readonly number[]).includes(res.status) && res.headers['retry-after'] !== undefined) {
          retryAfterSeconds = parseRetryAfterSeconds(res.headers['retry-after'], res.headers['date'], options.clock);
        }
        await drain(res.stream, options.maxBytes).catch(() => undefined);
        return {
          outcome: {
            ok: false,
            retryable: true,
            status: res.status,
            retryAfterSeconds,
            message: `${method} ${url.toString()} returned ${res.status}`,
          },
        };
      }
      let body: Buffer;
      try {
        body = await drain(res.stream, options.maxBytes);
      } catch (err) {
        if (err instanceof ArtifactCorruptError) throw err;
        return {
          outcome: {
            ok: false,
            retryable: true,
            status: res.status,
            retryAfterSeconds: undefined,
            message: `${method} ${url.toString()}: ${(err as Error).message}`,
          },
        };
      }
      return {
        outcome: { ok: true, retryable: false, status: res.status, retryAfterSeconds: undefined, message: '' },
        value: { status: res.status, headers: res.headers, url: url.toString(), body },
      };
    }
  });
}

export interface ToSinkResult extends HttpResult {
  readonly size: number;
}

/**
 * GET a (potentially large) object straight to a sink — the layer blob.
 * Every retry **restarts the sink from scratch**: a transient failure
 * partway through a download cannot be resumed, so the caller's sink
 * (`unpack.ts`'s temp file + hasher) must support `reset()`. `maxBytes`
 * should be the exact size the manifest's layer descriptor declares —
 * exceeding it is `ArtifactCorruptError`, never retried, exactly like an
 * oversize manifest.
 */
export async function requestToSink(
  startUrl: string,
  options: RequestOptions,
  sink: { reset(): void; write(chunk: Buffer): void },
): Promise<ToSinkResult> {
  return withRetry(options.clock, async () => {
    let url = new URL(startUrl);
    const firstOrigin = url;
    let redirects = 0;
    let bearerToken: string | undefined;
    let triedAnonToken = false;
    let authDropped = false;
    options.onRequest?.(url.toString());
    for (;;) {
      let res: RawResponse;
      try {
        const headers = buildHeaders(
          url,
          options.headers,
          authDropped ? undefined : options.token,
          options.tokenHosts,
          bearerToken,
        );
        res = await rawRequest(url, 'GET', headers);
      } catch (err) {
        return {
          outcome: {
            ok: false,
            retryable: !isDeadHostError(err),
            status: undefined,
            retryAfterSeconds: undefined,
            message: `GET ${url.toString()} failed: ${(err as Error).message}`,
          },
        };
      }
      if (res.status >= 300 && res.status < 400 && res.headers['location'] !== undefined) {
        res.stream.resume();
        if (redirects >= MAX_REDIRECTS) {
          return {
            outcome: {
              ok: false,
              retryable: false,
              status: res.status,
              retryAfterSeconds: undefined,
              message: `too many redirects (> ${MAX_REDIRECTS}) fetching ${startUrl}`,
            },
          };
        }
        const next = new URL(res.headers['location'], url);
        redirects += 1;
        const crossOrigin = !isSameOrigin(firstOrigin, next) || !isSameOrigin(url, next);
        url = next;
        if (crossOrigin) {
          authDropped = true;
          bearerToken = undefined;
        }
        continue;
      }
      if (isRetiredStatus(res.status)) {
        // A retired repository (guide §2): permanent, so thrown rather than
        // returned, which skips the retry loop and every base loop above it.
        const body = await readRetiredBody(res.stream, RETIRED_BODY_MAX_BYTES);
        throw new SourceRetiredError(`chtypes: ${retiredText(url.toString(), res.status, body)}`);
      }
      if (res.status === 401) {
        const challenge = parseBearerChallenge(res.headers['www-authenticate']);
        res.stream.resume();
        if (!triedAnonToken && challenge !== undefined) {
          triedAnonToken = true;
          const token = await fetchAnonymousToken(challenge, options.clock);
          if (token !== undefined) {
            bearerToken = token;
            continue;
          }
        }
        throw new SourceUnauthorizedError(`chtypes: GET ${startUrl}: 401 from ${url.host}`);
      }
      if (res.status === 403) {
        res.stream.resume();
        throw new SourceForbiddenError(`chtypes: GET ${startUrl}: 403 from ${url.host}`);
      }
      if (isRetryableStatus(res.status) || (res.status === 404 && options.retryOn404 === true)) {
        let retryAfterSeconds: number | undefined;
        if ((RETRY_AFTER_STATUSES as readonly number[]).includes(res.status) && res.headers['retry-after'] !== undefined) {
          retryAfterSeconds = parseRetryAfterSeconds(res.headers['retry-after'], res.headers['date'], options.clock);
        }
        res.stream.resume();
        return {
          outcome: {
            ok: false,
            retryable: true,
            status: res.status,
            retryAfterSeconds,
            message: `GET ${url.toString()} returned ${res.status}`,
          },
        };
      }
      if (res.status !== 200 && res.status !== 206) {
        res.stream.resume();
        return {
          outcome: { ok: true, retryable: false, status: res.status, retryAfterSeconds: undefined, message: '' },
          value: { status: res.status, headers: res.headers, url: url.toString(), size: 0 },
        };
      }
      sink.reset();
      let size = 0;
      let oversize = false;
      try {
        await new Promise<void>((resolve, reject) => {
          res.stream.on('data', (chunk: Buffer) => {
            size += chunk.length;
            if (size > options.maxBytes) {
              oversize = true;
              res.stream.destroy();
              reject(new ArtifactCorruptError(`chtypes: layer exceeds the ${options.maxBytes}-byte limit`));
              return;
            }
            sink.write(chunk);
          });
          res.stream.on('end', resolve);
          res.stream.on('error', reject);
        });
      } catch (err) {
        if (oversize) throw err;
        return {
          outcome: {
            ok: false,
            retryable: true,
            status: res.status,
            retryAfterSeconds: undefined,
            message: `GET ${url.toString()}: ${(err as Error).message}`,
          },
        };
      }
      return {
        outcome: { ok: true, retryable: false, status: res.status, retryAfterSeconds: undefined, message: '' },
        value: { status: res.status, headers: res.headers, url: url.toString(), size },
      };
    }
  });
}

/**
 * Reads a `file://` base directly — no retry, no redirect, no query string
 * ever appended (a fixture rule: a route tree served from `file://` is a
 * plain static tree). ENOENT reads as a 404 so callers share one not-found
 * path across transports. An oversize file is `ArtifactCorruptError`, the
 * same verdict a too-large network response gets.
 */
export async function readFileUrl(url: string, maxBytes: number): Promise<BufferedResult> {
  const filePath = fileURLToPath(url);
  try {
    const body = await readFile(filePath);
    if (body.length > maxBytes) {
      throw new ArtifactCorruptError(`chtypes: ${filePath} exceeds the ${maxBytes}-byte limit`);
    }
    return { status: 200, headers: {}, url, body };
  } catch (err) {
    if (err instanceof ArtifactCorruptError) throw err;
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      return { status: 404, headers: {}, url, body: Buffer.alloc(0) };
    }
    throw err;
  }
}
