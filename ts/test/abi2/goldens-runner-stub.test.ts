/**
 * The goldens runner (`../goldens-v1/runner.test.ts`) proved before a release exists: it runs, as a real
 * separate process tree, against the ABI stub library `scripts/abi-v1/build-stubs.sh` builds
 * (`$CHTYPES_ABI2_STUBS`) and a small hand-written goldens document, and then
 * `scripts/goldens-v1/compare.py` judges the report it wrote:
 *
 *   - the honest report PASSES, which proves the runner records the stub's documents and refusals
 *     byte for byte, ran one process per setup, and ran the binding's decoders over every OK document;
 *   - the same report with one planted wrong byte in a recorded document FAILS, which proves the
 *     comparator is actually comparing this runner's bytes and not passing whatever it is handed.
 *
 * The document's identity (version, build, ABI fingerprint) is read from the stub's own predicate, which
 * `stubs.json` carries beside the library, never typed here. This file lives under `test/abi2/` so it rides
 * the existing `v1-abi-conformance` leg (`scripts/abi-v1/conformance/ts.sh` runs `vitest run test/abi2`),
 * which is where the stubs are available; without `$CHTYPES_ABI2_STUBS` it SKIPS LOUDLY by name.
 */

import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';

const STUBS_DIR = process.env['CHTYPES_ABI2_STUBS'];
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

const TS_ROOT = path.resolve(import.meta.dirname, '../..');
const REPO_ROOT = path.resolve(TS_ROOT, '..');
const COMPARE = path.join(REPO_ROOT, 'scripts', 'goldens-v1', 'compare.py');
const FIXTURE = path.join(import.meta.dirname, '..', 'goldens-v1', 'stub-goldens.json');

interface StubPredicate {
  readonly abi: number;
  readonly os: string;
  readonly arch: string;
  readonly build: string;
  readonly clickhouse_version: string;
  readonly abi_fingerprint: string;
}

const scratch = mkdtempSync(path.join(tmpdir(), 'goldens-stub-'));
afterAll(() => {
  rmSync(scratch, { recursive: true, force: true });
});

function compare(goldens: string, report: string): { status: number | null; out: string } {
  const r = spawnSync('python3', [COMPARE, '--goldens', goldens, '--report', report], { encoding: 'utf8' });
  return { status: r.status, out: `${r.stdout}${r.stderr}` };
}

describe('the goldens runner against the ABI stub', () => {
  if (!stubsAvailable) {
    console.warn('goldens runner stub test: SKIPPED. Set CHTYPES_ABI2_STUBS (scripts/abi-v1/build-stubs.sh --out DIR) to run it.');
  }

  it.skipIf(!stubsAvailable)(
    'records a report the comparator passes, and refuses the same report with one planted wrong byte',
    () => {
      const stubs = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
        variants: Record<string, { predicate: StubPredicate }>;
      };
      const ok = stubs.variants['ok'];
      if (ok === undefined) throw new Error('stubs.json has no "ok" variant');
      const platform = `${ok.predicate.os}-${ok.predicate.arch}`;

      // The hand-written document, with its identity taken from the stub's own predicate (the ABI v2
      // stub's: an UNSTABLE description's moving fingerprint never needs a hand edit here).
      const doc = JSON.parse(readFileSync(FIXTURE, 'utf8')) as Record<string, unknown>;
      doc['abi'] = ok.predicate.abi;
      doc['abi_fingerprint'] = ok.predicate.abi_fingerprint;
      doc['clickhouse_version'] = ok.predicate.clickhouse_version;
      doc['build'] = ok.predicate.build;
      const goldens = path.join(scratch, 'goldens.json');
      writeFileSync(goldens, `${JSON.stringify(doc)}\n`);

      const report = path.join(scratch, 'report.json');
      const documentOut = path.join(scratch, 'document-out.json');
      const vitestBin = path.join(path.dirname(createRequire(import.meta.url).resolve('vitest/package.json')), 'vitest.mjs');
      const env: NodeJS.ProcessEnv = {};
      for (const [k, v] of Object.entries(process.env)) if (!k.startsWith('VITEST')) env[k] = v;
      Object.assign(env, {
        CHTYPES_GOLDENS_LIBRARY: path.join(STUBS_DIR as string, 'ok.so'),
        CHTYPES_GOLDENS_DOCUMENT_IN: goldens,
        CHTYPES_GOLDENS_PLATFORM: platform,
        CHTYPES_GOLDENS_REPORT: report,
        CHTYPES_GOLDENS_DOCUMENT: documentOut,
        CHTYPES_ALLOW_UNVERIFIED_LIBRARY: '1',
      });
      mkdirSync(path.dirname(report), { recursive: true });
      const run = spawnSync(process.execPath, [vitestBin, 'run', 'test/goldens-v1/runner.test.ts', '--reporter=default'], {
        cwd: TS_ROOT,
        env,
        encoding: 'utf8',
        maxBuffer: 256 * 1024 * 1024,
      });
      expect(run.status, `the runner failed\n${run.stdout}\n${run.stderr}`).toBe(0);

      // The runner wrote the document's exact bytes where the verdict job reads them.
      expect(readFileSync(documentOut).equals(readFileSync(goldens))).toBe(true);

      const rep = JSON.parse(readFileSync(report, 'utf8')) as {
        binding: string;
        cases: { id: string; setup: string; result: string; decoded_ok?: boolean | null; document_b64?: string }[];
      };
      expect(rep.binding).toBe('ts');
      // One record per case, from both setups (each of which ran in a process of its own).
      expect(rep.cases.map((c) => `${c.setup}/${c.id}`).sort()).toEqual([
        'tokyo-defaults/stub-row-document-tokyo',
        'utc/stub-row-document',
        'utc/stub-row-refused',
        'utc/stub-schema-refused',
      ]);
      // The binding's decoder ran over every OK document and accepted it.
      for (const c of rep.cases.filter((x) => x.document_b64 !== undefined)) expect(c.decoded_ok, c.id).toBe(true);

      const honest = compare(goldens, report);
      expect(honest.status, honest.out).toBe(0);

      // One planted wrong byte inside a recorded document: the comparator must refuse it.
      const planted = JSON.parse(JSON.stringify(rep)) as typeof rep;
      const target = planted.cases.find((c) => c.id === 'stub-row-document');
      if (target?.document_b64 === undefined) throw new Error('the report has no document for stub-row-document');
      const bytes = Buffer.from(target.document_b64, 'base64');
      const at = bytes.indexOf(Buffer.from('"ab"'));
      expect(at, 'the recorded document carries the stub payload').toBeGreaterThan(-1);
      bytes[at + 2] = 'x'.charCodeAt(0);
      target.document_b64 = bytes.toString('base64');
      const plantedPath = path.join(scratch, 'report-planted.json');
      writeFileSync(plantedPath, JSON.stringify({ ...planted, cases: planted.cases }));
      // A copy of the honest report keeps its other fields; only the one document differs.
      const refused = compare(goldens, plantedPath);
      expect(refused.status, `the comparator passed a report with a planted wrong byte\n${refused.out}`).not.toBe(0);
    },
    180_000,
  );
});
