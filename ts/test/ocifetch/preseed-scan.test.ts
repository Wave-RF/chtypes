/**
 * Public issue #690: an offline lookup reads a bounded number of blobs,
 * whatever the number of builds the cache holds. The verdicts come from the
 * blob reads the lookup itself made (`blobReadSeam`), never from a timing.
 */

import { createHash } from 'node:crypto';
import { cp, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { useFetchV1ForTests } from '../../src/ocifetch/channel.js';
import { resolveInstalled } from '../../src/ocifetch/ensure.js';
import { blobReadSeam } from '../../src/ocifetch/layout.js';
import type { TrustedKey } from '../../src/ocifetch/types.js';

useFetchV1ForTests();

const FIXTURES = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../tests/fixtures/fetch-v1');
const LAYOUT = path.join(FIXTURES, 'layouts', 'offline-hit');
// The offline-hit layout's one build: 26.1.1.1 on linux-arm64.
const BUILD_MANIFEST_HEX = 'b461315c30649fec6d9cc988330ed3081ffd6a2a45aa21acda6ff212413f8637';
const BUILD_LAYER_HEX = 'df72d6f06f8ed738ecdd8e080dbd71244b66024daee20c85f53d3f822a49463b';
const BUNDLE_REF_HEX = '887b88511be68e82d653c734f57fc0e2c9c2e64bda4f23f25fe0ae1648f6244b';

const dirs: string[] = [];
afterEach(async () => {
  blobReadSeam.observe = undefined;
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

async function key(name: string): Promise<TrustedKey> {
  const ed25519Hex = (await readFile(path.join(FIXTURES, name, 'public.hex'), 'utf8')).trim();
  const keyid = createHash('sha256').update(Buffer.from(ed25519Hex, 'hex')).digest('hex').slice(0, 16);
  return { keyid, ed25519Hex };
}

/**
 * The offline-hit layout plus n-1 more pre-seeded entries of the same signed
 * build: each its own manifest (the original plus one annotation), its own
 * signature referrer manifest naming it, and its own copy of the bundle (the
 * original bytes plus trailing whitespace, which the signature does not
 * cover). The layer stays the one signed layer.
 */
async function seedClones(n: number): Promise<{ cache: string; digests: string[] }> {
  const parent = await mkdtemp(path.join(os.tmpdir(), 'preseed-scan-'));
  dirs.push(parent);
  const cache = path.join(parent, 'cache');
  await cp(LAYOUT, cache, { recursive: true });
  const blobs = path.join(cache, 'blobs', 'sha256');
  const put = async (b: Buffer): Promise<{ digest: string; size: number }> => {
    const hex = createHash('sha256').update(b).digest('hex');
    await writeFile(path.join(blobs, hex), b);
    return { digest: `sha256:${hex}`, size: b.length };
  };
  const index = JSON.parse(await readFile(path.join(cache, 'index.json'), 'utf8')) as { manifests: Record<string, unknown>[] };
  const first = index.manifests[0]!;
  const digests = [first['digest'] as string];
  const manifest = JSON.parse(await readFile(path.join(blobs, BUILD_MANIFEST_HEX), 'utf8')) as Record<string, unknown>;
  const ref = JSON.parse(await readFile(path.join(blobs, BUNDLE_REF_HEX), 'utf8')) as Record<string, unknown>;
  const bundleDesc = (ref['layers'] as Record<string, unknown>[])[0]!;
  const bundle = await readFile(path.join(blobs, (bundleDesc['digest'] as string).slice('sha256:'.length)));
  for (let i = 1; i < n; i++) {
    manifest['annotations'] = { 'test.clone': String(i) };
    const m = await put(Buffer.from(JSON.stringify(manifest)));
    const b = await put(Buffer.concat([bundle, Buffer.from(' '.repeat(i))]));
    bundleDesc['digest'] = b.digest;
    bundleDesc['size'] = b.size;
    ref['subject'] = { mediaType: 'application/vnd.oci.image.manifest.v1+json', digest: m.digest, size: m.size };
    await put(Buffer.from(JSON.stringify(ref)));
    const { annotations: _drop, ...rest } = first;
    index.manifests.push({ ...rest, digest: m.digest, size: m.size });
    digests.push(m.digest);
  }
  await writeFile(path.join(cache, 'index.json'), JSON.stringify(index));
  return { cache, digests };
}

/** Every blob of `cache` read while fn runs, by hex name. */
async function blobReads(cache: string, fn: () => Promise<unknown>): Promise<Map<string, number>> {
  const reads = new Map<string, number>();
  blobReadSeam.observe = (root, hex) => {
    if (root === cache) reads.set(hex, (reads.get(hex) ?? 0) + 1);
  };
  try {
    await fn();
  } finally {
    blobReadSeam.observe = undefined;
  }
  return reads;
}

function totals(reads: Map<string, number>): { total: number; most: number } {
  let total = 0;
  let most = 0;
  for (const n of reads.values()) {
    total += n;
    most = Math.max(most, n);
  }
  return { total, most };
}

describe('an offline lookup reads a bounded number of blobs (public issue #690)', () => {
  it('reads no layer for entries it does not install, at N=2 and N=20 alike', async () => {
    for (const n of [2, 20]) {
      const { cache } = await seedClones(n);
      // A signature that verifies under no trusted key: no lookup installs
      // these entries, so every lookup meets them again.
      const options = { cacheDir: cache, systemDirs: [], trustedKeys: [await key('other-key')] };
      let res: unknown;
      const reads = await blobReads(cache, async () => {
        res = await resolveInstalled('26.1', 'linux-arm64', options);
      });
      expect(res).toBeUndefined();
      const { total, most } = totals(reads);
      console.log(`N=${n}: ${total} blob reads, ${reads.size} distinct, most ${most}, layer ${reads.get(BUILD_LAYER_HEX) ?? 0}`);
      expect(reads.get(BUILD_LAYER_HEX) ?? 0, `N=${n}: layer reads`).toBe(0);
      expect(most, `N=${n}: the most reads of one blob`).toBeLessThanOrEqual(2);
      // Per entry: its manifest twice, its referrer manifest once and its
      // bundle twice; plus the two shared config blobs, once each.
      expect(total, `N=${n}: blob reads in one lookup`).toBeLessThanOrEqual(5 * n + 2);
    }
  });

  it('reads no blob on a miss when every entry is installed, at N=2 and N=20', async () => {
    for (const n of [2, 20]) {
      const { cache, digests } = await seedClones(n);
      const options = { cacheDir: cache, systemDirs: [], trustedKeys: [await key('test-key')] };
      // The first lookup installs every pre-seeded entry of the build.
      expect(await resolveInstalled('26.1', 'linux-arm64', options)).toBeDefined();
      for (const d of digests) await stat(path.join(cache, 'unpacked', 'sha256', d.slice('sha256:'.length)));
      let res: unknown;
      const reads = await blobReads(cache, async () => {
        res = await resolveInstalled('26.3', 'linux-arm64', options);
      });
      expect(res).toBeUndefined();
      expect(totals(reads).total, `N=${n}: blob reads on the miss`).toBe(0);
    }
  });

  it('still verifies the layer of the entry it installs', async () => {
    const { cache, digests } = await seedClones(20);
    const layer = path.join(cache, 'blobs', 'sha256', BUILD_LAYER_HEX);
    const b = await readFile(layer);
    b[b.length - 1] = b[b.length - 1]! ^ 0xff;
    await writeFile(layer, b);
    const options = { cacheDir: cache, systemDirs: [], trustedKeys: [await key('test-key')] };
    expect(await resolveInstalled('26.1', 'linux-arm64', options)).toBeUndefined();
    await expect(stat(path.join(cache, 'unpacked', 'sha256', digests[0]!.slice('sha256:'.length)))).rejects.toThrow();
  });
});
