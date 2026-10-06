/**
 * The ABI v2 loader (plan §3, steps 1-7; the 2.0.0-dev binding, public issue #511). Hand-written (the plan's "about 200
 * lines per binding" FIRM piece); everything it reaches for — `rawDlopen`/
 * `rawDlsym`/`glibcVersionString` (`./libc.ts`), `defineRawFunctions`/
 * `rawCall` (`./raw.ts`), `DESCRIBED_SYMBOLS`/`SYMBOL`/`ABI_FINGERPRINT`/
 * `CROSS_CHECK` (`./decls.gen.ts`) — is generated or generic, so this file
 * never spells a `chs_` name itself.
 *
 * STEPS, IN ORDER (plan §3.2; `spec/abi-v2/sdk.json`'s `loader.refusals`
 * names each reason):
 *
 *   1. glibc (Linux only; darwin skips it). BEFORE dlopen.
 *   2. `dlopen(path, RTLD_NOW | RTLD_LOCAL)`, through `./libc.ts` (plan
 *      §3.3(b): the real fix for ffi-rs's own `RTLD_LAZY` open).
 *   3. `chs_abi_version`: present (`dlsym`, plan §3.3(c)) and, once present,
 *      `=== ABI_VERSION` (2; a real call — only now safe through ffi-rs's own by-name
 *      `open`/`define`, plan §3.3(d), since step 2 already proved the image
 *      binds completely under `RTLD_NOW`).
 *   4. `chs_build_info`: present, parses (ASCII, no duplicate keys at any
 *      nesting depth, `schema === 1`), and its `abi_fingerprint` matches
 *      this binding's own compiled-in `ABI_FINGERPRINT` byte for byte. While
 *      the description is unstable (`ABI_STABILITY`) the refusal carries rule
 *      r6's exact dev message (`./errors.ts`, `devFingerprintMessage`).
 *   5. the nine `CROSS_CHECK` fields (`spec/abi-v2/sdk.json`) against
 *      `LoadInput.predicate`.
 *   6. every OTHER described symbol, present (`dlsym` only — no call).
 *   7. `chs_initialize(zone)`, then `chs_set_defaults(defaults)` when there are
 *      any, once per image, from the process setup the caller passes in
 *      (`LoadInput.timezone` / `defaults`). A failure here is the CALL's own
 *      error, mapped by the status table — never a loader refusal reason.
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
import { type BuildInfo, decodeBuildInfo } from './buildinfo.js';
import { Calls } from './calls.gen.js';
import { ABI_FINGERPRINT, ABI_VERSION, CROSS_CHECK, DESCRIBED_SYMBOLS, SYMBOL } from './decls.gen.js';
import { ArtifactIncompatibleError, LoaderCorruptError, UsageError, usageError } from './errors.js';
import { compareDottedVersions, ffiOpen, glibcVersionString, lastDlError, rawDlopen, rawDlsym } from './libc.js';
import { defineRawFunctions, type RawApi, rawCall } from './raw.js';

/** The predicate a loaded artifact is checked against: the verified signed statement's own fields, passed through verbatim (plan §3.1 — "predicate is passed verbatim, as the seam promises"). Only the fields this loader reads are typed; a real predicate carries more, and the fetch layer's own `ArtifactPredicate` satisfies this shape as it is. */
export interface Predicate {
  readonly abi: number;
  readonly abi_fingerprint: string;
  readonly clickhouse_version: string;
  readonly channel: string;
  readonly build: string;
  readonly os: string;
  readonly arch: string;
  readonly core_commit: string;
  readonly inputs_sha256: string;
  readonly glibc_floor?: string | undefined;
}

/** The process setup step 7 applies to every image: the image zone and the default settings. */
export interface ImageSetup {
  /** The image zone `chs_initialize` is given (an IANA name); absent or empty means UTC. */
  readonly timezone?: string;
  /** Seeded once with `chs_set_defaults` when there are any. */
  readonly defaults?: Readonly<Record<string, string>>;
}

/** The loader's own input, decoupled from the fetch layer's `Resolved` type: the adapter in `../registry.ts` joins them and passes the predicate verbatim. */
export interface LoadInput extends ImageSetup {
  readonly libraryPath: string;
  readonly predicate: Predicate;
  readonly platform: string;
}

