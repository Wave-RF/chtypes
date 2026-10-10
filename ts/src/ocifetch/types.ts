/**
 * Shared types for the v1 fetch layer: the seam (`docs/guides/fetch-v1.md`
 * §9, the v1 fetch-layer plan's §1.3) and the pieces behind it. Nothing
 * outside this module imports from anywhere but `index.ts`.
 */

import { MEDIA_TYPE_INDEX, MEDIA_TYPE_MANIFEST, PLATFORMS } from './constants.gen.js';

/**
 * Every `GET …/manifests/<ref>` — by tag or by digest — sends this `Accept`
 * header (the delivery side's review): our own host ignores it, but a
 * mirror may need it to answer with the right media type, and a mirror can
 * be asked for a manifest exactly as our host can.
 */
export const MANIFEST_ACCEPT_HEADER = `${MEDIA_TYPE_INDEX}, ${MEDIA_TYPE_MANIFEST}`;

/** One of the three platform keys v1 ships: no darwin-amd64 (A18). */
export type PlatformKey = (typeof PLATFORMS)[number]['key'];

/** True for exactly the three v1 platform keys. */
export function isPlatformKey(value: string): value is PlatformKey {
  return (PLATFORMS as readonly { readonly key: string }[]).some((p) => p.key === value);
}

/** `PLATFORMS`'s row for one key — never `undefined` for a `PlatformKey`. */
export function platformInfo(key: PlatformKey): { readonly key: PlatformKey; readonly os: string; readonly architecture: string } {
  const row = (PLATFORMS as readonly { readonly key: string; readonly os: string; readonly architecture: string }[]).find(
    (p) => p.key === key,
  );
  if (row === undefined) throw new Error(`chtypes: unreachable: ${key} is not in PLATFORMS`);
  return row as { readonly key: PlatformKey; readonly os: string; readonly architecture: string };
}

/** This host's platform key, or `undefined` when v1 does not ship this os/arch (e.g. darwin-amd64). */
export function hostPlatformKey(osName: string, archName: string): PlatformKey | undefined {
  const arch = archName === 'x64' ? 'amd64' : archName;
  const key = `${osName}-${arch}`;
  return isPlatformKey(key) ? key : undefined;
}

/** A version request exactly as given: `"26.8"`, `"26.8.15"` or `"26.8.15.10"`. */
export type VersionRequest = string;

/** The digests of every object a resolve touched, as the guide's `Resolved` names them. */
export interface ResolvedDigests {
  readonly index?: string;
  readonly manifest: string;
  readonly layer: string;
  readonly bundle: string;
  readonly bundleManifest?: string;
}

/**
 * The signed predicate (`layout-v2.md` §4.1), verified and carried through
 * **verbatim** — the fetch layer never interprets `abi_fingerprint`,
 * `library_sha256`, `library_bytes` or `glibc_floor` beyond the checks
 * `docs/guides/fetch-v1.md` §4 and §5 name.
 */
export interface ArtifactPredicate {
  readonly abi: number;
  readonly abi_fingerprint: string;
  readonly clickhouse_version: string;
  readonly channel: string;
  readonly clickhouse_minor: string;
  readonly clickhouse_commit: string;
  readonly os: string;
  readonly arch: string;
  readonly build: string;
  readonly core_commit: string;
  readonly inputs_sha256: string;
  readonly library: string;
  readonly library_sha256: string;
  readonly library_bytes: number;
  readonly glibc_floor?: string;
}

/** An in-toto Statement v1, decoded and duplicate-key-checked (`dsse.ts`). */
export interface Statement {
  readonly _type: string;
  readonly subject: readonly { readonly name: string; readonly digest: { readonly sha256: string } }[];
  readonly predicateType: string;
  /** Opaque beyond what `dsse.ts` itself checks; callers of `fetchSigned` read this themselves. */
  readonly predicate: Readonly<Record<string, unknown>>;
}

/** One verified Sigstore bundle's outcome, inside `verifyAnyBundle`. */
export interface VerifiedBundle {
  readonly statement: Statement;
  /** The trusted key id that verified it. */
  readonly signedBy: string;
  readonly bundleDigest: string;
}

/** What a fetch returns on success (`docs/guides/fetch-v1.md` §9). */
export interface Resolved {
  /** The ABI generation the fetch contract speaks: 2 for this 2.0.0-dev SDK (`./channel.ts`). */
  readonly abiGeneration: number;
  readonly platform: PlatformKey;
  readonly request: VersionRequest;
  readonly version: string;
  readonly channel?: string;
  readonly build: string;
  /** Absolute path to the unpacked library file. */
  readonly libraryPath: string;
  /** `<layout>/unpacked/sha256/<manifest-hex>`. */
  readonly dir: string;
  readonly digests: ResolvedDigests;
  readonly predicate: ArtifactPredicate;
  /** The key id that verified the signature, or `""` under allow-unsigned. */
  readonly signedBy: string;
  /** A base URL, `"cache"`, or `"system:<dir>"`. */
  readonly source: string;
  readonly alreadyInstalled: boolean;
  readonly warnings: readonly string[];
}

/** One `verify_installed` row. */
export interface VerifyResult {
  readonly platform: PlatformKey;
  readonly dir: string;
  readonly ok: boolean;
  readonly detail: string;
}

/**
 * Test-only injection point (plan §3.2 "Sleeps are asserted exactly"): every
 * binding's fetch core takes an injected `sleep(seconds)` and `now()` so
 * retry cases run instantly and deterministically. Production code uses
 * `realClock()`.
 */
