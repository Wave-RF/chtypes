import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { decodeRecord, encodeRecord, readVerifiedRecord, writeVerifiedRecord, type VerifiedRecord } from '../../src/ocifetch/layout.js';
import type { ArtifactPredicate } from '../../src/ocifetch/types.js';

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
