/**
 * The artifact-directory loader: one subdirectory per ClickHouse minor line,
 * each self-contained (docs/reference/artifact.md), plus — since #284 — every
 * OTHER installed exact patch of a line, in a sibling `patches/` tree:
 *
 *   <registry>/25.8/{manifest.json, libchtypes.dylib, CH_VERSION, unsafe_families.txt}
 *   <registry>/patches/25.8/25.8.28.1-lts/{manifest.json, libchtypes.dylib, ...}
 *
 * The flat `<registry>/<minor>/` slot is what a LINE request resolves to —
 * exactly the pre-#284 layout, so every SDK through 0.4.x keeps reading it
 * unchanged. Any OTHER exact patch of that line lives in
 * `<registry>/patches/<minor>/<clickhouse_version>/`, which no released SDK
 * (0.4.x and earlier) scans, fetches into or deletes (measured, issue #284
 * comment 5919199794): it is a new, additive tree, not a migration of the old
 * one. When a line fetch changes which patch occupies the flat slot, the
 * outgoing install is DEMOTED — an atomic, same-filesystem rename into
 * `patches/<minor>/<its version>/` — never deleted, so a server still on the
 * older patch keeps its exact match with no re-fetch.
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
 *    `DB::DataTypeFactory` live in one process — now routinely two builds of
 *    the SAME minor line, one per patch. ffi-rs loads through libloading,
 *    which uses `RTLD_LAZY | RTLD_LOCAL`; `assertLocalSymbolScope()` in the
 *    test suite proves it from outside rather than trusting the claim.
 *
 * Where a registry IS follows the search path of docs/guides/fetch.md §1 (`paths.ts`):
 * the explicit directory, `CHTYPES_REGISTRY`, the per-user cache, then the
 * reserved system locations. Construction READS THE MANIFESTS on that path and
 * `dlopen`s nothing; a version is taken, on request, from the first directory
 * that has it. Resolution (docs/reference/bindings.md §Version selection):
 *
 *  - **A line request** ("25.8") never falls back and never crosses lines: the
 *    newest patch of the line, in the first search-path directory that holds
 *    any patch of it (a nested install beating a flat one on a version tie),
 *    pinned for this registry for as long as it runs.
 *  - **A patch request** ("25.8.28.1-lts") loads that exact patch when it is
 *    open or installed anywhere on the search path. Otherwise it falls back to
 *    the newest installed patch of the same line, flags the result
 *    `exact: false`, and warns once per (requested, actual) pair per process —
 *    never another line, which stays the one §7 error, `ArtifactMissingError`.
 *
 * A patch spelled with no channel suffix matches that patch on any channel
 * (docs/guides/fetch.md Decision 7); ordering is numeric, channel ignored.
 */

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { ABI_REVISION, ArtifactMissingError, ArtifactUnpublishedError, FETCH_COMMAND, RegistryError } from './errors.js';
import { compareVersions, type EnsureOptions, ensure, parseVersionSpelling, patchMatches, type VersionRequest } from './fetch.js';
import { NativeLibrary } from './ffi.js';
import { Library, minorOf } from './library.js';
import {
  cacheRegistryDir,
  ENV_AUTOFETCH,
  ENV_REGISTRY,
  fetchDestination,
  hostPlatform,
  registrySearchPath,
  systemRegistryDirs,
} from './paths.js';

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
   *
   * A policy on the REGISTRY, not a property of `preload`: a library's
   * checksum is computed immediately before that library is `dlopen`ed and at
   * no other time — at construction for the preloaded lines, at first use for
   * the rest, never for a line nobody asks for.
   *
   * What this option ADDS is the sha256 comparison. The cheaper
   * `manifest.library_bytes` size check that runs just before it is NOT
   * conditional on this option — `load()` below always makes it, on every
   * open, regardless (issue #82). Its timing is the same as the checksum's:
   * at construction for the preloaded lines, at first use for the rest.
   */
  verifyChecksums?: boolean;
  /**
   * Open these lines AT CONSTRUCTION — the one eager path, and the same option
   * Go spells `WithPreload(...)`, Python `preload=[...]` and Rust
   * `RegistryOptions::preload`.
   *
   * Each entry is a version spelling resolved exactly as `for()` resolves one:
   * a minor line (`'25.8'`) or an exact patch (`'25.8.28.1-lts'`), never a
   * path. They are opened in the order given, before the constructor returns,
   * and an entry no directory on the §1 search path holds is
   * `ArtifactMissingError` — the same §7 error the first `for()` would have
   * thrown, thrown earlier. A patch entry that falls back to its line warns at
   * construction, exactly as `for()` would.
   *
   * It NEVER fetches, even with `autofetch` on: this constructor is
   * synchronous and `ensure()` is not, and autofetch is a first-use behavior
   * in all four bindings. An empty list is exactly the default.
   *
   * Deliberately a list of lines rather than "everything in the directory": a
   * registry directory is whatever a fetch left behind, and each open costs
   * about 120 MB resident.
   */
  preload?: readonly string[] | undefined;
  /**
   * Lazy fetch on first open (docs/guides/fetch.md §6): `open()` of a version no
   * directory on the search path holds runs `ensure()` first, into the
   * directory a fetch writes to (§1), once per process per (destination,
   * spelling). Off by default — a production process must not begin a 250 MB
   * download inside a request — and `CHTYPES_AUTOFETCH=1` turns it on from the
   * environment. `for()` / `resolve()` stay synchronous and never fetch.
   */
  autofetch?: boolean | undefined;
  /** Options handed to `ensure()` by autofetch: source (`url`/`tag`), lock, keys, progress. */
  fetch?: EnsureOptions | undefined;
}

