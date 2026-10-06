/**
 * Bytes and unpack (`docs/guides/fetch-v1.md` §5): download the layer to a
 * temp file **while hashing**, verify size and sha256 against the manifest's
 * own layer descriptor **before any decompression**, zstd-decode (multi-frame
 * accepted, a window log past the limit refused before a frame's decoder
 * commits to it), unpack with the tightened v1 tar rules (`../tar.ts`'s
 * `extractTarStream`: regular files and directories only, no duplicate entry
 * names, a hard cap on total unpacked bytes), then re-hash the installed
 * library against the signed predicate.
 *
 * **Multi-frame zstd needs its own loop here — measured, not assumed.**
 * Node's built-in `zlib.createZstdDecompress()` (and `zstdDecompressSync`)
 * decode only the **first** frame of a concatenated multi-frame stream and
 * then stop silently (no error, no second frame): verified on this Mac
 * (Node 22.23.2) by concatenating two independently-produced zstd frames and
 * observing the decoder emit only the first frame's content, with
 * `bytesWritten` on the stream reporting exactly the first frame's
 * compressed length. `bytesWritten` is therefore how many input bytes one
 * frame consumed, so `decompressAllFrames` below runs one `createZstdDecompress`
 * per frame, each capped at `ZSTD_d_windowLogMax`, advancing through the
 * buffer by exactly that many bytes each time — the same "loop over frames"
 * the plan already calls out for Rust's `ruzstd`, just also true here.
 *
 * Every check below runs in the order the guide names, and a failure at any
 * step leaves nothing a later `--offline` read could mistake for a verified
 * install: the temp layer file and a partially-unpacked destination are both
 * the caller's (`ensure.ts`'s) to remove, since only it knows whether the
 * destination is a fresh temp directory or an existing one being replaced.
 */

import { createHash } from 'node:crypto';
import { closeSync, createReadStream, ftruncateSync, openSync, writeSync } from 'node:fs';
import { Readable } from 'node:stream';
import * as zlib from 'node:zlib';
import { extractTarStream, type ExtractedEntry } from '../tar.js';
import { MAX_UNPACKED_BYTES, ZSTD_WINDOW_LOG_MAX } from './constants.gen.js';
import { ArtifactCorruptError } from './errors.js';
import type { RequestOptions } from './http.js';
import { type ByteSink, type Descriptor, fetchBlobToSink, verifyDigestAndSize } from './oci.js';

/**
 * Streams into a file on disk while hashing, synchronously: `requestToSink`
 * (`http.ts`) calls `write` from inside a plain callback with no backpressure
 * of its own, so a sync write keeps every chunk in order with no risk of two
 * writes racing. `reset()` is called once before the first attempt and again
 * before every retry — a retried download restarts the file from empty,
 * exactly as it restarts the hash. Also keeps an in-memory copy: the
 * decompressor needs the whole compressed buffer anyway (see this module's
 * header), so a second disk read would buy nothing.
 */
class HashingFileSink implements ByteSink {
  private fd: number;
  private hash = createHash('sha256');
  private bytesWritten = 0;
  private chunks: Buffer[] = [];

  constructor(path: string) {
    this.fd = openSync(path, 'w');
  }

  reset(): void {
    this.hash = createHash('sha256');
    this.bytesWritten = 0;
    this.chunks = [];
    ftruncateSync(this.fd, 0);
  }

  write(chunk: Buffer): void {
    writeSync(this.fd, chunk);
    this.hash.update(chunk);
    this.chunks.push(chunk);
    this.bytesWritten += chunk.length;
  }

  /** Closes the file and returns what was actually written — never call `write` after this. */
  finish(): { readonly sha256: string; readonly size: number; readonly bytes: Buffer } {
    const sha256 = this.hash.digest('hex');
    const bytes = Buffer.concat(this.chunks);
    closeSync(this.fd);
    return { sha256, size: this.bytesWritten, bytes };
  }

  /** For an error path: close without finalizing the hash (which `digest()` would do anyway, uselessly). */
  abort(): void {
    try {
      closeSync(this.fd);
    } catch {
      // Already closed, or never fully opened — nothing left to release.
    }
  }
}

/**
 * Decodes every zstd frame in `compressed`, one `createZstdDecompress`
 * instance per frame (see this module's header for why), each capped at
 * `windowLogMax`. Yields decompressed chunks in order. Throws
 * `ArtifactCorruptError` on a decode error (including a window log past the
 * cap) or on a frame that consumes zero input bytes (would loop forever
 * otherwise).
 */
