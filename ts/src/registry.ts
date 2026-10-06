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
 * Construction opens nothing: `Registry.open` is asynchronous only because the
 * fetch layer is, and nothing opens an artifact except a request for a version
 * or `preload`. The binding orders and matches no versions itself: which
 * installed build a floating request means is the fetch layer's.
 */

import os from 'node:os';
import { openAbi1 } from './abi1/index.js';
import { usageError } from './abi1/index.js';
import { type Library, libraryOf } from './library.js';
import {
  ArtifactMissingError,
  ensure,
  type FetchV1Options,
  hostPlatformKey,
  listInstalled,
  type PlatformKey,
  type Resolved,
  resolveInstalled,
} from './ocifetch/index.js';
import { ENV_AUTOFETCH_NAME, SPELLING_REGEX } from './ocifetch/constants.gen.js';
import { withEnvironment } from './env.js';
import { commitSetup, latchSetup, settleFailedOpen, setupGeneration } from './setup.js';

/** The fetch layer's options (bases, cache directory, system directories, trusted keys, token, allow-unsigned, offline, frozen, lock path, ...) without its test-only hooks. */
export type FetchOptions = Omit<FetchV1Options, 'beforeIndexRename' | 'httpLog' | 'clock'>;

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

  /** Construct a registry. It opens nothing, except each `preload` request, in list order. */
  static async open(options: RegistryOptions = {}): Promise<Registry> {
    const registry = new Registry(options);
    for (const request of options.preload ?? []) {
      await registry.#memoized(request, false);
    }
    return registry;
  }

  /** Open a version: the installed build the request names, fetched first when autofetch is on and none is installed. */
  async for(request: string): Promise<Library> {
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
    // A failed open clears the setup record while no image has completed load step 7, whatever failed: the spelling, the fetch, the signature or any load step (bindings-v1.md §6, rule 4).
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
    checkSpelling(request); // a refused spelling is a failed open too, settled by #memoized
    const platform: PlatformKey | undefined = this.#fetch.platform ?? hostPlatformKey(os.platform(), os.arch());
    if (platform === undefined) {
      throw new ArtifactMissingError(`chtypes: no v1 artifact is published for this host (${os.platform()}-${os.arch()})`);
    }
    let resolved = await resolveInstalled(request, platform, this.#fetch);
    if (resolved === undefined) {
      if (!mayFetch) {
        throw new ArtifactMissingError(
          `chtypes: ${request} (${platform}) is not installed. Fetch it first, or turn autofetch on (the autofetch option, or ${ENV_AUTOFETCH_NAME}=1)`,
        );
      }
      resolved = await ensure(request, this.#fetch);
    }
    const setup = commitSetup();
    // The adapter: the predicate goes through exactly as the fetch layer returned it.
    const image = openAbi1({
      libraryPath: resolved.libraryPath,
      predicate: resolved.predicate,
      platform: resolved.platform,
      timezone: setup.timezone,
      defaults: setup.defaults,
    });
    latchSetup();
    return libraryOf(image, resolved);
  }
}
