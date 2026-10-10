/**
 * `Registry`: from a version request to a loaded `Library`
 * (`docs/reference/bindings-v1.md` §6, "The sequence").
 *
 *   registry.for(request)
 *     1. the registry's memo: a request opened before returns the same Library,
 *        for the registry's life (a line request never moves mid-process: a
 *        newer patch installed later is picked up by a new registry)
 *     2. resolved = resolveInstalled(request, host platform, fetch options)   never the network
 *     3. on a miss: autofetch on  -> resolved = ensure(request, fetch options)  may use the network
 *                   autofetch off -> ArtifactMissingError, naming request and platform
 *     4. input = adapt(resolved): the library path, the predicate VERBATIM (never
 *        re-encoded: a re-encoding is a second derivation of the statement the
 *        signature covered) and the platform
 *     5. loader steps 1-6, once per image; step 7 under the process setup
 *     6. the image's Library, whose `resolved` is the record that first opened it
 *
 *   registry.fetch(request)   (public issue #492)
 *     ensure(request, fetch options) for this host, and nothing opened: no
 *     image loaded, no setup touched, nothing memoized or in `libraries()`.
 *
 * One fetch per request at a time: an open that misses the cache and may fetch
 * and a `fetch` of the same request share the fetch in progress, and each gets
 * its answer; a failed fetch is never remembered.
 *
 * Every build the registry opens or fetches is held by this process until it
 * exits (`hold`, the shared flock on the entry's `verified.json`), so
 * `chtypes prune` never removes it meanwhile (public issue #494). A build a
 * concurrent prune removed between the lookup and the hold is looked up (or
 * fetched) again, once.
 *
 * This is the 2.0.0-dev registry: its fetch layer speaks the ABI v2 dev channel
 * (`./ocifetch/channel.ts`; `spec/abi-v2/docs.md`, rules r5 and r6). A pinning
 * option (`frozen`, `lockPath`, `lockWrite`, `update`) is refused at
 * construction, as misuse, and a base or trust option is ignored with one
 * warning.
 *
 * Construction opens nothing: `Registry.open` is asynchronous only because the
 * fetch layer is, and nothing opens an artifact except a request for a version
 * or `preload`. The binding orders and matches no versions itself: which
 * installed build a floating request means is the fetch layer's.
 */

import os from 'node:os';
import { corruptRefusal, openAbi2 } from './abi2/index.js';
import { usageError } from './abi2/index.js';
import { type Library, libraryOf } from './library.js';
import {
  activeChannel,
  ArtifactMissingError,
  cacheRoot as fetchCacheRoot,
  ensure,
  type FetchV1Options,
  hold,
  hostPlatformKey,
  isFilesystemError,
  listInstalled,
  missingNotes,
  PinningRefusedError,
  type PlatformKey,
  refusePinning,
  removedWhileHeld,
  type Resolved,
  resolveInstalled,
  satisfiesRequest,
  searchDirs as fetchSearchDirs,
  withNotes,
} from './ocifetch/index.js';
import { ENV_AUTOFETCH_NAME, SPELLING_REGEX } from './ocifetch/constants.gen.js';
import { trustedKeysOfHex, withEnvironment } from './env.js';
import { commitSetup, latchSetup, settleFailedOpen, setupGeneration } from './setup.js';

/**
 * What the fetch layer is configured with (`docs/guides/fetch-v1.md`; `docs/reference/bindings-v1.md` §6), under
 * the same names as Go's, Python's and Rust's fields, each falling back to its environment variable and then to
 * the fetch layer's own default. The fetch layer's test hooks are not here, and a registry opens libraries for
 * this host only (`chtypes fetch --platform` names another).
 */
