/**
 * The CLI over the v1 fetch layer, driven in-process (`runCli`) against a static `file://` tree and an
 * empty cache. Exit statuses are read from the generated `ERROR_EXIT_CODES` (the `errors` table of
 * `spec/fetch-v1/constants.json`), never typed here, and a usage error is the one status that table
 * does not own. Nothing here needs a library: a fetch that would need one is refused earlier.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { EXIT_OK, EXIT_USAGE, runCli } from '../src/cli.js';
import { ERROR_EXIT_CODES } from '../src/ocifetch/constants.gen.js';
import { listTags } from '../src/ocifetch/index.js';

let tmp: string;
let cache: string;
let tree: string;

beforeEach(() => {
  tmp = mkdtempSync(path.join(tmpdir(), 'chtypes-cli-'));
  cache = path.join(tmp, 'cache');
  tree = path.join(tmp, 'tree');
  mkdirSync(path.join(tree, 'tags'), { recursive: true });
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

function writeTags(tags: readonly string[]): string {
  writeFileSync(path.join(tree, 'tags', 'list'), JSON.stringify({ name: 'chtypes/v1', tags }));
  return pathToFileURL(tree).href;
}

async function run(...argv: string[]): Promise<{ code: number; out: string; err: string }> {
  let out = '';
  let err = '';
  const code = await runCli(argv, { stdout: (t) => (out += t), stderr: (t) => (err += t) });
  return { code, out, err };
}

describe('usage', () => {
  it('prints usage and exits with the usage status when given nothing, an unknown command or a bad flag', async () => {
    expect((await run()).code).toBe(EXIT_USAGE);
    expect((await run('frobnicate')).code).toBe(EXIT_USAGE);
    expect((await run('where', '--nosuch')).code).toBe(EXIT_USAGE);
  });

  it('exits 0 for --help and --version, printing to stdout', async () => {
    const help = await run('--help');
    expect(help.code).toBe(EXIT_OK);
    expect(help.out).toContain('chtypes fetch');
    const version = await run('--version');
    expect(version.code).toBe(EXIT_OK);
    expect(version.out).toMatch(/^\d+\.\d+\.\d+/);
  });

  it('refuses contradictory or incomplete fetch arguments as usage errors', async () => {
    expect((await run('fetch', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '--all', '26.8', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '--all', '--frozen', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--frozen', '--update', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--platform', 'windows-amd64', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('where', 'extra')).code).toBe(EXIT_USAGE);
  });
});

describe('commands that touch no network', () => {
  it('where prints the cache root it was given', async () => {
    const r = await run('where', '--cache', cache);
    expect(r.code).toBe(EXIT_OK);
    expect(r.out).toBe(`${path.resolve(cache)}\n`);
  });

  it('verify and list --offline on an empty cache succeed and say nothing is installed', async () => {
    const verify = await run('verify', '--cache', cache);
    expect(verify.code).toBe(EXIT_OK);
    const list = await run('list', '--offline', '--cache', cache);
    expect(list.code).toBe(EXIT_OK);
    expect(list.out).toContain('(nothing)');
  });

  it('fetch --offline of something not installed exits with CHTYPES_ARTIFACT_MISSING\'s status', async () => {
    const r = await run('fetch', '26.8', '--offline', '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING']);
    expect(r.err).toContain('CHTYPES_ARTIFACT_MISSING');
  });

  it('fetch --frozen with no lock file exits with CHTYPES_ARTIFACT_PINNED\'s status', async () => {
    const r = await run('fetch', '26.8', '--frozen', '--lock', path.join(tmp, 'nosuch.lock'), '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_PINNED']);
  });
});

describe('against a static tree', () => {
  it('lists the version spellings a repository publishes, in numeric order, and drops every other tag', async () => {
    const base = writeTags(['26.8', '26.8.15.10', 'sha256-0123abcd', 'latest', '25.10', '25.3', '26.7', 'v26.8']);
    expect(await listTags({ bases: [base] })).toEqual(['25.3', '25.10', '26.7', '26.8', '26.8.15.10']);
  });

  it('prints the published tags from the list command', async () => {
    const base = writeTags(['26.8', '25.10']);
    const r = await run('list', '--base', base, '--cache', cache);
    expect(r.code).toBe(EXIT_OK);
    expect(r.out).toContain('  25.10\n  26.8\n');
  });

  it('a tag nobody published exits with CHTYPES_ARTIFACT_UNPUBLISHED\'s status', async () => {
    const base = writeTags(['26.8']);
    const r = await run('fetch', '26.9', '--base', base, '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_UNPUBLISHED']);
  });

  it('a version spelling the guide refuses before any network call is an error, never a fetch', async () => {
    const base = writeTags(['26.8']);
    const r = await run('fetch', 'v26.8', '--base', base, '--cache', cache);
    expect(r.code).not.toBe(EXIT_OK);
    expect(r.err).toContain('v1 version spelling');
  });

  it('fetch --all over lines this platform has no build for is an UNPUBLISHED failure, not a success', async () => {
    const base = writeTags(['26.8', '26.8.15.10']);
    const r = await run('fetch', '--all', '--base', base, '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_UNPUBLISHED']);
  });
});
