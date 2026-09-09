/**
 * The artifact-directory loader: one subdirectory per ClickHouse minor line,
 * each self-contained (spec/artifact.md).
 *
 *   <registry>/25.8/{manifest.json, libchtypes.dylib, CH_VERSION, unsafe_families.txt}
 *
 * Two rules here were paid for and are not negotiable:
 *
 *  - **The shared library's file name comes from `manifest.json`'s `library`
 *    field.** Not from a hard-coded `libchtypes.so`, not from a glob, not from
 *    the platform. The current tree proves why: every darwin artifact says
 *    `libchtypes.dylib` while the *shipping* linux artifacts still say
 *    `libchtypes_s1.so`, so a loader that constructs the name finds nothing on
 *    the platform that matters.
 *  - **Each artifact is loaded into its own symbol scope (`RTLD_LOCAL`).** That
 *    is the entire mechanism by which two builds that both define
 *    `DB::DataTypeFactory` live in one process. ffi-rs loads through libloading,
 *    which uses `RTLD_LAZY | RTLD_LOCAL`; `assertLocalSymbolScope()` in the test
 *    suite proves it from outside rather than trusting the claim.
 *
 * Where a registry IS follows the search path of docs/fetch.md §1 (`paths.ts`):
 * the explicit directory, `CHTYPES_REGISTRY`, the per-user cache, then the
 * reserved system locations. The first directory holding artifacts is scanned
 * and loaded at construction; a line it lacks is taken, on request, from the
 * first later directory that has it. A line no directory has is the one §7
 * error, `ArtifactMissingError` — or, with `autofetch`, a fetch on first
 * `open()`.
 */

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { ArtifactMissingError, FETCH_COMMAND, RegistryError } from './errors.js';
import { ensure, type EnsureOptions } from './fetch.js';
import { NativeLibrary } from './ffi.js';
import { Library, minorOf } from './library.js';
import { cacheRegistryDir, fetchDestination, hostPlatform, registrySearchPath, systemRegistryDirs } from './paths.js';

/** The nine fields `lib/build.sh` writes. Unknown fields are ignored. */
export interface Manifest {
  library: string;
  library_bytes?: number;
  library_sha256?: string;
  clickhouse_version?: string;
  clickhouse_minor?: string;
  clickhouse_commit?: string;
  os?: string;
  arch?: string;
  unsafe_families?: string;
}

/** Options for `new Registry(dir, options)`. */
export interface RegistryOptions {
  /**
   * The server timezone assumed for bare DateTime / DateTime64 columns. Defaults
   * to UTC — what a stock ClickHouse container uses — and is deliberately not
   * read from the environment, so the host's TZ cannot leak into results.
   */
  timezone?: string;
  /**
   * Re-hash each library and compare against `manifest.library_sha256` before
   * loading. SHOULD be on for an artifact that came from anywhere but a local
   * build, and MUST be on for one that came over a network.
   */
  verifyChecksums?: boolean;
  /**
   * Lazy fetch on first open (docs/fetch.md §6): `open()` of a line no
   * directory on the search path holds runs `ensure()` first, into the
   * directory a fetch writes to (§1), once per process per line. Off by
   * default — a production process must not begin a 250 MB download inside
   * a request — and `CHTYPES_AUTOFETCH=1` turns it on from the environment.
   * `for()` stays synchronous and never fetches.
   */
  autofetch?: boolean | undefined;
  /** Options handed to `ensure()` by autofetch: source (`url`/`tag`), lock, keys, progress. */
  fetch?: EnsureOptions | undefined;
}

