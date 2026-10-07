/**
 * The ABI v2 dev channel (`src/ocifetch/channel.ts`; `spec/abi-v2/docs.md`,
 * rules r5 and r6), tested as every process but a test speaks it: this file
 * selects it with `useDevChannelForTests` (the default already; said
 * explicitly), and only the control of the last case switches to the v1
 * contract for its own duration. Nothing here reaches the network: every
 * refusal is proven to come before the first request, and every cache read is
 * offline. Go's `go/internal/ocifetch/devchannel_test.go` is the same test.
 */

import diagnosticsChannel from 'node:diagnostics_channel';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ABI_FINGERPRINT } from '../../src/abi2/decls.gen.js';
import {
  activeChannel,
  aliasTag,
  allowOverridesForTests,
  captureIgnoredForTests,
  channelName,
  DEV_CACHE_DIR,
  DEV_CHANNEL_BASE,
  DEV_KEY_HEX,
  DEV_KEY_ID,
  ignoredSettingsWarned,
  offlineMode,
  PINNING_REFUSED,
  PinningRefusedError,
  useDevChannelForTests,
  useFetchV1ForTests,
  visibleToChannel,
} from '../../src/ocifetch/channel.js';
import { RELEASE_KEYS, TEST_KEYS } from '../../src/ocifetch/constants.gen.js';
import { checkArtifactStatement, keyIdOfRawKey, parseStatement } from '../../src/ocifetch/dsse.js';
import { effectiveFetchOptions, ensure, listInstalled, resolveInstalled } from '../../src/ocifetch/ensure.js';
import { cacheRoot, decodeRecord, encodeRecord, systemDirs, unpackedDir, type VerifiedRecord } from '../../src/ocifetch/layout.js';
import { ArtifactMissingError, FetchV1Error } from '../../src/ocifetch/errors.js';
import type { ArtifactPredicate, FetchV1Options } from '../../src/ocifetch/types.js';

useDevChannelForTests();

