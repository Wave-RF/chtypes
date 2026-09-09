/**
 * Where artifacts are looked for and where a fetch writes — the §1 search
 * path of docs/fetch.md, spelled once so the loader (`Registry`), the fetch
 * (`ensure`) and the CLI (`chtypes where`) cannot disagree about it.
 *
 * The search path, in order, first directory that holds the requested line
 * wins:
 *
 *   1. a path given explicitly to the registry constructor
 *   2. `CHTYPES_REGISTRY`
 *   3. `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>` — the
 *      per-user cache, where fetch installs
 *   4. `/usr/local/share/chtypes/artifacts/<os>-<arch>`, then
 *      `/opt/chtypes/artifacts/<os>-<arch>` — system locations, reserved
 *
 * A fetch WRITES to the first of (1), (2), (3) that is set; never to (4).
 */

import os from 'node:os';
import path from 'node:path';

/**
 * This host's platform key, `<os>-<arch>` in the artifact spellings
 * (`linux`/`darwin`, `arm64`/`amd64`), e.g. `darwin-arm64`.
 */
export function hostPlatform(): string {
  const arch = ({ x64: 'amd64', arm64: 'arm64' } as Record<string, string>)[os.arch()] ?? os.arch();
  return `${os.platform()}-${arch}`;
}

/** True for the four platform keys a release can carry. */
export function isPlatformKey(key: string): boolean {
  return /^(linux|darwin)-(arm64|amd64)$/.test(key);
}

/** `${XDG_CACHE_HOME:-~/.cache}/chtypes` — the root every chtypes cache lives under. */
export function cacheRoot(): string {
  const xdg = process.env['XDG_CACHE_HOME'];
  const base = xdg !== undefined && xdg !== '' ? xdg : path.join(os.homedir(), '.cache');
  return path.join(base, 'chtypes');
}

/** The per-user artifact cache for one platform — search-path slot (3). */
export function cacheRegistryDir(platform: string = hostPlatform()): string {
  return path.join(cacheRoot(), 'artifacts', platform);
}

/** The reserved system locations — search-path slot (4), never written by fetch. */
export function systemRegistryDirs(platform: string = hostPlatform()): string[] {
  return [`/usr/local/share/chtypes/artifacts/${platform}`, `/opt/chtypes/artifacts/${platform}`];
}

/**
 * The §1 search path for one platform: every candidate directory, in order,
 * whether or not it exists. `explicit` is slot (1); `CHTYPES_REGISTRY` is
 * slot (2) when set. Duplicates collapse to their first position.
 */
export function registrySearchPath(explicit?: string, platform: string = hostPlatform()): string[] {
  const out: string[] = [];
  if (explicit !== undefined && explicit !== '') out.push(path.resolve(explicit));
  const env = process.env['CHTYPES_REGISTRY'];
  if (env !== undefined && env !== '') out.push(path.resolve(env));
  out.push(cacheRegistryDir(platform), ...systemRegistryDirs(platform));
  return [...new Set(out)];
}

/**
 * Where a fetch writes: the first of (1) `explicit`, (2) `CHTYPES_REGISTRY`,
 * (3) the per-user cache that is set — never a system location.
 *
 * `CHTYPES_REGISTRY` names THIS host's registry, so it is only a destination
 * for this host's own platform; artifacts fetched for another platform (a
 * container's `linux-arm64` from a Mac) land in the cache keyed by that
 * platform unless `explicit` says otherwise.
 */
export function fetchDestination(explicit?: string, platform: string = hostPlatform()): string {
  if (explicit !== undefined && explicit !== '') return path.resolve(explicit);
  const env = process.env['CHTYPES_REGISTRY'];
  if (env !== undefined && env !== '' && platform === hostPlatform()) return path.resolve(env);
  return cacheRegistryDir(platform);
}
