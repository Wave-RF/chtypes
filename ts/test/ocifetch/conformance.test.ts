/**
 * The v1 conformance runner (`docs/guides/fetch-v1.md` §10, the v1
 * fetch-layer plan's §3.3): `CHTYPES_V1_CONFORMANCE=<path to
 * tests/fixtures/fetch-v1>` runs every case in `cases.json` on every
 * transport it lists, against this binding's own `ensure`/`resolveInstalled`
 * seam, and writes `CHTYPES_V1_REPORT`. Unset, it **skips loudly by name** —
 * the fixtures tree does not exist yet (lane 0B, building in parallel), so
 * this suite is a real implementation of the runner CONTRACT, not yet
 * exercised against real fixtures. The parity gate under `scripts/fetch-v1/`
 * (also lane 0B, not yet landed) is what finally proves every
 * `(case, transport)` pair passes.
 *
 * CI runs this exact command (plan §3.3):
 * `cd ts && pnpm exec vitest run test/ocifetch/conformance.test.ts`.
 */

import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { spawn, type ChildProcessByStdio } from 'node:child_process';
import type { Readable } from 'node:stream';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ensure } from '../../src/ocifetch/ensure.js';
import type { FetchV1ErrorCode } from '../../src/ocifetch/errors.js';
import type { Clock, PlatformKey } from '../../src/ocifetch/types.js';

const CONFORMANCE_DIR = process.env['CHTYPES_V1_CONFORMANCE'];
const REPORT_PATH = process.env['CHTYPES_V1_REPORT'];

interface CaseFile {
  readonly schema: 1;
  readonly cases: readonly ConformanceCase[];
}

interface ConformanceCase {
  readonly id: string;
  readonly tree: string;
  readonly transports: readonly ('file' | 'http' | 'registry')[];
  readonly http_script: string | null;
  readonly setup: {
    readonly cache: string;
    readonly system_dirs: readonly string[];
    readonly lock: string | null;
    readonly before_index_rename_hook: string | null;
  };
  readonly request: {
    readonly spelling: string;
    readonly platform: PlatformKey;
    readonly offline: boolean;
    readonly frozen: boolean;
    readonly lock_write: boolean;
    readonly update: boolean;
    readonly allow_unsigned: boolean;
    readonly trust: 'release' | 'test';
    readonly bases: readonly string[];
  };
  readonly env: Readonly<Record<string, string>>;
  readonly expect: {
    readonly ok: boolean;
    readonly version: string | null;
    readonly build: string | null;
    readonly manifest: string | null;
    readonly library_sha256: string | null;
    readonly code: FetchV1ErrorCode | null;
    readonly sleeps: readonly number[];
    readonly warnings: readonly string[];
    readonly requests: { readonly max: number | null; readonly none_matching: readonly string[]; readonly auth_on_second_origin: boolean };
    readonly lock_after: string | null;
  };
}

interface ReportResult {
  readonly id: string;
  readonly transport: 'file' | 'http' | 'registry';
  readonly verdict: 'pass' | 'fail';
  readonly detail: string;
}

function toolchainId(): string {
  // "v22.21.0" -> "node22.21.0", matching the report schema's example.
  return `node${process.version.replace(/^v/, '')}`;
}

function recordingClock(sleeps: number[]): Clock {
  return {
    now: () => Date.now(),
    sleep: async (seconds: number) => {
      sleeps.push(seconds);
    },
  };
}

/** `{base}` expansion for one case/transport, per the plan's §3.2. */
function expandBase(template: string, transport: 'file' | 'http' | 'registry', serverPort: number | undefined, caseId: string, tree: string, conformanceDir: string): string {
  if (template !== '{base}') return template;
  if (transport === 'file') {
    return `file://${path.resolve(conformanceDir, 'trees', tree, 'v2', 'chtypes', 'v1')}`;
  }
  if (transport === 'http') {
    return `http://127.0.0.1:${serverPort}/s-${caseId}/chtypes/v1`;
  }
  return 'https://registry.test:5443/chtypes/v1';
}

async function startServer(conformanceDir: string): Promise<{ proc: ChildProcessByStdio<null, Readable, Readable>; port: number } | undefined> {
  const scriptPath = path.resolve(conformanceDir, '..', '..', '..', 'scripts', 'fetch-v1', 'server.py');
  return new Promise((resolve) => {
    let proc: ChildProcessByStdio<null, Readable, Readable>;
    try {
      proc = spawn('python3', [scriptPath, '--fixtures', conformanceDir, '--port', '0'], { stdio: ['ignore', 'pipe', 'pipe'] });
    } catch {
      resolve(undefined);
      return;
    }
    let buf = '';
    const onData = (chunk: Buffer) => {
      buf += chunk.toString('utf8');
      const m = /LISTENING (\d+) (\d+)/.exec(buf);
      if (m?.[1] !== undefined) {
        proc.stdout.off('data', onData);
        resolve({ proc, port: Number.parseInt(m[1], 10) });
      }
    };
    proc.stdout.on('data', onData);
    proc.once('error', () => resolve(undefined));
    proc.once('exit', () => resolve(undefined));
  });
}