export interface Clock {
  now(): number;
  sleep(seconds: number): Promise<void>;
}

export function realClock(): Clock {
  return {
    now: () => Date.now(),
    sleep: (seconds: number) =>
      new Promise((resolve) => {
        setTimeout(resolve, Math.max(0, seconds) * 1000);
      }),
  };
}

export interface TrustedKey {
  readonly keyid: string;
  readonly ed25519Hex: string;
}

/**
 * Options shared by every seam entry point. All optional; every default
 * matches `docs/guides/fetch-v1.md`.
 */
export interface FetchV1Options {
  /** Overrides `DEFAULT_BASES` / `CHTYPES_ARTIFACTS_URL`, in order. */
  readonly bases?: readonly string[];
  /** Overrides the OCI layout root (`CHTYPES_CACHE`). */
  readonly cacheDir?: string;
  /** Overrides `SYSTEM_CACHE_DIRS`. */
  readonly systemDirs?: readonly string[];
  /** Replaces (never appends to) the default trust list. */
  readonly trustedKeys?: readonly TrustedKey[];
  readonly allowUnsigned?: boolean;
  /** Sent as `Authorization: Bearer <token>` to configured base hosts only. */
  readonly token?: string;
  /** `resolve_installed`'s mode for `ensure`: cache/system dirs only, no network. Unset reads `CHTYPES_OFFLINE` ("1" is on); offline is on when this is true OR the variable is "1"; neither turns the other off (public issue #528). */
  readonly offline?: boolean;
  /** Skip resolution; fetch the lock's pinned digests directly. */
  readonly frozen?: boolean;
  /** Path to the lock file (`LOCK_DEFAULT_FILE` under the caller's cwd by default). */
  readonly lockPath?: string;
  /** Write (or update) the lock after a successful `ensure`. */
  readonly lockWrite?: boolean;
  /** Pin every platform the index offers, not only the host's (guide §6) — `lockWrite` only. On unless `false`. */
  readonly lockAllPlatforms?: boolean;
  /** Re-resolve every locked request and rewrite the lock, ignoring `frozen`. */
  readonly update?: boolean;
  /** Overrides the host platform (tests, and a cross-platform lock write). */
  readonly platform?: PlatformKey;
  /**
   * Strict mode (public issue #486): every fault of the cache and of an
   * existing system dir is a `CacheUnusableError` naming the path, never "not
   * installed", and never a fall-through to a system dir. Unset reads
   * `CHTYPES_CACHE_STRICT` (`1` is on), else off.
   */
  readonly strictCache?: boolean;
  readonly clock?: Clock;
  /** Test-only: runs after the index's temp-write, before its atomic rename. */
  readonly beforeIndexRename?: () => Promise<void> | void;
  /** Test-only: an explicit `now()`-relative deadline is not used; retries read `clock`. */
  readonly httpLog?: (event: Readonly<Record<string, unknown>>) => void;
}

/** Thrown-safe helper: the exact platform this call targets. */
export function resolvePlatformOption(option: PlatformKey | undefined, osName: string, archName: string): PlatformKey {
  if (option !== undefined) return option;
  const host = hostPlatformKey(osName, archName);
  if (host === undefined) {
    throw new Error(
      `chtypes: this host (${osName}-${archName}) is not one of v1's platforms (${(PLATFORMS as readonly { key: string }[])
        .map((p) => p.key)
        .join(', ')})`,
    );
  }
  return host;
}

/** Every digest in this module is `sha256:<64 hex>` (OCI's own spelling); this is the one place that parses it. */
export function hexOfDigest(digest: string): string {
  const [algo, hex] = digest.split(':', 2);
  if (algo !== 'sha256' || hex === undefined || !/^[0-9a-f]{64}$/.test(hex)) {
    throw new Error(`chtypes: not a sha256 digest: ${JSON.stringify(digest)}`);
  }
  return hex;
}

/** `<hex>` back to `sha256:<hex>`. */
export function digestOfHex(hex: string): string {
  return `sha256:${hex}`;
}

// --------------------------------------------------------------- URL shape
//
// A "repository root" (what `docs/guides/fetch-v1.md` §2 calls a base, and
// what the generic `fetchSigned` seam calls `repository`) is one string that
// already carries the registry origin AND the repository path, e.g.
// `https://registry.wavehouse.dev/chtypes/v1` — exactly `DEFAULT_BASES`'
// own spelling, with no `/v2` segment. The real OCI distribution path DOES
// carry `/v2/` (`oras`/`docker` insert it from a plain `host/repo:tag`
// reference the same way), so an http(s) root gets `/v2` spliced in between
// its origin and its own path. A `file://` root is different on purpose: the
// fixture trees are static mirrors of the real URL paths *including* the
// literal `v2` directory, so nothing is inserted — see
// `tests/fixtures/fetch-v1/trees/*/v2/...` (lane 0B) once it exists.

export function trimTrailingSlash(s: string): string {
  return s.endsWith('/') ? s.slice(0, -1) : s;
}

/** `${repositoryRoot}${suffix}` with the OCI `/v2` segment inserted for http(s), never for `file:`. */
export function endpointUrl(repositoryRoot: string, suffix: string): string {
  const root = trimTrailingSlash(repositoryRoot);
  if (root.startsWith('file:')) return `${root}${suffix}`;
  const u = new URL(root);
  const repoPath = trimTrailingSlash(u.pathname);
  return `${u.origin}/v2${repoPath}${suffix}`;
}
