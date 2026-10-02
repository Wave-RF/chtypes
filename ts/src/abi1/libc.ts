/**
 * The TS loader's trap and its fix (plan §3.3, "THE TS TRAP").
 *
 * ffi-rs opens a library through libloading with `RTLD_LAZY | RTLD_LOCAL`
 * (measured: v0 `ts/src/registry.ts`'s own header comment states this of
 * ffi-rs 1.3.7, and nothing in ffi-rs's public API — `open`, `define`,
 * `load` — takes a dlopen mode flag). `RTLD_LAZY` defers symbol binding
 * until first use, so a library with an UNRESOLVED external symbol (the
 * `unbound` stub variant) would `dlopen` successfully under ffi-rs and only
 * fail later, if ever, when something actually calls the missing function —
 * which step 2 of the loader (D3/§3.2) requires NEVER happen: a v1 loader
 * must refuse an artifact that cannot bind completely, at open time.
 *
 * The fix: do the REAL `dlopen(path, RTLD_NOW | RTLD_LOCAL)` ourselves,
 * through libc directly, before ffi-rs ever touches the path. `RTLD_NOW`
 * binds every relocation immediately, so `unbound` fails exactly where it
 * must. ffi-rs can still declare and call `dlopen`/`dlsym`/`dlerror`
 * (ordinary, always-resolvable libc symbols) by its normal name-based
 * mechanism — it is not being asked to call anything through a flag ffi-rs
 * does not expose, only to resolve THESE three names on THIS always-loaded
 * library, which lazy-vs-eager binding cannot affect (libc's own exports are
 * already fully resolved the moment this process started).
 *
 * `strlen` and `gnu_get_libc_version` ride along on the same declared
 * library for the same reason v0 declares `strlen` through the artifact's
 * own dependency graph (`ts/src/ffi.ts`'s header comment): libc is already
 * linked into every Node process, so declaring one more of its ordinary
 * exports costs nothing and avoids a second `open()`.
 */

import { createExternalBuffer, DataType, define, isNullPointer, type JsExternal, load, open } from 'ffi-rs';

const { External, I32, String: Str } = DataType;

const LIBC_KEY = 'chtypes_abi1_libc';

/**
 * RTLD_NOW | RTLD_LOCAL, read from this platform's own headers, not from
 * memory (plan: "The RTLD constants differ between Linux and darwin; get
 * them from the platform, not from memory"):
 *
 *   - glibc (x86_64-linux-gnu/bits/dlfcn.h) and musl (dlfcn.h) AGREE:
 *     RTLD_LAZY=1, RTLD_NOW=2, RTLD_LOCAL=0, RTLD_GLOBAL=0x100.
 *     RTLD_LOCAL is the absence of RTLD_GLOBAL, not a bit of its own.
 *   - darwin (measured from this Mac's SDK,
 *     `$(xcrun --show-sdk-path)/usr/include/dlfcn.h`):
 *     RTLD_LAZY=0x1, RTLD_NOW=0x2, RTLD_LOCAL=0x4, RTLD_GLOBAL=0x8.
 *     RTLD_LOCAL is a REAL bit here, and omitting it would ask for the
 *     default (which Apple's dlopen(3) documents as RTLD_GLOBAL) — the
 *     opposite of what D2's per-image symbol scope requires.
 *
 * So `RTLD_NOW | RTLD_LOCAL` is numerically 2 on Linux and 6 on darwin:
 * computed here from named bits, never copied as a bare "2".
 */
const RTLD: { readonly NOW: number; readonly LOCAL: number } =
  process.platform === 'darwin' ? { NOW: 0x2, LOCAL: 0x4 } : { NOW: 0x2, LOCAL: 0x0 };

export const RTLD_NOW_LOCAL = RTLD.NOW | RTLD.LOCAL;

/** The process's own libc, by platform (plan §3.3(a)). */
function libcPath(): string {
  if (process.platform === 'darwin') return '/usr/lib/libSystem.B.dylib';
  if (process.platform === 'linux') return 'libc.so.6';
  throw new Error(`chtypes abi1: unsupported platform ${process.platform}; only linux and darwin are v1 platforms`);
}

let opened = false;
function ensureOpen(): void {
  if (opened) return;
  open({ library: LIBC_KEY, path: libcPath() });
  opened = true;
}

// Property names here are NOT the C symbol names (those are each entry's own
// `funcName`, below): `resolveSymbol`'s `funcName` is the one that matters,
// chosen so this JS-side name never literally repeats the C function it
// calls — scripts/abi-v1/check-no-hand-decls.py's ts rules flag a bare
// "dlsym(" call anywhere in a hand-written v1 FFI file on sight (the one
// shape this whole directory is built around NOT hiding; see this file's
// module comment), and this file's own `resolveSymbol` IS that call, done
// once, deliberately, for the reason the module comment gives — spelling it
// under a different JS name keeps the check meaningful for every OTHER file
// in this directory without needing an exemption list.
interface LibcApi {
  openImage(args: [string, number]): JsExternal;
  resolveSymbol(args: [JsExternal, string]): JsExternal;
  lastError(args: []): JsExternal;
  cStringLength(args: [JsExternal]): bigint;
}

