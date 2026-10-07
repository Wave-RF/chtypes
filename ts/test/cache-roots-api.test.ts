/**
 * Public issue #530: `cacheRoot` and `searchDirs` report the resolution the fetch layer runs, from the one table
 * every binding's test reads (`tests/fixtures/cache-roots/cases.json`), and create nothing.
 */

import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cacheRoot, searchDirs } from '../src/index.js';

interface Case {
  readonly name: string;
  readonly env: Readonly<Record<string, string>>;
  readonly cache_dir: string | null;
  readonly system_dirs: readonly string[] | null;
  readonly search_dirs: readonly string[];
}

const table = JSON.parse(readFileSync(fileURLToPath(new URL('../../tests/fixtures/cache-roots/cases.json', import.meta.url)), 'utf8')) as { cases: Case[] };

let tmp: string;
beforeEach(() => {
  tmp = mkdtempSync(path.join(tmpdir(), 'cache-roots-api-'));
});
afterEach(() => {
  vi.unstubAllEnvs();
  rmSync(tmp, { recursive: true, force: true });
});

const sub = (v: string): string => v.replace('<TMP>', tmp).replace('<CWD>', process.cwd());

describe('the shared table', () => {
  for (const c of table.cases) {
    it(c.name, () => {
      vi.stubEnv('CHTYPES_CACHE', '');
      vi.stubEnv('XDG_CACHE_HOME', '');
      for (const [k, v] of Object.entries(c.env)) vi.stubEnv(k, sub(v));
      const options = {
        ...(c.cache_dir === null ? {} : { cacheDir: sub(c.cache_dir) }),
        ...(c.system_dirs === null ? {} : { systemDirs: c.system_dirs.map(sub) }),
      };
      const want = c.search_dirs.map(sub);
      expect(searchDirs(options)).toEqual(want);
      expect(cacheRoot(options)).toBe(want[0]);
    });
  }
});

describe('the defaults and the no-create rule', () => {
  it('no options is the defaults, and an empty cacheDir counts as unset', () => {
    vi.stubEnv('CHTYPES_CACHE', path.join(tmp, 'env'));
    expect(cacheRoot()).toBe(path.join(tmp, 'env'));
    expect(searchDirs()[0]).toBe(path.join(tmp, 'env'));
    expect(cacheRoot({ cacheDir: '' })).toBe(path.join(tmp, 'env'));
  });

  it('creates nothing', () => {
    vi.stubEnv('CHTYPES_CACHE', '');
    vi.stubEnv('XDG_CACHE_HOME', path.join(tmp, 'xdg'));
    vi.stubEnv('HOME', path.join(tmp, 'home'));
    cacheRoot();
    searchDirs();
    searchDirs({ cacheDir: path.join(tmp, 'c'), systemDirs: [path.join(tmp, 's')] });
    expect(readdirSync(tmp)).toEqual([]);
  });
});