/**
 * The artifact-directory loader — the multi-version entry point of this
 * package. Construction scans one subdirectory per ClickHouse version, dlopens
 * each artifact into its own symbol scope (`RTLD_LOCAL`, ~120 MB resident per
 * version), verifies its ABI revision against this binding's `ABI_REVISION`
 * (a different nonzero revision is refused; 0 means the artifact predates the
 * probe and degrades per symbol), and runs each library's one-time
 * `chs_init` with the UTC default timezone and the artifact's own
 * `unsafe_families.txt`.
 *
 * Libraries are never dlclosed; `close()` joins background threads only.
 * Loading the same artifact path from two `Registry` instances shares one
 * loaded image, so `chs_init` still runs exactly once per artifact.
 */
export class Registry {
  /**
   * The primary registry directory: the first on the search path that held
   * artifacts, scanned and loaded at construction — or, with `autofetch` and
   * nothing installed anywhere, the directory the first fetch will create.
   */
  readonly dir: string;
  /** The §1 search path, in order, every candidate whether or not it exists. */
  readonly searchPath: readonly string[];
  /** This host's platform key, e.g. `darwin-arm64` — the only artifacts a process can dlopen. */
  readonly platform: string;

  private readonly byId = new Map<string, Library>();
  private readonly byPath = new Map<string, Library>();
  private readonly loaded: Library[] = [];
  private readonly timezone: string;
  private readonly verifyChecksums: boolean;
  private readonly autofetch: boolean;
  private readonly fetchOptions: EnsureOptions;
  private readonly explicit: string | undefined;

  /**
   * Scan and load a registry.
   *
   * @param dir - the registry root; the head of the search path. Absent, the
   *   path is `CHTYPES_REGISTRY`, the per-user artifact cache, then the system
   *   locations (see `registrySearchPath` / `defaultRegistryDir`).
   * @param options - timezone, checksum verification, autofetch — see `RegistryOptions`.
   * @throws {RegistryError} when a directory named explicitly (the argument or
   *   `CHTYPES_REGISTRY`) does not exist and autofetch is off, when no
   *   directory on the search path holds an artifact (and autofetch is off),
   *   the primary directory cannot be read, an artifact fails its checksum,
   *   fails to load, or reports a ClickHouse version different from its
   *   manifest's.
   * @throws {ChtypesError} when an artifact reports a different nonzero ABI
   *   revision than this binding speaks, or `chs_init` fails (e.g. an unknown
   *   timezone — the message names it).
   */
  constructor(dir?: string, options: RegistryOptions = {}) {
    this.platform = hostPlatform();
    this.timezone = options.timezone ?? 'UTC';
    this.verifyChecksums = options.verifyChecksums ?? false;
    this.autofetch = options.autofetch ?? envFlag('CHTYPES_AUTOFETCH');
    this.fetchOptions = options.fetch ?? {};
    this.explicit = dir !== undefined && dir !== '' ? path.resolve(dir) : undefined;
    this.searchPath = registrySearchPath(dir, this.platform);

    // A directory somebody NAMED and that does not exist is a configuration
    // mistake and must be an error naming it, never a silent fallback — unless
    // autofetch is on, in which case it is the destination the first fetch creates.
    const fromEnv = process.env['CHTYPES_REGISTRY'];
    const named = [this.explicit, fromEnv !== undefined && fromEnv !== '' ? path.resolve(fromEnv) : undefined];
    for (const d of named) {
      if (d !== undefined && !isDirectory(d) && !this.autofetch) {
        throw new RegistryError(`chtypes: cannot read registry ${d}: not a directory`);
      }
    }

    const primary = this.searchPath.find((d) => looksLikeRegistry(d));
    if (primary === undefined) {
      if (!this.autofetch) {
        throw new RegistryError(
          `chtypes: no artifacts in any registry directory. Looked in: ${this.searchPath.join(', ')}.\n` +
            `Install one:  ${FETCH_COMMAND} <line>\n` +
            'or set CHTYPES_AUTOFETCH=1 to fetch on first use.',
        );
      }
      this.dir = fetchDestination(this.explicit, this.platform);
      return;
    }
    this.dir = primary;

    let entries: string[];
    try {
      entries = readdirSync(this.dir);
    } catch (err) {
      throw new RegistryError(`chtypes: cannot read registry ${this.dir}: ${String(err)}`, { cause: err });
    }
    for (const entry of entries.sort()) {
      // A dot-directory is never a version: fetch stages its downloads and
      // unpacks in hidden siblings, and a .DS_Store is not a version either.
      if (entry.startsWith('.')) continue;
      const sub = path.join(this.dir, entry);
      if (!isDirectory(sub)) continue;
      // A registry may legitimately hold scratch directories: a missing or
      // unparseable manifest is skipped in silence.
      const manifest = readManifest(sub);
      if (manifest === null) continue;
      this.load(sub, manifest);
    }
    if (this.loaded.length === 0) {
      throw new RegistryError(`chtypes: no version artifacts under ${this.dir}`);
    }
  }