/** One opened, verified ABI v2 image: the resolved raw table, its typed call wrappers, and the decoded build info. */
export class LoadedImage {
  readonly path: string;
  readonly raw: RawApi;
  readonly calls: Calls;
  readonly buildInfo: BuildInfo;

  constructor(path: string, raw: RawApi, buildInfo: BuildInfo) {
    this.path = path;
    this.raw = raw;
    this.calls = new Calls(raw);
    this.buildInfo = buildInfo;
  }

  get version(): string {
    return this.buildInfo.clickhouseVersion;
  }
}

function refuseIncompatible(path: string, reason: string, want?: string, got?: string): never {
  throw new ArtifactIncompatibleError({ reason, path, want, got });
}

function refuseCorrupt(path: string, reason: string, want?: string, got?: string): never {
  throw new LoaderCorruptError({ reason, path, want, got });
}

// --------------------------------------------------------------- image cache

const loadedByKey = new Map<string, LoadedImage>();
/** The typed call table per image, so a load retried after a late refusal declares it once. */
const rawTables = new Map<string, RawApi>();
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

// ------------------------------------------------------------------- step 5

/** `abi_fingerprint` -> `abiFingerprint`: the decoded `BuildInfo`'s own spelling of a build-info key. */
function camelOf(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
}

function crossCheckField(got: unknown, want: unknown, compare: 'bytes' | 'int'): boolean {
  if (compare === 'int') return Number(got) === Number(want);
  return String(got) === String(want);
}

function checkCrossFields(path: string, buildInfo: BuildInfo, predicate: Predicate): void {
  const pred = predicate as unknown as Record<string, unknown>;
  const info = buildInfo as unknown as Record<string, unknown>;
  for (const field of CROSS_CHECK) {
    const got = info[camelOf(field.buildInfo)];
    const want = pred[field.predicate];
    if (!crossCheckField(got, want, field.compare)) {
      refuseCorrupt(path, `build_info_mismatch:${field.buildInfo}`, JSON.stringify(want), JSON.stringify(got));
    }
  }
}

// ---------------------------------------------------------------- the whole

/**
 * Steps 2-4 and 6, shared by the verified and the unverified path: open the
 * image `RTLD_NOW`, prove the generation, read and decode the build info and
 * compare its fingerprint, resolve every other described symbol for presence.
 */
function openAndCheck(path: string, key: string): { raw: RawApi; buildInfo: BuildInfo } {
  const osHandle = openImage(path); // step 2

  // step 3: chs_abi_version present, then (through ffi-rs, now safe) === ABI_VERSION.
  if (rawDlsym(osHandle, SYMBOL.ABI_VERSION) === null) {
    refuseIncompatible(path, 'not_v1', String(ABI_VERSION), '(the generation handshake is not exported)');
  }
  ffiOpen(key, path); // plan §3.3(d): only now, after RTLD_NOW already bound the image.
  let raw = rawTables.get(key);
  if (raw === undefined) {
    raw = defineRawFunctions(key);
    rawTables.set(key, raw);
  }
  const abiVersionCall = rawCall(raw, SYMBOL.ABI_VERSION, []);
  const abiVersion = abiVersionCall.outcome === 'value' ? Number(abiVersionCall.value) : Number.NaN;
  if (abiVersion !== ABI_VERSION) refuseIncompatible(path, 'abi_version', String(ABI_VERSION), String(abiVersion));

  // step 4: the build info present, decoded (malformed is corrupt), fingerprint matches.
  if (rawDlsym(osHandle, SYMBOL.BUILD_INFO) === null) {
    refuseIncompatible(path, `missing_symbol:${SYMBOL.BUILD_INFO}`, undefined, `(${SYMBOL.BUILD_INFO} is not exported)`);
  }
  const buildInfoCall = rawCall(raw, SYMBOL.BUILD_INFO, []);
  const buildInfoText = buildInfoCall.outcome === 'value' ? (buildInfoCall.value as string | null) : null;
  if (buildInfoText === null) refuseCorrupt(path, 'build_info_malformed', 'a JSON object', 'null');
  const buildInfo = decodeBuildInfo(path, buildInfoText);
  if (buildInfo.abiFingerprint !== ABI_FINGERPRINT) {
    refuseIncompatible(path, 'fingerprint', ABI_FINGERPRINT, buildInfo.abiFingerprint);
  }

  // step 6: every other described symbol, present.
  for (const sym of DESCRIBED_SYMBOLS) {
    if (sym === SYMBOL.ABI_VERSION || sym === SYMBOL.BUILD_INFO) continue; // already proved present above
    if (rawDlsym(osHandle, sym) === null) {
      refuseIncompatible(path, `missing_symbol:${sym}`, undefined, `(${sym} is not exported)`);
    }
  }
  return { raw, buildInfo };
}

