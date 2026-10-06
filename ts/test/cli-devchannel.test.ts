/**
 * The command line under the ABI v2 dev channel (`spec/abi-v2/docs.md`, rules
 * r5 and r6). The first cases drive the CLI in-process (`runCli`) on the dev
 * channel exactly as every process but a test speaks it; the last runs the
 * real built CLI (`dist/cli.js`, what `npm install` puts on PATH), which no
 * test seam can reach (they throw outside a vitest worker), and proves the
 * same answers from it. Nothing here reaches the network. Go's
 * `go/cmd/chtypes/devchannel_test.go` is the same test.
 *
 * The real CLI needs a build (`pnpm build`); without `dist/cli.js` that case
 * SKIPS LOUDLY by name. CI's `ts` job builds before its suite, so it runs
 * there.
 */

import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EXIT_OK, EXIT_USAGE, runCli } from '../src/cli.js';
import { DEV_CACHE_DIR, PINNING_REFUSED, useDevChannelForTests } from '../src/ocifetch/channel.js';
import { ERROR_EXIT_CODES, TEST_KEYS } from '../src/ocifetch/constants.gen.js';

useDevChannelForTests();

const dirs: string[] = [];
function tempDir(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), 'ts-cli-devchannel-'));
  dirs.push(d);
  return d;
}

afterEach(() => {
  vi.unstubAllEnvs();
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

async function run(...argv: string[]): Promise<{ code: number; out: string; err: string }> {
  let out = '';
  let err = '';
  const code = await runCli(argv, {
    stdout: (t) => {
      out += t;
    },
    stderr: (t) => {
      err += t;
    },
  });
  return { code, out, err };
}

describe('the CLI on the dev channel, in process', () => {
  it('where names the v2-dev subroot of an explicit cache, and the v2-dev default root (rule r5)', async () => {
    for (const k of ['CHTYPES_CACHE', 'CHTYPES_ARTIFACTS_URL', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED']) vi.stubEnv(k, '');
    const dir = tempDir();
    vi.stubEnv('CHTYPES_CACHE', dir);
    expect(await run('where')).toEqual({ code: EXIT_OK, out: `${path.join(dir, DEV_CACHE_DIR)}\n`, err: '' });
    vi.stubEnv('CHTYPES_CACHE', '');
    expect(await run('where', '--cache', dir)).toEqual({ code: EXIT_OK, out: `${path.join(dir, DEV_CACHE_DIR)}\n`, err: '' });
    const xdg = tempDir();
    vi.stubEnv('XDG_CACHE_HOME', xdg);
    expect(await run('where')).toEqual({ code: EXIT_OK, out: `${path.join(xdg, 'chtypes', DEV_CACHE_DIR)}\n`, err: '' });
  });

  it('refuses --lock, --frozen and --update with the usage status and the dev-channel reason, before anything else (rule r6)', async () => {
    const lock = path.join(tempDir(), 'chtypes.lock');
    for (const argv of [
      ['fetch', '26.8', '--lock', lock],
      ['fetch', '26.8', '--frozen'],
      ['fetch', '26.8', '--frozen', '--lock', lock],
      ['fetch', '--update', '--lock', lock],
      ['fetch', '--all', '--frozen'],
    ]) {
      const r = await run(...argv, '--cache', tempDir());
      expect(r.code, argv.join(' ')).toBe(EXIT_USAGE);
      expect(r.out, argv.join(' ')).toBe('');
      expect(r.err, argv.join(' ')).toContain(PINNING_REFUSED);
    }
    expect(existsSync(lock)).toBe(false);
  });
});

const CLI = path.resolve(import.meta.dirname, '../dist/cli.js');
const built = existsSync(CLI);
if (!built) {
  console.warn(`the real CLI on the dev channel: SKIPPED. ${CLI} does not exist; run \`pnpm build\` in ts first.`);
}

describe.skipIf(!built)('the real built CLI speaks the dev channel, with no test seam', () => {
  it('names the v2-dev roots, refuses pinning, and names each ignored override exactly once', () => {
    // A build older than the dev channel is a stale build, never a pass.
    expect(existsSync(path.resolve(import.meta.dirname, '../dist/ocifetch/channel.js')), 'dist/ predates the dev channel: run pnpm build').toBe(true);
    const xdg = tempDir();
    const home = tempDir();
    const cli = (env: Record<string, string>, ...args: string[]) => {
      const r = spawnSync(process.execPath, [CLI, ...args], {
        env: { HOME: home, XDG_CACHE_HOME: xdg, PATH: process.env['PATH'] ?? '', ...env },
        encoding: 'utf8',
      });
      return { code: r.status, out: r.stdout, err: r.stderr };
    };

    expect(cli({}, 'where')).toMatchObject({ code: 0, out: `${path.join(xdg, 'chtypes', DEV_CACHE_DIR)}\n` });
    const cache = tempDir();
    expect(cli({ CHTYPES_CACHE: cache }, 'where')).toMatchObject({ code: 0, out: `${path.join(cache, DEV_CACHE_DIR)}\n` });

    const frozen = cli({ CHTYPES_CACHE: cache }, 'fetch', '26.8', '--frozen');
    expect(frozen.code).toBe(EXIT_USAGE);
    expect(frozen.err).toContain(PINNING_REFUSED);

    const overrides = {
      CHTYPES_CACHE: cache,
      CHTYPES_ARTIFACTS_URL: 'http://127.0.0.1:9/chtypes/v1',
      CHTYPES_TRUSTED_KEYS: TEST_KEYS[0].ed25519Hex,
      CHTYPES_ALLOW_UNSIGNED: '1',
    };
    const offline = cli(overrides, 'fetch', '26.8', '--offline', '--platform', 'linux-amd64');
    expect(offline.code, offline.err).toBe(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_MISSING']);
    expect(offline.out).toBe('');
    expect(offline.err).toContain('CHTYPES_ARTIFACT_MISSING');
    for (const name of ['CHTYPES_ARTIFACTS_URL', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED']) {
      expect(offline.err.split(`WARNING: ${name} is set and IGNORED`).length - 1, `${name} warned once by the real CLI`).toBe(1);
    }
  });
});
