/**
 * Public issue #486: every cache fault, in the default mode (absent, plus a
 * warning naming the path) and in strict mode (`CacheUnusableError` naming the
 * path and the reason, and no fall-through to a system dir), and the class of
 * a failed write. Every fault is made by a real chmod, a real write or a real
 * install, and each chmod is proved to have taken effect before the case runs.
 */

import { createHash } from 'node:crypto';
import { chmod, mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { isChtypesError } from '../../src/abi2/index.js';
import { CacheUnusableError, type FetchV1Options } from '../../src/ocifetch/index.js';
import { ensure, listInstalled, missingNotes, probeCache, resolveInstalled, verifyInstalled } from '../../src/ocifetch/ensure.js';
import { encodeRecord, unpackedDir, type VerifiedRecord } from '../../src/ocifetch/layout.js';
import type { ArtifactPredicate } from '../../src/ocifetch/types.js';
import { Registry } from '../../src/registry.js';
import { useFetchV1ForTests } from '../../src/ocifetch/channel.js';

// This file tests the v1 fetch contract that the ABI v2 dev channel narrows
// (src/ocifetch/channel.ts): its fixtures name their own registry and key, and
// write schema-1 records, abi-1 predicates and locks. The dev channel's own
// rules (spec/abi-v2/docs.md r5, r6) are test/ocifetch/devchannel.test.ts and
// test/cli-devchannel.test.ts.
useFetchV1ForTests();

const asRoot = typeof process.geteuid === 'function' && process.geteuid() === 0;

const dirs: string[] = [];
const restores: (() => Promise<void>)[] = [];
afterEach(async () => {
  for (const r of restores.splice(0).reverse()) await r().catch(() => {});
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

async function tmp(): Promise<string> {
  const d = await mkdtemp(path.join(os.tmpdir(), 'cache-strict-'));
  dirs.push(d);
  return d;
}

function sha256(text: string): string {
  return createHash('sha256').update(text).digest('hex');
}

async function writeRecord(root: string, version: string, build: string): Promise<string> {
  const library = `library ${version} ${build}`;
  const hex = sha256(`${root}${version}${build}`);
  const record: VerifiedRecord = {
    platform: 'linux-arm64',
    version,
    channel: null,
    build,
    library: 'lib.so',
    librarySha256: sha256(library),
    libraryBytes: library.length,
    indexDigest: null,
    manifestDigest: `sha256:${hex}`,
    layerDigest: `sha256:${'c'.repeat(64)}`,
    bundleDigest: null,
    bundleManifestDigest: null,
    signedBy: null,
    predicate: { clickhouse_version: version, build } as unknown as ArtifactPredicate,
  };
  const entry = unpackedDir(root, hex);
  await mkdir(entry, { recursive: true });
  await writeFile(path.join(entry, 'lib.so'), library);
  await writeFile(path.join(entry, 'verified.json'), encodeRecord(record));
  return entry;
}

async function zeroXRegistry(root: string): Promise<void> {
  await mkdir(path.join(root, '26.1'), { recursive: true });
  await writeFile(path.join(root, '26.1', 'manifest.json'), '{}');
}

/** chmod, restored when the test ends so its directory can be removed. */
async function chmodFor(p: string, mode: number): Promise<void> {
  const restore = (await stat(p)).isDirectory() ? 0o755 : 0o644;
  await chmod(p, mode);
  restores.push(() => chmod(p, restore));
}

/** The positive control: the chmod took effect for this process. */
async function denied(p: string): Promise<void> {
  const read = (await stat(p)).isDirectory() ? readdir(p) : readFile(p);
  await expect(read).rejects.toMatchObject({ code: 'EACCES' });
}

const faults: readonly {
  readonly name: string;
  readonly reason: string;
  readonly warns: boolean;
  readonly apply: (cache: string, entry: string) => Promise<string>;
}[] = [
  {
    name: 'root-000',
    reason: 'unreadable_root',
    warns: true,
    apply: async (cache) => {
      await chmodFor(cache, 0);
      await denied(cache);
      return cache;
    },
  },
  {
    name: 'unpacked-000',
    reason: 'unreadable_root',
    warns: true,
    apply: async (cache) => {
      const p = path.join(cache, 'unpacked', 'sha256');
      await chmodFor(p, 0);
      await denied(p);
      return p;
    },
  },
  {
    name: 'entry-000',
    reason: 'unreadable_entry',
    warns: true,
    apply: async (_cache, entry) => {
      await chmodFor(entry, 0);
      await denied(entry);
      return entry;
    },
  },
  {
    name: 'record-000',
    reason: 'unreadable_entry',
    warns: true,
    apply: async (_cache, entry) => {
      const p = path.join(entry, 'verified.json');
      await chmodFor(p, 0);
      await denied(p);
      return p;
    },
  },
  {
    name: 'record-garbage-noblobs',
    reason: 'unacceptable_record',
    warns: false,
    apply: async (cache, entry) => {
      const p = path.join(entry, 'verified.json');
      await writeFile(p, 'not json {');
      await expect(stat(path.join(cache, 'blobs'))).rejects.toMatchObject({ code: 'ENOENT' });
      return p;
    },
  },
  {
    name: 'root-is-a-file',
    reason: 'not_a_directory',
    warns: true,
    apply: async (cache) => {
      await rm(cache, { recursive: true, force: true });
      await writeFile(cache, 'not a cache');
      return cache;
    },
  },
  {
    name: 'layout-0x',
    reason: 'layout_0x',
    warns: false,
    apply: async (cache) => {
      await rm(cache, { recursive: true, force: true });
      await zeroXRegistry(cache);
      return cache;
    },
  },
];

describe.skipIf(asRoot)('every cache fault in both modes (public issue #486)', () => {
  for (const fault of faults) {
    for (const withSystem of [false, true]) {
      it(`${fault.name}, ${withSystem ? 'with' : 'without'} a system dir`, async () => {
        const base = await tmp();
        const cache = path.join(base, 'cache');
        const system = path.join(base, 'system');
        const entry = await writeRecord(cache, '26.8.1.1', '20260801.000001');
        const sysEntry = await writeRecord(system, '26.8.1.1', '20260801.000001');
        const faultPath = await fault.apply(cache, entry);
        const warning = `${faultPath} could not be read (`;
        const systemDirs = withSystem ? [system] : [];

        // Default mode: the fault reads as absent, with a warning for K1/K2.
        const lenient: FetchV1Options = { cacheDir: cache, systemDirs, platform: 'linux-arm64', strictCache: false };
        const got = await resolveInstalled('26.8', 'linux-arm64', lenient);
        if (withSystem) {
          expect(got?.dir).toBe(sysEntry);
          expect((got?.warnings ?? []).join('\n').includes(warning)).toBe(fault.warns);
        } else {
          expect(got).toBeUndefined();
        }
        expect((await missingNotes(lenient)).join('\n').includes(warning)).toBe(fault.warns);
        await listInstalled(lenient);
        await verifyInstalled(lenient);

        // Strict mode: CacheUnusableError, the same with or without a system dir.
        const strict: FetchV1Options = { ...lenient, strictCache: true };
        for (const call of [
          () => resolveInstalled('26.8', 'linux-arm64', strict),
          () => listInstalled(strict),
          () => verifyInstalled(strict),
          () => ensure('26.8', { ...strict, offline: true }),
          () => probeCache(strict),
        ]) {
          let caught: unknown;
          try {
            await call();
          } catch (err) {
            caught = err;
          }
          expect(caught).toBeInstanceOf(CacheUnusableError);
          expect(isChtypesError(caught)).toBe(true);
          const e = caught as CacheUnusableError;
          expect({ code: e.code, reason: e.reason, path: e.path, exit: e.exitStatus }).toEqual({
            code: 'CHTYPES_CACHE_UNUSABLE',
            reason: fault.reason,
            path: faultPath,
            exit: 9,
          });
          expect(e.message).toContain(`${faultPath} is unusable as a cache: ${fault.reason}`);
          expect(e.osError !== undefined).toBe(fault.warns);
        }
      });
    }
  }
});

describe('strict mode comes from the option, then the environment (public issue #486)', () => {
  for (const c of [
    { env: '', option: undefined, strict: false },
    { env: '1', option: undefined, strict: true },
    { env: '0', option: undefined, strict: false },
    { env: '1', option: false, strict: false },
    { env: '', option: true, strict: true },
  ]) {
    it(`CHTYPES_CACHE_STRICT=${JSON.stringify(c.env)}, option ${String(c.option)}`, async () => {
      const cache = path.join(await tmp(), 'cache');
      await zeroXRegistry(cache);
      const saved = process.env['CHTYPES_CACHE_STRICT'];
      process.env['CHTYPES_CACHE_STRICT'] = c.env;
      try {
        const options: FetchV1Options = { cacheDir: cache, systemDirs: [], ...(c.option !== undefined ? { strictCache: c.option } : {}) };
        if (c.strict) await expect(listInstalled(options)).rejects.toBeInstanceOf(CacheUnusableError);
        else expect(await listInstalled(options)).toEqual([]);
      } finally {
        if (saved === undefined) delete process.env['CHTYPES_CACHE_STRICT'];
        else process.env['CHTYPES_CACHE_STRICT'] = saved;
      }
    });
  }
});

describe.skipIf(asRoot)('strict system dirs and failed writes (public issue #486)', () => {
  it('a system dir that exists but cannot be read is an error in strict mode; a missing one is skipped', async () => {
    const base = await tmp();
    const cache = path.join(base, 'cache');
    const system = path.join(base, 'system');
    await writeRecord(cache, '26.8.1.1', '20260801.000001');
    await writeRecord(system, '26.8.1.1', '20260801.000001');
    const options: FetchV1Options = { cacheDir: cache, systemDirs: [path.join(base, 'absent'), system], strictCache: true };
    expect(await resolveInstalled('26.8', 'linux-arm64', options)).toBeDefined();
    await chmodFor(system, 0);
    await denied(system);
    await expect(resolveInstalled('26.8', 'linux-arm64', options)).rejects.toMatchObject({ path: system, reason: 'unreadable_root' });
  });

  it('a write the fetch layer needed that failed is CacheUnusableError with reason unwritable, in the default mode too', async () => {
    const cache = path.join(await tmp(), 'cache');
    await mkdir(cache);
    await chmodFor(cache, 0o555);
    await expect(mkdir(path.join(cache, 'probe'))).rejects.toMatchObject({ code: 'EACCES' });
    let caught: unknown;
    try {
      // The layout is made before anything is fetched, so no registry is needed for the write to fail.
      await ensure('26.8', { cacheDir: cache, systemDirs: [], platform: 'linux-arm64', bases: ['http://127.0.0.1:9/chtypes/v1'] });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(CacheUnusableError);
    const e = caught as CacheUnusableError;
    expect({ reason: e.reason, osError: e.osError, under: e.path.startsWith(cache) }).toEqual({ reason: 'unwritable', osError: 'EACCES', under: true });
  });
});

describe('the public class (public issue #486)', () => {
  it('a strict registry over a 0.x registry throws CacheUnusableError naming the path and the reason', async () => {
    const zeroX = path.join(await tmp(), 'zero-x');
    await zeroXRegistry(zeroX);
    const registry = await Registry.open({ fetch: { cacheDir: zeroX, systemDirs: [], strictCache: true }, autofetch: false });
    await expect(registry.for('26.1')).rejects.toMatchObject({ code: 'CHTYPES_CACHE_UNUSABLE', path: zeroX, reason: 'layout_0x' });
    await expect(registry.installed()).rejects.toBeInstanceOf(CacheUnusableError);
  });
});
