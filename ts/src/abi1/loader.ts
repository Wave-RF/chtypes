/**
 * The ABI v1 loader (plan §3, steps 1-7). Hand-written (the plan's "about 200
 * lines per binding" FIRM piece); everything it reaches for — `rawDlopen`/
 * `rawDlsym`/`glibcVersionString` (`./libc.ts`), `defineRawFunctions`/
 * `rawCall` (`./raw.ts`), `DESCRIBED_SYMBOLS`/`SYMBOL`/`ABI_FINGERPRINT`/
 * `CROSS_CHECK` (`./decls.gen.ts`) — is generated or generic, so this file
 * never spells a `chs_` name itself.
 *
 * STEPS, IN ORDER (plan §3.2; `spec/abi-v1/sdk.json`'s `loader.refusals`
 * names each reason):
 *
 *   1. glibc (Linux only; darwin skips it). BEFORE dlopen.
 *   2. `dlopen(path, RTLD_NOW | RTLD_LOCAL)`, through `./libc.ts` (plan
 *      §3.3(b): the real fix for ffi-rs's own `RTLD_LAZY` open).
 *   3. `chs_abi_version`: present (`dlsym`, plan §3.3(c)) and, once present,
 *      `=== 1` (a real call — only now safe through ffi-rs's own by-name
 *      `open`/`define`, plan §3.3(d), since step 2 already proved the image
 *      binds completely under `RTLD_NOW`).
 *   4. `chs_build_info`: present, parses (ASCII, no duplicate keys at any
 *      nesting depth, `schema === 1`), and its `abi_fingerprint` matches
 *      this binding's own compiled-in `ABI_FINGERPRINT` byte for byte.
 *   5. the nine `CROSS_CHECK` fields (`spec/abi-v1/sdk.json`) against
 *      `LoadInput.predicate`.
 *   6. every OTHER described symbol, present (`dlsym` only — no call).
 *   7. a no-op hook (`onLoaded`); A5 is unsettled, so nothing lives here yet.
 *
 * No `dlclose`, ever (plan: "v0 measured a segfault on reopen"). Images are
 * deduplicated by realpath + `dev:ino` (plan §3.2), re-implemented here
 * rather than imported from any v0 file (plan §3.4: this directory touches
 * no v0 file).
 *
 * `openUnverified` (plan §3.1) is the one path that skips steps 1 and 5: for
 * core's own local builds and the (not-yet-built) linked mode, gated on BOTH
 * an explicit flag and `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`, never the
 * default.
 */

import { realpathSync, statSync } from 'node:fs';
import type { JsExternal } from 'ffi-rs';
import { ABI_FINGERPRINT, CROSS_CHECK, DESCRIBED_SYMBOLS, SYMBOL } from './decls.gen.js';
import { ArtifactCorruptError, ArtifactIncompatibleError } from './errors.js';
import { compareDottedVersions, ffiOpen, glibcVersionString, lastDlError, rawDlopen, rawDlsym } from './libc.js';
import { defineRawFunctions, type RawApi, rawCall } from './raw.js';

/** The predicate a loaded artifact is checked against: the verified signed statement's own fields, passed through verbatim (plan §3.1 — "predicate is passed verbatim, as the seam promises"). Only the fields this loader reads are typed; a real predicate carries more. */
export interface Predicate {
  readonly schema: number;
  readonly abi: number;
  readonly abi_fingerprint: string;
  readonly clickhouse_version: string;
  readonly channel: string;
  readonly build: string;
  readonly os: string;
  readonly arch: string;
  readonly core_commit: string;
  readonly inputs_sha256: string;
  readonly glibc_floor?: string;
}

/** The loader's own input, decoupled from the fetch layer's `Resolved` type (plan §3.1): a three-line adapter joins them in wave C. */
export interface LoadInput {
  readonly libraryPath: string;
  readonly predicate: Predicate;
  readonly platform: string;
  /** The image zone `chs_initialize` is given at step 7 (an IANA name); absent or empty means UTC. The public setup API arrives in wave C; until then only a test supplies one. */
  readonly timezone?: string;
}

/** One opened, verified ABI v1 image. */
export class Library {
  readonly path: string;
  readonly raw: RawApi;
  readonly buildInfo: Readonly<Record<string, unknown>>;
  readonly version: string;