export interface FetchOptions {
  /** Base URLs, most preferred first; overrides `CHTYPES_ARTIFACTS_URL` and the built-in list. */
  readonly bases?: readonly string[] | undefined;
  /** The cache (`CHTYPES_CACHE`). */
  readonly cacheDir?: string | undefined;
  /** Read-only system directories searched after the cache; undefined is the built-in list, `[]` none. */
  readonly systemDirs?: readonly string[] | undefined;
  /** Raw 32-byte ed25519 public keys, each as 64 hex characters (`CHTYPES_TRUSTED_KEYS`'s spelling); the key id is derived. A non-empty list REPLACES the default trust. */
  readonly trustedKeys?: readonly string[] | undefined;
  /** Proceed, with a warning, when no signed statement verifies (`CHTYPES_ALLOW_UNSIGNED`). */
  readonly allowUnsigned?: boolean | undefined;
  /** Sent as `Authorization: Bearer <token>` to configured base hosts only (`CHTYPES_DOWNLOAD_TOKEN`). */
  readonly token?: string | undefined;
  /** The cache only, no request; on when this is true or `CHTYPES_OFFLINE` is `1` (public issue #528). */
  readonly offline?: boolean | undefined;
  /** Fetch exactly the lock's pinned digests. */
  readonly frozen?: boolean | undefined;
  /** The lock file. */
  readonly lockPath?: string | undefined;
  /** Write (or update) the lock after a successful fetch. */
  readonly lockWrite?: boolean | undefined;
  /** Re-resolve every locked request and rewrite the lock. */
  readonly update?: boolean | undefined;
  /** Strict mode (public issue #486): every fault of the cache and of an existing system dir is a `CacheUnusableError`. Unset reads `CHTYPES_CACHE_STRICT`. */
  readonly strictCache?: boolean | undefined;
}

/** The public options as the fetch layer's own: each set field copied, and the trusted keys read from hex. */
function fetchLayerOptions(options: FetchOptions = {}): FetchV1Options {
  const out: { -readonly [K in keyof FetchV1Options]: FetchV1Options[K] } = {};
  if (options.bases !== undefined) out.bases = options.bases;
  if (options.cacheDir !== undefined) out.cacheDir = options.cacheDir;
  if (options.systemDirs !== undefined) out.systemDirs = options.systemDirs;
  if (options.trustedKeys !== undefined) {
    // A channel that honors no trust override ignores the option, with its one
    // warning, whatever it holds (rule r6), as every binding does.
    out.trustedKeys = activeChannel().overridable ? trustedKeysOfHex(options.trustedKeys, 'the trustedKeys option') : [];
  }
  if (options.allowUnsigned !== undefined) out.allowUnsigned = options.allowUnsigned;
  if (options.token !== undefined) out.token = options.token;
  if (options.offline !== undefined) out.offline = options.offline;
  if (options.frozen !== undefined) out.frozen = options.frozen;
  if (options.lockPath !== undefined) out.lockPath = options.lockPath;
  if (options.lockWrite !== undefined) out.lockWrite = options.lockWrite;
  if (options.update !== undefined) out.update = options.update;
  if (options.strictCache !== undefined) out.strictCache = options.strictCache;
  return out;
}

/**
 * The cache root a fetch, list or `chtypes where` with `options` would use: `options.cacheDir`, else
 * `CHTYPES_CACHE` (each used through its `v2-dev` subroot, rule r5 of `spec/abi-v2/docs.md`), else `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`. It is the
 * fetch layer's own resolution and the first entry of {@link searchDirs}; it creates nothing and reads no cache.
 */
export function cacheRoot(options: FetchOptions = {}): string {
  return fetchCacheRoot(options.cacheDir);
}

/**
 * Every directory a lookup reads for installed builds, in the order it reads them: the cache root first, then
 * each read-only system directory (`options.systemDirs`; undefined is the built-in list, `[]` none). The order is
 * the fetch layer's own, and on a tie the earlier directory wins. It creates nothing and touches no file.
 */
export function searchDirs(options: FetchOptions = {}): readonly string[] {
  return fetchSearchDirs(fetchLayerOptions(options));
}

export interface RegistryOptions {
  readonly fetch?: FetchOptions | undefined;
  /** Fetch on a miss. Defaults to the `CHTYPES_AUTOFETCH` environment variable, and is off when that is unset. */
  readonly autofetch?: boolean | undefined;
  /** Requests opened at construction, in list order. They never fetch, even with autofetch on. */
  readonly preload?: readonly string[] | undefined;
}

function envFlag(name: string): boolean {
  const v = process.env[name];
  return v !== undefined && ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
}

const spelling = new RegExp(SPELLING_REGEX);

function checkSpelling(request: string): void {
  if (typeof request !== 'string' || !spelling.test(request)) {
    throw usageError(
      `${JSON.stringify(request)} is not a version spelling: give two, three or four dotted parts (26.8, 26.8.15, 26.8.15.10), with no "v" prefix and no channel suffix`,
    );
  }
}

