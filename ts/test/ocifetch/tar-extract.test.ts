/**
 * The v1-only tar rules `../../src/tar.ts`'s `extractTarStream` adds on top
 * of v0's `extractTarGz`: refusing a duplicate entry name and capping total
 * unpacked bytes — both off by default, so v0's `extractTarGz` (tested
 * extensively in `../fetch.test.ts`) is unaffected. Builds tiny ustar
 * archives by hand; no gzip/zstd involved, since `extractTarStream` takes an
 * already-decompressed stream.
 *
 * `extractTarStream` lives in the shared v0 `tar.ts`, so it throws v0's own
 * `ArtifactCorruptError` (`../../src/errors.js`) — a different class from
 * `../../src/ocifetch/errors.js`'s, despite the identical name.
 * `unpack.ts`'s caller re-wraps it into the v1 class; this file asserts
 * against `extractTarStream` directly, so it checks the v0 class.
 */

import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { Readable } from 'node:stream';
import { afterEach, describe, expect, it } from 'vitest';
import { extractTarStream } from '../../src/tar.js';
import { ArtifactCorruptError } from '../../src/errors.js';

const BLOCK = 512;

function ustarHeader(name: string, size: number, typeflag: string): Buffer {
  const header = Buffer.alloc(BLOCK);
  header.write(name, 0, 'utf8');
  header.write('0000644\0', 100, 'ascii');
  header.write('0000000\0', 108, 'ascii');
  header.write('0000000\0', 116, 'ascii');
  header.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 'ascii');
  header.write('00000000000\0', 136, 'ascii');
  header.write('        ', 148, 'ascii'); // checksum placeholder (spaces)
  header.write(typeflag, 156, 'ascii');
  header.write('ustar\0', 257, 'ascii');
  header.write('00', 263, 'ascii');
  let sum = 0;
  for (let i = 0; i < BLOCK; i++) sum += header[i]!;
  header.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 'ascii');
  return header;
}

function padTo(buf: Buffer): Buffer {
  const rem = buf.length % BLOCK;
  if (rem === 0) return buf;
  return Buffer.concat([buf, Buffer.alloc(BLOCK - rem)]);
}

function fileEntry(name: string, content: string): Buffer {
  const data = Buffer.from(content, 'utf8');
  return Buffer.concat([ustarHeader(name, data.length, '0'), padTo(data)]);
}

function tarOf(...entries: Buffer[]): Buffer {
  return Buffer.concat([...entries, Buffer.alloc(BLOCK * 2)]);
}

let tmp: string | undefined;

afterEach(async () => {
  if (tmp !== undefined) await rm(tmp, { recursive: true, force: true });
  tmp = undefined;
});

async function freshDir(): Promise<string> {
  tmp = await mkdtemp(path.join(tmpdir(), 'ocifetch-v1-tar-'));
  return tmp;
}

describe('extractTarStream', () => {
  it('unpacks regular files, permissively by default (v0 compat)', async () => {
    const dest = await freshDir();
    const tar = tarOf(fileEntry('manifest.json', '{}'), fileEntry('libchtypes.so', 'binary-stand-in'));
    const entries = await extractTarStream(Readable.from(tar), dest);
    expect(entries.map((e) => e.name).sort()).toEqual(['libchtypes.so', 'manifest.json']);
    expect(await readFile(path.join(dest, 'libchtypes.so'), 'utf8')).toBe('binary-stand-in');
  });

  it('refuses a duplicate entry name only when asked to (CORRUPT)', async () => {
    const dest = await freshDir();
    const tar = tarOf(fileEntry('libchtypes.so', 'first'), fileEntry('libchtypes.so', 'second'));
    // Off by default — last one wins, exactly like `extractTarGz`.
    const entries = await extractTarStream(Readable.from(tar), dest);
    expect(entries).toHaveLength(2);
    expect(await readFile(path.join(dest, 'libchtypes.so'), 'utf8')).toBe('second');
  });

  it('refuses a duplicate entry name when refuseDuplicateNames is set', async () => {
    const dest = await freshDir();
    const tar = tarOf(fileEntry('libchtypes.so', 'first'), fileEntry('libchtypes.so', 'second'));
    await expect(extractTarStream(Readable.from(tar), dest, { refuseDuplicateNames: true })).rejects.toThrow(ArtifactCorruptError);
  });

  it('enforces a total-bytes cap as the stream is written', async () => {
    const dest = await freshDir();
    const tar = tarOf(fileEntry('a.txt', 'x'.repeat(100)), fileEntry('b.txt', 'y'.repeat(100)));
    await expect(extractTarStream(Readable.from(tar), dest, { maxTotalBytes: 150 })).rejects.toThrow(ArtifactCorruptError);
  });

  it('a cap large enough for the whole archive succeeds', async () => {
    const dest = await freshDir();
    const tar = tarOf(fileEntry('a.txt', 'x'.repeat(100)), fileEntry('b.txt', 'y'.repeat(100)));
    const entries = await extractTarStream(Readable.from(tar), dest, { maxTotalBytes: 1000 });
    expect(entries).toHaveLength(2);
  });

  it('still refuses a symlink/device entry with the options tightened', async () => {
    const dest = await freshDir();
    const bad = Buffer.concat([ustarHeader('evil', 0, '2'), Buffer.alloc(BLOCK * 2)]);
    await expect(extractTarStream(Readable.from(bad), dest, { refuseDuplicateNames: true, maxTotalBytes: 1000 })).rejects.toThrow(
      ArtifactCorruptError,
    );
  });

  it('propagates an error from an upstream pipeline stage (array form), not just the extractor', async () => {
    const dest = await freshDir();
    async function* bad(): AsyncGenerator<Buffer> {
      yield Buffer.from('not a tar header at all, too short');
      throw new Error('upstream decompressor failed');
    }
    await expect(extractTarStream([Readable.from(bad())], dest)).rejects.toThrow();
  });
});
