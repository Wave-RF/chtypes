/**
 * An open never waits for another request's fetch, and concurrent opens of one
 * request share one fetch (public issue #491).
 *
 * Each case serves the stub, signed with the fetch fixtures' TEST key, from the
 * fetch fixture server (`scripts/fetch-v1/server.py`) over HTTP, and holds a
 * fetch in flight with the server's test-only gate: the request is logged and
 * parked until the test opens the gate. Every wait is on an event (the
 * server's parked count, a promise), never on a clock; a bound only turns a
 * hang into a failure. The verdicts come from what the server logged and what
 * each open returned.
 *
 * The registry keeps one promise per request in its memo, set synchronously by
 * `for()`, so opens made in one tick share one attempt by construction; there
 * is no lock to hold across a fetch. `for()` takes no `AbortSignal`, so there is
 * no cancellation case, and the registry has no synchronous or worker path.
 *
 * Needs `$CHTYPES_ABI2_STUBS`; without it every case here SKIPS LOUDLY by name.
 */

import { type ChildProcessByStdio, spawn } from 'node:child_process';
import { createHash, createPrivateKey, sign } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { Readable } from 'node:stream';
import { promisify } from 'node:util';
import { zstdCompress } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Predicate } from '../../src/abi2/loader.js';
import { ArtifactError, type Library, Registry } from '../../src/index.js';
import {
  ARTIFACT_TYPE,
  DSSE_PAYLOAD_TYPE,
  MEDIA_TYPE_BUNDLE,
  MEDIA_TYPE_CONFIG,
  MEDIA_TYPE_EMPTY_CONFIG,
  MEDIA_TYPE_INDEX,
  MEDIA_TYPE_LAYER,
  MEDIA_TYPE_MANIFEST,
  PREDICATE_TYPE_ARTIFACT,
  STATEMENT_TYPE,
  TEST_KEYS,
} from '../../src/ocifetch/constants.gen.js';
import { allowOverridesForTests } from '../../src/ocifetch/channel.js';
import { resetSetupForTests } from '../../src/setup.js';

// The dev channel, with the test key's trust and the base overrides honored.
allowOverridesForTests();

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..');
const KEY_PEM = path.join(REPO_ROOT, 'tests/fixtures/fetch-v1/test-key/private.pem');
const TEST_KEY = TEST_KEYS[0];
const CASES = ['flight-install', 'flight-held', 'flight-one-fetch', 'flight-fails'] as const;
type CaseId = (typeof CASES)[number];
/** Turns a hang into a failure; a working registry answers at once. */
const BOUND_MS = 30_000;
const N = 8;
const zstd = promisify(zstdCompress);

const sha256 = (b: Uint8Array): string => createHash('sha256').update(b).digest('hex');

// ---------------------------------------------------------------- tar, by hand

const BLOCK = 512;

function ustarHeader(name: string, size: number): Buffer {
  const header = Buffer.alloc(BLOCK);
  header.write(name, 0, 'utf8');
  header.write('0000755\0', 100, 'ascii');
  header.write('0000000\0', 108, 'ascii');
  header.write('0000000\0', 116, 'ascii');
  header.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 'ascii');
  header.write('00000000000\0', 136, 'ascii');
  header.write('        ', 148, 'ascii');
  header.write('0', 156, 'ascii');
  header.write('ustar\0', 257, 'ascii');
  header.write('00', 263, 'ascii');
  let sum = 0;
  for (let i = 0; i < BLOCK; i++) sum += header[i] as number;
  header.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 'ascii');
  return header;
}

function tarOfOneFile(name: string, content: Buffer): Buffer {
  const rem = content.length % BLOCK;
  const padded = rem === 0 ? content : Buffer.concat([content, Buffer.alloc(BLOCK - rem)]);
  return Buffer.concat([ustarHeader(name, content.length), padded, Buffer.alloc(BLOCK * 2)]);
}

// ------------------------------------------------------------ the route tree

interface StubsDoc {
  readonly variants: Record<string, { readonly predicate: Predicate }>;
}

