/**
 * Public issue #486, the behavior fixes: modes that follow the umask, one root
 * order, read-only lookups that create nothing, and the 0.x hint. Every fault
 * here is made by a real write or a real install, never by a stubbed reader.
 */

import { createHash } from 'node:crypto';
import { lstat, mkdir, mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { Registry } from '../../src/registry.js';
import { ArtifactMissingError } from '../../src/ocifetch/errors.js';
import { ensure, listInstalled, resolveInstalled, verifyInstalled } from '../../src/ocifetch/ensure.js';
import {
  commitStaging,
  encodeRecord,
  ensureLayout,
  freshStagingDir,
  installBlob,
  unpackedDir,
  upsertIndexEntry,
  type VerifiedRecord,
  writeVerifiedRecord,
} from '../../src/ocifetch/layout.js';
import type { ArtifactPredicate } from '../../src/ocifetch/types.js';

const HINT_TAIL = `holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at \${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`;

const dirs: string[] = [];
afterEach(async () => {
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

async function tmp(): Promise<string> {
  const d = await mkdtemp(path.join(os.tmpdir(), 'cache-roots-'));
  dirs.push(d);
  return d;
}

function sha256(text: string): string {
  return createHash('sha256').update(text).digest('hex');
}

function recordFor(version: string, build: string, salt: string): { record: VerifiedRecord; hex: string; library: string } {
  const library = `library ${version} ${build}`;
  const hex = sha256(`${salt}${version}${build}`);
  return {
    hex,
    library,
    record: {
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
    },
  };
}

/** One record installed by hand under `root`, as any binding leaves it; returns the entry directory. */
async function writeRecord(root: string, version: string, build: string): Promise<string> {
  const { record, hex, library } = recordFor(version, build, root);
  const entry = unpackedDir(root, hex);
  await mkdir(entry, { recursive: true });
  await writeFile(path.join(entry, 'lib.so'), library);
  await writeFile(path.join(entry, 'verified.json'), encodeRecord(record));
  return entry;
}

/** Every path under `root` with its size, or `undefined` when `root` is absent. */
async function treeOf(root: string): Promise<Record<string, number> | undefined> {
  try {
    await lstat(root);
  } catch {
    return undefined;
  }
  const out: Record<string, number> = {};
  const walk = async (dir: string): Promise<void> => {
    for (const name of await readdir(dir)) {
      const p = path.join(dir, name);
      const st = await lstat(p);
      out[p] = st.size;
      if (st.isDirectory()) await walk(p);
    }
  };
  await walk(root);
  return out;
}

/** What a 0.x install left behind: `<minor>/manifest.json`, no `oci-layout`. */
async function zeroXRegistry(root: string): Promise<void> {
  for (const [rel, body] of [
    ['26.1/manifest.json', '{}'],
    ['26.1/libchtypes.so', '0.x library'],
    ['patches/26.1.3.4/manifest.json', '{}'],
  ] as const) {
    await mkdir(path.dirname(path.join(root, rel)), { recursive: true });
    await writeFile(path.join(root, rel), body);
  }
}

describe('modes follow the umask (public issue #486)', () => {
  for (const umask of [0o022, 0o077]) {
    it(`an install at umask ${umask.toString(8).padStart(3, '0')} creates 0777 and 0666 less the umask`, async () => {
      const root = path.join(await tmp(), 'cache');
      const old = process.umask(umask);
      let entry: string;
      try {
        const { record, hex, library } = recordFor('26.8.15.10', '20261001.183455', 'modes');
        await ensureLayout(root);
        await installBlob(root, hex, Buffer.from('{}'));
        const staging = await freshStagingDir(root);
        await mkdir(path.join(staging, 'sub'), { recursive: true });
        await writeFile(path.join(staging, 'lib.so'), library);
        await writeVerifiedRecord(staging, record);
        entry = unpackedDir(root, hex);
        await commitStaging(staging, entry);
        await upsertIndexEntry(root, { mediaType: 'application/vnd.oci.image.manifest.v1+json', digest: `sha256:${hex}`, size: 2 });
      } finally {
        process.umask(old);
      }
      const seen: string[] = [];
      const walk = async (dir: string): Promise<void> => {
        for (const name of await readdir(dir)) {
          const p = path.join(dir, name);
          const st = await lstat(p);
          const want = (st.isDirectory() ? 0o777 : 0o666) & ~umask;
          expect({ path: p, mode: (st.mode & 0o777).toString(8) }).toEqual({ path: p, mode: want.toString(8) });
          seen.push(p);
          if (st.isDirectory()) await walk(p);
        }
      };
      await walk(root);
      expect(seen).toContain(entry);
      expect(seen).toContain(path.join(entry, 'verified.json'));
      expect(seen).toContain(path.join(root, 'index.json'));
    });
  }
});

describe('one root order (public issue #486)', () => {
  for (const c of [
    { name: 'the system dir holds the newer version', cache: ['26.8.1.1', '20260801.000001'], sys: ['26.8.2.1', '20260802.000001'], winner: 'system' },
    { name: 'the system dir holds a newer build', cache: ['26.8.1.1', '20260801.000001'], sys: ['26.8.1.1', '20260801.000002'], winner: 'system' },
    { name: 'the cache holds the newer version', cache: ['26.8.2.1', '20260802.000001'], sys: ['26.8.1.1', '20260801.000001'], winner: 'cache' },
    { name: 'a tie goes to the cache', cache: ['26.8.1.1', '20260801.000001'], sys: ['26.8.1.1', '20260801.000001'], winner: 'cache' },
  ] as const) {
    it(`resolve, list and verify read the system dirs too: ${c.name}`, async () => {
      const base = await tmp();
      const cache = path.join(base, 'cache');
      const sys = path.join(base, 'system');
      const cacheEntry = await writeRecord(cache, c.cache[0], c.cache[1]);
      const sysEntry = await writeRecord(sys, c.sys[0], c.sys[1]);
      const options = { cacheDir: cache, systemDirs: [sys], platform: 'linux-arm64' as const };
      const got = await resolveInstalled('26.8', 'linux-arm64', options);
      const want = c.winner === 'system' ? { dir: sysEntry, source: `system:${sys}` } : { dir: cacheEntry, source: 'cache' };
      expect({ dir: got?.dir, source: got?.source }).toEqual(want);
      expect((await ensure('26.8', { ...options, offline: true })).dir).toBe(want.dir);
      const listed = await listInstalled(options);
      expect(listed.map((r) => [r.dir, r.source])).toEqual([
        [cacheEntry, 'cache'],
        [sysEntry, `system:${sys}`],
      ]);
      const verified = await verifyInstalled(options);
      expect(verified.map((r) => [r.dir, r.ok])).toEqual([
        [cacheEntry, true],
        [sysEntry, true],
      ]);
    });
  }
});

describe('read-only lookups create nothing (public issue #486)', () => {
  it('resolve, list, verify and an offline ensure leave a missing cache and a 0.x registry untouched', async () => {
    const base = await tmp();
    const zeroX = path.join(base, 'zero-x');
    await zeroXRegistry(zeroX);
    const noSystem = path.join(base, 'no-such-system-dir');
    for (const cache of [path.join(base, 'no-such-cache'), zeroX]) {
      const before = await treeOf(cache);
      const options = { cacheDir: cache, systemDirs: [noSystem], platform: 'linux-arm64' as const };
      expect(await resolveInstalled('26.1', 'linux-arm64', options)).toBeUndefined();
      expect(await listInstalled(options)).toEqual([]);
      expect(await verifyInstalled(options)).toEqual([]);
      await expect(ensure('26.1', { ...options, offline: true })).rejects.toBeInstanceOf(ArtifactMissingError);
      expect(await treeOf(cache)).toEqual(before);
      expect(await treeOf(noSystem)).toBeUndefined();
    }
  });
});

describe('the 0.x hint (public issue #486)', () => {
  it('a MISSING answer from a 0.x registry names it and the v1 root, from the offline fetch and the registry alike', async () => {
    const base = await tmp();
    const zeroX = path.join(base, 'zero-x');
    await zeroXRegistry(zeroX);
    const options = { cacheDir: zeroX, systemDirs: [], platform: 'linux-arm64' as const };
    await expect(ensure('26.1', { ...options, offline: true })).rejects.toThrow(`${zeroX} ${HINT_TAIL}`);

    const registry = await Registry.open({ fetch: { cacheDir: zeroX, systemDirs: [] }, autofetch: false });
    await expect(registry.for('26.1')).rejects.toThrow(`${zeroX} ${HINT_TAIL}`);
  });
});
