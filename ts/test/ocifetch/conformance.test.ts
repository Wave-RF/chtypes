/**
 * The v1 conformance runner (`docs/guides/fetch-v1.md` §10, the v1
 * fetch-layer plan's §3.3): `CHTYPES_V1_CONFORMANCE=<path to
 * tests/fixtures/fetch-v1>` runs every case in `cases.json` on every
 * transport it lists for this binding (`file`, `http`; `registry` is the
 * `v1-network` job's own leg, not attempted here), against this binding's
 * `ensure`/`fetchSigned` seam, and writes `CHTYPES_V1_REPORT`. Unset, it
 * **skips loudly by name**.
 *
 * CI runs this exact command (plan §3.3, `.github/workflows/v1.yml`):
 * `cd ts && pnpm exec vitest run test/ocifetch/conformance.test.ts`.
 */

import { type ChildProcessByStdio, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { Readable } from 'node:stream';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ensure, fetchSigned, listTags, resolveInstalled } from '../../src/ocifetch/ensure.js';
import { FIXTURES_REPO_SUFFIX, PREDICATE_TYPE_FIXTURES, PREDICATE_TYPE_GOLDENS, RELEASE_KEYS, TEST_KEYS } from '../../src/ocifetch/constants.gen.js';
import { ArtifactMissingError, type FetchV1ErrorCode } from '../../src/ocifetch/errors.js';
import { verifyAndInstallFromLocalBlobs } from '../../src/ocifetch/localverify.js';
import { readLock, type LockFile } from '../../src/ocifetch/lock.js';
import type { Clock, PlatformKey } from '../../src/ocifetch/types.js';
import { channelName, useAliasFingerprintForTests, useFetchV1ForTests } from '../../src/ocifetch/channel.js';

// This file tests the v1 fetch contract that the ABI v2 dev channel narrows
// (src/ocifetch/channel.ts): its fixtures name their own registry and key, and
// write schema-1 records, abi-1 predicates and locks. The dev channel's own
// rules (spec/abi-v2/docs.md r5, r6) are test/ocifetch/devchannel.test.ts and
// test/cli-devchannel.test.ts.
useFetchV1ForTests();

const CONFORMANCE_DIR = process.env['CHTYPES_V1_CONFORMANCE'];
const REPORT_PATH = process.env['CHTYPES_V1_REPORT'];
const REGISTRY_BASE = process.env['CHTYPES_V1_REGISTRY_BASE'] === '' ? undefined : process.env['CHTYPES_V1_REGISTRY_BASE'];

// ---------------------------------------------------------------- the shapes

interface CaseFile {
  readonly schema: 1;
  readonly cases: readonly ConformanceCase[];
}

type Transport = 'file' | 'http' | 'registry';

interface ConformanceCase {
  readonly id: string;
  readonly tree: string;
  readonly transports: readonly Transport[];
  readonly http_script: string | null;
  readonly setup: {
    readonly cache: string;
    readonly system_dirs: readonly string[];
    readonly lock: string | null;
    readonly before_index_rename_hook: string | null;
  };
  readonly request: {
    readonly spelling: string;
    readonly platform: PlatformKey;
    readonly offline: boolean;
    readonly frozen: boolean;
    readonly lock_write: boolean;
    readonly update: boolean;
    readonly allow_unsigned: boolean;
    readonly trust: 'release' | 'test';
    readonly bases: readonly string[];
    /** The dev channel's alias step under this fixture fingerprint (guide §3); null runs the v1 contract as it is. */
    readonly alias_fingerprint: string | null;
  };
  readonly env: Readonly<Record<string, string>>;
  readonly expect: {
    readonly ok: boolean;
    readonly version: string | null;
    readonly build: string | null;
    readonly manifest: string | null;
    readonly library_sha256: string | null;
    readonly code: FetchV1ErrorCode | null;
    readonly sleeps: readonly number[];
    readonly warnings: readonly string[];
    readonly requests: { readonly max: number | null; readonly none_matching: readonly string[]; readonly auth_on_second_origin: boolean };
    readonly lock_after: string | null;
    /** A `list-tags-` case's listing, in order. */
    readonly tags: readonly string[] | null;
  };
}

