/**
 * What can be wrong with a cache root, and what each mode does about it
 * (`docs/guides/fetch-v1.md` §1, the cache faults; public issue #486). The
 * default mode keeps "unreadable is absent" and says so in a warning; strict
 * mode (`strictCache`, `CHTYPES_CACHE_STRICT=1`, `--strict`) makes every
 * fault a `CacheUnusableError` naming the path, and never falls through to a
 * system dir. A write the fetch layer needed that failed is a
 * `CacheUnusableError` in every mode. Go, Python and Rust probe the same way,
 * in the same order, and say the same sentences.
 */

import { lstat, readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { CACHE_UNPACKED_DIR, CACHE_VERIFIED_RECORD, ENV_CACHE_STRICT_NAME } from './constants.gen.js';
import { CacheUnusableError } from './errors.js';
import { decodeRecord, isFilesystemError, zeroXHint, zeroXShape } from './layout.js';

export const REASON_UNREADABLE_ROOT = 'unreadable_root';
export const REASON_NOT_A_DIRECTORY = 'not_a_directory';
export const REASON_UNREADABLE_ENTRY = 'unreadable_entry';
export const REASON_UNACCEPTABLE_RECORD = 'unacceptable_record';
export const REASON_LAYOUT_0X = 'layout_0x';
export const REASON_UNWRITABLE = 'unwritable';

const ENTRY_NAME = /^[0-9a-f]{64}$/;

interface Fault {
  readonly path: string;
  readonly reason: string;
  readonly osError: string;
}

/** Whether strict mode is on: the option, else `CHTYPES_CACHE_STRICT=1`. */
export function strictMode(option: boolean | undefined): boolean {
  return option ?? process.env[ENV_CACHE_STRICT_NAME] === '1';
}

/** The `CHTYPES_CACHE_UNUSABLE` for one fault: the same sentence in every binding, `<path> is unusable as a cache: <reason> (<errno>)`. */
export async function cacheError(faultPath: string, reason: string, osError: string, cause?: unknown): Promise<CacheUnusableError> {
  let detail = osError === '' ? reason : `${reason} (${osError})`;
  if (reason === REASON_LAYOUT_0X) {
    const hint = await zeroXHint(faultPath);
    if (hint !== undefined) detail += `. ${hint}`;
  }
  return new CacheUnusableError(
    `chtypes: ${faultPath} is unusable as a cache: ${detail}`,
    { path: faultPath, reason, osError: osError === '' ? undefined : osError },
    cause === undefined ? undefined : { cause },
  );
}

function warns(f: Fault): boolean {
  return f.reason === REASON_UNREADABLE_ROOT || f.reason === REASON_NOT_A_DIRECTORY || f.reason === REASON_UNREADABLE_ENTRY;
}

function warning(f: Fault): string {
  return `${f.path} could not be read (${f.osError}); treated as not installed. Set ${ENV_CACHE_STRICT_NAME}=1 to make this an error.`;
}

function codeOf(err: unknown): string {
  return isFilesystemError(err) && err.code !== undefined ? err.code : '';
}

async function denied(p: string): Promise<boolean> {
  try {
    await stat(p);
    return false;
  } catch (err) {
    return codeOf(err) === 'EACCES' || codeOf(err) === 'EPERM';
  }
}

/** What is wrong with one root, in a fixed order: the root itself, then the 0.x shape (the cache only), then each entry by name. A root that does not exist is the empty cache, not a fault. */
async function probeRoot(root: string, isCache: boolean): Promise<readonly Fault[]> {
  try {
    const st = await stat(root);
    if (!st.isDirectory()) return [{ path: root, reason: REASON_NOT_A_DIRECTORY, osError: 'ENOTDIR' }];
  } catch (err) {
    const code = codeOf(err);
    if (code === 'ENOENT') return [];
    return [{ path: root, reason: code === 'ENOTDIR' ? REASON_NOT_A_DIRECTORY : REASON_UNREADABLE_ROOT, osError: code }];
  }
  if (isCache && (await zeroXShape(root)) !== undefined) return [{ path: root, reason: REASON_LAYOUT_0X, osError: '' }];
  const unpacked = path.join(root, 'unpacked');
  const sha = path.join(root, CACHE_UNPACKED_DIR);
  let names: string[];
  try {
    names = (await readdir(sha, { withFileTypes: true }))
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .sort();
  } catch (err) {
    const code = codeOf(err);
    if (code === 'ENOENT') return [];
    if (code === 'ENOTDIR') {
      let where = sha;
      try {
        if (!(await stat(unpacked)).isDirectory()) where = unpacked;
      } catch {
        // unpacked/ itself is not there to blame
      }
      return [{ path: where, reason: REASON_NOT_A_DIRECTORY, osError: 'ENOTDIR' }];
    }
    // The path that blocks the listing: the first one that cannot be passed through, else unpacked/sha256 itself.
    const where = (await denied(unpacked)) ? root : (await denied(sha)) ? unpacked : sha;
    return [{ path: where, reason: REASON_UNREADABLE_ROOT, osError: code }];
  }
  const faults: Fault[] = [];
  for (const name of names) {
    if (!ENTRY_NAME.test(name)) continue;
    const entry = path.join(sha, name);
    const record = path.join(entry, CACHE_VERIFIED_RECORD);
    let raw: string;
    try {
      raw = await readFile(record, 'utf8');
    } catch (err) {
      const code = codeOf(err);
      if (code === 'ENOENT') continue; // no record yet: a pre-seed or an unfinished install
      faults.push({ path: (await denied(record)) ? entry : record, reason: REASON_UNREADABLE_ENTRY, osError: code });
      continue;
    }
    if (isCache && decodeRecord(raw) === undefined) {
      try {
        await lstat(path.join(root, 'blobs', 'sha256', name));
      } catch {
        faults.push({ path: record, reason: REASON_UNACCEPTABLE_RECORD, osError: '' });
      }
    }
  }
  return faults;
}

/**
 * Checks the cache (`roots[0]`), then every system dir. In strict mode the
 * first fault is thrown as its `CacheUnusableError`, the cache's before any
 * system dir's, so nothing falls through. In the default mode one warning per
 * unusable root or unreadable entry is returned. A system dir that does not
 * exist is skipped in both, as a default list.
 */
export async function probeRoots(roots: readonly string[], strict: boolean): Promise<readonly string[]> {
  const warnings: string[] = [];
  for (const [i, root] of roots.entries()) {
    for (const f of await probeRoot(root, i === 0)) {
      if (strict) {
        if (i > 0 && !warns(f)) continue;
        throw await cacheError(f.path, f.reason, f.osError);
      }
      if (warns(f)) warnings.push(warning(f));
    }
  }
  return warnings;
}

/**
 * A filesystem failure of a write the fetch layer needed, under the cache, as
 * a `CacheUnusableError` with reason `unwritable` in every mode, never a raw
 * Node error. Anything else (an error naming a path outside the cache, such as
 * the lock file or a `file://` base, or not a filesystem error) is returned as
 * it came.
 */
export async function unwritable(err: unknown, cacheRoot: string): Promise<unknown> {
  if (!isFilesystemError(err)) return err;
  const e = err as NodeJS.ErrnoException & { dest?: string };
  for (const p of [e.dest, e.path]) {
    if (p === undefined) continue;
    const abs = path.resolve(p);
    if (abs === cacheRoot || abs.startsWith(cacheRoot + path.sep)) return cacheError(abs, REASON_UNWRITABLE, codeOf(err), err);
  }
  return err;
}