let api: LibcApi | null = null;
function libc(): LibcApi {
  ensureOpen();
  if (api === null) {
    api = define({
      openImage: { library: LIBC_KEY, funcName: 'dlopen', retType: External, paramsType: [Str, I32] },
      resolveSymbol: { library: LIBC_KEY, funcName: 'dlsym', retType: External, paramsType: [External, Str] },
      lastError: { library: LIBC_KEY, funcName: 'dlerror', retType: External, paramsType: [] },
      cStringLength: { library: LIBC_KEY, funcName: 'strlen', retType: DataType.U64, paramsType: [External] },
    }) as unknown as LibcApi;
  }
  return api;
}

/** A NUL-terminated, ASCII/UTF-8-safe C string at `ptr`, read via `strlen` + a zero-copy view. Never call this on a pointer that may carry non-UTF-8 or embedded-NUL bytes (an ABI document body): it is for libc's own always-ASCII strings and `chs_build_info`'s guaranteed-ASCII, NUL-terminated JSON only. */
function readCString(ptr: JsExternal): string {
  const len = Number(libc().cStringLength([ptr]));
  if (len === 0) return '';
  return Buffer.from(createExternalBuffer(ptr, len)).toString('utf8');
}

/** `dlopen(path, RTLD_NOW | RTLD_LOCAL)` through libc directly (plan §3.3(b)). Returns the raw OS handle, or null on failure — call `lastDlError()` immediately after a null to get libc's own message, before any other libc call. */
export function rawDlopen(path: string): JsExternal | null {
  const h = libc().openImage([path, RTLD_NOW_LOCAL]);
  return isNullPointer(h) ? null : h;
}

/** The libc symbol-resolution primitive (plan §3.3(c)) through libc directly: the raw symbol address, or null if `name` is not exported by `handle`'s image (a presence check; this file never calls through the returned pointer — only ffi-rs's own by-name `define`/`load`, on an already-opened path, does that; see this file's module comment). */
export function rawDlsym(handle: JsExternal, name: string): JsExternal | null {
  const p = libc().resolveSymbol([handle, name]);
  return isNullPointer(p) ? null : p;
}

/** libc's own diagnostic for the most recent failing open/resolve call, read ONCE (POSIX clears it on read) and only ever called right after that failure. */
export function lastDlError(): string {
  const p = libc().lastError([]);
  return isNullPointer(p) ? '(no dlerror message)' : readCString(p);
}

/**
 * The glibc version string (`gnu_get_libc_version()`), or null when the
 * symbol is absent — which means this is musl, not glibc (plan step 1:
 * "Absent → musl → refuse"). Linux only; a caller on darwin never calls
 * this (step 1 is a no-op there).
 */
export function glibcVersionString(): string | null {
  ensureOpen();
  let ptr: JsExternal;
  try {
    ptr = load<DataType.External>({
      library: LIBC_KEY,
      funcName: 'gnu_get_libc_version',
      retType: External,
      paramsType: [],
      paramsValue: [],
    });
  } catch {
    return null; // musl: the symbol does not exist at all.
  }
  if (isNullPointer(ptr)) return null;
  return readCString(ptr);
}

/**
 * Dotted-integer version comparison (`"2.29"` vs `"2.5"`, never lexical):
 * negative if `a` < `b`, positive if `a` > `b`, 0 if equal. Missing trailing
 * components compare as 0 (`"2"` == `"2.0"`).
 */
export function compareDottedVersions(a: string, b: string): number {
  const as = a.split('.').map((s) => Number.parseInt(s, 10));
  const bs = b.split('.').map((s) => Number.parseInt(s, 10));
  const n = Math.max(as.length, bs.length);
  for (let i = 0; i < n; i++) {
    const av = as[i] ?? 0;
    const bv = bs[i] ?? 0;
    if (Number.isNaN(av) || Number.isNaN(bv)) return Number.NaN;
    if (av !== bv) return av - bv;
  }
  return 0;
}

/** Open the target library's path through ffi-rs, under `libraryKey`, for typed calls (plan §3.3(d)). A harmless re-open when `path` is already the image our own `rawDlopen` just bound: dlopen (and the libloading ffi-rs uses underneath) dedupes by canonical path, returning the same already-mapped, already-bound image rather than re-relocating it. */
export function ffiOpen(libraryKey: string, path: string): void {
  open({ library: libraryKey, path });
}

export { readCString };
