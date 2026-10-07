/**
 * The fetch contract this build speaks. A 2.0.0-dev SDK speaks the ABI v2 dev
 * channel (`spec/abi-v2/docs.md`, rules r5 and r6), which is the v1 fetch
 * contract (`docs/guides/fetch-v1.md`) narrowed in five ways:
 *
 *   - it fetches ONLY from `DEV_CHANNEL_BASE` and trusts ONLY the staging key
 *     (`DEV_KEY_HEX`, id `DEV_KEY_ID`); the release key is not in its trust
 *     list;
 *   - it has no override: `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and
 *     `CHTYPES_ALLOW_UNSIGNED`, and the options that set a base, a trust list
 *     or an unsigned fetch (`bases`, `trustedKeys`, `allowUnsigned`, and
 *     `fetchSigned`'s repository), are ignored, each with one loud warning per
 *     process;
 *   - it refuses `frozen`, `lockWrite`, `lockPath` and `update` (`--frozen`,
 *     `--lock`, `--update`) before any network call: a dev build is
 *     replaceable and a superseded one expires, so nothing may pin one;
 *   - its cache is one no released 1.x reader ever reads: `verified.json`
 *     records are schema 2, the default root is
 *     `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`, and an explicit cache
 *     (`CHTYPES_CACHE`, `--cache`, the `cacheDir` option) is used through its
 *     subroot `<cache>/v2-dev`, never as a whole layout;
 *   - a signed predicate must say abi 2.
 *
 * The v1 contract itself stays in this module, unchanged, because the
 * fetch-v1 conformance cases (`tests/fixtures/fetch-v1`) are its
 * specification and the dev channel shares every other rule with it. Only a
 * test reaches it: `useFetchV1ForTests` and `allowOverridesForTests` throw
 * anywhere but inside a vitest worker, and neither is exported from the
 * package's entry point, so neither is an override a user can reach.
 */

import {
  ABI_GENERATION,
  DEFAULT_BASES,
  ENV_ALLOW_UNSIGNED_NAME,
  ENV_BASES_NAME,
  ENV_TRUSTED_KEYS_NAME,
  RELEASE_KEYS,
  SCHEMA_VERSION,
  SYSTEM_CACHE_DIRS,
} from './constants.gen.js';
import type { FetchV1Options, TrustedKey } from './types.js';

/** The only base a 2.0.0-dev SDK fetches from (rule r6). */
export const DEV_CHANNEL_BASE = 'https://registry-staging.wavehouse.dev/chtypes/v2-dev';
/** The staging key's id (`sha256-first16hex` over `DEV_KEY_HEX`). */
export const DEV_KEY_ID = '824345f9bcf8e5bf';
/** The staging key, the only key a 2.0.0-dev SDK trusts: an ed25519 public key, raw 32 bytes as lowercase hex. */
export const DEV_KEY_HEX = '5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c';
/** The dev channel's cache directory name: the default root's last element and an explicit cache's subroot (rule r5). */
export const DEV_CACHE_DIR = 'v2-dev';
/** The `verified.json` schema the dev channel writes, and the only one it reads (rule r5). */
export const DEV_RECORD_SCHEMA = 2;
/** The abi a dev predicate must carry. */
export const DEV_ABI_GENERATION = 2;

/**
 * The environment twin of the `offline` option (public issue #528): `CHTYPES_OFFLINE=1` reads the cache only and
 * makes no request. It names no source, so rule r6 holds. Not a generated constant: the generated set is the v1
 * fetch contract's.
 */
export const ENV_OFFLINE_NAME = 'CHTYPES_OFFLINE';

/** Whether offline mode is on: the option is true OR `CHTYPES_OFFLINE=1`; neither turns the other off. Read per call, with the other environment variables. */
export function offlineMode(option: boolean | undefined): boolean {
  return option === true || process.env[ENV_OFFLINE_NAME] === '1';
}

/** What every lock, frozen or update request gets from a 2.0.0-dev SDK, before any network call (rule r6). */
export const PINNING_REFUSED =
  '--lock, --frozen and --update are refused by a 2.0.0-dev SDK: a dev build is replaceable, ' +
  'and a superseded one expires after 14 days, so nothing may pin one (spec/abi-v2/docs.md, rule r6)';