/**
 * What a `resolve()` / `openResolution()` call hands back: the loaded
 * `Library`, the caller's own spelling, what actually loaded, and whether the
 * two are the same patch (docs/reference/bindings.md §Version selection).
 * `for()` / `open()` run the same resolution and hand back only `.library`.
 */
export interface Resolution {
  /** The `Library` this resolution loaded (or found already loaded). */
  readonly library: Library;
  /** The caller's own spelling, trimmed — never normalized further. */
  readonly requested: string;
  /**
   * What actually loaded, i.e. `library.version`. Equal to the requested
   * patch when `exact` is true; the newest installed/published patch of the
   * requested line otherwise.
   */
  readonly version: string;
  /**
   * `true` for a line request — it asked for "a patch of this line" and got
   * one. For a patch request, `true` only when the loaded version matches the
   * requested patch (a spelled channel matches only itself; an unspelled one
   * matches on any channel). `false` means the fallback within the line was
   * taken, and a warning was already issued for this (requested, actual) pair
   * — once per pair per process.
   */
  readonly exact: boolean;
}

/** One patch this registry found on disk: which directory, which slot, from which search-path root. */
interface PatchLocation {
  readonly minor: string;
  readonly version: string;
  readonly dir: string;
  /** `true`: the flat `<root>/<minor>/` slot. `false`: `<root>/patches/<minor>/<version>/`. */
  readonly flat: boolean;
  /** Index into the registry's search path — lower sorts first. */
  readonly rootIndex: number;
}

interface Resolved {
  readonly library: Library;
  readonly actual: string;
  readonly exact: boolean;
}

/** Every patch (flat + `patches/`) a search-path root holds, whatever line it claims. */
function flatLocationsInRoot(root: string, rootIndex: number): PatchLocation[] {
  const out: PatchLocation[] = [];
  if (!isDirectory(root)) return out;
  let entries: string[];
  try {
    entries = readdirSync(root);
  } catch {
    return out;
  }
  for (const entry of entries.sort()) {
    // A dot-directory is never a version (fetch stages downloads in hidden
    // siblings), and `patches/` is the sibling tree, never a line itself.
    if (entry.startsWith('.') || entry === 'patches') continue;
    const sub = path.join(root, entry);
    if (!isDirectory(sub)) continue;
    const manifest = readManifest(sub);
    if (manifest === null) continue;
    const claimedMinor = manifest.clickhouse_minor ?? minorOf(manifest.clickhouse_version ?? entry);
    const minor = claimedMinor !== '' ? claimedMinor : entry;
    const version = manifest.clickhouse_version !== undefined && manifest.clickhouse_version !== '' ? manifest.clickhouse_version : entry;
    out.push({ minor, version, dir: sub, flat: true, rootIndex });
  }
  return out;
}

/** Every patch under `<root>/patches/*\/*\/`. */
function nestedLocationsInRoot(root: string, rootIndex: number): PatchLocation[] {
  const out: PatchLocation[] = [];
  const patchesRoot = path.join(root, 'patches');
  if (!isDirectory(patchesRoot)) return out;
  let minorEntries: string[];
  try {
    minorEntries = readdirSync(patchesRoot);
  } catch {
    return out;
  }
  for (const minorEntry of minorEntries.sort()) {
    if (minorEntry.startsWith('.')) continue;
    const minorDir = path.join(patchesRoot, minorEntry);
    if (!isDirectory(minorDir)) continue;
    let versionEntries: string[];
    try {
      versionEntries = readdirSync(minorDir);
    } catch {
      continue;
    }
    for (const versionEntry of versionEntries.sort()) {
      if (versionEntry.startsWith('.')) continue;
      const sub = path.join(minorDir, versionEntry);
      if (!isDirectory(sub)) continue;
      const manifest = readManifest(sub);
      if (manifest === null) continue;
      const claimedMinor = manifest.clickhouse_minor ?? minorOf(manifest.clickhouse_version ?? minorEntry);
      const minor = claimedMinor !== '' ? claimedMinor : minorEntry;
      const version = manifest.clickhouse_version !== undefined && manifest.clickhouse_version !== '' ? manifest.clickhouse_version : versionEntry;
      out.push({ minor, version, dir: sub, flat: false, rootIndex });
    }
  }
  return out;
}