interface Descriptor {
  readonly mediaType: string;
  readonly digest: string;
  readonly size: number;
}

/**
 * Write the stub as `tag`'s one-platform signed artifact in the route-tree
 * shape the fixture server serves (`manifests/<tag or digest>`,
 * `blobs/<digest>`, `referrers/<digest>`), and return the layer's digest.
 */
async function writeRouteTree(repo: string, tag: string): Promise<string> {
  const stubs = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as StubsDoc;
  const variant = stubs.variants.ok;
  if (variant === undefined) throw new Error('stubs.json has no ok variant');
  const library = readFileSync(path.join(STUBS_DIR as string, 'ok.so'));
  const libName = process.platform === 'darwin' ? 'libchtypes.dylib' : 'libchtypes.so';
  for (const sub of ['manifests', 'blobs', 'referrers']) mkdirSync(path.join(repo, sub), { recursive: true });
  const put = (sub: string, name: string, bytes: Buffer): void => writeFileSync(path.join(repo, sub, name), bytes);
  const blob = (mediaType: string, bytes: Buffer): Descriptor => {
    const digest = `sha256:${sha256(bytes)}`;
    put('blobs', digest, bytes);
    return { mediaType, digest, size: bytes.length };
  };
  const manifest = (doc: unknown): Descriptor => {
    const bytes = Buffer.from(`${JSON.stringify(doc)}\n`);
    const digest = `sha256:${sha256(bytes)}`;
    put('manifests', digest, bytes);
    return { mediaType: MEDIA_TYPE_MANIFEST, digest, size: bytes.length };
  };

  const layer = blob(MEDIA_TYPE_LAYER, await zstd(tarOfOneFile(libName, library)));
  const predicate: Record<string, unknown> = {
    ...variant.predicate,
    library: libName,
    library_sha256: sha256(library),
    library_bytes: library.length,
  };
  const config = blob(MEDIA_TYPE_CONFIG, Buffer.from(`${JSON.stringify(predicate)}\n`));
  const platformManifest = manifest({ schemaVersion: 2, mediaType: MEDIA_TYPE_MANIFEST, artifactType: ARTIFACT_TYPE, config, layers: [layer] });

  const statement = Buffer.from(
    `${JSON.stringify({
      _type: STATEMENT_TYPE,
      subject: [{ name: `${libName}.tar.zst`, digest: { sha256: layer.digest.slice('sha256:'.length) } }],
      predicateType: PREDICATE_TYPE_ARTIFACT,
      predicate,
    })}\n`,
  );
  const pae = Buffer.concat([Buffer.from(`DSSEv1 ${DSSE_PAYLOAD_TYPE.length} ${DSSE_PAYLOAD_TYPE} ${statement.length} `, 'ascii'), statement]);
  const signature = sign(null, pae, createPrivateKey(readFileSync(KEY_PEM)));
  const bundle = blob(
    MEDIA_TYPE_BUNDLE,
    Buffer.from(
      JSON.stringify({
        mediaType: MEDIA_TYPE_BUNDLE,
        verificationMaterial: { publicKey: { hint: TEST_KEY.keyid } },
        dsseEnvelope: { payload: statement.toString('base64'), payloadType: DSSE_PAYLOAD_TYPE, signatures: [{ sig: signature.toString('base64') }] },
      }),
    ),
  );
  const empty = blob(MEDIA_TYPE_EMPTY_CONFIG, Buffer.from('{}'));
  const referrer = manifest({
    schemaVersion: 2,
    mediaType: MEDIA_TYPE_MANIFEST,
    artifactType: MEDIA_TYPE_BUNDLE,
    config: empty,
    layers: [bundle],
    subject: platformManifest,
  });
  const referrers = Buffer.from(
    JSON.stringify({ schemaVersion: 2, mediaType: MEDIA_TYPE_INDEX, manifests: [{ ...referrer, artifactType: MEDIA_TYPE_BUNDLE }] }),
  );
  put('referrers', platformManifest.digest, referrers);
  put('manifests', `sha256-${platformManifest.digest.slice('sha256:'.length)}`, referrers);
  put(
    'manifests',
    tag,
    Buffer.from(
      JSON.stringify({
        schemaVersion: 2,
        mediaType: MEDIA_TYPE_INDEX,
        manifests: [
          { ...platformManifest, artifactType: ARTIFACT_TYPE, platform: { os: String(variant.predicate.os), architecture: String(variant.predicate.arch) } },
        ],
      }),
    ),
  );
  return layer.digest;
}

