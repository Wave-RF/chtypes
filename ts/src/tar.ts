/**
 * A streaming reader for the artifact tarballs — ustar, pax extended
 * headers, GNU long names — written here rather than taken from a
 * dependency: this package's one runtime dependency is ffi-rs, and a reader
 * that extracts regular files under one directory is a page of code.
 *
 * Files only. Directories are created; everything else — symlinks, hard
 * links, devices, FIFOs — is skipped without being followed: an artifact is
 * `manifest.json`, one library and two text files, and a link inside one is
 * an attack, not a feature. Every entry name is confined to the destination
 * (no absolute names, no `..` components, no escape once resolved), and a
 * name that escapes is refused as corrupt rather than clamped.
 *
 * The archive is consumed as a stream: a 300 MB library is written to disk
 * as it inflates and never sits in memory whole.
 */

import { createReadStream } from 'node:fs';
import { mkdir, open as openFile, type FileHandle } from 'node:fs/promises';
import path from 'node:path';
import { Writable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createGunzip } from 'node:zlib';
import { ArtifactCorruptError, FetchError } from './errors.js';

const BLOCK = 512;
/** pax records and long names are tiny; a megabyte of them is not a tarball. */
const MAX_META = 1 << 20;

/** One regular file the extractor wrote. */
export interface ExtractedEntry {
  /** The entry name as confined under the destination, `/`-separated. */
  readonly name: string;
  readonly size: number;
}

/**
 * Inflate and unpack `archive` (a `.tar.gz`) into `dest`, which must already
 * exist. Returns the regular files written, in archive order.
 *
 * @throws {ArtifactCorruptError} when the bytes are not a gzip stream, a
 *   header fails its checksum, the archive is truncated, or an entry name
 *   would land outside `dest`.
 */
export async function extractTarGz(archive: string, dest: string): Promise<ExtractedEntry[]> {
  const extractor = new TarExtractor(path.resolve(dest));
  try {
    await pipeline(createReadStream(archive), createGunzip(), extractor);
  } catch (err) {
    if (err instanceof FetchError) throw err;
    throw new ArtifactCorruptError(`chtypes: ${archive} is not a readable tar.gz: ${errorText(err)}`, {
      cause: err,
    });
  }
  return extractor.entries;
}

type Pending =
  | { readonly kind: 'file'; readonly handle: FileHandle }
  | { readonly kind: 'meta'; readonly flag: number; readonly chunks: Buffer[] }
  | { readonly kind: 'skip' };

class TarExtractor extends Writable {
  readonly entries: ExtractedEntry[] = [];

  private readonly head = Buffer.alloc(BLOCK);
  private headFill = 0;
  /** Data bytes still to consume for the current entry, then the block padding after them. */
  private remaining = 0;
  private padding = 0;
  private pending: Pending | null = null;
  /** Overrides a pax `x` or GNU `L` entry set for the entry that follows. */
  private nextName: string | null = null;
  private nextSize: number | null = null;
  private zeroBlocks = 0;
  private ended = false;