/** Every patch (both slots) a root holds. */
function locationsInRoot(root: string, rootIndex: number): PatchLocation[] {
  return [...flatLocationsInRoot(root, rootIndex), ...nestedLocationsInRoot(root, rootIndex)];
}

/** R3/R4: the newest patch, among locations from the FIRST root that has any — ties: nested beats flat. */
function pickLineWinner(locs: readonly PatchLocation[]): PatchLocation | undefined {
  if (locs.length === 0) return undefined;
  const firstRoot = Math.min(...locs.map((l) => l.rootIndex));
  const candidates = locs.filter((l) => l.rootIndex === firstRoot);
  let best = candidates[0]!;
  for (const cur of candidates.slice(1)) {
    const cmp = compareVersions(cur.version, best.version);
    if (cmp > 0 || (cmp === 0 && !cur.flat && best.flat)) best = cur;
  }
  return best;
}

/** R4 step 2: the first root (in search-path order) holding a location whose version matches `req` under R2. */
function pickPatchMatch(locs: readonly PatchLocation[], req: VersionRequest): PatchLocation | undefined {
  return [...locs].sort((a, b) => a.rootIndex - b.rootIndex).find((l) => patchMatches(l.version, req));
}

/** §3: the fallback warning, once per (requested, actual) pair per process — shared by every Registry instance. */
const warnedPatchFallbacks = new Set<string>();

function emitPatchFallbackWarning(requested: string, actual: string, minor: string, platform: string, kind: 'installed' | 'published'): void {
  const key = `${requested}\u0000${actual}`;
  // Recorded BEFORE emitting: a caller's warning filter that throws must not
  // cause a retry to warn again for the same pair.
  if (warnedPatchFallbacks.has(key)) return;
  warnedPatchFallbacks.add(key);
  const body =
    kind === 'installed'
      ? `ClickHouse ${requested} is not installed for ${platform}; using ${actual}, the newest installed patch of ${minor}. ` +
        `Behavior can differ between patches. If ${requested} is published, install it with: ${FETCH_COMMAND} ${requested}`
      : `ClickHouse ${requested} is not published for ${platform} at ABI revision ${ABI_REVISION}; using ${actual}, ` +
        `the newest published patch of ${minor}. Behavior can differ between patches.`;
  process.emitWarning(`chtypes: ${body}`, { type: 'PatchFallbackWarning', code: 'CHTYPES_PATCH_FALLBACK' });
}

/** TEST-ONLY: forget every warned pair, so a suite can assert a fresh warning fires. Not re-exported from index.ts. */
export function resetPatchFallbackWarnings(): void {
  warnedPatchFallbacks.clear();
}

/**
 * R-c: re-scan the search path for a patch fallback at most this often, per
 * requested patch, per process. Caches the CHOSEN directory, not a loaded
 * `Library` — the load itself (one known path, not a directory read per
 * search-path entry) still runs on every call, so a fallback whose chosen
 * artifact is broken keeps reporting the same failure rather than being
 * silently remembered as unresolvable.
 */
const FALLBACK_RECHECK_MS = 60_000;
const fallbackCache = new Map<string, { dir: string; checkedAt: number }>();

/**
 * The artifact-directory loader — the multi-version entry point of this
 * package.
 *
 * **Construction reads `manifest.json` files and `dlopen`s nothing**, with or
 * without a directory. Nothing in this package opens an artifact except a
 * request for a specific version (`for()` / `resolve()` / `open()` /
 * `openResolution()`) or an explicit `preload` — not `versions()`, not
 * `libraries()`, not `has()`. An open costs about 120 MB resident per patch,
 * which a listing call must not spend on a caller's behalf, and which grows
 * with every DISTINCT patch a process is asked for — libraries are never
 * dlclosed (docs/guides/multi-version.md).
 *
 * An open dlopens the artifact into its own symbol scope (`RTLD_LOCAL`),
 * verifies its ABI revision against this binding's `ABI_REVISION` (a different
 * nonzero revision is refused; 0 means the artifact predates the probe and
 * degrades per symbol), and runs that library's one-time `chs_init` with the
 * registry's timezone and the artifact's own `unsafe_families.txt`.
 *
 * Libraries are never dlclosed; `close()` joins background threads only.
 * Loading the same artifact FILE from two `Registry` instances — by one path,
 * another spelling, a symlink or a hardlink — shares one loaded image, so
 * `chs_init` still runs exactly once per artifact; a second timezone for an
 * image already initialized is refused as `InitConflictError`.
 */
