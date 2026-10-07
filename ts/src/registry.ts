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
import { LoaderCorruptError, openAbi2 } from './abi2/index.js';
import { usageError } from './abi2/index.js';
import { type Library, libraryOf } from './library.js';
import {
  ArtifactMissingError,
  cacheRoot as fetchCacheRoot,
  ensure,
  type FetchV1Options,
  hostPlatformKey,
  isFilesystemError,
  listInstalled,
  missingNotes,
  PinningRefusedError,
  type PlatformKey,
  refusePinning,
  type Resolved,
  resolveInstalled,
  satisfiesRequest,
  searchDirs as fetchSearchDirs,
  withNotes,
} from './ocifetch/index.js';
import { ENV_AUTOFETCH_NAME, SPELLING_REGEX } from './ocifetch/constants.gen.js';
import { withEnvironment } from './env.js';
import { commitSetup, latchSetup, settleFailedOpen, setupGeneration } from './setup.js';

/** The fetch layer's options (bases, cache directory, system directories, trusted keys, token, allow-unsigned, offline, frozen, lock path, ...) without its test-only hooks. */
export type FetchOptions = Omit<FetchV1Options, 'beforeIndexRename' | 'httpLog' | 'clock'>;

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
  return fetchSearchDirs(options);
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

export class Registry {
  readonly #fetch: FetchOptions;
  readonly #autofetch: boolean;
  readonly #memo = new Map<string, Promise<Library>>();
  readonly #opened: Library[] = [];

  private constructor(options: RegistryOptions) {
    this.#fetch = withEnvironment(options.fetch ?? {});
    this.#autofetch = options.autofetch ?? envFlag(ENV_AUTOFETCH_NAME);
  }

  /** Construct a registry. It opens nothing, except each `preload` request, in list order. A pinning fetch option is refused here, as a `UsageError` (rule r6). */
  static async open(options: RegistryOptions = {}): Promise<Registry> {
    try {
      refusePinning(options.fetch ?? {});
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
    const platform: PlatformKey | undefined = this.#fetch.platform ?? hostPlatformKey(os.platform(), os.arch());
    if (platform === undefined) {
      throw new ArtifactMissingError(`chtypes: no artifact is published for this host (${os.platform()}-${os.arch()})`);
    }
    let resolved: Resolved | undefined;
    try {
      resolved = await resolveInstalled(request, platform, this.#fetch);
      if (resolved === undefined && mayFetch) resolved = await ensure(request, this.#fetch);
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
      throw new LoaderCorruptError({
        reason: 'build_info_mismatch:clickhouse_version',
        path: resolved.libraryPath,
        want: request,
        got: library.version,
      });
    }
    return library;
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