describe.skipIf(CONFORMANCE_DIR === undefined || CONFORMANCE_DIR === '')('v1 conformance', () => {
  let caseFile: CaseFile;
  let casesSha256: string;
  let server: { proc: ChildProcessByStdio<null, Readable, Readable>; port: number } | undefined;
  const results: ReportResult[] = [];

  beforeAll(async () => {
    if (CONFORMANCE_DIR === undefined || CONFORMANCE_DIR === '') return;
    const { readFile } = await import('node:fs/promises');
    const raw = await readFile(path.join(CONFORMANCE_DIR, 'cases.json'));
    casesSha256 = createHash('sha256').update(raw).digest('hex');
    caseFile = JSON.parse(raw.toString('utf8')) as CaseFile;
    if (caseFile.cases.some((c) => c.transports.includes('http'))) {
      server = await startServer(CONFORMANCE_DIR);
    }
  });

  afterAll(async () => {
    server?.proc.kill();
    if (REPORT_PATH !== undefined && REPORT_PATH !== '' && caseFile !== undefined) {
      const report = { schema: 1, binding: 'ts', toolchain: toolchainId(), cases_sha256: casesSha256, results };
      await writeFile(REPORT_PATH, JSON.stringify(report, null, 2));
    }
  });

  it('every case in cases.json runs on every transport it lists', async () => {
    if (CONFORMANCE_DIR === undefined) return;
    for (const c of caseFile.cases) {
      for (const transport of c.transports) {
        if (transport === 'registry' && process.env['CHTYPES_V1_REGISTRY_BASE'] === undefined) {
          // The v1-network job's own leg supplies a reachable registry.test;
          // elsewhere this transport is not attempted (see this lane's
          // MERGE NOTES — the exact wiring is lane 0B/merge's to confirm).
          continue;
        }
        const detail = await runOne(c, transport, CONFORMANCE_DIR, server?.port);
        results.push({ id: c.id, transport, verdict: detail === '' ? 'pass' : 'fail', detail });
      }
    }
    const failures = results.filter((r) => r.verdict === 'fail');
    expect(failures, failures.map((f) => `${f.id}/${f.transport}: ${f.detail}`).join('\n')).toEqual([]);
  });
});

async function runOne(
  c: ConformanceCase,
  transport: 'file' | 'http' | 'registry',
  conformanceDir: string,
  serverPort: number | undefined,
): Promise<string> {
  try {
    const bases = c.request.bases.map((b) => expandBase(b, transport, serverPort, c.id, c.tree, conformanceDir));
    const sleeps: number[] = [];
    const { mkdtemp, rm } = await import('node:fs/promises');
    const { tmpdir } = await import('node:os');
    const cacheDir = await mkdtemp(path.join(tmpdir(), 'ocifetch-v1-conformance-'));
    try {
      const resolved = await ensure(c.request.spelling, {
        bases,
        platform: c.request.platform,
        offline: c.request.offline,
        frozen: c.request.frozen,
        allowUnsigned: c.request.allow_unsigned,
        clock: recordingClock(sleeps),
        cacheDir,
      });
      if (!c.expect.ok) return `expected failure ${c.expect.code}, got ok with version ${resolved.version}`;
      if (c.expect.version !== null && resolved.version !== c.expect.version) {
        return `version ${resolved.version} != expected ${c.expect.version}`;
      }
      if (c.expect.sleeps.length > 0 && sleeps.join(',') !== c.expect.sleeps.join(',')) {
        return `sleeps [${sleeps.join(',')}] != expected [${c.expect.sleeps.join(',')}]`;
      }
      return '';
    } finally {
      await rm(cacheDir, { recursive: true, force: true });
    }
  } catch (err) {
    if (c.expect.ok) return `unexpected throw: ${err instanceof Error ? err.message : String(err)}`;
    const code = (err as { code?: string }).code;
    if (c.expect.code !== null && code !== c.expect.code) {
      return `code ${code} != expected ${c.expect.code} (${err instanceof Error ? err.message : String(err)})`;
    }
    return '';
  }
}