export class Registry {
  /**
   * The primary registry directory: the first on the search path that holds
   * artifacts — or, with `autofetch` and nothing installed anywhere, the
   * directory the first fetch will create.
   */
  readonly dir: string;
  /** The §1 search path, in order, every candidate whether or not it exists. */
  readonly searchPath: readonly string[];
  /** This host's platform key, e.g. `darwin-arm64` — the only artifacts a process can dlopen. */
  readonly platform: string;

  private readonly byPath = new Map<string, Library>();
  private readonly loaded: Library[] = [];
  /** Line -> the library currently answering FOR THE LINE. Set once per line; never re-pointed while this registry runs (R3/R8). */
  private readonly linePins = new Map<string, Library>();
  /**
   * Line -> every patch the construction-time manifest scan found for it,
   * across the whole search path. What `versions()` answers from, and what
   * makes "no artifact anywhere" and a bad `preload` entry decidable at
   * construction without a single `dlopen`. Resolution itself re-scans the
   * live filesystem on every call (a patch installed after construction, by
   * this process or another, is used from the next call on).
   */
  private readonly known = new Map<string, PatchLocation[]>();
  private readonly timezone: string;
  private readonly verifyChecksums: boolean;
  private readonly autofetch: boolean;
  private readonly fetchOptions: EnsureOptions;
  private readonly explicit: string | undefined;
  /** (dest, line) already `Ensure()`d successfully by THIS registry's autofetch — never re-read over the network again. */
  private readonly ensuredLines = new Set<string>();
  /** (dest, exact patch) autofetch has already confirmed CHTYPES_ARTIFACT_UNPUBLISHED for — never retried within this registry's lifetime. */
  private readonly unpublishedPatches = new Set<string>();

  /**
   * Scan a registry. Reads manifests; opens nothing unless `preload` names a
   * version.
   *
   * @param dir - the registry root; the head of the search path. Absent, the
   *   path is `CHTYPES_REGISTRY`, the per-user artifact cache, then the system
   *   locations (see `registrySearchPath` / `defaultRegistryDir`).
   * @param options - timezone, checksum verification, autofetch, preload — see
   *   `RegistryOptions`.
   * @throws {RegistryError} for what manifests can decide, and only that: a
   *   directory named explicitly (the argument or `CHTYPES_REGISTRY`) that does
   *   not exist, and no directory on the search path holding a readable
   *   manifest at either slot — both suppressed when autofetch is on. A
   *   `preload` entry no directory holds is `ArtifactMissingError`. Everything
   *   a bad artifact can be wrong about — a failed checksum, a load failure, a
   *   library whose ClickHouse version disagrees with its manifest — is
   *   reported by the call that opens it, which is `preload`'s open at
   *   construction or the first `for()` otherwise.
   * @throws {ChtypesError} when an artifact reports a different nonzero ABI
   *   revision than this binding speaks, or `chs_init` fails (e.g. an unknown
   *   timezone — the message names it); again, from the call that opens it.
   */
  constructor(dir?: string, options: RegistryOptions = {}) {
    this.platform = hostPlatform();
    this.timezone = options.timezone ?? 'UTC';
    this.verifyChecksums = options.verifyChecksums ?? false;
    this.autofetch = options.autofetch ?? envFlag(ENV_AUTOFETCH);
    this.fetchOptions = options.fetch ?? {};
    this.explicit = dir !== undefined && dir !== '' ? path.resolve(dir) : undefined;
    this.searchPath = registrySearchPath(dir, this.platform);

    // A directory somebody NAMED and that does not exist is a configuration
    // mistake and must be an error naming it, never a silent fallback — unless
    // autofetch is on, in which case it is the destination the first fetch creates.
    const fromEnv = process.env[ENV_REGISTRY];
    const named = [this.explicit, fromEnv !== undefined && fromEnv !== '' ? path.resolve(fromEnv) : undefined];
    for (const d of named) {
      if (d !== undefined && !isDirectory(d) && !this.autofetch) {
        throw new RegistryError(`chtypes: cannot read registry ${d}: not a directory`);
      }
    }

    // The scan: every directory on the path, in order, every patch at either
    // slot. Manifests only — this is the cheap half of what construction used
    // to do, and it is all that is left of it.
    this.searchPath.forEach((root, rootIndex) => {
      for (const loc of locationsInRoot(root, rootIndex)) {
        const arr = this.known.get(loc.minor) ?? [];
        arr.push(loc);
        this.known.set(loc.minor, arr);
      }
    });

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
    } else {
      this.dir = primary;
    }