async function* decompressAllZstdFrames(compressed: Buffer, windowLogMax: number): AsyncGenerator<Buffer> {
  let offset = 0;
  while (offset < compressed.length) {
    const remaining = compressed.subarray(offset);
    const decoder = zlib.createZstdDecompress({ params: { [zlib.constants.ZSTD_d_windowLogMax]: windowLogMax } });
    const chunks: Buffer[] = [];
    let consumed = 0;
    try {
      await new Promise<void>((resolve, reject) => {
        decoder.on('data', (chunk: Buffer) => chunks.push(chunk));
        decoder.on('end', () => {
          consumed = decoder.bytesWritten;
          resolve();
        });
        decoder.on('error', reject);
        decoder.end(remaining);
      });
    } catch (err) {
      throw new ArtifactCorruptError(`chtypes: zstd decode failed: ${errorText(err)}`, { cause: err });
    }
    if (consumed <= 0) {
      throw new ArtifactCorruptError('chtypes: zstd stream made no forward progress (a malformed or empty frame)');
    }
    for (const chunk of chunks) yield chunk;
    offset += consumed;
  }
}

/**
 * Verifies already-in-memory layer bytes against their own descriptor, then
 * zstd-decodes and unpacks into `destDir` (guide §5, steps 1-4) — the shared
 * core both `fetchVerifyAndUnpackLayer` (downloaded bytes) and
 * `localverify.ts` (bytes read straight from a local `blobs/sha256/<hex>`,
 * for a pre-seeded cache entry) build on.
 */
export async function verifyAndUnpackLayerBytes(layerBytes: Buffer, layer: Descriptor, destDir: string): Promise<readonly ExtractedEntry[]> {
  const sha256 = createHash('sha256').update(layerBytes).digest('hex');
  // Size and sha256 against the descriptor, BEFORE any decompression
  // (guide §5 step 1) — `tampered-layer` must show no unpack attempted.
  verifyDigestAndSize(layer, sha256, layerBytes.length, `layer ${layer.digest}`);

  const decompressed = Readable.from(decompressAllZstdFrames(layerBytes, ZSTD_WINDOW_LOG_MAX));
  try {
    return await extractTarStream([decompressed], destDir, {
      refuseDuplicateNames: true,
      maxTotalBytes: MAX_UNPACKED_BYTES,
    });
  } catch (err) {
    if (err instanceof ArtifactCorruptError) throw err;
    throw new ArtifactCorruptError(`chtypes: layer ${layer.digest} failed to decode or unpack: ${errorText(err)}`, { cause: err });
  }
}

/**
 * Downloads `layer` (verified against its own descriptor) and unpacks it
 * into `destDir`, which must already exist and be empty. `tempLayerPath` is
 * a caller-owned scratch file for the compressed bytes (removed by the
 * caller once this returns, success or failure).
 */
export async function fetchVerifyAndUnpackLayer(
  bases: readonly string[],
  layer: Descriptor,
  tempLayerPath: string,
  destDir: string,
  options: RequestOptions,
): Promise<readonly ExtractedEntry[]> {
  const sink = new HashingFileSink(tempLayerPath);
  let downloaded: { sha256: string; size: number; bytes: Buffer };
  try {
    await fetchBlobToSink(bases, layer, options, sink);
    downloaded = sink.finish();
  } catch (err) {
    sink.abort();
    throw err;
  }
  return verifyAndUnpackLayerBytes(downloaded.bytes, layer, destDir);
}

/** The sha256 (lowercase hex) and byte length of the unpacked library, as the canonical `verified.json` records them. */
export async function measureLibrary(libraryPath: string): Promise<{ readonly sha256: string; readonly bytes: number }> {
  const hash = createHash('sha256');
  let size = 0;
  await new Promise<void>((resolve, reject) => {
    const s = createReadStream(libraryPath);
    s.on('data', (chunk: Buffer) => {
      size += chunk.length;
      hash.update(chunk);
    });
    s.on('end', resolve);
    s.on('error', reject);
  });
  return { sha256: hash.digest('hex'), bytes: size };
}

/** Re-hashes the installed library against the signed predicate's `library_sha256`/`library_bytes` (guide §5 step 5). */
export async function verifyInstalledLibrary(libraryPath: string, expectedSha256Hex: string, expectedBytes: number): Promise<void> {
  const hash = createHash('sha256');
  let size = 0;
  await new Promise<void>((resolve, reject) => {
    const s = createReadStream(libraryPath);
    s.on('data', (chunk: Buffer) => {
      size += chunk.length;
      hash.update(chunk);
    });
    s.on('end', resolve);
    s.on('error', reject);
  });
  if (size !== expectedBytes) {
    throw new ArtifactCorruptError(`chtypes: installed library is ${size} bytes, the signed predicate named ${expectedBytes}`);
  }
  const actual = hash.digest('hex');
  if (actual !== expectedSha256Hex) {
    throw new ArtifactCorruptError("chtypes: installed library's sha256 does not match the signed predicate's library_sha256");
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