/** The test hook a registry calls each time an open or a `fetch` starts waiting on a fetch, its own or another's; see `onFetchWaitForTests`. */
const fetchWaitHooks = new WeakMap<Registry, (request: string) => void>();

/**
 * Test only: `hook` is called with the request each time an open or a `fetch`
 * of `registry` starts waiting on a fetch, its own or another's, so a test can
 * hold a fetch until every caller it starts is waiting on it, on events rather
 * than a clock. `undefined` removes it. Not part of the public API.
 */
export function onFetchWaitForTests(registry: Registry, hook: ((request: string) => void) | undefined): void {
  if (hook === undefined) fetchWaitHooks.delete(registry);
  else fetchWaitHooks.set(registry, hook);
}

export class Registry {
  readonly #fetch: FetchV1Options;
  readonly #autofetch: boolean;
  readonly #memo = new Map<string, Promise<Library>>();
  readonly #opened: Library[] = [];
  /** The fetches in progress, by request: every open that misses the cache and every `fetch` of the request waits on the one there. */
  readonly #fetches = new Map<string, Promise<Resolved>>();

  private constructor(options: RegistryOptions) {
    // The environment fills what the caller left unset. CHTYPES_TARGET names
    // `chtypes fetch`'s platform, never a registry's: it opens for this host.
    const { platform: _cliOnly, ...fetch } = withEnvironment(fetchLayerOptions(options.fetch));
    this.#fetch = fetch;
    this.#autofetch = options.autofetch ?? envFlag(ENV_AUTOFETCH_NAME);
  }

  /** Construct a registry. It opens nothing, except each `preload` request, in list order. A pinning fetch option is refused here, as a `UsageError` (rule r6). */
  static async open(options: RegistryOptions = {}): Promise<Registry> {
    try {
      refusePinning(fetchLayerOptions(options.fetch));
    } catch (err) {
      if (err instanceof PinningRefusedError) throw usageError(err.message);
      throw err;
    }
    const registry = new Registry(options);
    for (const request of options.preload ?? []) {
      checkSpelling(request);
      await registry.#memoized(request, false);
    }
    return registry;
  }

  /** Open a version: the installed build the request names, fetched first when autofetch is on and none is installed. */
  async for(request: string): Promise<Library> {
    checkSpelling(request); // misuse, refused before anything is attempted: it unlocks nothing
    return this.#memoized(request, this.#autofetch);
  }