    // The one eager path, and the only thing here that opens anything.
    for (const version of options.preload ?? []) {
      this.preloadLine(version);
    }
  }

  /**
   * Open one `preload` entry, before the constructor returns, without
   * fetching. Resolution is `for()`'s (minus any fetch), and so is the
   * failure: a version no directory holds anywhere is the same
   * `ArtifactMissingError`, raised earlier. A patch entry that falls back
   * warns here, at construction, exactly as `for()` would.
   */
  private preloadLine(version: string): void {
    if (version === '') {
      throw new RegistryError("chtypes: preload: an empty version does not mean 'pick one'");
    }
    const req = parseVersionSpelling(version);
    const resolved = this.resolveSync(req);
    if (resolved === undefined) {
      throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
    }
    // resolveSync (resolvePatchSync, for a patch entry) already warns
    // internally when it falls back — nothing further to do here.
  }

  /** dlopen one artifact directory, cross-check it, `chs_init` it, index it. */
  private load(sub: string, manifest: Manifest): Library {
    const already = this.byPath.get(sub);
    if (already !== undefined) return already;

    const libPath = path.join(sub, manifest.library);
    // Unconditional, unlike the hash below: nearly free (one stat, never a
    // re-hash of the library's contents), and it catches the commonest shape
    // of a broken artifact directory — a truncated or partially-written
    // library file (issue #82).
    checkLibraryBytes(libPath, manifest);
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
    native.init(this.timezone, readUnsafeFamilies(sub, manifest), libPath);

    const library = new Library(native);
    this.loaded.push(library);
    // Full numeric version order (docs/reference/bindings.md §Version
    // selection, rule 2): two patches of one line both sort by their own
    // exact version, never merely grouped by minor.
    this.loaded.sort((a, b) => compareVersions(a.version, b.version));
    this.byPath.set(sub, library);
    return library;
  }

  /** Load an artifact directory whose manifest is already known-readable, or return null when it is not one. */
  private loadDir(dir: string): Library | null {
    const manifest = readManifest(dir);
    if (manifest === null) return null;
    return this.load(dir, manifest);
  }

  /** Every patch (both slots, every search-path root) this registry can currently see for `minor` — a live scan. */
  private scanLine(minor: string): PatchLocation[] {
    const out: PatchLocation[] = [];
    this.searchPath.forEach((root, rootIndex) => {
      for (const loc of locationsInRoot(root, rootIndex)) {
        if (loc.minor === minor) out.push(loc);
      }
    });
    return out;
  }

  /** R3: line request — the pin if this registry already has one, else the newest patch in the first root that holds any. */
  private resolveLineSync(req: VersionRequest): Resolved | undefined {
    const pinned = this.linePins.get(req.line);
    if (pinned !== undefined) return { library: pinned, actual: pinned.version, exact: true };
    const winner = pickLineWinner(this.scanLine(req.line));
    if (winner === undefined) return undefined;
    const library = this.loadDir(winner.dir);
    if (library === null) return undefined;
    this.linePins.set(req.line, library);
    return { library, actual: library.version, exact: true };
  }

  /** R4 steps 1-2: an exact match, already open or anywhere on the search path — never a fallback. */
  private resolveExactPatchSync(req: VersionRequest): Library | undefined {
    const already = this.loaded.find((l) => patchMatches(l.version, req));
    if (already !== undefined) return already;
    const matched = pickPatchMatch(this.scanLine(req.line), req);
    if (matched === undefined) return undefined;
    return this.loadDir(matched.dir) ?? undefined;
  }

  /**
   * R4 in full, synchronous shape: exact, else the newest-installed fallback
   * within the line. R-c: the already-open check is free (no I/O) and always
   * current; everything past it needs a directory read per search-path entry,
   * so once a request has fallen back, the WHOLE re-check — retrying the exact
   * match and picking the fallback alike — is throttled together, at most
   * once per `FALLBACK_RECHECK_MS` per requested patch per process.
   *
   * §3: the warning fires as soon as the fallback patch is CHOSEN — its
   * version is already known from the manifest scan, before `loadDir` ever
   * runs — so a caller sees the warning even when the chosen directory then
   * fails to load (a broken artifact is still a fallback that was taken).
   */
  private resolvePatchSync(req: VersionRequest): Resolved | undefined {
    const already = this.loaded.find((l) => patchMatches(l.version, req));
    if (already !== undefined) return { library: already, actual: already.version, exact: true };

    const cacheKey = req.exact!;
    const cached = fallbackCache.get(cacheKey);
    const now = Date.now();
    if (cached !== undefined && now - cached.checkedAt < FALLBACK_RECHECK_MS) {
      // Within the window: skip the re-scan (the exact-match retry AND the
      // fallback pick alike), but still attempt to load the chosen directory
      // — one already-known path, not a directory read per search-path
      // entry, so a broken artifact keeps failing rather than being silently
      // remembered as fine.
      const library = this.loadDir(cached.dir);
      if (library === null) return undefined;
      return { library, actual: library.version, exact: false };
    }

    const locs = this.scanLine(req.line);
    const matched = pickPatchMatch(locs, req);
    if (matched !== undefined) {
      const library = this.loadDir(matched.dir);
      if (library !== null) {
        fallbackCache.delete(cacheKey);
        return { library, actual: library.version, exact: true };
      }
    }
    const winner = pickLineWinner(locs);
    if (winner === undefined) {
      fallbackCache.delete(cacheKey);
      return undefined;
    }
    this.warnFallback(req, winner.version, 'installed');
    fallbackCache.set(cacheKey, { dir: winner.dir, checkedAt: now });
    const library = this.loadDir(winner.dir);
    if (library === null) return undefined;
    return { library, actual: library.version, exact: false };
  }

  private resolveSync(req: VersionRequest): Resolved | undefined {
    return req.exact === null ? this.resolveLineSync(req) : this.resolvePatchSync(req);
  }

  /** Would `req` resolve without opening anything? Mirrors `resolveSync` with no `load()` call. */
  private wouldResolve(req: VersionRequest): boolean {
    if (req.exact === null) {
      if (this.linePins.has(req.line)) return true;
      return pickLineWinner(this.scanLine(req.line)) !== undefined;
    }
    if (this.loaded.some((l) => patchMatches(l.version, req))) return true;
    // A patch resolves via its line's fallback too (R9: has() is true when the
    // patch, or any patch of its line, is installed or open).
    return this.scanLine(req.line).length > 0;
  }

  private warnFallback(req: VersionRequest, actual: string, kind: 'installed' | 'published'): void {
    emitPatchFallbackWarning(req.exact!, actual, req.line, this.platform, kind);
  }

  /**
   * Every ClickHouse minor line this registry CAN ANSWER FOR, oldest first —
   * the ones it has opened plus the ones its construction-time manifest scan
   * discovered on the search path, at either slot.
   *
   * That is one meaning in all four bindings, and it is the meaning that
   * survives lazy loading: "the lines that happen to be open" would read as an
   * empty registry until the first `for()`. It opens nothing.
   */
  versions(): string[] {
    const lines = new Set(this.loaded.map((l) => l.minor));
    for (const line of this.known.keys()) lines.add(line);
    return [...lines].sort(compareMinor);
  }

  /**
   * The libraries this registry has OPENED, in full numeric version order —
   * what is open right now, never what could be. Two patches of one line both
   * appear, each in its own slot in this order. A discovered patch that no
   * `for()` and no `preload` has opened appears in `versions()` and not here.
   * It opens nothing.
   */
  libraries(): readonly Library[] {
    return this.loaded;
  }

  /**
   * Resolve a version to its library. A minor line ("25.8") never falls back:
   * the newest patch of the line, from the first search-path directory that
   * holds any patch of it. An exact patch ("25.8.28.1-lts") loads that patch
   * when it is open or installed anywhere on the search path; otherwise the
   * newest installed patch of the same line is loaded instead, and one
   * warning is written per (requested, actual) pair per process — `resolve()`
   * reports the same fallback as `exact: false` instead of only warning.
   *
   * **This is what opens an artifact.** Construction does not: a version is
   * loaded on first request, `dlopen`ed once, and joins `libraries()` from
   * then on. Never a fetch: this call is synchronous; `open()` /
   * `openResolution()` are the ones that may fetch.
   *
   * Failure is the one §7 error, never a fallback to another line: answering
   * 26.7 semantics from a 25.8 artifact is a lie, and silent wrongness is what
   * the rigs score hardest.
   *
   * @param version - a minor line (`"25.8"`) or an exact patch
   *   (`"25.8.28.1-lts"`), e.g. what `parseVersionResult` discovered.
   * @returns the loaded `Library` for that version.
   * @throws {ArtifactMissingError} (`code` `CHTYPES_ARTIFACT_MISSING`, a
   *   `RegistryError`) when no directory on the search path holds a matching
   *   or fallback patch; the message names every directory looked in and the
   *   fetch command.
   * @throws {ChtypesError} when `version` is not a ClickHouse version spelling at all.
   * @throws {RegistryError} when a directory holds a version but it does not load.
   */
  for(version: string): Library {
    return this.resolve(version).library;
  }

  /**
   * `for()`, but returns the full `Resolution` — the requested spelling, what
   * actually loaded, and whether the two are the same patch. Never fetches;
   * `openResolution()` is the async twin that may.
   *
   * @throws {ArtifactMissingError} as `for()`.
   * @throws {ChtypesError} when `version` is not a ClickHouse version spelling at all.
   */
  resolve(version: string): Resolution {
    const requested = version.trim();
    const req = parseVersionSpelling(version);
    // resolveSync (resolvePatchSync, for a patch request) warns internally
    // when it falls back, at the moment the fallback patch is CHOSEN — before
    // load, so the warning still fires even if that load then fails.
    const resolved = this.resolveSync(req);
    if (resolved === undefined) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
    return { library: resolved.library, requested, version: resolved.actual, exact: resolved.exact };
  }

  /**
   * `for()`, with the lazy fetch of docs/guides/fetch.md §6 in front of it. A
   * LINE request no directory holds is fetched through `ensure()` and loaded.
   * A PATCH request tries `ensure()` of that exact patch first; on
   * `CHTYPES_ARTIFACT_UNPUBLISHED` (remembered for this registry, so it costs
   * one network round trip, not one per call) it falls back to `ensure()` of
   * the line and loads whatever that installs, with `exact: false` and one
   * warning. Any other fetch failure (untrusted, corrupt, pinned, source
   * unreachable) surfaces as itself and is never remembered, so a later call
   * retries. With `autofetch` off, this is `for()` behind a promise.
   *
   * @throws {ArtifactMissingError} when nothing is found and autofetch is off.
   * @throws {FetchError} the §7 fetch verdicts (`CHTYPES_ARTIFACT_UNTRUSTED`,
   *   `…_CORRUPT`, `…_PINNED`, `…_UNPUBLISHED`, `CHTYPES_SOURCE_UNREACHABLE`).
   * @throws {RegistryError} when the fetched artifact does not load.
   */
  async open(version: string): Promise<Library> {
    return (await this.openResolution(version)).library;
  }

  /** `open()`, but returns the full `Resolution` — see `resolve()` and `open()`. */
  async openResolution(version: string): Promise<Resolution> {
    const requested = version.trim();
    const req = parseVersionSpelling(version);
    const dest = this.fetchOptions.dest ?? fetchDestination(this.explicit, this.platform);

    if (req.exact === null) {
      const sync = this.resolveLineSync(req);
      if (sync !== undefined) return { library: sync.library, requested, version: sync.actual, exact: true };
      if (!this.autofetch) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
      await this.ensureLineOnce(req.line, dest);
      const after = this.resolveLineSync(req);
      if (after === undefined) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
      return { library: after.library, requested, version: after.actual, exact: true };
    }

    const exact = this.resolveExactPatchSync(req);
    if (exact !== undefined) return { library: exact, requested, version: exact.version, exact: true };

    if (this.autofetch) {
      const unpublishedKey = `${dest}\u0000${req.exact}`;
      if (!this.unpublishedPatches.has(unpublishedKey)) {
        try {
          await ensure(req.exact, { ...this.fetchOptions, dest, platform: this.platform });
        } catch (err) {
          if (err instanceof ArtifactUnpublishedError) {
            this.unpublishedPatches.add(unpublishedKey);
          } else {
            // Untrusted, corrupt, pinned or unreachable: surface it as-is and
            // remember nothing, so a later call retries (R4 step 3).
            throw err;
          }
        }
        if (!this.unpublishedPatches.has(unpublishedKey)) {
          const found = this.resolveExactPatchSync(req);
          if (found !== undefined) return { library: found, requested, version: found.version, exact: true };
        }
      }
      // Step 4 (autofetch on): Ensure(line) at most once per (dest, line) for this registry.
      await this.ensureLineOnce(req.line, dest);
      const winner = pickLineWinner(this.scanLine(req.line));
      if (winner === undefined) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
      // Warn on the CHOSEN version, before the load — so a caller sees it even
      // if the chosen directory then fails to load (§3).
      this.warnFallback(req, winner.version, 'published');
      const library = this.loadDir(winner.dir);
      if (library === null) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
      return { library, requested, version: library.version, exact: false };
    }

    // Autofetch off: the same synchronous fallback `for()`/`resolve()` take
    // (resolvePatchSync warns internally when it falls back).
    const resolved = this.resolvePatchSync(req);
    if (resolved === undefined) throw new ArtifactMissingError(req.line, this.platform, this.searchPath);
    return { library: resolved.library, requested, version: resolved.actual, exact: resolved.exact };
  }

  /** `ensure(line)`, at most once per (dest, line) over this registry's lifetime — never re-read over the network again. */
  private async ensureLineOnce(line: string, dest: string): Promise<void> {
    const key = `${dest}\u0000${line}`;
    if (this.ensuredLines.has(key)) return;
    const result = await ensure(line, { ...this.fetchOptions, dest, platform: this.platform });
    this.loadDir(result.dir);
    this.ensuredLines.add(key);
  }

  /**
   * True when this registry can answer for a version without fetching or
   * loading: already open, or held by a directory on the search path (at
   * either slot) which `for()` would load. A patch resolves true when the
   * patch itself, or any patch of its line, is installed or open — the same
   * condition `for()` would resolve, fallback included. Never a fetch, and
   * never a load.
   */
  has(version: string): boolean {
    let req: VersionRequest;
    try {
      req = parseVersionSpelling(version);
    } catch {
      return false;
    }
    return this.wouldResolve(req);
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
 * holds artifacts (docs/guides/fetch.md §1). An explicit path and the environment
 * variable are returned as given — a wrong one must produce an error naming
 * it, not a silent fallback — while the unnamed candidates only count if they
 * actually hold artifacts.
 */
export function resolveRegistryDir(explicit?: string): string | null {
  if (explicit !== undefined && explicit !== '') return path.resolve(explicit);
  const fromEnv = process.env[ENV_REGISTRY];
  if (fromEnv !== undefined && fromEnv !== '') return path.resolve(fromEnv);
  const platform = hostPlatform();
  for (const candidate of [cacheRegistryDir(platform), ...systemRegistryDirs(platform)]) {
    if (looksLikeRegistry(candidate)) return candidate;
  }
  return null;
}

/**
 * The per-user artifact cache for this host —
 * `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/abi<R>/<os>-<arch>`, R this
 * binding's `ABI_REVISION` and `<arch>` spelled the artifact way (`amd64`,
 * `arm64`). Where `chtypes fetch` and `scripts/fetch.sh` install, and what
 * every SDK's tests and playgrounds fall back to when `CHTYPES_REGISTRY` is
 * unset — one directory the four SDKs at the same revision agree on. A path,
 * not a promise: `Registry` still throws if nothing is there.
 */
export function defaultRegistryDir(): string {
  return cacheRegistryDir(hostPlatform());
}

/**
 * Does this directory hold at least one artifact with a usable manifest, at
 * either slot — the flat `<dir>/<minor>/` layout or the `<dir>/patches/<minor>/<version>/`
 * sibling tree? A registry that holds only nested installs is not empty.
 */
export function looksLikeRegistry(dir: string): boolean {
  if (!isDirectory(dir)) return false;
  try {
    const hasFlat = readdirSync(dir).some((entry) => {
      if (entry.startsWith('.') || entry === 'patches') return false;
      const sub = path.join(dir, entry);
      if (!isDirectory(sub)) return false;
      const manifest = readManifest(sub);
      return manifest !== null && existsSync(path.join(sub, manifest.library));
    });
    if (hasFlat) return true;
    const patchesDir = path.join(dir, 'patches');
    if (!isDirectory(patchesDir)) return false;
    return readdirSync(patchesDir).some((minorEntry) => {
      const minorDir = path.join(patchesDir, minorEntry);
      if (!isDirectory(minorDir)) return false;
      let versionEntries: string[];
      try {
        versionEntries = readdirSync(minorDir);
      } catch {
        return false;
      }
      return versionEntries.some((versionEntry) => {
        const sub = path.join(minorDir, versionEntry);
        if (!isDirectory(sub)) return false;
        const manifest = readManifest(sub);
        return manifest !== null && existsSync(path.join(sub, manifest.library));
      });
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

/**
 * The refuse-list to pass to `chs_init` (docs/reference/artifact.md step 9):
 * `unsafe_families.txt` beside `dir` when that file is present, even empty;
 * otherwise `manifest`'s own `unsafe_families` field when IT is present,
 * even empty. Neither present throws `RegistryError` rather than falling
 * back to an empty guard — `chs_init` must never run with an empty
 * refuse-list by default.
 *
 * Exported for tests only (the same shape as this module's own
 * `resetPatchFallbackWarnings`): not re-exported from `index.ts`, so it
 * never reaches the public API.
 */
export function readUnsafeFamilies(dir: string, manifest: Manifest): string {
  try {
    return readFileSync(path.join(dir, 'unsafe_families.txt'), 'utf8').trim();
  } catch {
    // fall through to the manifest field below
  }
  if (manifest.unsafe_families !== undefined) return manifest.unsafe_families.trim();
  throw new RegistryError(
    `chtypes: ${dir} has neither unsafe_families.txt nor manifest.json's unsafe_families field; ` +
      'refusing to load without an explicit refuse-list',
  );
}

/**
 * Compare `libPath`'s on-disk size to `manifest.library_bytes` — the load-path
 * check `load()` runs on EVERY open, whether or not `verifyChecksums` is on
 * (issue #82). A manifest with no `library_bytes` (`undefined`, the shape a
 * manifest predating the field parses to) is not asked, so this is a no-op
 * for one.
 */
function checkLibraryBytes(libPath: string, manifest: Manifest): void {
  if (manifest.library_bytes === undefined) return;
  const actual = statSync(libPath).size;
  if (actual !== manifest.library_bytes) {
    throw new RegistryError(`chtypes: ${libPath} is ${actual} bytes, manifest says ${manifest.library_bytes}`);
  }
}

function verifyChecksum(libPath: string, manifest: Manifest): void {
  const expected = manifest.library_sha256;
  if (expected === undefined || expected === '') {
    throw new RegistryError(`chtypes: ${libPath} cannot be verified: its manifest carries no library_sha256`);
  }
  const bytes = readFileSync(libPath);
  const actual = createHash('sha256').update(bytes).digest('hex');
  // Case-insensitive: a hex digest is the same digest in either case, and Go
  // and Rust already lower-case before comparing. Comparing raw here made an
  // uppercase manifest digest pass in two bindings and fail in two.
  if (actual !== expected.toLowerCase()) {
    throw new RegistryError(`chtypes: ${libPath} sha256 ${actual} does not match manifest ${expected}`);
  }
}