interface ReportResult {
  readonly id: string;
  readonly transport: Transport;
  readonly verdict: 'pass' | 'fail';
  readonly detail: string;
}

interface RequestLogEntry {
  readonly method: string;
  readonly path: string;
  readonly origin: string;
  readonly headers: Record<string, string>;
}

/**
 * The leg's name, exactly as `v1.yml`'s matrix and `parity.py`'s `TOOLCHAINS`
 * spell it: `node22.21.0` (the pinned floor, full version) and `node24` (the
 * moving major) — a bare `process.version` would read `node24.21.0` and parity
 * would call the report missing.
 */
function toolchainId(): string {
  // The job's own matrix value, when it exports one (lane NW), is authoritative.
  const fromEnv = process.env['CHTYPES_V1_TOOLCHAIN'];
  if (fromEnv !== undefined && fromEnv !== '') return fromEnv;
  const full = process.version.replace(/^v/, '');
  const major = full.split('.')[0];
  return major === '22' ? `node${full}` : `node${major}`;
}

function recordingClock(sleeps: number[]): Clock {
  return {
    now: () => Date.now(),
    sleep: async (seconds: number) => {
      sleeps.push(seconds);
    },
  };
}

// ----------------------------------------------------------- the http server

type ServerProcess = ChildProcessByStdio<null, Readable, Readable>;

async function startServer(conformanceDir: string): Promise<{ proc: ServerProcess; port: number; port2: number } | undefined> {
  const scriptPath = path.resolve(conformanceDir, '..', '..', '..', 'scripts', 'fetch-v1', 'server.py');
  return new Promise((resolve) => {
    let proc: ServerProcess;
    try {
      proc = spawn('python3', [scriptPath, '--fixtures', conformanceDir, '--port', '0'], { stdio: ['ignore', 'pipe', 'pipe'] });
    } catch {
      resolve(undefined);
      return;
    }
    let buf = '';
    const onData = (chunk: Buffer) => {
      buf += chunk.toString('utf8');
      const m = /LISTENING (\d+) (\d+)/.exec(buf);
      if (m?.[1] !== undefined && m[2] !== undefined) {
        proc.stdout.off('data', onData);
        resolve({ proc, port: Number.parseInt(m[1], 10), port2: Number.parseInt(m[2], 10) });
      }
    };
    proc.stdout.on('data', onData);
    proc.once('error', () => resolve(undefined));
    proc.once('exit', () => resolve(undefined));
  });
}

async function fetchRequestLog(port: number, caseId: string): Promise<readonly RequestLogEntry[]> {
  const res = await fetch(`http://127.0.0.1:${port}/_log/s-${caseId}`);
  if (!res.ok) return [];
  return (await res.json()) as readonly RequestLogEntry[];
}

/**
 * `{base}`/`{base2}` expansion for one case/transport (plan §3.2, guide
 * §10's "{base} per transport"; `{base2}`, http transport only, decided
 * lane 0B 2026-10-02: the same `s-<case-id>/chtypes/v1` suffix as `{base}`,
 * rooted at `server.py`'s SECOND origin — the second port on its own
 * `LISTENING <port> <port2>` line — for a case needing two genuinely
 * independent bases over the same case id, such as a mirror-failover case).
 */
function expandBase(
  template: string,
  transport: Transport,
  serverPort: number | undefined,
  serverPort2: number | undefined,
  caseId: string,
  tree: string,
  conformanceDir: string,
): string {
  if (template.includes('{base2}')) {
    if (transport !== 'http') {
      throw new Error(`chtypes: {base2} is an http-transport-only template, got transport ${transport}`);
    }
    return template.replace('{base2}', `http://127.0.0.1:${serverPort2}/s-${caseId}/chtypes/v1`);
  }
  if (!template.includes('{base}')) return template;
  const base =
    transport === 'file'
      ? `file://${path.resolve(conformanceDir, 'trees', tree, 'v2', 'chtypes', 'v1')}`
      : transport === 'http'
        ? `http://127.0.0.1:${serverPort}/s-${caseId}/chtypes/v1`
        : (REGISTRY_BASE ?? (() => { throw new Error('chtypes: registry transport requested with no CHTYPES_V1_REGISTRY_BASE set'); })());
  return template.replace('{base}', base);
}