/** One fetch contract. */
export interface Channel {
  readonly name: string;
  /** The abi a signed predicate must carry, and `Resolved.abiGeneration`. */
  readonly abi: number;
  /** The `verified.json` schema written, and the only one read. */
  readonly recordSchema: number;
  /** The default root is `${XDG_CACHE_HOME:-~/.cache}/chtypes/<rootLeaf>`. */
  readonly rootLeaf: string;
  /** An explicit cache is used as `<cache>/<subroot>`; `''` uses it whole. */
  readonly subroot: string;
  /** The read-only system cache directories searched after the cache. */
  readonly systemDirs: readonly string[];
  /** The default bases. */
  readonly bases: readonly string[];
  /** The default trust list. */
  readonly keys: readonly TrustedKey[];
  /** Whether the base, trust and unsigned overrides are honored. */
  readonly overridable: boolean;
  /** Whether lock, frozen and update are honored. */
  readonly pinnable: boolean;
}

/** What a 2.0.0-dev SDK speaks, and what every process speaks unless a test selected another contract. */
const DEV_CHANNEL: Channel = {
  name: DEV_CACHE_DIR,
  abi: DEV_ABI_GENERATION,
  recordSchema: DEV_RECORD_SCHEMA,
  rootLeaf: DEV_CACHE_DIR,
  subroot: DEV_CACHE_DIR,
  systemDirs: [`/usr/local/share/chtypes/${DEV_CACHE_DIR}`, `/opt/chtypes/${DEV_CACHE_DIR}`],
  bases: [DEV_CHANNEL_BASE],
  keys: [{ keyid: DEV_KEY_ID, ed25519Hex: DEV_KEY_HEX }],
  overridable: false,
  pinnable: false,
};

/** The v1 contract the fetch-v1 conformance cases specify, from the generated constants. Only `useFetchV1ForTests` selects it. */
const FETCH_V1_CHANNEL: Channel = {
  name: 'v1',
  abi: ABI_GENERATION,
  recordSchema: SCHEMA_VERSION,
  rootLeaf: 'v1',
  subroot: '',
  systemDirs: SYSTEM_CACHE_DIRS,
  bases: DEFAULT_BASES,
  keys: RELEASE_KEYS.map((k) => ({ keyid: k.keyid, ed25519Hex: k.ed25519Hex })),
  overridable: true,
  pinnable: true,
};

let current: Channel | undefined;

/** The contract this process fetches under: the dev channel, unless a test selected another. */
export function activeChannel(): Channel {
  return current ?? DEV_CHANNEL;
}

/** The fetch contract this process speaks: `v2-dev` in every process but a test that selected another. */
export function channelName(): string {
  return activeChannel().name;
}

/** Whether this process is a vitest worker: the only place a test seam may run. */
function inTestRunner(): boolean {
  return process.env['VITEST'] === 'true' && process.env['VITEST_WORKER_ID'] !== undefined;
}

function testOnly(what: string): void {
  if (!inTestRunner()) {
    throw new Error(`chtypes: ${what} is test-only; a 2.0.0-dev SDK has no override (spec/abi-v2/docs.md, rule r6)`);
  }
}

function use(c: Channel): () => void {
  const prev = current;
  current = c;
  return () => {
    current = prev;
  };
}

/**
 * Makes this TEST process speak the v1 fetch contract (`docs/guides/fetch-v1.md`):
 * overrides, locks, schema-1 records and abi-1 predicates. The fetch-v1
 * conformance cases and the v1-protocol tests run under it; every case names
 * its own fixture registry and the test key. Returns the function that
 * restores the previous contract; throws outside a vitest worker.
 */
export function useFetchV1ForTests(): () => void {
  testOnly('useFetchV1ForTests');
  return use(FETCH_V1_CHANNEL);
}

/**
 * Makes this TEST process's dev channel honor the base, trust and unsigned
 * overrides, so a test can reach a fixture registry signed with the test key;
 * everything else stays the dev channel's (abi 2, schema-2 records, the
 * v2-dev cache, no pinning). Returns the restore function; throws outside a
 * vitest worker.
 */
export function allowOverridesForTests(): () => void {
  testOnly('allowOverridesForTests');
  return use({ ...DEV_CHANNEL, overridable: true });
}

