import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { satisfiesRequest } from '../../src/ocifetch/ensure.js';
import {
  commitStaging,
  decodeRecord,
  encodeRecord,
  listVerified,
  readVerifiedRecord,
  type VerifiedRecord,
  writeVerifiedRecord,
} from '../../src/ocifetch/layout.js';
import type { ArtifactPredicate } from '../../src/ocifetch/types.js';
import { useFetchV1ForTests } from '../../src/ocifetch/channel.js';

// This file tests the v1 fetch contract that the ABI v2 dev channel narrows
// (src/ocifetch/channel.ts): its fixtures name their own registry and key, and
// write schema-1 records, abi-1 predicates and locks. The dev channel's own
// rules (spec/abi-v2/docs.md r5, r6) are test/ocifetch/devchannel.test.ts and
// test/cli-devchannel.test.ts.
useFetchV1ForTests();

const record: VerifiedRecord = {
  platform: 'linux-arm64',
  version: '26.8.15.10',
  channel: null,
  build: '20261001.183455',
  library: 'libchtypes.so',
  librarySha256: 'b'.repeat(64),
  libraryBytes: 3,
  indexDigest: null,
  manifestDigest: `sha256:${'a'.repeat(64)}`,
  layerDigest: `sha256:${'c'.repeat(64)}`,
  bundleDigest: null,
  bundleManifestDigest: null,
  signedBy: null,
  predicate: { build: '20261001.183455' } as unknown as ArtifactPredicate,
};

const dirs: string[] = [];
afterEach(async () => {
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

describe('the canonical verified.json', () => {
  it('writes every member, null where the schema allows, and reads it back', () => {
    const doc = JSON.parse(encodeRecord(record)) as Record<string, unknown>;
    for (const key of ['schema', 'platform', 'version', 'channel', 'build', 'library', 'library_sha256', 'library_bytes', 'digests', 'signed_by', 'predicate']) {
      expect(doc).toHaveProperty(key);
    }
    expect(doc['channel']).toBeNull();
    expect(doc['signed_by']).toBeNull();
    expect(Object.keys(doc['digests'] as object).sort()).toEqual(['bundle', 'bundle_manifest', 'index', 'layer', 'manifest']);
    expect(decodeRecord(encodeRecord(record))).toEqual(record);
  });

  it('treats every foreign or malformed record as absent', () => {
    const good = encodeRecord(record);
    const oldFlat = '{"platform":"linux-arm64","version":"26.8.15.10","build":"1","manifest_digest":"x"}';
    const bad = [
      'not json',
      '',
      oldFlat,
      good.replace('"schema":1', '"schema":2'),
      good.replace('"library":"libchtypes.so"', '"library":"/etc/passwd"'),
      good.replace('"library":"libchtypes.so"', '"library":"../x"'),
      good.replace('b'.repeat(64), 'abc'),
      good.replace('"signed_by":null,', ''),
    ];
    for (const doc of bad) expect(decodeRecord(doc), doc).toBeUndefined();
    expect(decodeRecord(good)).toBeDefined();
  });

  it('refuses to write a record that would not read back, and reads an unreadable one as absent', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'verified-record-'));
    dirs.push(dir);
    await expect(writeVerifiedRecord(dir, { ...record, build: '' })).rejects.toThrow();
    await writeFile(path.join(dir, 'verified.json'), '{"platform":"x"}');
    expect(await readVerifiedRecord(dir)).toBeUndefined();
    await mkdir(path.join(dir, 'sub'));
    await writeVerifiedRecord(path.join(dir, 'sub'), record);
    expect(await readVerifiedRecord(path.join(dir, 'sub'))).toEqual(record);
    expect((await readFile(path.join(dir, 'sub', 'verified.json'), 'utf8')).length).toBeGreaterThan(0);
  });
});

/** A staged install: a library and its record, the shape commitStaging receives. */
async function staged(parent: string, name: string): Promise<{ dir: string; ino: number }> {
  const dir = path.join(parent, name);
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, record.library), 'abc');
  await writeVerifiedRecord(dir, record);
  return { dir, ino: (await stat(dir)).ino };
}

describe('installing an entry (public issue #482)', () => {
  it('keeps the first of many concurrent installs, and every racer succeeds', async () => {
    for (let round = 0; round < 10; round++) {
      const root = await mkdtemp(path.join(os.tmpdir(), 'commit-race-'));
      dirs.push(root);
      const parent = path.join(root, 'unpacked', 'sha256');
      const finalDir = path.join(parent, 'a'.repeat(64));
      const stagings = await Promise.all(Array.from({ length: 16 }, (_, i) => staged(parent, `.staging-${i}`)));
      const lost = await Promise.all(stagings.map((s) => commitStaging(s.dir, finalDir)));
      expect(lost.filter((l) => !l)).toHaveLength(1);
      const winner = stagings[lost.indexOf(false)]!;
      expect((await stat(finalDir)).ino).toBe(winner.ino);
      expect(await readVerifiedRecord(finalDir)).toEqual(record);
      expect(await readdir(parent)).toEqual(['a'.repeat(64)]);
    }
  });

  it('never replaces an entry with an acceptable record, and replaces one without', async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'commit-keep-'));
    dirs.push(root);
    const parent = path.join(root, 'unpacked', 'sha256');
    const finalDir = path.join(parent, 'a'.repeat(64));
    const first = await staged(parent, '.staging-first');
    expect(await commitStaging(first.dir, finalDir)).toBe(false);
    const second = await staged(parent, '.staging-second');
    expect(await commitStaging(second.dir, finalDir)).toBe(true);
    expect((await stat(finalDir)).ino).toBe(first.ino);

    const torn = path.join(parent, 'b'.repeat(64));
    await mkdir(torn);
    await writeFile(path.join(torn, 'verified.json'), 'not json {');
    const fresh = await staged(parent, '.staging-fresh');
    expect(await commitStaging(fresh.dir, torn)).toBe(false);
    expect((await stat(torn)).ino).toBe(fresh.ino);
    expect(await readVerifiedRecord(torn)).toEqual(record);
    expect((await readdir(parent)).sort()).toEqual(['a'.repeat(64), 'b'.repeat(64)]);
  });

  it('lists only <manifest-hex> entries, never an installer\'s temporary directory', async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'list-entries-'));
    dirs.push(root);
    const parent = path.join(root, 'unpacked', 'sha256');
    await staged(parent, '.staging-x');
    await staged(parent, 'unpack-123');
    await staged(parent, 'c'.repeat(64));
    const listed = await listVerified(root);
    expect(listed.map((e) => path.basename(e.dir))).toEqual(['c'.repeat(64)]);
  });
});

describe('the load-time assertion (public issue #481)', () => {
  it('accepts a version within its request and nothing else', () => {
    for (const request of ['26.8', '26.8.5', '26.8.5.1', 'a-literal-tag']) expect(satisfiesRequest(request, '26.8.5.1'), request).toBe(true);
    for (const request of ['26.3', '26.3.4.1', '26.8.5.2', '26.80', '26.8.15']) expect(satisfiesRequest(request, '26.8.5.1'), request).toBe(false);
  });
});