  /** dlopen one artifact directory, cross-check it, `chs_init` it, index it. */
  private load(sub: string, manifest: Manifest): Library {
    const already = this.byPath.get(sub);
    if (already !== undefined) return already;

    const libPath = path.join(sub, manifest.library);
    if (this.verifyChecksums) verifyChecksum(libPath, manifest);

    // A directory that has a manifest and does not load is broken, not absent.
    let native: NativeLibrary;
    try {
      native = NativeLibrary.open(libPath);
    } catch (err) {
      throw new RegistryError(`chtypes: cannot load ${libPath}: ${String(err)}`, { cause: err });
    }

    // The right bytes in the wrong directory is the one corruption a hash
    // cannot catch, so cross-check what the library says about itself.
    const declared = manifest.clickhouse_version;
    if (declared !== undefined && declared !== '' && declared !== native.version) {
      throw new RegistryError(
        `chtypes: ${libPath} reports ClickHouse ${native.version} but its manifest says ${declared}`,
      );
    }

    // Each library keeps its own DateLUT and its own refuse-list. An absent or
    // empty unsafe_families.txt is a valid empty list, not a missing file.
    native.init(this.timezone, readUnsafeFamilies(sub, manifest));

    const library = new Library(native);
    this.loaded.push(library);
    // Release order, not scan order: the directory listing is lexical, which
    // put 25.10 before 25.8 (spec/bindings.md §Version selection, rule 2 —
    // every ordered surface uses numeric release order; fixed 2026-08-26).
    this.loaded.sort((a, b) => compareMinor(a.minor, b.minor));
    this.byPath.set(sub, library);
    // Indexed under both spellings: docker tags drift, and an exact-match-only
    // lookup silently loses a whole version column. First directory wins: a
    // line already loaded from earlier on the search path is not displaced.
    if (!this.byId.has(library.version)) this.byId.set(library.version, library);
    if (!this.byId.has(library.minor)) this.byId.set(library.minor, library);
    return library;
  }

  /** Load `<dir>/<minor>/` if it is an artifact directory; null when it is not. */
  private loadLine(dir: string, minor: string): Library | null {
    const sub = path.join(dir, minor);
    if (!isDirectory(sub)) return null;
    const manifest = readManifest(sub);
    if (manifest === null) return null;
    return this.load(sub, manifest);
  }

  private lookup(version: string): Library | undefined {
    return this.byId.get(version) ?? this.byId.get(minorOf(version));
  }

  /** The ClickHouse minor lines this registry has loaded, oldest first. */
  versions(): string[] {
    return [...new Set(this.loaded.map((l) => l.minor))].sort(compareMinor);
  }

  /** Every loaded library, in release order (oldest minor line first). */
  libraries(): readonly Library[] {
    return this.loaded;
  }

