/**
 * The in-use signal (`docs/guides/fetch-v1.md` §1, "In use"; public issue
 * #494). A process that is about to load an installed build, or that a
 * registry's fetch handed one to, takes a SHARED advisory lock (flock(2)) on
 * the entry's `verified.json` and keeps it until the process exits.
 * `chtypes prune` removes an entry only after it has taken the EXCLUSIVE lock
 * without waiting, so it never removes a build a live process holds. The
 * kernel drops a dead process's locks, so a crash never pins a build. flock
 * belongs to the open file description on Linux and darwin, so a hold taken by
 * this very process counts too, and so does one taken by any binding: all four
 * lock the same file the same way. Never fcntl or lockf, whose locks belong to
 * the process and would not be seen by a prune in it.
 *
 * Node has no file lock of its own: `flock` is the generated libc declaration
 * (`../abi2/libc.gen.ts`), and the file descriptors are Node's own, from
 * `openSync`, which are the operating system's.
 *
 * Everything here is synchronous on purpose. A prune holds an entry's
 * exclusive lock only across one synchronous rename, so no other code of this
 * process can run, and wait on the lock, while it is held.
 *
 * Only the registry holds (the public API): the fetch layer's own seam
 * functions never do, so the CLI and the conformance runner accumulate no
 * holds.
 */

import { closeSync, fstatSync, openSync, statSync } from 'node:fs';
import { constants as osConstants } from 'node:os';
import path from 'node:path';
import { flock } from '../abi2/libc.gen.js';
import { CACHE_VERIFIED_RECORD } from './constants.gen.js';
import { ArtifactMissingError } from './errors.js';

/** flock(2)'s operations, the same on Linux and darwin. */
const LOCK_SH = 1;
const LOCK_EX = 2;
const LOCK_NB = 4;

/**
 * What `hold` found:
 *
 *   - `held`: this process holds the entry, shared, for its life;
 *   - `unheld`: the record cannot be locked here (a filesystem without flock,
 *     or an error other than the entry being gone). The caller goes on: a
 *     prune on the same filesystem cannot take its exclusive lock either, so
 *     it keeps the entry;
 *   - `vanished`: the entry is gone, or another entry has replaced it: a prune
 *     removed it after the lookup chose it. The caller looks again.
 */
export type HoldState = 'held' | 'unheld' | 'vanished';

/**
 * This process's shared locks: one open record per entry directory, kept until
 * the process exits. Never closed, except an old one whose entry was removed
 * and installed again, which protects nothing.
 */
const holds = new Map<string, number>();

/**
 * Takes this process's shared hold on the installed entry `dir`
 * (`<root>/unpacked/sha256/<manifest hex>`) and keeps it for the life of the
 * process. It waits only while a prune holds the entry exclusively, which a
 * prune does across one rename; then the entry is gone, and `hold` says so.
 */
export function hold(dir: string): HoldState {
  const record = path.join(dir, CACHE_VERIFIED_RECORD);
  const held = holds.get(dir);
  if (held !== undefined) {
    if (sameFile(held, record)) return 'held';
    // The entry this process held was removed and installed again: the old
    // hold protects nothing now.
    closeQuietly(held);
    holds.delete(dir);
  }
  let fd: number;
  try {
    fd = openSync(record, 'r');
  } catch (err) {
    return gone(err) ? 'vanished' : 'unheld';
  }
  if (!lockShared(fd)) {
    closeQuietly(fd);
    return 'unheld';
  }
  if (!sameFile(fd, record)) {
    closeQuietly(fd);
    return 'vanished';
  }
  holds.set(dir, fd);
  return 'held';
}

/** What `claim` found: the exclusive lock, with its release; or the entry in use (or not lockable at all), kept; or the entry gone or replaced since it was listed, not the caller's to report. */
export type Claim = { readonly state: 'owned'; readonly release: () => void } | { readonly state: 'in-use' } | { readonly state: 'gone' };

/**
 * Takes the exclusive lock a prune needs on the entry `dir`, without waiting.
 * It is owned only when no process holds the entry and the record it locked is
 * still the one at the path; `release` drops the lock.
 */
export function claim(dir: string): Claim {
  const record = path.join(dir, CACHE_VERIFIED_RECORD);
  let fd: number;
  try {
    fd = openSync(record, 'r');
  } catch (err) {
    return gone(err) ? { state: 'gone' } : { state: 'in-use' };
  }
  if (lock(fd, LOCK_EX | LOCK_NB) !== 0) {
    closeQuietly(fd);
    return { state: 'in-use' };
  }
  if (!sameFile(fd, record)) {
    closeQuietly(fd);
    return { state: 'gone' };
  }
  return { state: 'owned', release: () => closeQuietly(fd) };
}

/**
 * `CHTYPES_ARTIFACT_MISSING` for a request whose build a concurrent prune
 * removed twice, each time between its install and this process's hold.
 */
export function removedWhileHeld(request: string, dir: string): ArtifactMissingError {
  return new ArtifactMissingError(
    `chtypes: ${dir} was removed by a concurrent prune before this process could hold it; ask for ${request} again`,
  );
}

/** flock(fd, LOCK_SH), waiting while an exclusive lock is held, and retrying a wait a signal interrupted. */
function lockShared(fd: number): boolean {
  for (;;) {
    const errno = lock(fd, LOCK_SH);
    if (errno === 0) return true;
    if (errno !== osConstants.errno.EINTR) return false;
  }
}

/** flock(fd, operation): 0, else the errno it failed with; -1 when the call itself could not be made. */
function lock(fd: number, operation: number): number {
  try {
    return flock(fd, operation);
  } catch {
    return -1;
  }
}

/** Whether the open file `fd` is still the file at `file`: the same device and inode. */
function sameFile(fd: number, file: string): boolean {
  try {
    const held = fstatSync(fd, { bigint: true });
    const now = statSync(file, { bigint: true });
    return held.dev === now.dev && held.ino === now.ino;
  } catch {
    return false;
  }
}

function gone(err: unknown): boolean {
  const code = (err as NodeJS.ErrnoException).code;
  return code === 'ENOENT' || code === 'ENOTDIR';
}

function closeQuietly(fd: number): void {
  try {
    closeSync(fd);
  } catch {
    // Already closed: nothing is held through it.
  }
}
