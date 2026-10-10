/**
 * The CLI over the v1 fetch layer, driven in-process (`runCli`) against a static `file://` tree and an
 * empty cache. Exit statuses are read from the generated `ERROR_EXIT_CODES` (the `errors` table of
 * `spec/fetch-v1/constants.json`), never typed here, and a usage error is the one status that table
 * does not own. Nothing here needs a library: a fetch that would need one is refused earlier.
 */

import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EXIT_OK, EXIT_USAGE, runCli } from '../src/cli.js';
import { ERROR_EXIT_CODES, TEST_KEYS } from '../src/ocifetch/constants.gen.js';
import { listTags } from '../src/ocifetch/index.js';
import { useFetchV1ForTests } from '../src/ocifetch/channel.js';

// This file tests the v1 fetch contract that the ABI v2 dev channel narrows
// (src/ocifetch/channel.ts): its fixtures name their own registry and key, and
// write schema-1 records, abi-1 predicates and locks. The dev channel's own
// rules (spec/abi-v2/docs.md r5, r6) are test/ocifetch/devchannel.test.ts and
// test/cli-devchannel.test.ts.
useFetchV1ForTests();

let tmp: string;
let cache: string;
let tree: string;

beforeEach(() => {
  tmp = mkdtempSync(path.join(tmpdir(), 'cli-test-'));
  cache = path.join(tmp, 'cache');
  tree = path.join(tmp, 'tree');
  mkdirSync(path.join(tree, 'tags'), { recursive: true });
});

afterEach(() => {
  vi.unstubAllEnvs();
  rmSync(tmp, { recursive: true, force: true });
});

function writeTags(tags: readonly string[]): string {
  writeFileSync(path.join(tree, 'tags', 'list'), JSON.stringify({ name: 'chtypes/v1', tags }));
  const base = pathToFileURL(tree).href;
  vi.stubEnv('CHTYPES_ARTIFACTS_URL', base);
  return base;
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
    expect(version.out).toMatch(/^chtypes \d+\.\d+\.\d+\S*\n$/);
    const anywhere = await run('fetch', '--help');
    expect(anywhere.code).toBe(EXIT_OK);
    expect(anywhere.out).toContain('chtypes fetch');
    expect(anywhere.err).toBe('');
    expect((await run('fetch', '26.8', '--version')).code).toBe(EXIT_USAGE);
  });

  it('refuses contradictory or incomplete fetch arguments as usage errors', async () => {
    expect((await run('fetch', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '--all', '26.8', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '--all', '--frozen', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--frozen', '--update', '--lock', 'x.lock', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--platform', 'windows-amd64', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('where', 'extra')).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--update', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--update', '--offline', '--lock', 'x.lock', '--cache', cache)).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '-q')).code).toBe(EXIT_USAGE);
    expect((await run('fetch', '26.8', '--base', 'x')).code).toBe(EXIT_USAGE);
    expect((await run('-V')).code).toBe(EXIT_USAGE);
    for (const command of ['verify', 'list', 'where']) {
      expect((await run(command, '--platform', 'linux-arm64', '--cache', cache)).code).toBe(EXIT_USAGE);
    }
  });
});