  /**
   * Resolve a version to its library. A minor line ("25.8") or an exact patch
   * ("25.8.28.1-lts") both work, and an unknown patch inside a loaded minor line
   * resolves to that line — asking for "25.8.30.16" finds the loaded 25.8.
   *
   * A line the primary directory lacks is loaded from the first later
   * directory on the search path that holds it (docs/fetch.md §1), and joins
   * `versions()` / `libraries()` from then on. Never a fetch: this call is
   * synchronous; `open()` is the one that may fetch.
   *
   * Failure is the one §7 error, never a fallback to the nearest version:
   * answering 26.7 semantics from a 25.8 artifact is a lie, and silent
   * wrongness is what the rigs score hardest.
   *
   * @param version - a minor line (`"25.8"`) or an exact patch
   *   (`"25.8.28.1-lts"`), e.g. what `parseVersionResult` discovered.
   * @returns the loaded `Library` for that version.
   * @throws {ArtifactMissingError} (`code` `CHTYPES_ARTIFACT_MISSING`, a
   *   `RegistryError`) when no directory on the search path holds the line;
   *   the message names every directory looked in and the fetch command.
   * @throws {RegistryError} when a directory holds the line but it does not load.
   */
  for(version: string): Library {
    const hit = this.lookup(version);
    if (hit !== undefined) return hit;
    const minor = minorOf(version);
    for (const dir of this.searchPath) {
      if (this.loadLine(dir, minor) !== null) {
        const found = this.lookup(version);
        if (found !== undefined) return found;
      }
    }
    throw new ArtifactMissingError(minor, this.platform, this.searchPath);
  }

  /**
   * `for()`, with the lazy fetch of docs/fetch.md §6 in front of it: a line no
   * directory on the search path holds is fetched through `ensure()` — into
   * the directory a fetch writes to (§1), verified, once per process per line
   * even under concurrent opens — and then loaded. With `autofetch` off (the
   * default) this is `for()` behind a promise, and a missing line rejects
   * with the same `ArtifactMissingError`.
   *
   * @throws {ArtifactMissingError} when the line is missing and autofetch is off.
   * @throws {FetchError} the §7 fetch verdicts (`CHTYPES_ARTIFACT_UNTRUSTED`,
   *   `…_CORRUPT`, `…_PINNED`, `…_UNPUBLISHED`, `CHTYPES_SOURCE_UNREACHABLE`).
   * @throws {RegistryError} when the fetched artifact does not load.
   */
  async open(version: string): Promise<Library> {
    try {
      return this.for(version);
    } catch (err) {
      if (!(err instanceof ArtifactMissingError) || !this.autofetch) throw err;
    }
    const minor = minorOf(version);
    const dest = this.fetchOptions.dest ?? fetchDestination(this.explicit, this.platform);
    const result = await ensure(minor, { ...this.fetchOptions, dest, platform: this.platform });
    this.loadLine(result.registry, result.line);
    return this.for(version);
  }

  /**
   * True when this registry can answer for a version: loaded already, or held
   * by a directory on the search path (which `for()` would load). Never a
   * fetch, and never a load.
   */
  has(version: string): boolean {
    if (this.lookup(version) !== undefined) return true;
    const minor = minorOf(version);
    return this.searchPath.some((dir) => {
      const sub = path.join(dir, minor);
      const manifest = readManifest(sub);
      return manifest !== null && existsSync(path.join(sub, manifest.library));
    });
  }

  /**
   * Join every loaded library's background threads. `chs_init` registers
   * `chs_shutdown` with `atexit`, so an ordinary process needs no call; a test
   * that must not depend on `atexit` should make one.
   */
  close(): void {
    for (const library of this.loaded) library.shutdown();
  }

  /** `using registry = new Registry(...)` closes it at scope exit. */
  [Symbol.dispose](): void {
    this.close();
  }
}

/** Numeric ordering: 25.10 is a later minor than 25.8, whatever strings say. */
export function compareMinor(a: string, b: string): number {
  const pa = a.split('.').map((p) => Number.parseInt(p, 10));
  const pb = b.split('.').map((p) => Number.parseInt(p, 10));
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? -1;
    const y = pb[i] ?? -1;
    if (Number.isNaN(x) || Number.isNaN(y)) return a < b ? -1 : a > b ? 1 : 0;
    if (x !== y) return x - y;
  }
  return 0;
}