  constructor(path: string, raw: RawApi, buildInfo: Readonly<Record<string, unknown>>, version: string) {
    this.path = path;
    this.raw = raw;
    this.buildInfo = buildInfo;
    this.version = version;
  }
}

function refuseIncompatible(path: string, reason: string, want?: string, got?: string): never {
  throw new ArtifactIncompatibleError({ reason, path, want, got });
}

function refuseCorrupt(path: string, reason: string, want?: string, got?: string): never {
  throw new ArtifactCorruptError({ reason, path, want, got });
}

// --------------------------------------------------------------- image cache

const loadedByKey = new Map<string, Library>();
const keyBySpelling = new Map<string, string>();

/** The key `dlopen` will dedupe this path to (realpath + dev:ino, or an already-known spelling's key), per plan §3.2. */
function resolveImageKey(path: string): string {
  let key: string;
  let resolved: string;
  try {
    const st = statSync(path, { bigint: true });
    key = `${st.dev}:${st.ino}`;
    resolved = realpathSync(path);
  } catch (err) {
    throw new ArtifactIncompatibleError({
      reason: 'dlopen',
      path,
      got: `cannot stat this path: ${err instanceof Error ? err.message : String(err)}`,
    });
  }
  const known = keyBySpelling.get(path) ?? keyBySpelling.get(resolved);
  const realKey = known ?? key;
  keyBySpelling.set(path, realKey);
  keyBySpelling.set(resolved, realKey);
  return realKey;
}

// ------------------------------------------------------------------- step 1

function checkGlibc(path: string, predicate: Predicate, platform: string): void {
  if (!platform.startsWith('linux')) return; // darwin: skip (plan §3.2 step 1)
  const actual = glibcVersionString();
  if (actual === null) {
    refuseIncompatible(path, 'no_glibc', predicate.glibc_floor, '(musl, or gnu_get_libc_version did not resolve)');
  }
  const floor = predicate.glibc_floor;
  if (floor === undefined) {
    refuseIncompatible(path, 'predicate_malformed', 'a glibc_floor field', '(absent)');
  }
  if (compareDottedVersions(actual, floor) < 0) {
    refuseIncompatible(path, 'glibc_floor', floor, actual);
  }
}

// ------------------------------------------------------------------- step 2

function openImage(path: string): JsExternal {
  const handle = rawDlopen(path);
  if (handle === null) refuseIncompatible(path, 'dlopen', undefined, lastDlError());
  return handle;
}

// ------------------------------------------------------------------- step 4

/**
 * `chs_build_info()`'s text, parsed: ASCII-only, no duplicate keys at ANY
 * nesting depth (a hand-rolled scan — `JSON.parse` has already silently
 * folded a duplicate key to its last value by the time any reviver could
 * see it, so this runs over the raw text first), `schema === 1`.
 */
function isAsciiOnly(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    if ((text.charCodeAt(i) as number) > 0x7f) return false;
  }
  return true;
}

function parseBuildInfo(path: string, text: string): Readonly<Record<string, unknown>> {
  if (!isAsciiOnly(text)) {
    refuseCorrupt(path, 'build_info_malformed', 'ASCII only', 'a non-ASCII byte');
  }
  const dup = firstDuplicateKey(text);
  if (dup !== null) {
    refuseCorrupt(path, 'build_info_malformed', 'no duplicate keys', `"${dup}" appears twice in one object`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    refuseCorrupt(path, 'build_info_malformed', 'valid JSON', 'a parse error');
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    refuseCorrupt(path, 'build_info_malformed', 'a JSON object', typeof parsed);
  }
  const obj = parsed as Record<string, unknown>;
  if (obj.schema !== 1) {
    refuseCorrupt(path, 'build_info_malformed', 'schema === 1', JSON.stringify(obj.schema));
  }
  return obj;
}

/** The first key repeated within the SAME enclosing `{...}` object in `text` (any nesting depth), or null. A minimal JSON scanner: tracks string literals (with escapes) and `{`/`}` nesting; a quoted token immediately followed by `:` is unambiguously an object key in valid JSON, in both array and object contexts. */
function firstDuplicateKey(text: string): string | null {
  const stack: Set<string>[] = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const c = text[i];
    if (c === '"') {
      const start = i + 1;
      let j = start;
      while (j < n) {
        if (text[j] === '\\') {
          j += 2;
          continue;
        }
        if (text[j] === '"') break;
        j++;
      }
      const raw = text.slice(start, j);
      let k = j + 1;
      while (k < n && /\s/.test(text[k] as string)) k++;
      if (text[k] === ':' && stack.length > 0) {
        const top = stack[stack.length - 1] as Set<string>;
        if (top.has(raw)) return raw;
        top.add(raw);
      }
      i = j + 1;
      continue;
    }
    if (c === '{') {
      stack.push(new Set());
      i++;
      continue;
    }
    if (c === '}') {
      stack.pop();
      i++;
      continue;
    }
    i++;
  }
  return null;
}

