/**
 * The in-use signal (`docs/guides/fetch-v1.md` §1, "In use"; public issue
 * #494): `hold`, the shared flock a registry takes on an entry's
 * `verified.json` before it loads a build, and `prune`, which takes the
 * exclusive lock without waiting and keeps every build it cannot take it on.
 * A hold in this very process counts (flock belongs to the open file
 * description), and so does one in another process: here a python3 child
 * holding the same flock(2), which is what every binding takes. Go's
 * `go/internal/ocifetch/hold_test.go` is the same test.
 */

import { type ChildProcessByStdio, spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { Readable, Writable } from 'node:stream';
import { afterEach, describe, expect, it } from 'vitest';
import { useFetchV1ForTests } from '../../src/ocifetch/channel.js';
import { CACHE_VERIFIED_RECORD } from '../../src/ocifetch/constants.gen.js';
import { claim, hold } from '../../src/ocifetch/hold.js';
import { buildRecord, writeVerifiedRecord } from '../../src/ocifetch/layout.js';
import { prune } from '../../src/ocifetch/prune.js';

// The v1 contract: an explicit cache is the layout itself, and every record is visible.
useFetchV1ForTests();

const dirs: string[] = [];
function tempDir(): string {
  const d = mkdtempSync(path.join(tmpdir(), 'ts-hold-'));
  dirs.push(d);
  return d;
}

afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

/** Installs a fabricated linux-arm64 build of `version` into the layout `root`, with a record every reader accepts, and returns its entry directory. */
async function install(root: string, version: string, build: string): Promise<string> {
  const hex = createHash('sha256').update(`${version} ${build}`).digest('hex');
  const dir = path.join(root, 'unpacked', 'sha256', hex);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, 'libchtypes.so'), 'not a library');
  const line = version.split('.').slice(0, 2).join('.');
  await writeVerifiedRecord(
    dir,
    buildRecord({
      platform: 'linux-arm64',
      predicate: {
        abi: 1,
        abi_fingerprint: `sha256:${'f'.repeat(64)}`,
        clickhouse_version: version,
        channel: 'lts',
        clickhouse_minor: line,
        clickhouse_commit: '0'.repeat(40),
        os: 'linux',
        arch: 'arm64',
        build,
        core_commit: '0'.repeat(40),
        inputs_sha256: 'e'.repeat(64),
        library: 'libchtypes.so',
        library_sha256: 'a'.repeat(64),
        library_bytes: 13,
      },
      library: { sha256: 'a'.repeat(64), bytes: 13 },
      indexDigest: null,
      manifestDigest: `sha256:${hex}`,
      layerDigest: `sha256:${'b'.repeat(64)}`,
      bundleDigest: null,
      bundleManifestDigest: null,
      signedBy: null,
    }),
  );
  return dir;
}

describe('hold', () => {
  it('is vanished for an entry that is gone, held for one that is there, and held again on an entry removed and installed again', () => {
    expect(hold(path.join(tempDir(), 'absent'))).toBe('vanished');
    const dir = tempDir();
    const record = path.join(dir, CACHE_VERIFIED_RECORD);
    writeFileSync(record, '{}');
    expect(hold(dir)).toBe('held');
    expect(hold(dir)).toBe('held');
    // Removed and installed again: the old hold protects nothing, and a new one is taken on the new record.
    unlinkSync(record);
    writeFileSync(record, '{}');
    expect(hold(dir)).toBe('held');
    // A hold in this process keeps prune's claim out, as one in another process does.
    expect(claim(dir).state).toBe('in-use');
  });

  it('lets a claim own an entry no process holds, and only until it is released', () => {
    const dir = tempDir();
    writeFileSync(path.join(dir, CACHE_VERIFIED_RECORD), '{}');
    const first = claim(dir);
    expect(first.state).toBe('owned');
    expect(claim(dir).state).toBe('in-use');
    if (first.state === 'owned') first.release();
    const again = claim(dir);
    expect(again.state).toBe('owned');
    if (again.state === 'owned') again.release();
    expect(claim(path.join(tempDir(), 'absent')).state).toBe('gone');
  });

  it('makes prune keep a superseded build this process holds, and report it in use', async () => {
    const root = tempDir();
    const older = await install(root, '26.8.14.1', '20260901.000000');
    const newer = await install(root, '26.8.15.2', '20261001.000000');
    expect(hold(older)).toBe('held');
    const got = await prune({ cacheDir: root, systemDirs: [] }, { keep: 1 });
    expect(got.map((s) => ({ dir: s.dir, inUse: s.inUse }))).toEqual([{ dir: older, inUse: true }]);
    expect(existsSync(older)).toBe(true);
    expect(existsSync(newer)).toBe(true);
  });
});

const HOLDER = [
  'import fcntl, sys',
  "f = open(sys.argv[1], 'rb')",
  'fcntl.flock(f.fileno(), fcntl.LOCK_SH)',
  "print('held', flush=True)",
  'sys.stdin.read()',
].join('\n');
const python = spawnSync('python3', ['--version']).status === 0;

describe.skipIf(!python)('hold across processes (python3 holds the same flock)', () => {
  it('makes prune keep a build another process holds, and remove it once that process has exited', async () => {
    const root = tempDir();
    const older = await install(root, '26.8.14.1', '20260901.000000');
    const newer = await install(root, '26.8.15.2', '20261001.000000');
    const dry = await prune({ cacheDir: root, systemDirs: [] }, { keep: 1, dryRun: true });
    expect(dry.map((s) => ({ dir: s.dir, inUse: s.inUse }))).toEqual([{ dir: older, inUse: false }]);

    const holder: ChildProcessByStdio<Writable, Readable, null> = spawn('python3', ['-c', HOLDER, path.join(older, CACHE_VERIFIED_RECORD)], {
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    const exited = new Promise<number | null>((resolve) => {
      holder.once('exit', resolve);
    });
    try {
      const said = await new Promise<string>((resolve, reject) => {
        let buf = '';
        holder.stdout.on('data', (chunk: Buffer) => {
          buf += chunk.toString('utf8');
          if (buf.includes('\n')) resolve(buf.trim());
        });
        holder.once('error', reject);
        holder.once('exit', (code) => reject(new Error(`the holder exited ${code} before it held the build`)));
      });
      expect(said).toBe('held');
      const kept = await prune({ cacheDir: root, systemDirs: [] }, { keep: 1 });
      expect(kept.map((s) => ({ dir: s.dir, inUse: s.inUse }))).toEqual([{ dir: older, inUse: true }]);
      expect(existsSync(path.join(older, CACHE_VERIFIED_RECORD))).toBe(true);
    } finally {
      holder.stdin.end();
    }
    expect(await exited).toBe(0);
    const removed = await prune({ cacheDir: root, systemDirs: [] }, { keep: 1 });
    expect(removed.map((s) => ({ dir: s.dir, inUse: s.inUse }))).toEqual([{ dir: older, inUse: false }]);
    expect(existsSync(older)).toBe(false);
    // Nothing is left beside the newer entry: the aside directory went with the build.
    expect(readdirSync(path.join(root, 'unpacked', 'sha256'))).toEqual([path.basename(newer)]);
    expect(await prune({ cacheDir: root, systemDirs: [] }, { keep: 1 })).toEqual([]);
  }, 60_000);
});