describe('commands that touch no network', () => {
  it('where prints the cache root it was given', async () => {
    const r = await run('where', '--cache', cache);
    expect(r.code).toBe(EXIT_OK);
    expect(r.out).toBe(`${path.resolve(cache)}\n`);
  });

  it('where --all lists every directory searched, the cache root first; the default stays the root alone (public issue #530)', async () => {
    const all = await run('where', '--all', '--cache', cache);
    expect(all.code).toBe(EXIT_OK);
    expect(all.out).toBe(`${path.resolve(cache)}\n/usr/local/share/chtypes/v1\n/opt/chtypes/v1\n`);
    expect((await run('where', '--cache', cache)).out).toBe(`${path.resolve(cache)}\n`);
  });

  it('verify and list --offline on an empty cache succeed and say nothing is installed', async () => {
    const verify = await run('verify', '--cache', cache);
    expect(verify.code).toBe(EXIT_OK);
    expect(verify.out).toBe('');
    // An empty pass must never look like a good one (public issue #486).
    expect(verify.err).toBe(`chtypes: verified 0 builds under ${path.resolve(cache)}\n`);
    const list = await run('list', '--offline', '--cache', cache);
    expect(list.code).toBe(EXIT_OK);
    expect(list.out).toBe('');
  });

  it('verify and fetch --offline name a 0.x registry used as the cache, and write nothing into it (public issue #486)', async () => {
    const zeroX = path.join(tmp, 'zero-x');
    mkdirSync(path.join(zeroX, '26.1'), { recursive: true });
    writeFileSync(path.join(zeroX, '26.1', 'manifest.json'), '{}');
    const hint = `${zeroX} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at `;
    const verify = await run('verify', '--cache', zeroX);
    expect(verify.code).toBe(EXIT_OK);
    expect(verify.err).toContain(`verified 0 builds under ${zeroX}`);
    expect(verify.err).toContain(hint);
    const fetch = await run('fetch', '26.1', '--offline', '--platform', 'linux-arm64', '--cache', zeroX);
    expect(fetch.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING']);
    expect(fetch.err).toContain(hint);
    expect(existsSync(path.join(zeroX, 'oci-layout'))).toBe(false);
  });

  it('--strict makes every command\'s cache fault CHTYPES_CACHE_UNUSABLE and a verify of nothing CHTYPES_ARTIFACT_MISSING (public issue #486)', async () => {
    const empty = path.join(tmp, 'empty');
    mkdirSync(empty);
    const verify = await run('verify', '--strict', '--cache', empty);
    expect(verify.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING']);
    expect(verify.err).toContain('CHTYPES_ARTIFACT_MISSING');
    vi.stubEnv('CHTYPES_CACHE_STRICT', '1');
    expect((await run('verify', '--cache', empty)).code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING']);
    vi.stubEnv('CHTYPES_CACHE_STRICT', '');
    const where = await run('where', '--strict', '--cache', empty);
    expect({ code: where.code, out: where.out }).toEqual({ code: EXIT_OK, out: `${path.resolve(empty)}\n` });
    const zeroX = path.join(tmp, 'zero-x');
    mkdirSync(path.join(zeroX, '26.1'), { recursive: true });
    writeFileSync(path.join(zeroX, '26.1', 'manifest.json'), '{}');
    for (const argv of [['where', '--strict'], ['list', '--offline', '--strict'], ['verify', '--strict'], ['fetch', '26.1', '--offline', '--platform', 'linux-arm64', '--strict']]) {
      const r = await run(...argv, '--cache', zeroX);
      expect({ argv, code: r.code, out: r.out }).toEqual({ argv, code: ERROR_EXIT_CODES['CHTYPES_CACHE_UNUSABLE'], out: '' });
      expect(r.err).toContain(`${zeroX} is unusable as a cache: layout_0x`);
      expect(r.err).toContain('CHTYPES_CACHE_UNUSABLE');
    }
  });

  it.skipIf(typeof process.geteuid === 'function' && process.geteuid() === 0)(
    'an unreadable cache is not installed with a warning by default, and CHTYPES_CACHE_UNUSABLE with --strict (public issue #486)',
    async () => {
      const locked = path.join(tmp, 'locked');
      mkdirSync(path.join(locked, 'unpacked', 'sha256'), { recursive: true });
      chmodSync(locked, 0);
      try {
        expect(() => readdirSync(locked)).toThrow(/EACCES/);
        const lenient = await run('list', '--offline', '--cache', locked);
        expect({ code: lenient.code, out: lenient.out }).toEqual({ code: EXIT_OK, out: '' });
        expect(lenient.err).toContain(`${locked} could not be read (EACCES); treated as not installed. Set CHTYPES_CACHE_STRICT=1 to make this an error.`);
        const strict = await run('list', '--offline', '--strict', '--cache', locked);
        expect(strict.code).toBe(ERROR_EXIT_CODES['CHTYPES_CACHE_UNUSABLE']);
        expect(strict.err).toContain(`${locked} is unusable as a cache: unreadable_root (EACCES)`);
      } finally {
        chmodSync(locked, 0o755);
      }
    },
  );

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
  it('lists the lines a repository publishes, in numeric order, and drops every other tag', async () => {
    const base = writeTags(['26.8', '26.8.15.10', 'sha256-0123abcd', 'latest', '25.10', '25.3', '26.7', 'v26.8']);
    expect(await listTags({ bases: [base] })).toEqual(['25.3', '25.10', '26.7', '26.8']);
  });

  it('prints the published tags from the list command', async () => {
    writeTags(['26.8', '25.10']);
    const r = await run('list', '--cache', cache);
    expect(r.code).toBe(EXIT_OK);
    expect(r.out).toBe('published 25.10 support unknown\npublished 26.8 support unknown\n');
  });

  it('a tag nobody published exits with CHTYPES_ARTIFACT_UNPUBLISHED\'s status', async () => {
    writeTags(['26.8']);
    const r = await run('fetch', '26.9', '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_UNPUBLISHED']);
  });

  it('a version spelling the guide refuses before any network call is an error, never a fetch', async () => {
    writeTags(['26.8']);
    const r = await run('fetch', 'v26.8', '--cache', cache);
    expect(r.code).not.toBe(EXIT_OK);
    expect(r.err).toContain('v1 version spelling');
  });

  it('fetch --all over lines this platform has no build for is an UNPUBLISHED failure, not a success', async () => {
    writeTags(['26.8', '26.8.15.10']);
    const r = await run('fetch', '--all', '--cache', cache);
    expect(r.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_UNPUBLISHED']);
  });
});

/**
 * `chtypes resolve` (public issue #493) and `chtypes prune` (public issue #494) over the conformance fixtures' `basic`
 * tree, signed with the fixtures' test key, as Go's `go/cmd/chtypes/resolve_prune_test.go` drives them.
 */
describe('resolve and prune', () => {
  const basicTree = path.resolve(import.meta.dirname, '../../tests/fixtures/fetch-v1/trees/basic/v2/chtypes/v1');

  /** The environment every case here runs under: the basic tree, the test key, `cache`, and nothing else set. */
  function useBasicTree(): void {
    for (const k of ['CHTYPES_ARTIFACTS_URL', 'CHTYPES_CACHE', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED', 'CHTYPES_TARGET', 'CHTYPES_CACHE_STRICT', 'CHTYPES_OFFLINE']) {
      vi.stubEnv(k, '');
    }
    vi.stubEnv('CHTYPES_ARTIFACTS_URL', pathToFileURL(basicTree).href);
    vi.stubEnv('CHTYPES_TRUSTED_KEYS', TEST_KEYS[0].ed25519Hex);
    vi.stubEnv('CHTYPES_CACHE', cache);
  }

  it('refuses a missing, extra or refused spelling, an option the command does not take, and a bad --line or --keep, with the usage status and nothing on stdout', async () => {
    useBasicTree();
    for (const argv of [
      ['resolve'],
      ['resolve', '26.8', '26.9'],
      ['resolve', 'v26.8'],
      ['resolve', '26.8', '--platform', 'linux-arm64'],
      ['resolve', '26.8', '--keep', '2'],
      ['prune', '26.8'],
      ['prune', '--keep', '0'],
      ['prune', '--keep', 'x'],
      ['prune', '--line', '26.8.15'],
      ['prune', '--line', 'v26.8'],
      ['prune', '--offline'],
      ['prune', '--platform', 'linux-arm64'],
      ['prune', '--json'],
      ['list', '--json'],
      ['where', '--dry-run'],
    ]) {
      const r = await run(...argv);
      expect({ argv, code: r.code, out: r.out }).toEqual({ argv, code: EXIT_USAGE, out: '' });
      expect(r.err).not.toBe('');
    }
  });

  it('resolve names the build and manifest of every platform in platform order, as lines or one JSON array, and installs nothing; offline it is the cache\'s answer', async () => {
    useBasicTree();
    const lined = await run('resolve', '26.8');
    expect({ code: lined.code, err: lined.err }).toEqual({ code: EXIT_OK, err: '' });
    const lines = lined.out.replace(/\n$/, '').split('\n');
    expect(lines).toHaveLength(3);
    for (const [i, platform] of ['linux-amd64', 'linux-arm64', 'darwin-arm64'].entries()) {
      expect(lines[i]).toMatch(new RegExp(`^resolved 26\\.8\\.15\\.10 ${platform} 20261001\\.183455 sha256:[0-9a-f]{64}$`));
    }
    // Nothing was installed: resolve downloads no layer and writes nothing.
    expect(existsSync(cache)).toBe(false);

    const json = await run('resolve', '--json', '26.8');
    expect(json.code).toBe(EXIT_OK);
    expect(json.out.endsWith(']\n')).toBe(true);
    expect(json.out.split('\n')).toHaveLength(2);
    expect(json.out.startsWith('[{"platform":"linux-amd64","version":"26.8.15.10","build":"20261001.183455","manifest":"sha256:')).toBe(true);
    const doc = JSON.parse(json.out) as readonly Record<string, string>[];
    expect(doc.map((row) => Object.keys(row).join(','))).toEqual(lines.map(() => 'platform,version,build,manifest'));
    expect(doc.map((row) => `resolved ${row['version']} ${row['platform']} ${row['build']} ${row['manifest']}`)).toEqual(lines);

    const miss = await run('resolve', '26.8', '--offline');
    expect({ code: miss.code, out: miss.out }).toEqual({ code: ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING'], out: '' });
    expect(miss.err).toContain('CHTYPES_ARTIFACT_MISSING');
    vi.stubEnv('CHTYPES_TARGET', 'linux-arm64');
    expect((await run('fetch', '26.8')).code).toBe(EXIT_OK);
    const hit = await run('resolve', '26.8', '--offline');
    expect({ code: hit.code, out: hit.out }).toEqual({ code: EXIT_OK, out: `${lines[1]}\n` });

    const unpublished = await run('resolve', '1.1');
    expect(unpublished.code).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_UNPUBLISHED']);
    expect(unpublished.err).toContain('CHTYPES_ARTIFACT_UNPUBLISHED');
  });

  it('prune removes the builds of a line a newer one supersedes, keeping the newest n per platform; a dry run removes nothing', async () => {
    useBasicTree();
    vi.stubEnv('CHTYPES_TARGET', 'linux-arm64');
    const dirs = new Map<string, string>();
    for (const spelling of ['26.7.10.3', '26.7', '26.8']) {
      const r = await run('fetch', spelling);
      expect({ spelling, code: r.code }).toEqual({ spelling, code: EXIT_OK });
      dirs.set(spelling, r.out.trim());
    }
    const victim = dirs.get('26.7.10.3') as string;
    const older = `would-prune 26.7.10.3 linux-arm64 ${victim}\n`;
    const root = path.resolve(cache);

    const dry = await run('prune', '--dry-run');
    expect({ code: dry.code, out: dry.out }).toEqual({ code: EXIT_OK, out: older });
    expect(dry.err).toContain(`chtypes: would prune 1 build(s) under ${root}\n`);
    expect(existsSync(victim)).toBe(true);

    expect(await run('prune', '--line', '26.8')).toMatchObject({ code: EXIT_OK, out: '' });
    expect(await run('prune', '--keep', '2')).toMatchObject({ code: EXIT_OK, out: '' });
    expect(existsSync(victim)).toBe(true);

    const pruned = await run('prune', '--line', '26.7');
    expect({ code: pruned.code, out: pruned.out }).toEqual({ code: EXIT_OK, out: older.replace('would-prune', 'pruned') });
    expect(pruned.err).toContain(`chtypes: pruned 1 build(s) under ${root}\n`);
    expect(existsSync(victim)).toBe(false);

    const listed = await run('list', '--offline');
    expect(listed.code).toBe(EXIT_OK);
    expect(listed.out).not.toContain('26.7.10.3');
    expect(listed.out).toContain('installed 26.7.15.5 ');
    expect(listed.out).toContain('installed 26.8.15.10 ');
    expect(await run('prune')).toMatchObject({ code: EXIT_OK, out: '' });
  });
});