  constructor(private readonly dest: string) {
    super();
  }

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.consume(chunk).then(
      () => callback(),
      (err: unknown) => callback(err instanceof Error ? err : new Error(String(err))),
    );
  }

  override _final(callback: (error?: Error | null) => void): void {
    const truncated = !this.ended && (this.headFill !== 0 || this.remaining !== 0 || this.pending !== null);
    this.closePending().then(
      () => callback(truncated ? new ArtifactCorruptError('chtypes: tarball is truncated') : null),
      (err: unknown) => callback(err instanceof Error ? err : new Error(String(err))),
    );
  }

  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    this.closePending().then(
      () => callback(error),
      () => callback(error),
    );
  }

  private async closePending(): Promise<void> {
    const p = this.pending;
    this.pending = null;
    if (p !== null && p.kind === 'file') await p.handle.close();
  }

  private async consume(chunk: Buffer): Promise<void> {
    let off = 0;
    while (off < chunk.length) {
      // Bytes after the end-of-archive marker are padding; nothing lives there.
      if (this.ended) return;
      if (this.remaining > 0) {
        const n = Math.min(this.remaining, chunk.length - off);
        await this.data(chunk.subarray(off, off + n));
        this.remaining -= n;
        off += n;
        if (this.remaining === 0) await this.finishEntry();
        continue;
      }
      if (this.padding > 0) {
        const n = Math.min(this.padding, chunk.length - off);
        this.padding -= n;
        off += n;
        continue;
      }
      const n = Math.min(BLOCK - this.headFill, chunk.length - off);
      chunk.copy(this.head, this.headFill, off, off + n);
      this.headFill += n;
      off += n;
      if (this.headFill === BLOCK) {
        this.headFill = 0;
        await this.header(this.head);
      }
    }
  }

  private async header(block: Buffer): Promise<void> {
    if (isZero(block)) {
      // Two zero blocks end the archive; one followed by EOF is tolerated in _final.
      this.zeroBlocks += 1;
      if (this.zeroBlocks >= 2) this.ended = true;
      return;
    }
    this.zeroBlocks = 0;
    if (!checksumOk(block)) throw new ArtifactCorruptError('chtypes: tarball header fails its checksum');

    const flag = block[156]!;
    const ownSize = parseSize(block, 124, 12);
    if (Number.isNaN(ownSize)) throw new ArtifactCorruptError('chtypes: tarball header carries an unreadable size');
    const isMeta = flag === 0x78 /* x */ || flag === 0x67 /* g */ || flag === 0x4c /* L */;

    let name: string;
    let size: number;
    if (isMeta) {
      name = '';
      size = ownSize;
      if (size > MAX_META) throw new ArtifactCorruptError('chtypes: tarball extended header is implausibly large');
      this.pending = { kind: 'meta', flag, chunks: [] };
    } else {
      const magic = block.toString('latin1', 257, 262);
      const prefix = magic === 'ustar' ? cstr(block, 345, 155) : '';
      const base = cstr(block, 0, 100);
      name = this.nextName ?? (prefix !== '' ? `${prefix}/${base}` : base);
      size = this.nextSize ?? ownSize;
      this.nextName = null;
      this.nextSize = null;

      if (flag === 0x30 /* 0 */ || flag === 0x00 || flag === 0x37 /* 7 */) {
        const target = this.confine(name, false);
        await mkdir(path.dirname(target), { recursive: true });
        const handle = await openFile(target, 'w');
        this.pending = { kind: 'file', handle };
        this.entries.push({ name: path.relative(this.dest, target).split(path.sep).join('/'), size });
      } else if (flag === 0x35 /* 5 */) {
        const target = this.confine(name, true);
        if (target !== this.dest) await mkdir(target, { recursive: true });
        this.pending = { kind: 'skip' };
      } else {
        // Links, devices, FIFOs: files only. Skipped, never followed.
        this.pending = { kind: 'skip' };
      }
    }

    this.remaining = size;
    this.padding = (BLOCK - (size % BLOCK)) % BLOCK;
    if (size === 0) await this.finishEntry();
  }

  private async data(slice: Buffer): Promise<void> {
    const p = this.pending;
    if (p === null) return;
    if (p.kind === 'file') {
      let written = 0;
      while (written < slice.length) {
        const r = await p.handle.write(slice, written, slice.length - written);
        if (r.bytesWritten <= 0) throw new Error('short write');
        written += r.bytesWritten;
      }
    } else if (p.kind === 'meta') {
      p.chunks.push(Buffer.from(slice));
    }
  }

  private async finishEntry(): Promise<void> {
    const p = this.pending;
    this.pending = null;
    if (p === null) return;
    if (p.kind === 'file') {
      await p.handle.close();
    } else if (p.kind === 'meta') {
      const payload = Buffer.concat(p.chunks);
      if (p.flag === 0x4c /* L: GNU long name */) {
        this.nextName = cstr(payload, 0, payload.length);
      } else if (p.flag === 0x78 /* x: pax, for the next entry */) {
        for (const [key, value] of paxRecords(payload)) {
          if (key === 'path') this.nextName = value;
          else if (key === 'size') {
            const n = Number(value);
            if (!/^\d+$/.test(value) || !Number.isSafeInteger(n)) {
              throw new ArtifactCorruptError('chtypes: tarball pax header carries an unreadable size');
            }
            this.nextSize = n;
          }
        }
      }
      // 'g' (global pax) carries nothing an artifact needs; consumed, ignored.
    }
  }

  /** Resolve an entry name under the destination, refusing anything that would leave it. */
  private confine(name: string, isDir: boolean): string {
    const bad = (why: string): never => {
      throw new ArtifactCorruptError(`chtypes: tarball entry ${JSON.stringify(name)} refused: ${why}`);
    };
    if (name === '') bad('empty name');
    if (name.includes('\\')) bad('backslash in name');
    if (name.startsWith('/')) bad('absolute path');
    const parts = name.split('/').filter((p) => p !== '' && p !== '.');
    if (parts.some((p) => p === '..')) bad('path traversal');
    if (parts.length === 0) {
      if (isDir) return this.dest;
      bad('no file name');
    }
    const target = path.join(this.dest, ...parts);
    if (target !== this.dest && !target.startsWith(this.dest + path.sep)) bad('escapes the destination');
    return target;
  }
}

