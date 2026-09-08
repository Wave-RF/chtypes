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
 */

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { RegistryError } from './errors.js';
import { NativeLibrary } from './ffi.js';
import { Library, minorOf } from './library.js';

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
  /** The resolved registry directory that was scanned. */
  readonly dir: string;
  private readonly byId = new Map<string, Library>();
  private readonly loaded: Library[] = [];

  /**
   * Scan and load a registry directory.
   *
   * @param dir - the registry root; defaults to `CHTYPES_REGISTRY`, then the
   *   per-user artifact cache (see `resolveRegistryDir` / `defaultRegistryDir`).
   * @param options - timezone and checksum verification — see `RegistryOptions`.
   * @throws {RegistryError} when no registry can be found, the directory
   *   cannot be read, holds no loadable artifact, an artifact fails its
   *   checksum, fails to load, or reports a ClickHouse version different from
   *   its manifest's.
   * @throws {ChtypesError} when an artifact reports a different nonzero ABI
   *   revision than this binding speaks, or `chs_init` fails (e.g. an unknown
   *   timezone — the message names it).
   */
  constructor(dir?: string, options: RegistryOptions = {}) {
    const resolved = resolveRegistryDir(dir);
    if (resolved === null) {
      throw new RegistryError(
        'chtypes: no artifact registry found. Pass one to new Registry(dir), set ' +
          'CHTYPES_REGISTRY, or fetch artifacts with scripts/fetch.sh (see docs/artifacts.md).',
      );
    }
    this.dir = resolved;
    const timezone = options.timezone ?? 'UTC';

    let entries: string[];
    try {
      entries = readdirSync(this.dir);
    } catch (err) {
      throw new RegistryError(`chtypes: cannot read registry ${this.dir}: ${String(err)}`, { cause: err });
    }

    for (const entry of entries.sort()) {
      const sub = path.join(this.dir, entry);
      if (!isDirectory(sub)) continue;

      // A registry may legitimately hold scratch directories, and a .DS_Store is
      // not a version: a missing or unparseable manifest is skipped in silence.
      const manifest = readManifest(sub);
      if (manifest === null) continue;

      const libPath = path.join(sub, manifest.library);
      if (options.verifyChecksums) verifyChecksum(libPath, manifest);

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
      native.init(timezone, readUnsafeFamilies(sub, manifest));

      const library = new Library(native);
      this.loaded.push(library);
      // Indexed under both spellings: docker tags drift, and an exact-match-only
      // lookup silently loses a whole version column.
      this.byId.set(library.version, library);
      this.byId.set(library.minor, library);
    }

    if (this.loaded.length === 0) {
      throw new RegistryError(`chtypes: no version artifacts under ${this.dir}`);
    }
    // Release order, not scan order: the directory listing is lexical, which
    // put 25.10 before 25.8 (spec/bindings.md §Version selection, rule 2 —
    // every ordered surface uses numeric release order; fixed 2026-08-26).
    this.loaded.sort((a, b) => compareMinor(a.minor, b.minor));
  }

  /** The ClickHouse minor lines this registry can answer for, oldest first. */
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
   * Failure is an error naming what *is* loaded, never a fallback to the nearest
   * version: answering 26.7 semantics from a 25.8 artifact is a lie, and silent
   * wrongness is what the rigs score hardest.
   *
   * @param version - a minor line (`"25.8"`) or an exact patch
   *   (`"25.8.28.1-lts"`), e.g. what `parseVersionResult` discovered.
   * @returns the loaded `Library` for that version.
   * @throws {RegistryError} when no loaded artifact matches; the message names
   *   the versions that ARE loaded.
   */
  for(version: string): Library {
    const exact = this.byId.get(version);
    if (exact !== undefined) return exact;
    const line = this.byId.get(minorOf(version));
    if (line !== undefined) return line;
    throw new RegistryError(
      `chtypes: no vendored build for ClickHouse ${version} (have ${this.versions().join(', ')})`,
    );
  }

  /** True when this registry can answer for a version. */
  has(version: string): boolean {
    return this.byId.has(version) || this.byId.has(minorOf(version));
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
 * the per-user artifact cache (`defaultRegistryDir`). An explicit path and the
 * environment variable are returned as given — a wrong one must produce an error
 * naming it, not a silent fallback — while the package-relative guess only counts
 * if it actually holds artifacts.
 */
export function resolveRegistryDir(explicit?: string): string | null {
  if (explicit !== undefined && explicit !== '') return path.resolve(explicit);
  const fromEnv = process.env['CHTYPES_REGISTRY'];
  if (fromEnv !== undefined && fromEnv !== '') return path.resolve(fromEnv);
  const fallback = defaultRegistryDir();
  return looksLikeRegistry(fallback) ? fallback : null;
}

/**
 * The per-user artifact cache for this host —
 * `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>`, `<arch>` spelled
 * the artifact way (`amd64`, `arm64`). Where `scripts/fetch.sh` installs, where a
 * core-repository build lands, and what every SDK's tests and playgrounds fall
 * back to when `CHTYPES_REGISTRY` is unset — one directory the four SDKs agree
 * on. A path, not a promise: `Registry` still throws if nothing is there.
 */
export function defaultRegistryDir(): string {
  const base =
    process.env['XDG_CACHE_HOME'] !== undefined && process.env['XDG_CACHE_HOME'] !== ''
      ? process.env['XDG_CACHE_HOME']
      : path.join(os.homedir(), '.cache');
  const arch = ({ x64: 'amd64', arm64: 'arm64' } as Record<string, string>)[os.arch()] ?? os.arch();
  return path.join(base, 'chtypes', 'artifacts', `${os.platform()}-${arch}`);
}

/** Does this directory hold at least one artifact with a usable manifest? */
export function looksLikeRegistry(dir: string): boolean {
  if (!isDirectory(dir)) return false;
  try {
    return readdirSync(dir).some((entry) => {
      const sub = path.join(dir, entry);
      if (!isDirectory(sub)) return false;
      const manifest = readManifest(sub);
      return manifest !== null && existsSync(path.join(sub, manifest.library));
    });
  } catch {
    return false;
  }
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