/**
 * Load, verify and open one ABI v2 artifact (plan §3.2, steps 1-7). Returns
 * the SAME `LoadedImage` for a path, a hardlink, a symlink or a second
 * spelling of an already-loaded image: steps 2-4, 6 and 7 run once per image.
 * An already-open image is still checked against every new signed statement a
 * request brings (steps 1 and 5; step 4's fingerprint is the binding's own
 * constant, which the first load already matched): a mismatch refuses that
 * request and leaves the image open for the requests it did match.
 */
export function openAbi2(input: LoadInput): LoadedImage {
  const path = input.libraryPath;
  const key = resolveImageKey(path);
  const cached = loadedByKey.get(key);
  if (cached !== undefined) {
    checkGlibc(path, input.predicate, input.platform); // step 1
    checkCrossFields(path, cached.buildInfo, input.predicate); // step 5
    return cached;
  }

  checkGlibc(path, input.predicate, input.platform); // step 1
  const { raw, buildInfo } = openAndCheck(path, key); // steps 2-4, 6
  checkCrossFields(path, buildInfo, input.predicate); // step 5, before step 7 touches the image

  const image = new LoadedImage(path, raw, buildInfo);
  initializeImage(image, input); // step 7
  loadedByKey.set(key, image);
  return image;
}

/**
 * Step 7: `chs_initialize(zone)`, then `chs_set_defaults` when there are defaults. A failure is the call's own error, mapped by the status table; the zone asked for is named in the message either way.
 * The caller caches the image only after this returns, so a failure is never remembered and the next open runs every step again; the public layer settles the setup with the open's outcome (`../setup.ts`).
 */
function initializeImage(image: LoadedImage, setup: ImageSetup): void {
  const timezone = setup.timezone ?? '';
  try {
    image.calls.initialize(Buffer.from(timezone, 'utf8'));
  } catch (err) {
    if (err instanceof UsageError) {
      // The library names the spelling it already holds; name the one asked for too.
      throw usageError(
        `a different image zone was asked for (${JSON.stringify(timezone === '' ? 'UTC' : timezone)}) than the one this image already holds: ${err.messageBytes.toString('utf8')}`,
      );
    }
    throw err;
  }
  const defaults = setup.defaults;
  if (defaults !== undefined && Object.keys(defaults).length > 0) {
    image.calls.setDefaults(Buffer.from(JSON.stringify(defaults), 'utf8'));
  }
}

// ----------------------------------------------------------------- unverified

const UNVERIFIED_ENV = 'CHTYPES_ALLOW_UNVERIFIED_LIBRARY';

/** `openUnverified`'s own gate, alone: a `UsageError` unless BOTH `allow` is true AND `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set. The public layer asks it before an open is attempted, so the caller's misuse is told apart from a failed load. */
export function checkUnverifiedAllowed(path: string, allow: boolean): void {
  if (!allow || process.env[UNVERIFIED_ENV] !== '1') {
    throw usageError(`opening ${path} unverified requires BOTH allow: true and ${UNVERIFIED_ENV}=1`);
  }
}

/**
 * Load `path` with NO predicate: skips step 1 (nothing to check a floor
 * against) and step 5 (nothing to cross-check), still runs 2, 3, 4, 6 and 7.
 * Refuses with a `UsageError` unless BOTH `options.allow` is true AND
 * `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set; warns loudly, once per path,
 * when it proceeds. Never the default path — a local, trusted build is its
 * only caller.
 */
export function openUnverified(path: string, options: { readonly allow: boolean } & ImageSetup): LoadedImage {
  checkUnverifiedAllowed(path, options.allow);
  warnUnverifiedOnce(path);

  const key = resolveImageKey(path);
  const cached = loadedByKey.get(key);
  if (cached !== undefined) return cached;

  const { raw, buildInfo } = openAndCheck(path, key);
  const image = new LoadedImage(path, raw, buildInfo);
  initializeImage(image, options);
  loadedByKey.set(key, image);
  return image;
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