// ------------------------------------------------------------------- step 5

function crossCheckField(got: unknown, want: unknown, compare: 'bytes' | 'int'): boolean {
  if (compare === 'int') return Number(got) === Number(want);
  return String(got) === String(want);
}

function checkCrossFields(path: string, buildInfo: Readonly<Record<string, unknown>>, predicate: Predicate): void {
  const pred = predicate as unknown as Record<string, unknown>;
  for (const field of CROSS_CHECK) {
    const got = buildInfo[field.buildInfo];
    const want = pred[field.predicate];
    if (!crossCheckField(got, want, field.compare)) {
      refuseCorrupt(path, `build_info_mismatch:${field.buildInfo}`, JSON.stringify(want), JSON.stringify(got));
    }
  }
}

// ---------------------------------------------------------------- the whole

/** Load, verify and open one ABI v1 artifact (plan §3.2, steps 1-7). Returns the SAME `Library` object for a path, a hardlink, a symlink or a second spelling of an already-loaded image (plan: "S3 runs once per image"). */
export function openAbi1(input: LoadInput): Library {
  const path = input.libraryPath;
  const key = resolveImageKey(path);
  const cached = loadedByKey.get(key);
  if (cached !== undefined) return cached;

  checkGlibc(path, input.predicate, input.platform); // step 1

  const osHandle = openImage(path); // step 2

  // step 3: chs_abi_version present, then (through ffi-rs, now safe) === 1.
  if (rawDlsym(osHandle, SYMBOL.ABI_VERSION) === null) {
    refuseIncompatible(path, 'not_v1', '1', '(chs_abi_version is not exported)');
  }
  ffiOpen(key, path); // plan §3.3(d): only now, after RTLD_NOW already bound the image.
  const raw = defineRawFunctions(key);
  const abiVersionCall = rawCall(raw, SYMBOL.ABI_VERSION, []);
  const abiVersion = abiVersionCall.outcome === 'value' ? Number(abiVersionCall.value) : Number.NaN;
  if (abiVersion !== 1) refuseIncompatible(path, 'abi_version', '1', String(abiVersion));

  // step 4: chs_build_info present, parses, fingerprint matches.
  if (rawDlsym(osHandle, SYMBOL.BUILD_INFO) === null) {
    refuseIncompatible(path, `missing_symbol:${SYMBOL.BUILD_INFO}`, undefined, '(chs_build_info is not exported)');
  }
  const buildInfoCall = rawCall(raw, SYMBOL.BUILD_INFO, []);
  const buildInfoText = buildInfoCall.outcome === 'value' ? (buildInfoCall.value as string | null) : null;
  if (buildInfoText === null) refuseCorrupt(path, 'build_info_malformed', 'a JSON object', 'null');
  const buildInfo = parseBuildInfo(path, buildInfoText);
  if (buildInfo.abi_fingerprint !== ABI_FINGERPRINT) {
    refuseIncompatible(path, 'fingerprint', ABI_FINGERPRINT, String(buildInfo.abi_fingerprint));
  }

  // step 5: the nine cross-check fields.
  checkCrossFields(path, buildInfo, input.predicate);

  // step 6: every other described symbol, present.
  for (const sym of DESCRIBED_SYMBOLS) {
    if (sym === SYMBOL.ABI_VERSION || sym === SYMBOL.BUILD_INFO) continue; // already proved present above
    if (rawDlsym(osHandle, sym) === null) {
      refuseIncompatible(path, `missing_symbol:${sym}`, undefined, `(${sym} is not exported)`);
    }
  }

  // step 7: chs_initialize(timezone), once per image (a process-once call).
  onLoaded(path, raw, input.timezone ?? '');

  const library = new Library(path, raw, buildInfo, String(buildInfo.clickhouse_version ?? ''));
  loadedByKey.set(key, library);
  return library;
}