/** Makes this TEST process speak the dev channel exactly as every other process does (undoing either function above). Returns the restore function. */
export function useDevChannelForTests(): () => void {
  testOnly('useDevChannelForTests');
  return use(DEV_CHANNEL);
}

// ------------------------------------------------------------ the warnings

const warned = new Set<string>();
let warnSink: (text: string) => void = (text) => {
  process.stderr.write(text);
};

/** Prints, once per process per setting, that a dev SDK ignores it (rule r6: "a dev SDK says so, once and loudly"). */
export function warnIgnored(setting: string): void {
  if (warned.has(setting)) return;
  warned.add(setting);
  warnSink(
    `chtypes: WARNING: ${setting} is set and IGNORED: this is a 2.0.0-dev SDK, which fetches only from ${DEV_CHANNEL_BASE} ` +
      `and trusts only the staging key ${DEV_KEY_ID} (spec/abi-v2/docs.md, rule r6)\n`,
  );
}

/** The settings warned about so far, sorted. */
export function ignoredSettingsWarned(): readonly string[] {
  return [...warned].sort();
}

/** Test-only: sends the warnings to `sink` and forgets which were given, so a test sees exactly its own. Returns the restore function. */
export function captureIgnoredForTests(sink: (text: string) => void): () => void {
  testOnly('captureIgnoredForTests');
  const prevSink = warnSink;
  const prevWarned = [...warned];
  warnSink = sink;
  warned.clear();
  return () => {
    warnSink = prevSink;
    warned.clear();
    for (const s of prevWarned) warned.add(s);
  };
}

function envSet(name: string, trim: boolean): boolean {
  const v = process.env[name];
  return v !== undefined && (trim ? v.trim() : v) !== '';
}

/**
 * Names, once each and loudly, every base, trust and unsigned override
 * `options` or the environment sets, when the active contract has none (rule
 * r6). Every seam entry point calls it first, so an ignored setting is named
 * whatever the command does with the network.
 */
export function noteIgnoredOverrides(options: FetchV1Options): void {
  if (activeChannel().overridable) return;
  if (options.bases !== undefined && options.bases.length > 0) warnIgnored('the bases option');
  if (envSet(ENV_BASES_NAME, true)) warnIgnored(ENV_BASES_NAME);
  if (options.trustedKeys !== undefined) warnIgnored('the trustedKeys option');
  if (envSet(ENV_TRUSTED_KEYS_NAME, true)) warnIgnored(ENV_TRUSTED_KEYS_NAME);
  if (options.allowUnsigned === true) warnIgnored('the allowUnsigned option');
  if (envSet(ENV_ALLOW_UNSIGNED_NAME, false)) warnIgnored(ENV_ALLOW_UNSIGNED_NAME);
}

/** Whether an unsigned fetch was asked for and the active contract honors it. */
export function allowUnsigned(options: FetchV1Options): boolean {
  return activeChannel().overridable && options.allowUnsigned === true;
}

// ------------------------------------------------------------ the pinning

/** The pinning requests `options` makes, by name. */
export function pinningRequested(options: FetchV1Options): readonly string[] {
  const set: string[] = [];
  if (options.frozen === true) set.push('frozen');
  if (options.lockWrite === true) set.push('lock');
  if (options.update === true) set.push('update');
  if (options.lockPath !== undefined && options.lockPath !== '') set.push('a lock path');
  if (options.lockAllPlatforms === true) set.push('lock all platforms');
  return set;
}

/** A lock, frozen or update request refused by a 2.0.0-dev SDK. It is the caller's misuse, never an artifact failure: the CLI exits with the usage status. */
export class PinningRefusedError extends Error {
  readonly requested: readonly string[];

  constructor(requested: readonly string[]) {
    super(`chtypes: ${PINNING_REFUSED} (requested: ${requested.join(', ')})`);
    this.name = new.target.name;
    this.requested = requested;
  }
}

/** Throws `PinningRefusedError` when the active contract refuses the pinning `options` asks for: before anything else, the network above all (rule r6). */
export function refusePinning(options: FetchV1Options): void {
  if (activeChannel().pinnable) return;
  const set = pinningRequested(options);
  if (set.length > 0) throw new PinningRefusedError(set);
}