// --------------------------------------------------------------- lock compare

/** Structural equality for a lock, ignoring `platforms`' array order (the schema does not require one). */
function locksEqual(a: LockFile, b: LockFile): boolean {
  if (a.schema !== b.schema || a.abi !== b.abi) return false;
  if ([...a.platforms].sort().join(',') !== [...b.platforms].sort().join(',')) return false;
  return deepEqual(a.requests, b.requests);
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  const ak = Object.keys(a as Record<string, unknown>).sort();
  const bk = Object.keys(b as Record<string, unknown>).sort();
  if (ak.join(',') !== bk.join(',')) return false;
  return ak.every((k) => deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

// ------------------------------------------------------------------ the suite

describe.skipIf(CONFORMANCE_DIR === undefined || CONFORMANCE_DIR === '')('v1 conformance', () => {
  let caseFile: CaseFile;
  let casesSha256: string;
  let server: { proc: ServerProcess; port: number; port2: number } | undefined;
  const results: ReportResult[] = [];

  beforeAll(async () => {
    if (CONFORMANCE_DIR === undefined || CONFORMANCE_DIR === '') return;
    // The cases are the v1 fetch contract's specification; the 2.0.0-dev
    // binding's dev channel narrows it, and its own rules are tested apart.
    console.log(`fetch contract: ${channelName()} (the fetch-v1 cases' own; the dev channel this binding ships is test/ocifetch/devchannel.test.ts)`);
    if (channelName() !== 'v1') throw new Error(`the fetch-v1 conformance cases must run under the v1 contract, not ${channelName()}`);
    const raw = await readFile(path.join(CONFORMANCE_DIR, 'cases.json'));
    casesSha256 = createHash('sha256').update(raw).digest('hex');
    caseFile = JSON.parse(raw.toString('utf8')) as CaseFile;
    if (caseFile.cases.some((c) => c.transports.includes('http'))) {
      server = await startServer(CONFORMANCE_DIR);
    }
  }, 30_000);

  afterAll(async () => {
    server?.proc.kill();
    if (REPORT_PATH !== undefined && REPORT_PATH !== '' && caseFile !== undefined) {
      const report = { schema: 1, binding: 'ts', toolchain: toolchainId(), cases_sha256: casesSha256, results };
      await mkdir(path.dirname(REPORT_PATH), { recursive: true });
      await writeFile(REPORT_PATH, JSON.stringify(report, null, 2));
    }
  });

  it('every case in cases.json passes on every transport it lists (file, http, and registry when CHTYPES_V1_REGISTRY_BASE is set)', async () => {
    if (CONFORMANCE_DIR === undefined) return;
    for (const c of caseFile.cases) {
      for (const transport of c.transports) {
        // The `registry` transport runs only where CHTYPES_V1_REGISTRY_BASE
        // names a live repository root (the v1-network and staging jobs set
        // it); everywhere else its pairs are skipped LOUDLY, by name.
        if (transport === 'registry' && REGISTRY_BASE === undefined) {
          console.warn(`SKIP ${c.id}/registry: CHTYPES_V1_REGISTRY_BASE is unset`);
          continue;
        }
        const detail = await runOne(c, transport, CONFORMANCE_DIR, server?.port, server?.port2).catch(
          (err: unknown) => `runner threw: ${err instanceof Error ? err.stack ?? err.message : String(err)}`,
        );
        results.push({ id: c.id, transport, verdict: detail === '' ? 'pass' : 'fail', detail });
      }
    }
    const failures = results.filter((r) => r.verdict === 'fail');
    expect(failures, failures.map((f) => `${f.id}/${f.transport}: ${f.detail}`).join('\n')).toEqual([]);
  }, 300_000);
});

// ------------------------------------------------------------------ one case

async function runOne(
  c: ConformanceCase,
  transport: Transport,
  conformanceDir: string,
  serverPort: number | undefined,
  serverPort2: number | undefined,
): Promise<string> {
  const work = await mkdtemp(path.join(tmpdir(), 'ocifetch-v1-case-'));
  try {
    const cacheDir = path.join(work, 'cache');
    await mkdir(cacheDir, { recursive: true });
    if (c.setup.cache !== 'empty') {
      await cp(path.join(conformanceDir, 'layouts', c.setup.cache), cacheDir, { recursive: true });
    }

    const trustedKeys =
      c.request.trust === 'test'
        ? [{ keyid: TEST_KEYS[0]!.keyid, ed25519Hex: TEST_KEYS[0]!.ed25519Hex }]
        : RELEASE_KEYS.map((k) => ({ keyid: k.keyid, ed25519Hex: k.ed25519Hex }));

    // installed.json: pre-install exactly these digests, offline, from local
    // blobs, before the timed part of the case (plan §3.2's `installed.json`
    // convention, guide §10's "Cache fixtures and installed.json").
    const installedPath = path.join(cacheDir, 'installed.json');
    try {
      const raw = JSON.parse(await readFile(installedPath, 'utf8')) as { installed: readonly string[] };
      for (const digest of raw.installed) {
        await verifyAndInstallFromLocalBlobs(cacheDir, cacheDir, digest, c.request.platform, trustedKeys);
      }
    } catch {
      // No installed.json for this fixture — nothing to pre-install.
    }

    const systemDirRoots: string[] = [];
    for (const name of c.setup.system_dirs) {
      const sysDir = path.join(work, 'system', name);
      await mkdir(sysDir, { recursive: true });
      await cp(path.join(conformanceDir, 'layouts', name), sysDir, { recursive: true });
      systemDirRoots.push(sysDir);
    }

    const lockPath = path.join(work, 'chtypes.lock');
    if (c.setup.lock !== null) {
      await cp(path.join(conformanceDir, 'locks', 'inputs', `${c.setup.lock}.json`), lockPath);
    }

    const beforeIndexRename = c.setup.before_index_rename_hook === null ? undefined : indexRenameHook(c.setup.before_index_rename_hook, cacheDir);

    const sleeps: number[] = [];
    const bases = c.request.bases.map((b) => expandBase(b, transport, serverPort, serverPort2, c.id, c.tree, conformanceDir));
    const token = c.env['CHTYPES_DOWNLOAD_TOKEN'];

    const baseOptions = {
      bases,
      platform: c.request.platform,
      offline: c.request.offline,
      frozen: c.request.frozen,
      allowUnsigned: c.request.allow_unsigned,
      clock: recordingClock(sleeps),
      cacheDir,
      systemDirs: systemDirRoots,
      lockPath,
      trustedKeys,
      ...(token !== undefined ? { token } : {}),
      ...(beforeIndexRename !== undefined ? { beforeIndexRename } : {}),
    };

    // The dev channel's alias step (guide §3), under the case's fixture
    // fingerprint; a null one leaves the v1 contract as it is.
    const restoreAlias = c.request.alias_fingerprint === null ? undefined : useAliasFingerprintForTests(c.request.alias_fingerprint);
    let resultDetail: string;
    try {
      if (c.id.startsWith('goldens-') || c.id.startsWith('fixtures-')) {
        resultDetail = await runGenericFetch(c, bases, baseOptions);
      } else if (c.id.startsWith('list-tags-')) {
        resultDetail = await runListTags(c, baseOptions);
      } else {
        resultDetail = await runEnsure(c, baseOptions, lockPath, conformanceDir);
      }
    } finally {
      restoreAlias?.();
    }
    if (resultDetail !== '') return resultDetail;

    if (c.expect.sleeps.length > 0 || sleeps.length > 0) {
      if (sleeps.join(',') !== c.expect.sleeps.join(',')) {
        return `sleeps [${sleeps.join(',')}] != expected [${c.expect.sleeps.join(',')}]`;
      }
    }

    if (transport === 'http' && serverPort !== undefined) {
      const logDetail = await checkRequestLog(serverPort, c);
      if (logDetail !== '') return logDetail;
    }

    return '';
  } finally {
    await rm(work, { recursive: true, force: true }).catch(() => {});
  }
}

type EnsureOptions = Parameters<typeof ensure>[1];

async function runEnsure(c: ConformanceCase, baseOptions: EnsureOptions, lockPath: string, conformanceDir: string): Promise<string> {
  try {
    let resolved: Awaited<ReturnType<typeof ensure>>;
    if (c.id.startsWith('resolve-installed-')) {
      // The cache-only seam entry (guide §10): a miss is reported as
      // CHTYPES_ARTIFACT_MISSING, the code --offline gives.
      const found = await resolveInstalled(c.request.spelling, c.request.platform as PlatformKey, baseOptions);
      if (found === undefined) throw new ArtifactMissingError(`chtypes: no installed artifact satisfies ${c.request.spelling}`);
      resolved = found;
    } else {
      resolved = await ensure(c.request.spelling, {
        ...baseOptions,
        lockWrite: c.request.lock_write,
        lockAllPlatforms: c.request.lock_write,
        update: c.request.update,
      });
    }
    if (!c.expect.ok) return `expected failure (code ${c.expect.code}), got ok with version ${resolved.version}`;
    if (c.expect.version !== null && resolved.version !== c.expect.version) {
      return `version ${resolved.version} != expected ${c.expect.version}`;
    }
    if (c.expect.build !== null && resolved.build !== c.expect.build) {
      return `build ${resolved.build} != expected ${c.expect.build}`;
    }
    if (c.expect.manifest !== null && resolved.digests.manifest !== c.expect.manifest) {
      return `manifest ${resolved.digests.manifest} != expected ${c.expect.manifest}`;
    }
    if (c.expect.library_sha256 !== null && resolved.predicate.library_sha256 !== c.expect.library_sha256) {
      return `library_sha256 ${resolved.predicate.library_sha256} != expected ${c.expect.library_sha256}`;
    }
    for (const w of c.expect.warnings) {
      if (!resolved.warnings.some((have) => have.includes(w))) {
        return `expected a warning containing ${JSON.stringify(w)}, got [${resolved.warnings.join(' | ')}]`;
      }
    }
    if (c.expect.lock_after !== null) {
      const expectedRaw = await readFile(path.join(conformanceDir, 'locks', 'expected', `${c.expect.lock_after}.json`), 'utf8');
      const expected = JSON.parse(expectedRaw) as LockFile;
      const actual = await readLock(lockPath);
      if (actual === undefined) return `expected a lock to be written at ${lockPath}, found none`;
      if (!locksEqual(actual, expected)) {
        return `lock_after mismatch:\n  got: ${JSON.stringify(actual)}\n  want: ${JSON.stringify(expected)}`;
      }
    }
    return '';
  } catch (err) {
    if (c.expect.ok) return `unexpected throw: ${err instanceof Error ? err.message : String(err)}`;
    const code = (err as { code?: string }).code;
    if (c.expect.code !== null && code !== c.expect.code) {
      return `code ${code} != expected ${c.expect.code} (${err instanceof Error ? err.message : String(err)})`;
    }
    return '';
  }
}

/** A `list-tags-` case (guide §10): what `chtypes list` names as published, from the registry's tags/list. */
async function runListTags(c: ConformanceCase, baseOptions: EnsureOptions): Promise<string> {
  try {
    const listed = await listTags(baseOptions);
    if (!c.expect.ok) return `expected failure (code ${c.expect.code}), got the listing [${listed.join(', ')}]`;
    if (c.expect.tags !== null && listed.join(' ') !== c.expect.tags.join(' ')) {
      return `listed [${listed.join(', ')}] != expected [${c.expect.tags.join(', ')}]`;
    }
    return '';
  } catch (err) {
    if (c.expect.ok) return `unexpected throw: ${err instanceof Error ? err.message : String(err)}`;
    const code = (err as { code?: string }).code;
    if (c.expect.code !== null && code !== c.expect.code) {
      return `code ${code} != expected ${c.expect.code} (${err instanceof Error ? err.message : String(err)})`;
    }
    return '';
  }
}

async function runGenericFetch(c: ConformanceCase, bases: readonly string[], baseOptions: EnsureOptions): Promise<string> {
  const isGoldens = c.id.startsWith('goldens-');
  const repository = isGoldens ? bases[0]! : `${bases[0]}${FIXTURES_REPO_SUFFIX}`;
  const predicateType = isGoldens ? PREDICATE_TYPE_GOLDENS : PREDICATE_TYPE_FIXTURES;
  try {
    // A goldens case's `request.spelling` is the SUBJECT (a platform
    // manifest): `fetchSigned` itself discovers its goldens referrers,
    // verifies each and returns the highest revision (guide §9). A fixtures
    // case names the object's digest directly.
    const ref = c.request.spelling;
    const result = await fetchSigned(repository, ref, predicateType, baseOptions);
    if (!c.expect.ok) return `expected failure (code ${c.expect.code}), got ok`;
    if (c.expect.manifest !== null && result.digests.manifest !== c.expect.manifest) {
      return `manifest ${result.digests.manifest} != expected ${c.expect.manifest}`;
    }
    if (c.expect.library_sha256 !== null) {
      const bytes = await readFile(result.path);
      const sha256 = createHash('sha256').update(bytes).digest('hex');
      if (sha256 !== c.expect.library_sha256) return `fetched content sha256 ${sha256} != expected ${c.expect.library_sha256}`;
    }
    return '';
  } catch (err) {
    if (c.expect.ok) return `unexpected throw: ${err instanceof Error ? err.message : String(err)}`;
    const code = (err as { code?: string }).code;
    if (c.expect.code !== null && code !== c.expect.code) {
      return `code ${code} != expected ${c.expect.code} (${err instanceof Error ? err.message : String(err)})`;
    }
    return '';
  }
}

// --------------------------------------------------------------- the request log

async function checkRequestLog(serverPort: number, c: ConformanceCase): Promise<string> {
  const log = await fetchRequestLog(serverPort, c.id);
  if (c.expect.requests.max !== null && log.length > c.expect.requests.max) {
    return `${log.length} requests were made, expected at most ${c.expect.requests.max}`;
  }
  for (const pattern of c.expect.requests.none_matching) {
    const re = new RegExp(pattern);
    for (const entry of log) {
      const line = `${entry.method} ${entry.path}`;
      if (re.test(line)) return `request ${JSON.stringify(line)} matched the forbidden pattern ${JSON.stringify(pattern)}`;
    }
  }
  const secondOriginAuthed = log.some((e) => e.origin === 'second' && e.headers['authorization'] !== undefined);
  if (c.expect.requests.auth_on_second_origin !== secondOriginAuthed) {
    return `auth_on_second_origin: expected ${c.expect.requests.auth_on_second_origin}, got ${secondOriginAuthed}`;
  }
  return '';
}

// ------------------------------------------------------------------- hooks

/**
 * `before_index_rename_hook` (plan §3.2): a test-setup instruction naming a
 * race scenario this runner itself simulates — never a fixture file. Each
 * named hook writes a competing `index.json` directly (bypassing this
 * binding's own writer) between this call and the real atomic rename, so
 * `layout.ts`'s optimistic-concurrency re-read-and-reapply loop has
 * something real to survive (`index-race-reapply`, guide §1).
 */
function indexRenameHook(name: string, cacheDir: string): () => Promise<void> {
  if (name === 'index-race-reapply') {
    let fired = false;
    return async () => {
      if (fired) return; // the retry loop calls this again on its second pass; race once.
      fired = true;
      const competing = {
        schemaVersion: 2,
        mediaType: 'application/vnd.oci.image.index.v1+json',
        manifests: [
          {
            mediaType: 'application/vnd.oci.image.manifest.v1+json',
            digest: `sha256:${'0'.repeat(64)}`,
            size: 1,
            artifactType: 'application/vnd.wavehouse.chtypes.artifact.v1',
            platform: { os: 'linux', architecture: 'arm64' },
            annotations: { 'org.opencontainers.image.ref.name': '0.0.0.0' },
          },
        ],
      };
      await writeFile(path.join(cacheDir, 'index.json'), JSON.stringify(competing));
    };
  }
  return async () => {};
}
