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
 * must.
 *
 * This file is HAND-WRITTEN and declares nothing itself: the actual libc
 * declarations and the symbol-lookup primitive live in the GENERATED
 * `./libc.gen.ts` (scripts/abi-v1/emit/ts.py — see its module docstring).
 * That split is the lead's 2026-10-02 ruling: check-no-hand-decls.py's
 * `dlsym(` rule exists so every raw symbol lookup lives in generated code,
 * exempt by construction, rather than being worked around in a hand file
 * with a renamed table key or a call site shaped to dodge the text match.
 * This file calls the generated exports only, by their own names.
 */

import { createExternalBuffer, type JsExternal, open } from 'ffi-rs';
import { cStringLength, dlerror, dlopen, gnuGetLibcVersion, resolveSymbol } from './libc.gen.js';

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

/** A NUL-terminated, ASCII/UTF-8-safe C string at `ptr`, read via the generated `cStringLength` (strlen) + a zero-copy view. Never call this on a pointer that may carry non-UTF-8 or embedded-NUL bytes (an ABI document body): it is for libc's own always-ASCII strings and `chs_build_info`'s guaranteed-ASCII, NUL-terminated JSON only. */
function readCString(ptr: JsExternal): string {
  const len = cStringLength(ptr);
  if (len === 0) return '';
  return Buffer.from(createExternalBuffer(ptr, len)).toString('utf8');
}

/** `dlopen(path, RTLD_NOW | RTLD_LOCAL)` through the generated primitive (plan §3.3(b)). Returns the raw OS handle, or null on failure — call `lastDlError()` immediately after a null to get libc's own message, before any other libc call. */
export function rawDlopen(path: string): JsExternal | null {
  return dlopen(path, RTLD_NOW_LOCAL);
}

/** The loader's symbol-presence check (plan §3.3(c)), through the generated `resolveSymbol` helper: the raw symbol address, or null if `name` is not exported by `handle`'s image (this file never calls through the returned pointer — only ffi-rs's own by-name `define`/`load`, on an already-opened path, does that). */
export function rawDlsym(handle: JsExternal, name: string): JsExternal | null {
  return resolveSymbol(handle, name);
}

/** libc's own diagnostic for the most recent failing open/resolve call, read ONCE (POSIX clears it on read) and only ever called right after that failure. */
export function lastDlError(): string {
  const p = dlerror();
  return p === null ? '(no dlerror message)' : readCString(p);
}

/**
 * The glibc version string (`gnu_get_libc_version()`), or null when the
 * symbol is absent — which means this is musl, not glibc (plan step 1:
 * "Absent → musl → refuse"). Linux only; a caller on darwin never calls
 * this (step 1 is a no-op there).
 */
export function glibcVersionString(): string | null {
  const ptr = gnuGetLibcVersion();
  return ptr === null ? null : readCString(ptr);
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

/** Open the target library's path through ffi-rs, under `libraryKey`, for typed calls (plan §3.3(d)). A harmless re-open when `path` is already the image our own `rawDlopen` just bound: dlopen (and the libloading ffi-rs uses underneath) dedupes by canonical path, returning the same already-mapped, already-bound image rather than re-relocating it. Declares no symbol of its own — `open()` only registers a path under a key for a later `define()` — so it is unaffected by the generated/hand-written split above. */
export function ffiOpen(libraryKey: string, path: string): void {
  // Once per key: a load that failed after this point (a cross-check mismatch, a missing symbol) and is retried reaches here again for the same image.
  if (ffiOpened.has(libraryKey)) return;
  open({ library: libraryKey, path });
  ffiOpened.add(libraryKey);
}

const ffiOpened = new Set<string>();

export { readCString };