// ---------------------------------------------------------- the fixture server

type ServerProcess = ChildProcessByStdio<null, Readable, null>;

async function startServer(fixtures: string): Promise<{ proc: ServerProcess; origin: string }> {
  const script = path.join(REPO_ROOT, 'scripts/fetch-v1/server.py');
  const proc: ServerProcess = spawn('python3', [script, '--fixtures', fixtures, '--port', '0'], { stdio: ['ignore', 'pipe', 'inherit'] });
  return new Promise((resolve, reject) => {
    let buf = '';
    const onData = (chunk: Buffer): void => {
      buf += chunk.toString('utf8');
      const m = /LISTENING (\d+) (\d+)/.exec(buf);
      if (m?.[1] !== undefined) {
        proc.stdout.off('data', onData);
        proc.stdout.resume(); // keep draining, so the server never blocks on a full pipe
        resolve({ proc, origin: `http://127.0.0.1:${m[1]}` });
      }
    };
    proc.stdout.on('data', onData);
    proc.once('error', reject);
    proc.once('exit', (code) => reject(new Error(`${script} exited ${code} before LISTENING`)));
  });
}

/** `promise`, or a failure naming `what` when it has not settled within the bound. */
async function bounded<T>(promise: Promise<T>, what: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const hang = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what}: no answer in ${BOUND_MS} ms`)), BOUND_MS);
  });
  try {
    return await Promise.race([promise, hang]);
  } finally {
    clearTimeout(timer);
  }
}

/** A promise's outcome as a value: the error it rejected with, or undefined. */
function rejection(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => undefined,
    (err: unknown) => err,
  );
}

describe.skipIf(!stubsAvailable)('the registry with a fetch in flight (public issue #491), on the stub', () => {
  let work: string;
  let server: { proc: ServerProcess; origin: string };
  let layerPath: string;
  let cacheSeq = 0;

  beforeAll(async () => {
    resetSetupForTests();
    work = mkdtempSync(path.join(tmpdir(), 'ts-registry-flight-'));
    const fixtures = path.join(work, 'fixtures');
    const layer = await writeRouteTree(path.join(fixtures, 'trees/stub/v2/chtypes/v1'), '26.8');
    writeFileSync(path.join(fixtures, 'cases.json'), JSON.stringify({ schema: 1, cases: CASES.map((id) => ({ id, tree: 'stub' })) }));
    layerPath = `/chtypes/v1/blobs/${layer}`;
    server = await startServer(fixtures);
  });

  afterAll(() => {
    server?.proc.kill();
    rmSync(work, { recursive: true, force: true });
  });

  /** Autofetch on, the one base `caseId`'s repository on the server, over `cache`. */
  const registryOver = (caseId: CaseId, cache: string): Promise<Registry> =>
    Registry.open({
      fetch: {
        bases: [`${server.origin}/s-${caseId}/chtypes/v1`],
        cacheDir: cache,
        systemDirs: [],
        trustedKeys: [{ keyid: TEST_KEY.keyid, ed25519Hex: TEST_KEY.ed25519Hex }],
      },
      autofetch: true,
    });
  const freshCache = (): string => path.join(work, `cache-${cacheSeq++}`);

  async function control(route: string): Promise<unknown> {
    const res = await fetch(`${server.origin}${route}`);
    if (!res.ok) throw new Error(`GET ${route}: ${res.status}`);
    return res.json();
  }
  const closeGate = (caseId: CaseId): Promise<unknown> => control(`/_gate/close/s-${caseId}`);
  const openGate = (caseId: CaseId): Promise<unknown> => control(`/_gate/open/s-${caseId}`);
  /** Once `n` of `caseId`'s requests are parked at its gate: how many are. */
  const waitParked = async (caseId: CaseId, n: number): Promise<number> =>
    ((await control(`/_gate/parked/s-${caseId}?n=${n}`)) as { parked: number }).parked;
  /** `caseId`'s logged requests, counted by "METHOD path", the path relative to the case's own segment. */
  async function requests(caseId: CaseId): Promise<Map<string, number>> {
    const log = (await control(`/_log/s-${caseId}`)) as readonly { method: string; path: string }[];
    const prefix = `/v2/s-${caseId}`;
    const out = new Map<string, number>();
    for (const e of log) {
      const key = `${e.method} ${e.path.startsWith(prefix) ? e.path.slice(prefix.length) : e.path}`;
      out.set(key, (out.get(key) ?? 0) + 1);
    }
    return out;
  }

  it("never makes an installed line wait for another line's fetch: with 26.3's fetch held at the gate, 26.8 opens", async () => {
    const cache = freshCache();
    await (await registryOver('flight-install', cache)).for('26.8'); // install 26.8
    const registry = await registryOver('flight-held', cache);
    await closeGate('flight-held');
    try {
      let settled = false;
      const held = rejection(registry.for('26.3')).finally(() => {
        settled = true;
      });
      await waitParked('flight-held', 1);
      const installed = await bounded(registry.for('26.8'), "for('26.8'), installed, waiting for 26.3's fetch held at the gate");
      expect(installed.version).toBe('26.8.15.10');
      expect(settled).toBe(false); // 26.3's request is still parked
      expect(await waitParked('flight-held', 1)).toBe(1);
      await openGate('flight-held');
      expect(await bounded(held, "for('26.3') after the gate opened")).toBeInstanceOf(ArtifactError);
    } finally {
      await openGate('flight-held');
    }
    const made = [...(await requests('flight-held')).keys()].filter((r) => r.includes('/manifests/26.8'));
    expect(made).toEqual([]);
  }, 120_000);

  it('makes one fetch for concurrent opens of one request, and every open gets its Library', async () => {
    const registry = await registryOver('flight-one-fetch', freshCache());
    await closeGate('flight-one-fetch');
    let libraries: Library[];
    try {
      // All N calls are made in this tick: each finds the first one's promise in the memo.
      const opens = Array.from({ length: N }, () => registry.for('26.8'));
      expect(await waitParked('flight-one-fetch', 1)).toBe(1);
      await openGate('flight-one-fetch');
      libraries = await bounded(Promise.all(opens), `${N} opens of 26.8`);
    } finally {
      await openGate('flight-one-fetch');
    }
    const first = libraries[0];
    for (const library of libraries) expect(library).toBe(first);
    const log = await requests('flight-one-fetch');
    expect(log.get('GET /chtypes/v1/manifests/26.8')).toBe(1);
    expect(log.get(`GET ${layerPath}`)).toBe(1);
    expect(await registry.for('26.8')).toBe(first);
    expect(registry.libraries()).toEqual([first]);
  }, 120_000);

  it('gives every open waiting on a failed fetch its error, and never remembers the failure', async () => {
    const registry = await registryOver('flight-fails', freshCache());
    await closeGate('flight-fails');
    let errors: unknown[];
    try {
      const opens = Array.from({ length: N }, () => rejection(registry.for('26.3')));
      await waitParked('flight-fails', 1);
      await openGate('flight-fails');
      errors = await bounded(Promise.all(opens), `${N} opens of 26.3`);
    } finally {
      await openGate('flight-fails');
    }
    const first = errors[0];
    expect(first).toBeInstanceOf(ArtifactError);
    for (const err of errors) expect(err).toBe(first); // one attempt, one error
    const tag = 'GET /chtypes/v1/manifests/26.3';
    expect((await requests('flight-fails')).get(tag)).toBe(1);
    const again = await rejection(registry.for('26.3'));
    expect(again).toBeInstanceOf(ArtifactError);
    expect((again as ArtifactError).code).toBe((first as ArtifactError).code);
    expect((await requests('flight-fails')).get(tag)).toBe(2); // a failure is never remembered
  }, 120_000);
});