function onLoaded(path: string, raw: RawApi, timezone: string): void {
  // `chs_initialize` is process_once: a repeat with the same spelling is OK, a
  // different spelling is CHS_INVALID_ARGUMENT. A zero-length zone means UTC.
  const call = rawCall(raw, SYMBOL.INITIALIZE, [Buffer.from(timezone, 'utf8')]);
  if (call.outcome !== 'status' || call.statusName !== 'CHS_OK') {
    const got = call.outcome === 'status' ? call.statusName : call.outcome;
    refuseIncompatible(path, 'initialize', 'CHS_OK', got);
  }
}

// ----------------------------------------------------------------- unverified

const UNVERIFIED_ENV = 'CHTYPES_ALLOW_UNVERIFIED_LIBRARY';

/**
 * Load `path` with NO predicate: skips step 1 (nothing to check a floor
 * against) and step 5 (nothing to cross-check), still runs 2/3/4/6 (plan
 * §3.1). Refuses unless BOTH `explicit` is true AND
 * `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set; warns loudly, once per path,
 * when it proceeds. Never the default path — core's own local builds and
 * the linked mode are its only callers.
 */
export function openUnverified(path: string, options: { readonly explicit: boolean }): Library {
  if (!options.explicit || process.env[UNVERIFIED_ENV] !== '1') {
    throw new ArtifactIncompatibleError({
      reason: 'dlopen',
      path,
      got: `openUnverified requires BOTH the explicit flag and ${UNVERIFIED_ENV}=1`,
    });
  }
  warnUnverifiedOnce(path);

  const key = resolveImageKey(path);
  const cached = loadedByKey.get(key);
  if (cached !== undefined) return cached;

  const osHandle = openImage(path); // step 2
  if (rawDlsym(osHandle, SYMBOL.ABI_VERSION) === null) {
    refuseIncompatible(path, 'not_v1', '1', '(chs_abi_version is not exported)');
  }
  ffiOpen(key, path);
  const raw = defineRawFunctions(key);
  const abiVersionCall = rawCall(raw, SYMBOL.ABI_VERSION, []);
  const abiVersion = abiVersionCall.outcome === 'value' ? Number(abiVersionCall.value) : Number.NaN;
  if (abiVersion !== 1) refuseIncompatible(path, 'abi_version', '1', String(abiVersion));

  if (rawDlsym(osHandle, SYMBOL.BUILD_INFO) === null) {
    refuseIncompatible(path, `missing_symbol:${SYMBOL.BUILD_INFO}`, undefined, '(chs_build_info is not exported)');
  }
  const buildInfoCall = rawCall(raw, SYMBOL.BUILD_INFO, []);
  const buildInfoText = buildInfoCall.outcome === 'value' ? (buildInfoCall.value as string | null) : null;
  const buildInfo = buildInfoText === null ? {} : parseBuildInfo(path, buildInfoText);

  for (const sym of DESCRIBED_SYMBOLS) {
    if (sym === SYMBOL.ABI_VERSION || sym === SYMBOL.BUILD_INFO) continue;
    if (rawDlsym(osHandle, sym) === null) {
      refuseIncompatible(path, `missing_symbol:${sym}`, undefined, `(${sym} is not exported)`);
    }
  }

  onLoaded(path, raw, '');
  const library = new Library(path, raw, buildInfo, String(buildInfo.clickhouse_version ?? ''));
  loadedByKey.set(key, library);
  return library;
}

const warnedPaths = new Set<string>();
function warnUnverifiedOnce(path: string): void {
  if (warnedPaths.has(path)) return;
  warnedPaths.add(path);
  console.warn(
    `chtypes: ${path} was opened UNVERIFIED (${UNVERIFIED_ENV}=1): no predicate was checked. ` +
      'Never use this path for an artifact that came from anywhere but a local, trusted build.',
  );
}