const OVERRIDE_ENV = ['CHTYPES_ARTIFACTS_URL', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED', 'CHTYPES_CACHE', 'CHTYPES_CACHE_STRICT', 'CHTYPES_OFFLINE'];

function clearEnv(): void {
  for (const k of OVERRIDE_ENV) vi.stubEnv(k, '');
}

const dirs: string[] = [];
async function tempDir(): Promise<string> {
  const d = await mkdtemp(path.join(os.tmpdir(), 'ts-devchannel-'));
  dirs.push(d);
  return d;
}

afterEach(async () => {
  vi.unstubAllEnvs();
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

/** Counts every HTTP(S) client request this process starts, by Node's own diagnostics channel. */
function countRequests(): { readonly count: () => number; readonly stop: () => void } {
  let n = 0;
  const onStart = (): void => {
    n += 1;
  };
  diagnosticsChannel.subscribe('http.client.request.start', onStart);
  return { count: () => n, stop: () => diagnosticsChannel.unsubscribe('http.client.request.start', onStart) };
}

describe('rule r6: the staging base and key, and nothing else', () => {
  it('pins the base, the key and its id the rule names, and leaves the release key out of the trust list', () => {
    clearEnv();
    expect(channelName()).toBe('v2-dev');
    expect(activeChannel().abi).toBe(2);
    expect(DEV_CHANNEL_BASE).toBe('https://registry-staging.wavehouse.dev/chtypes/v2-dev');
    expect(DEV_KEY_HEX).toBe('5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c');
    // The id is derived by the fetch layer's own key-id algorithm, never taken from the constant.
    expect(keyIdOfRawKey(DEV_KEY_HEX)).toBe('824345f9bcf8e5bf');
    expect(DEV_KEY_ID).toBe('824345f9bcf8e5bf');
    const eff = effectiveFetchOptions({});
    expect(eff.bases).toEqual([DEV_CHANNEL_BASE]);
    expect(eff.trustedKeys).toEqual([{ keyid: DEV_KEY_ID, ed25519Hex: DEV_KEY_HEX }]);
    expect(eff.allowUnsigned).toBe(false);
    for (const rk of RELEASE_KEYS) {
      expect(eff.trustedKeys.map((k) => k.ed25519Hex)).not.toContain(rk.ed25519Hex);
    }
  });

  it('ignores every base, trust and unsigned override, from the environment and the options, naming each exactly once', async () => {
    clearEnv();
    const out: string[] = [];
    const restore = captureIgnoredForTests((text) => out.push(text));
    try {
      const testKey = TEST_KEYS[0];
      vi.stubEnv('CHTYPES_ARTIFACTS_URL', 'http://127.0.0.1:9/chtypes/v1');
      vi.stubEnv('CHTYPES_TRUSTED_KEYS', testKey.ed25519Hex);
      vi.stubEnv('CHTYPES_ALLOW_UNSIGNED', '1');
      const options = {
        bases: ['http://127.0.0.1:9/x'],
        trustedKeys: [{ keyid: testKey.keyid, ed25519Hex: testKey.ed25519Hex }],
        allowUnsigned: true,
        cacheDir: await tempDir(),
        systemDirs: [],
      };
      for (let i = 0; i < 3; i++) {
        // An offline lookup in an empty cache: no network, and every seam entry names what it ignores.
        expect(await resolveInstalled('26.8', 'linux-amd64', options)).toBeUndefined();
        await listInstalled(options);
        const eff = effectiveFetchOptions(options);
        expect(eff.bases).toEqual([DEV_CHANNEL_BASE]);
        expect(eff.trustedKeys.map((k) => k.keyid)).toEqual([DEV_KEY_ID]);
        expect(eff.allowUnsigned).toBe(false);
      }
      const want = [
        'CHTYPES_ALLOW_UNSIGNED',
        'CHTYPES_ARTIFACTS_URL',
        'CHTYPES_TRUSTED_KEYS',
        'the allowUnsigned option',
        'the bases option',
        'the trustedKeys option',
      ];
      expect(ignoredSettingsWarned()).toEqual(want);
      const text = out.join('');
      for (const s of want) {
        expect(text.split(`WARNING: ${s} is set and IGNORED`).length - 1, `${s} warned once`).toBe(1);
      }
      expect(text).toContain(DEV_CHANNEL_BASE);
      expect(text).toContain(DEV_KEY_ID);
    } finally {
      restore();
    }
  });
});

describe('rule r6: lock, frozen and update are refused before any request', () => {
  const cases: Record<string, FetchV1Options> = {
    frozen: { frozen: true },
    'lock write': { lockWrite: true, lockPath: 'chtypes.lock' },
    update: { update: true, lockPath: 'chtypes.lock' },
    'a lock path': { lockPath: 'chtypes.lock' },
  };
  for (const [name, pin] of Object.entries(cases)) {
    it(`${name}: refused with the dev-channel reason, no request made and the cache never created`, async () => {
      clearEnv();
      const cacheDir = path.join(await tempDir(), 'never-created');
      const requests = countRequests();
      let caught: unknown;
      try {
        await ensure('26.8', { ...pin, cacheDir, platform: 'linux-amd64' });
      } catch (err) {
        caught = err;
      } finally {
        requests.stop();
      }
      expect(caught).toBeInstanceOf(PinningRefusedError);
      expect((caught as Error).message).toContain(PINNING_REFUSED);
      expect((caught as Error).message).toContain('refused by a 2.0.0-dev SDK');
      expect((caught as Error).message).toContain('14 days');
      expect(requests.count()).toBe(0);
      await expect(stat(cacheDir)).rejects.toThrow(/ENOENT/);
    });
  }
});

describe('rule r5: the cache', () => {
  it('uses the v2-dev default root, and an explicit cache from the environment or the option only through its v2-dev subroot', async () => {
    clearEnv();
    const xdg = await tempDir();
    vi.stubEnv('XDG_CACHE_HOME', xdg);
    expect(cacheRoot()).toBe(path.join(xdg, 'chtypes', 'v2-dev'));
    const env = await tempDir();
    vi.stubEnv('CHTYPES_CACHE', env);
    expect(cacheRoot()).toBe(path.join(env, DEV_CACHE_DIR));
    const opt = await tempDir();
    expect(cacheRoot(opt)).toBe(path.join(opt, DEV_CACHE_DIR));
    for (const d of systemDirs()) expect(d.endsWith('/v2-dev'), `${d} is a v2-dev system dir: a 1.x one is never read`).toBe(true);
  });

  it('writes schema-2 records and reads only those: a schema-1 record (every released 1.x writer\'s) is absent', () => {
    const record: VerifiedRecord = {
      platform: 'linux-amd64',
      version: '26.8.15.10',
      channel: 'lts',
      build: '20261001.000000',
      library: 'libchtypes.so',
      librarySha256: 'a'.repeat(64),
      libraryBytes: 1,
      indexDigest: null,
      manifestDigest: `sha256:${'b'.repeat(64)}`,
      layerDigest: `sha256:${'c'.repeat(64)}`,
      bundleDigest: null,
      bundleManifestDigest: null,
      signedBy: DEV_KEY_ID,
      predicate: { abi: 2 } as unknown as ArtifactPredicate,
    };
    const text = encodeRecord(record);
    expect(JSON.parse(text).schema).toBe(2);
    expect(decodeRecord(text)).toBeDefined();
    const one = text.replace('"schema":2', '"schema":1');
    expect(one).not.toBe(text);
    expect(decodeRecord(one)).toBeUndefined();
  });

  it('never reads a cache a 1.x binding wrote, whether CHTYPES_CACHE names it or the 1.x layout sits in the subroot itself', async () => {
    clearEnv();
    const layout = path.resolve(import.meta.dirname, '../../../tests/fixtures/fetch-v1/layouts/cache-record-canonical');
    const cache = await tempDir();
    await cp(layout, cache, { recursive: true });
    await cp(layout, path.join(cache, DEV_CACHE_DIR), { recursive: true });
    const options = { cacheDir: cache, systemDirs: [] };

    // The control: the v1 contract reads the fixture as the 1.x cache it is.
    const restore = useFetchV1ForTests();
    const v1 = await listInstalled(options).finally(restore);
    expect(v1.length, 'the fixture is a 1.x cache the v1 contract lists').toBeGreaterThan(0);

    expect(channelName()).toBe('v2-dev');
    expect(await listInstalled(options)).toEqual([]);
    for (const r of v1) {
      expect(await resolveInstalled(r.version, r.platform, options)).toBeUndefined();
    }
  });
});

describe('rule r6: a signed predicate must say abi 2', () => {
  const layer = 'a'.repeat(64);
  const statement = (abi: number) =>
    parseStatement(
      Buffer.from(
        JSON.stringify({
          _type: 'https://in-toto.io/Statement/v1',
          subject: [{ name: 'chtypes.tar.zst', digest: { sha256: layer } }],
          predicateType: 'https://artifacts.wavehouse.dev/spec/artifact/v1',
          predicate: {
            abi,
            abi_fingerprint: `sha256:${'b'.repeat(64)}`,
            clickhouse_version: '26.8.15.10',
            channel: 'lts',
            clickhouse_minor: '26.8',
            clickhouse_commit: 'c'.repeat(40),
            os: 'linux',
            arch: 'arm64',
            build: '20261001.183455',
            core_commit: 'd'.repeat(40),
            inputs_sha256: 'e'.repeat(64),
            library: 'libchtypes.so',
            library_sha256: 'f'.repeat(64),
            library_bytes: 1,
          },
        }),
        'utf8',
      ),
    );
  const check = { os: 'linux', arch: 'arm64', requestedSpelling: '26.8' };

  it('refuses an abi-1 predicate (a 1.x build) even when every other field matches, and accepts abi 2', () => {
    expect(() => checkArtifactStatement(statement(1), 'https://artifacts.wavehouse.dev/spec/artifact/v1', `sha256:${layer}`, check)).toThrow(
      /not 2/,
    );
    expect(checkArtifactStatement(statement(2), 'https://artifacts.wavehouse.dev/spec/artifact/v1', `sha256:${layer}`, check).abi).toBe(2);
  });
});

describe('public issue #528: CHTYPES_OFFLINE=1 is the environment twin of the offline option', () => {
  const sha256 = (text: string): string => createHash('sha256').update(text).digest('hex');

  it('with nothing installed it is ARTIFACT_MISSING and not one request is made; the option is the same answer', async () => {
    clearEnv();
    vi.stubEnv('CHTYPES_OFFLINE', '1');
    const cacheDir = await tempDir();
    const cases: FetchV1Options[] = [{}, { offline: false }, { offline: true }];
    for (const options of cases) {
      if (options.offline === true) vi.stubEnv('CHTYPES_OFFLINE', '');
      const requests = countRequests();
      let caught: unknown;
      try {
        await ensure('26.8', { ...options, cacheDir, systemDirs: [], platform: 'linux-arm64' });
      } catch (err) {
        caught = err;
      } finally {
        requests.stop();
      }
      expect(caught).toBeInstanceOf(ArtifactMissingError);
      expect(requests.count()).toBe(0);
    }
  });

  it('with a build installed it loads it, still with no request; an explicit false does not turn the variable off; only "1" is on', async () => {
    clearEnv();
    vi.stubEnv('CHTYPES_OFFLINE', '1');
    const cacheDir = await tempDir();
    const root = cacheRoot(cacheDir);
    const library = 'library 26.8.1.1';
    const hex = sha256(`${root}26.8.1.1`);
    const record: VerifiedRecord = {
      platform: 'linux-arm64',
      version: '26.8.1.1',
      channel: null,
      build: '20260801.000001',
      library: 'lib.so',
      librarySha256: sha256(library),
      libraryBytes: library.length,
      indexDigest: null,
      manifestDigest: `sha256:${hex}`,
      layerDigest: `sha256:${'c'.repeat(64)}`,
      bundleDigest: null,
      bundleManifestDigest: null,
      signedBy: null,
      // A build of this SDK's own ABI: its predicate names the generated
      // fingerprint, as a dev artifact's signed predicate does, so the dev
      // channel's record filter sees it.
      predicate: { clickhouse_version: '26.8.1.1', build: '20260801.000001', abi_fingerprint: ABI_FINGERPRINT } as unknown as ArtifactPredicate,
    };
    const entry = unpackedDir(root, hex);
    await mkdir(entry, { recursive: true });
    await writeFile(path.join(entry, 'lib.so'), library);
    await writeFile(path.join(entry, 'verified.json'), encodeRecord(record));

    const requests = countRequests();
    try {
      const got = await ensure('26.8', { cacheDir, systemDirs: [], platform: 'linux-arm64' });
      expect(got.dir).toBe(entry);
      expect(offlineMode(undefined)).toBe(true);
      expect(offlineMode(false)).toBe(true);
      vi.stubEnv('CHTYPES_OFFLINE', 'true');
      expect(offlineMode(undefined)).toBe(false);
      vi.stubEnv('CHTYPES_OFFLINE', '0');
      expect(offlineMode(undefined)).toBe(false);
    } finally {
      requests.stop();
    }
    expect(requests.count()).toBe(0);
  });
});

describe('the alias step (guide §3): a dev SDK prefers the newest build of its own fingerprint', () => {
  const hexFp = ABI_FINGERPRINT.slice('sha256:'.length);

  it('names its alias with its own generated fingerprint (the one the header defines), only for a version spelling; the v1 contract has none', async () => {
    expect(ABI_FINGERPRINT.startsWith('sha256:')).toBe(true);
    expect(hexFp).toMatch(/^[0-9a-f]{64}$/);
    const header = await readFile(path.resolve(import.meta.dirname, '../../../include/v2/chtypes.h'), 'utf8');
    expect(header).toContain(`#define CHS_ABI_FINGERPRINT "${ABI_FINGERPRINT}"`);
    for (const tag of ['26.9', '26.9.8', '26.9.8.3']) expect(aliasTag(tag)).toBe(`${tag}--fp-${hexFp}`);
    expect(aliasTag('26.9')?.length).toBe(73);
    expect(aliasTag('latest')).toBeUndefined();
    expect(aliasTag('sha256-abc')).toBeUndefined();
    const restore = useFetchV1ForTests();
    try {
      expect(aliasTag('26.9')).toBeUndefined();
    } finally {
      restore();
    }
  });

  for (const [aliasStatus, attempts, code] of [
    [404, 1, 'CHTYPES_ARTIFACT_UNPUBLISHED'],
    [503, 5, 'CHTYPES_SOURCE_UNREACHABLE'],
  ] as const) {
    it(`requests its own alias FIRST; an alias ${aliasStatus} gives ${code}${aliasStatus === 404 ? ' after the tag' : ', and the tag is never requested'}`, async () => {
      clearEnv();
      const alias = `/v2/chtypes/v2-dev/manifests/26.9--fp-${hexFp}`;
      const tag = '/v2/chtypes/v2-dev/manifests/26.9';
      const paths: string[] = [];
      const server = createServer((req, res) => {
        paths.push(req.url ?? '');
        res.statusCode = req.url === alias ? aliasStatus : 404;
        res.end();
      });
      await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
      const restore = allowOverridesForTests();
      let caught: unknown;
      try {
        const { port } = server.address() as AddressInfo;
        await ensure('26.9', {
          bases: [`http://127.0.0.1:${port}/chtypes/v2-dev`],
          cacheDir: await tempDir(),
          systemDirs: [],
          platform: 'linux-amd64',
          clock: { now: () => Date.now(), sleep: async () => {} },
        });
      } catch (err) {
        caught = err;
      } finally {
        restore();
        await new Promise<void>((resolve) => server.close(() => resolve()));
      }
      expect(caught).toBeInstanceOf(FetchV1Error);
      expect((caught as FetchV1Error).code).toBe(code);
      expect(paths).toEqual([...Array<string>(attempts).fill(alias), ...(aliasStatus === 404 ? [tag] : [])]);
    });
  }
});

describe('the record filter (guide §3): a dev SDK sees only its own fingerprint\'s records', () => {
  it('sees a record of its own generated fingerprint and no other; the v1 contract sees every record', () => {
    const other = `sha256:${'0'.repeat(64)}`;
    expect(visibleToChannel({ abi_fingerprint: ABI_FINGERPRINT })).toBe(true);
    expect(visibleToChannel({ abi_fingerprint: other })).toBe(false);
    expect(visibleToChannel({})).toBe(false);
    expect(visibleToChannel({ abi_fingerprint: ABI_FINGERPRINT.slice('sha256:'.length) })).toBe(false);
    const restore = useFetchV1ForTests();
    try {
      expect(visibleToChannel({ abi_fingerprint: other })).toBe(true);
      expect(visibleToChannel({})).toBe(true);
    } finally {
      restore();
    }
  });
});