/**
 * Where the registry is: the explicit argument, else `CHTYPES_REGISTRY`, else
 * the first of the per-user artifact cache and the system locations that
 * holds artifacts (docs/fetch.md §1). An explicit path and the environment
 * variable are returned as given — a wrong one must produce an error naming
 * it, not a silent fallback — while the unnamed candidates only count if they
 * actually hold artifacts.
 */
export function resolveRegistryDir(explicit?: string): string | null {
  if (explicit !== undefined && explicit !== '') return path.resolve(explicit);
  const fromEnv = process.env['CHTYPES_REGISTRY'];
  if (fromEnv !== undefined && fromEnv !== '') return path.resolve(fromEnv);
  const platform = hostPlatform();
  for (const candidate of [cacheRegistryDir(platform), ...systemRegistryDirs(platform)]) {
    if (looksLikeRegistry(candidate)) return candidate;
  }
  return null;
}

/**
 * The per-user artifact cache for this host —
 * `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>`, `<arch>` spelled
 * the artifact way (`amd64`, `arm64`). Where `chtypes fetch` and
 * `scripts/fetch.sh` install, where a core-repository build lands, and what
 * every SDK's tests and playgrounds fall back to when `CHTYPES_REGISTRY` is
 * unset — one directory the four SDKs agree on. A path, not a promise:
 * `Registry` still throws if nothing is there.
 */
export function defaultRegistryDir(): string {
  return cacheRegistryDir(hostPlatform());
}

/** Does this directory hold at least one artifact with a usable manifest? */
export function looksLikeRegistry(dir: string): boolean {
  if (!isDirectory(dir)) return false;
  try {
    return readdirSync(dir).some((entry) => {
      if (entry.startsWith('.')) return false;
      const sub = path.join(dir, entry);
      if (!isDirectory(sub)) return false;
      const manifest = readManifest(sub);
      return manifest !== null && existsSync(path.join(sub, manifest.library));
    });
  } catch {
    return false;
  }
}

function envFlag(name: string): boolean {
  const v = process.env[name];
  return v !== undefined && ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
}

function isDirectory(p: string): boolean {
  try {
    // statSync follows symlinks on purpose: a registry is often a symlink
    // into ~/.cache/chtypes, which is where the artifacts actually live.
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function readManifest(dir: string): Manifest | null {
  try {
    const parsed = JSON.parse(readFileSync(path.join(dir, 'manifest.json'), 'utf8')) as Partial<Manifest>;
    if (typeof parsed.library !== 'string' || parsed.library === '') return null;
    return parsed as Manifest;
  } catch {
    return null;
  }
}

function readUnsafeFamilies(dir: string, manifest: Manifest): string {
  try {
    return readFileSync(path.join(dir, 'unsafe_families.txt'), 'utf8').trim();
  } catch {
    return (manifest.unsafe_families ?? '').trim();
  }
}

function verifyChecksum(libPath: string, manifest: Manifest): void {
  const expected = manifest.library_sha256;
  if (expected === undefined || expected === '') {
    throw new RegistryError(`chtypes: ${libPath} cannot be verified: its manifest carries no library_sha256`);
  }
  const bytes = readFileSync(libPath);
  if (manifest.library_bytes !== undefined && bytes.byteLength !== manifest.library_bytes) {
    throw new RegistryError(
      `chtypes: ${libPath} is ${bytes.byteLength} bytes, manifest says ${manifest.library_bytes}`,
    );
  }
  const actual = createHash('sha256').update(bytes).digest('hex');
  if (actual !== expected) {
    throw new RegistryError(`chtypes: ${libPath} sha256 ${actual} does not match manifest ${expected}`);
  }
}