function isZero(block: Buffer): boolean {
  for (let i = 0; i < block.length; i++) if (block[i] !== 0) return false;
  return true;
}

/** The header checksum, with the checksum field itself read as spaces; unsigned and signed sums both accepted. */
function checksumOk(block: Buffer): boolean {
  const recorded = parseOctal(block, 148, 8);
  if (Number.isNaN(recorded)) return false;
  let unsigned = 0;
  let signed = 0;
  for (let i = 0; i < BLOCK; i++) {
    const b = i >= 148 && i < 156 ? 0x20 : block[i]!;
    unsigned += b;
    signed += b >= 0x80 ? b - 0x100 : b;
  }
  return recorded === unsigned || recorded === signed;
}

function cstr(buf: Buffer, off: number, len: number): string {
  const end = Math.min(buf.length, off + len);
  let nul = off;
  while (nul < end && buf[nul] !== 0) nul++;
  return buf.toString('utf8', off, nul);
}

function parseOctal(buf: Buffer, off: number, len: number): number {
  const text = cstr(buf, off, len).trim();
  if (text === '') return 0;
  if (!/^[0-7]+$/.test(text)) return Number.NaN;
  return Number.parseInt(text, 8);
}

/** Sizes are octal, or base-256 with the high bit of the first byte set (GNU, for > 8 GiB). */
function parseSize(buf: Buffer, off: number, len: number): number {
  const first = buf[off]!;
  if ((first & 0x80) !== 0) {
    let value = first & 0x7f;
    for (let i = 1; i < len; i++) value = value * 256 + buf[off + i]!;
    return Number.isSafeInteger(value) ? value : Number.NaN;
  }
  return parseOctal(buf, off, len);
}

/** pax records: `<decimal length> <key>=<value>\n`, the length counting the whole record. */
function paxRecords(payload: Buffer): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  let pos = 0;
  while (pos < payload.length) {
    const space = payload.indexOf(0x20, pos);
    if (space < 0) break;
    const len = Number.parseInt(payload.toString('latin1', pos, space), 10);
    if (!Number.isInteger(len) || len <= 0 || pos + len > payload.length) {
      throw new ArtifactCorruptError('chtypes: tarball pax header is malformed');
    }
    const record = payload.toString('utf8', space + 1, pos + len - 1);
    const eq = record.indexOf('=');
    if (eq > 0) out.push([record.slice(0, eq), record.slice(eq + 1)]);
    pos += len;
  }
  return out;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