  /** What is installed: the fetch layer's own listing. */
  async installed(): Promise<readonly Resolved[]> {
    return listInstalled(this.#fetch);
  }

  /**
   * Resolve, fetch, verify and install the build `request` names into the cache, as `chtypes fetch` does, and open
   * nothing: no library is loaded and the process setup is untouched, so it needs no `setup` (public issue #492). It
   * honors the registry's fetch options (offline, the cache, and under a production channel the bases, the trust and
   * the lock), and it shares one fetch with a concurrent open or `fetch` of the same request. The `Resolved` names the
   * installed version, build, digests and library path. The build is held by this process until it exits, so no
   * `chtypes prune` removes it meanwhile (public issue #494). A refused spelling is a `UsageError`, as for `for`;
   * every other failure is the fetch layer's own error, an `ArtifactError` with its code.
   */
  async fetch(request: string): Promise<Resolved> {
    checkSpelling(request); // misuse, refused before anything is attempted
    if (hostPlatformKey(os.platform(), os.arch()) === undefined) {
      throw new ArtifactMissingError(`chtypes: no artifact is published for this host (${os.platform()}-${os.arch()})`);
    }
    try {
      return await this.#ensureHeld(request);
    } catch (err) {
      throw typedFilesystemError(err);
    }
  }

  /** The libraries this registry has opened, in order of first open. */
  libraries(): readonly Library[] {
    return [...this.#opened];
  }

  #memoized(request: string, mayFetch: boolean): Promise<Library> {
    const memo = this.#memo.get(request);
    if (memo !== undefined) return memo;
    // An open that attempted a load and failed unlocks the setup record while no image has completed load step 7, whatever failed: the resolve, the fetch, the signature or any load step (bindings-v1.md §6, rule 4). The spelling was checked before this, as misuse.
    const began = setupGeneration();
    const opening = this.#openRequest(request, mayFetch).then(
      (library) => {
        if (!this.#opened.includes(library)) this.#opened.push(library);
        return library;
      },
      (err: unknown) => {
        this.#memo.delete(request); // a failure is never remembered
        settleFailedOpen(began);
        throw err;
      },
    );
    this.#memo.set(request, opening);
    return opening;
  }

  async #openRequest(request: string, mayFetch: boolean): Promise<Library> {
    const platform: PlatformKey | undefined = hostPlatformKey(os.platform(), os.arch());
    if (platform === undefined) {
      throw new ArtifactMissingError(`chtypes: no artifact is published for this host (${os.platform()}-${os.arch()})`);
    }
    let resolved: Resolved | undefined;
    try {
      resolved = await this.#lookupHeld(request, platform);
      if (resolved === undefined && mayFetch) resolved = await this.#ensureHeld(request);
    } catch (err) {
      throw typedFilesystemError(err);
    }
    if (resolved === undefined) {
      throw new ArtifactMissingError(
        withNotes(
          `chtypes: ${request} (${platform}) is not installed. Fetch it first, or turn autofetch on (the autofetch option, or ${ENV_AUTOFETCH_NAME}=1)`,
          await missingNotes(this.#fetch),
        ),
      );
    }
    const setup = commitSetup();
    // The adapter: the predicate goes through exactly as the fetch layer returned it.
    const image = openAbi2({
      libraryPath: resolved.libraryPath,
      predicate: resolved.predicate,
      platform: resolved.platform,
      timezone: setup.timezone,
      defaults: setup.defaults,
    });
    latchSetup();
    const library = libraryOf(image, resolved);
    // The load-time assertion (fetch-v1.md §9; public issue #481): the library's
    // own build_info must report a version within the request, whatever the
    // cache answered. The image stays loaded for the requests it does answer.
    if (!satisfiesRequest(request, library.version)) {
      throw corruptRefusal({
        reason: 'build_info_mismatch:clickhouse_version',
        path: resolved.libraryPath,
        want: request,
        got: library.version,
      });
    }
    return library;
  }

  /**
   * The installed lookup, then this process's hold on the build it found (`hold`), taken before anything reads the
   * library. A build a concurrent prune removed between the two is looked up again, once; removed twice, it is a
   * miss.
   */
  async #lookupHeld(request: string, platform: PlatformKey): Promise<Resolved | undefined> {
    for (let attempt = 0; attempt < 2; attempt++) {
      const resolved = await resolveInstalled(request, platform, this.#fetch);
      if (resolved === undefined) return undefined;
      if (hold(resolved.dir) !== 'vanished') return resolved;
    }
    return undefined;
  }

  /**
   * The request's one fetch (`#ensure`), then this process's hold on the build it installed. A build a concurrent
   * prune removed between the two is fetched again, once; removed twice, it is `ArtifactMissingError`.
   */
  async #ensureHeld(request: string): Promise<Resolved> {
    for (let attempt = 0; ; attempt++) {
      const resolved = await this.#ensure(request);
      if (hold(resolved.dir) !== 'vanished') return resolved;
      if (attempt === 1) throw removedWhileHeld(request, resolved.dir);
    }
  }

  /**
   * Joins the fetch in progress for `request`, or starts it: every caller waiting on it gets its answer, and it
   * leaves the table as it settles, so a failed fetch is never remembered and the next caller starts a new one.
   */
  #ensure(request: string): Promise<Resolved> {
    let flight = this.#fetches.get(request);
    if (flight === undefined) {
      const started: Promise<Resolved> = ensure(request, this.#fetch).finally(() => {
        if (this.#fetches.get(request) === started) this.#fetches.delete(request);
      });
      this.#fetches.set(request, started);
      flight = started;
    }
    fetchWaitHooks.get(this)?.(request);
    return flight;
  }
}

/**
 * A filesystem failure in the fetch layer (an unwritable or full cache) as
 * the `UsageError` Go's `fetchError` gives the same failure, never a raw Node
 * error (public issue #482). Any other error passes through unchanged.
 */
function typedFilesystemError(err: unknown): unknown {
  if (!isFilesystemError(err)) return err;
  return usageError(`chtypes: the cache could not be read or written: ${err.message}`);
}
