/**
 * The TypeScript goldens RUNNER (`docs/guides/goldens-v1.md` §2). It executes the cases of one goldens
 * document and RECORDS what the library returned; it never compares (`scripts/goldens-v1/compare.py`
 * does that, once, for all four bindings) and it derives nothing.
 *
 * Inputs (the environment `.github/workflows/v1-goldens.yml` sets):
 *
 *   CHTYPES_GOLDENS_REGISTRY_BASE  the registry base to fetch from, repository path included
 *   CHTYPES_GOLDENS_VERSION        the exact four-part version
 *   CHTYPES_GOLDENS_PLATFORM       linux-amd64 | linux-arm64 | darwin-arm64
 *   CHTYPES_GOLDENS_REPORT         where the report goes (spec/goldens/v1/report.schema.json)
 *   CHTYPES_GOLDENS_DOCUMENT       where the goldens document's exact bytes go
 *
 * Without the first three this file SKIPS LOUDLY by name and passes, so a plain `pnpm test` stays green.
 * That is the only skip allowed.
 *
 * What it does:
 *
 *   1. `ensure` the release for the platform with the DEFAULT trust (the release key), then `fetchSigned`
 *      with the goldens predicate type on the PLATFORM manifest digest: the seam does the referrer and
 *      revision selection itself, so nothing here looks a referrer up. The document's exact bytes go to
 *      `CHTYPES_GOLDENS_DOCUMENT`.
 *   2. One OS process per `setups[]` entry, because `chs_initialize` is process-once. This file re-executes
 *      itself under vitest with `CHTYPES_GOLDENS_CHILD_SETUP=<id>`: that child loads the library, applies
 *      the setup once (`chs_initialize(image_zone)`, then `chs_set_defaults(defaults)`), runs every case of
 *      the setup and writes its records to a file; the parent merges the files into the report. Setup is
 *      never applied twice in one process.
 *   3. Per case the child makes exactly the `chs_*` sequence the case's `call` names, through the generated
 *      call layer (`src/abi2/calls.gen.ts`), and records `at`, `status`, the error fields as base64, and
 *      for CHS_OK the exact document bytes (`document_b64`, never re-serialized) and `export_b64`.
 *   4. For every CHS_OK document the child runs the binding's decoder for that document type (the same
 *      function the public `Schema` methods call) and records `decoded_ok`.
 *
 * THE ABI v2 DEV CHANNEL. This is the 2.0.0-dev binding: its fetch layer fetches only from the staging dev
 * channel under the staging key (`spec/abi-v2/docs.md`, rule r6), whatever `CHTYPES_GOLDENS_REGISTRY_BASE`
 * names (that base is ignored, with one warning). Its goldens are the v2-dev release's referrers, signed with
 * the staging key. The first v2-dev builds carry none: when the release is published but has no goldens
 * referrer, the runner SKIPS BY NAME, printing `SKIPPED: no v2-dev goldens published yet: ...`, which the
 * workflows read and list as skipped, never as a pass.
 *
 * To prove the runner before a release exists, point it at a library and a hand-written document with
 * `CHTYPES_GOLDENS_LIBRARY` (a path; opened unverified, which also needs `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`)
 * and `CHTYPES_GOLDENS_DOCUMENT_IN` (a path): both bypass the fetch, and `CHTYPES_GOLDENS_PLATFORM` is then
 * optional (default: this host). `test/abi2/goldens-runner-stub.test.ts` does exactly that against the ABI
 * stub and runs the comparator over the report.
 */

import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { CallError } from '../../src/abi2/errors.js';
import { type LoadedImage, openAbi2, openUnverified, type Predicate } from '../../src/abi2/loader.js';
import { Status } from '../../src/abi2/vocab.gen.js';
import type { FilterHandle, SchemaHandle } from '../../src/abi2/calls.gen.js';
import { decodeBatch, decodeFilterResult, decodeRow, decodeSchemaDescription } from '../../src/documents.js';
import { PREDICATE_TYPE_GOLDENS } from '../../src/ocifetch/constants.gen.js';
import {
  ArtifactUnpublishedError,
  channelName,
  DEV_CHANNEL_BASE,
  ensure,
  fetchSigned,
  hostPlatformKey,
  type PlatformKey,
} from '../../src/ocifetch/index.js';

// ------------------------------------------------------------------ the document

interface B64 {
  readonly [k: string]: unknown;
}
interface GoldenCase {
  readonly id: string;
  readonly kind: string;
  readonly call: 'schema_create' | 'preview_row' | 'preview_batch' | 'filter_eval_body';
  readonly platforms: readonly string[];
  readonly schema: { readonly create_table_b64: string; readonly settings_b64: string };
  readonly body?: {
    readonly format: number;
    readonly body_b64: string;
    readonly settings_b64: string;
    readonly columns_b64?: string;
    readonly export_format?: number;
    readonly doc_flags?: number;
  };
  readonly filter?: { readonly expr_b64: string; readonly query_params_b64: string; readonly settings_b64: string };
  readonly expect: B64;
}
interface GoldenSetup {
  readonly id: string;
  readonly image_zone_b64: string;
  readonly defaults_b64: string;
  readonly cases: readonly GoldenCase[];
}
interface GoldenDoc {
  readonly clickhouse_version: string;
  readonly build: string;
  readonly revision: number;
  readonly setups: readonly GoldenSetup[];
}

/** One record of the report, in the schema's own shape. */
interface Recorded {
  readonly id: string;
  readonly setup: string;
  readonly result: 'ran' | 'skipped';
  readonly skip_reason?: string;
  readonly at?: 'schema_create' | 'filter_create' | 'call';
  readonly status?: string;
  readonly error?: { ch_code: number; ch_name_b64: string; message_b64: string; column_b64: string };
  readonly document_b64?: string;
  readonly export_b64?: string;
  readonly decoded_ok?: boolean | null;
}

interface ChildOutput {
  readonly setup: string;
  readonly build_info_b64: string;
  readonly abi_fingerprint: string;
  readonly cases: readonly Recorded[];
}

interface Handoff {
  readonly libraryPath: string;
  /** The signed predicate for a fetched library, null for an unverified (stub) one. */
  readonly predicate: Predicate | null;
  readonly platform: string;
  readonly documentPath: string;
}

const ENV = process.env;
const CHILD_SETUP = ENV['CHTYPES_GOLDENS_CHILD_SETUP'];
const LIBRARY_OVERRIDE = ENV['CHTYPES_GOLDENS_LIBRARY'];
const DOCUMENT_IN = ENV['CHTYPES_GOLDENS_DOCUMENT_IN'];
const overrideMode = LIBRARY_OVERRIDE !== undefined && LIBRARY_OVERRIDE !== '' && DOCUMENT_IN !== undefined && DOCUMENT_IN !== '';
const fetchMode = ['CHTYPES_GOLDENS_REGISTRY_BASE', 'CHTYPES_GOLDENS_VERSION', 'CHTYPES_GOLDENS_PLATFORM'].every((k) => (ENV[k] ?? '') !== '');
const configured = overrideMode || fetchMode;
const TS_ROOT = path.resolve(import.meta.dirname, '../..');
const SELF = 'test/goldens-v1/runner.test.ts';

function b64(s: string): Buffer {
  return Buffer.from(s, 'base64');
}

function enc(b: Uint8Array): string {
  return Buffer.from(b).toString('base64');
}

/** The status names the report schema uses, from the generated status vocabulary's numbers. */
function statusName(status: number): string {
  switch (status) {
    case Status.Ok:
      return 'CHS_OK';
    case Status.Rejected:
      return 'CHS_REJECTED';
    case Status.Declined:
      return 'CHS_DECLINED';
    case Status.InvalidArgument:
      return 'CHS_INVALID_ARGUMENT';
    case Status.Internal:
      return 'CHS_INTERNAL';
    default:
      throw new Error(`goldens runner: the library answered status ${status}, which the status vocabulary does not list`);
  }
}

// ----------------------------------------------------------------- the child

/** Run `fn`; on a call error return the status and error fields it carried, never anything else. */
function attempt<T>(fn: () => T): { ok: true; value: T } | { ok: false; status: string; error: NonNullable<Recorded['error']> } {
  try {
    return { ok: true, value: fn() };
  } catch (err) {
    if (!(err instanceof CallError)) throw err;
    return {
      ok: false,
      status: statusName(err.status),
      error: {
        ch_code: err.chCode,
        ch_name_b64: enc(Buffer.from(err.chName, 'utf8')),
        message_b64: enc(err.messageBytes),
        column_b64: enc(err.column),
      },
    };
  }
}

/** Whether the decoder (the one the public `Schema` methods call) accepts a document: true when it throws nothing. */
function decodes(decode: () => unknown): boolean {
  try {
    decode();
    return true;
  } catch {
    return false;
  }
}

function runCase(image: LoadedImage, setup: string, platform: string, c: GoldenCase): Recorded {
  if (!c.platforms.includes(platform)) {
    return { id: c.id, setup, result: 'skipped', skip_reason: `platform ${platform} is not among this case's platforms (${c.platforms.join(', ')})` };
  }
  const calls = image.calls;
  let schema: SchemaHandle | undefined;
  let filter: FilterHandle | undefined;
  try {
    const created = attempt(() => calls.schemaCreate(null, b64(c.schema.create_table_b64), b64(c.schema.settings_b64), Buffer.alloc(0)));
    if (!created.ok) return { id: c.id, setup, result: 'ran', at: 'schema_create', status: created.status, error: created.error, decoded_ok: null };
    schema = created.value;

    if (c.call === 'schema_create') {
      const described = attempt(() => calls.schemaDescribe(schema as SchemaHandle));
      if (!described.ok) return { id: c.id, setup, result: 'ran', at: 'call', status: described.status, error: described.error, decoded_ok: null };
      const doc = described.value;
      return { id: c.id, setup, result: 'ran', at: 'call', status: 'CHS_OK', document_b64: enc(doc), decoded_ok: decodes(() => decodeSchemaDescription(doc)) };
    }

    const body = c.body;
    if (body === undefined) throw new Error(`goldens runner: case ${c.id} (${c.call}) carries no body`);
    const bodyBytes = b64(body.body_b64);
    const settings = b64(body.settings_b64);
    const columns = b64(body.columns_b64 ?? '');

    if (c.call === 'preview_row') {
      const row = attempt(() => calls.previewRow(schema as SchemaHandle, body.format, bodyBytes, settings, columns));
      if (!row.ok) return { id: c.id, setup, result: 'ran', at: 'call', status: row.status, error: row.error, decoded_ok: null };
      const doc = row.value;
      return { id: c.id, setup, result: 'ran', at: 'call', status: 'CHS_OK', document_b64: enc(doc), decoded_ok: decodes(() => decodeRow(doc)) };
    }

    if (c.call === 'preview_batch') {
      const exportFormat = body.export_format ?? -1;
      const batch = attempt(() =>
        calls.previewBatch(schema as SchemaHandle, body.format, bodyBytes, settings, columns, null, exportFormat, body.doc_flags ?? 7),
      );
      if (!batch.ok) return { id: c.id, setup, result: 'ran', at: 'call', status: batch.status, error: batch.error, decoded_ok: null };
      const { out, outExport } = batch.value;
      const exporting = exportFormat !== -1;
      return {
        id: c.id,
        setup,
        result: 'ran',
        at: 'call',
        status: 'CHS_OK',
        document_b64: enc(out),
        ...(exporting ? { export_b64: enc(outExport) } : {}),
        decoded_ok: decodes(() => decodeBatch(out, exporting ? outExport : undefined)),
      };
    }

    // filter_eval_body: chs_filter_create, then chs_filter_eval_body.
    const f = c.filter;
    if (f === undefined) throw new Error(`goldens runner: case ${c.id} (filter_eval_body) carries no filter`);
    const made = attempt(() => calls.filterCreate(schema as SchemaHandle, b64(f.expr_b64), b64(f.query_params_b64), b64(f.settings_b64)));
    if (!made.ok) return { id: c.id, setup, result: 'ran', at: 'filter_create', status: made.status, error: made.error, decoded_ok: null };
    filter = made.value;
    const evald = attempt(() => calls.filterEvalBody(filter as FilterHandle, body.format, bodyBytes, settings));
    if (!evald.ok) return { id: c.id, setup, result: 'ran', at: 'call', status: evald.status, error: evald.error, decoded_ok: null };
    const doc = evald.value;
    return { id: c.id, setup, result: 'ran', at: 'call', status: 'CHS_OK', document_b64: enc(doc), decoded_ok: decodes(() => decodeFilterResult(doc)) };
  } finally {
    filter?.close();
    schema?.close();
  }
}

/** Child mode: load the library, apply the one setup this process owns, run its cases, write the records. */
function runChild(setupId: string): void {
  const handoff = JSON.parse(readFileSync(requiredEnv('CHTYPES_GOLDENS_CHILD_HANDOFF'), 'utf8')) as Handoff;
  const doc = JSON.parse(readFileSync(handoff.documentPath, 'utf8')) as GoldenDoc;
  const setup = doc.setups.find((s) => s.id === setupId);
  if (setup === undefined) throw new Error(`goldens runner: the document has no setup ${JSON.stringify(setupId)}`);

  const zone = b64(setup.image_zone_b64).toString('utf8');
  const defaultsBytes = b64(setup.defaults_b64);
  const defaults = defaultsBytes.length === 0 ? {} : (JSON.parse(defaultsBytes.toString('utf8')) as Record<string, string>);
  // The loader's step 7 is `chs_initialize(zone)`, then `chs_set_defaults(defaults)` when there are any: once per image.
  const image: LoadedImage =
    handoff.predicate === null
      ? openUnverified(handoff.libraryPath, { allow: true, timezone: zone, defaults })
      : openAbi2({ libraryPath: handoff.libraryPath, predicate: handoff.predicate, platform: handoff.platform, timezone: zone, defaults });
  // A defaults object with no keys is still the document's `chs_set_defaults` argument: step 7 skips it, so make the call here, once.
  if (defaultsBytes.length > 0 && Object.keys(defaults).length === 0) image.calls.setDefaults(defaultsBytes);

  const platform = handoff.platform.replace('-', '/');
  const cases = setup.cases.map((c) => runCase(image, setup.id, platform, c));
  const out: ChildOutput = {
    setup: setup.id,
    build_info_b64: enc(image.buildInfo.raw),
    abi_fingerprint: image.buildInfo.abiFingerprint,
    cases,
  };
  writeFileSync(requiredEnv('CHTYPES_GOLDENS_CHILD_OUT'), JSON.stringify(out));
}

function requiredEnv(name: string): string {
  const v = ENV[name];
  if (v === undefined || v === '') throw new Error(`goldens runner: ${name} is required`);
  return v;
}

// ---------------------------------------------------------------- the parent

function bindingVersion(): string {
  const pkg = createRequire(import.meta.url)('../../package.json') as { version?: string };
  return pkg.version ?? '0.0.0';
}

/** Spawn this very file under vitest for one setup. The child is a fresh process: vitest's own markers are dropped so it does not mistake itself for a worker. */
function spawnChild(setupId: string, handoffPath: string, outPath: string): void {
  const vitestBin = path.join(path.dirname(createRequire(import.meta.url).resolve('vitest/package.json')), 'vitest.mjs');
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(ENV)) if (!k.startsWith('VITEST')) env[k] = v;
  env['CHTYPES_GOLDENS_CHILD_SETUP'] = setupId;
  env['CHTYPES_GOLDENS_CHILD_HANDOFF'] = handoffPath;
  env['CHTYPES_GOLDENS_CHILD_OUT'] = outPath;
  const r = spawnSync(process.execPath, [vitestBin, 'run', SELF, '--reporter=default'], {
    cwd: TS_ROOT,
    env,
    encoding: 'utf8',
    maxBuffer: 256 * 1024 * 1024,
  });
  if (r.status !== 0) {
    throw new Error(`goldens runner: the process for setup ${JSON.stringify(setupId)} failed (status ${r.status}, signal ${r.signal})\n${r.stdout}\n${r.stderr}`);
  }
}

/** Fetch mode: the release for the platform, then the goldens of its platform manifest; or the skip, by name, of a v2-dev release that carries none. */
async function fetchRelease(): Promise<
  { libraryPath: string; predicate: Predicate; platform: string; documentBytes: Buffer } | { readonly skipped: string }
> {
  const base = requiredEnv('CHTYPES_GOLDENS_REGISTRY_BASE');
  const version = requiredEnv('CHTYPES_GOLDENS_VERSION');
  const platform = requiredEnv('CHTYPES_GOLDENS_PLATFORM') as PlatformKey;
  // The default trust: no `trustedKeys`, no `allowUnsigned`. On the dev channel the base is the channel's own.
  const resolved = await ensure(version, { bases: [base], platform });
  let goldensPath: string;
  try {
    goldensPath = (await fetchSigned(base, resolved.digests.manifest, PREDICATE_TYPE_GOLDENS, { bases: [base], platform })).path;
  } catch (err) {
    if (err instanceof ArtifactUnpublishedError && channelName() === 'v2-dev') {
      // The build is published (ensure above succeeded) and carries no goldens
      // referrer: the first v2-dev builds ship none. A skip BY NAME, never a
      // pass: the workflows read this line.
      return {
        skipped: `SKIPPED: no v2-dev goldens published yet: ${version} ${platform} (${resolved.digests.manifest}) has no goldens referrer on ${DEV_CHANNEL_BASE}; the goldens did not run (${err.message})`,
      };
    }
    throw err;
  }
  return { libraryPath: resolved.libraryPath, predicate: resolved.predicate, platform, documentBytes: readFileSync(goldensPath) };
}

async function runParent(skip: (note: string) => never): Promise<void> {
  const scratch = mkdtempSync(path.join(tmpdir(), 'goldens-ts-'));
  let libraryPath: string;
  let predicate: Predicate | null;
  let platform: string;
  let documentBytes: Buffer;
  if (overrideMode) {
    libraryPath = path.resolve(LIBRARY_OVERRIDE as string);
    predicate = null;
    platform = ENV['CHTYPES_GOLDENS_PLATFORM'] || hostPlatformKey(process.platform, process.arch) || '';
    documentBytes = readFileSync(DOCUMENT_IN as string);
  } else {
    const fetched = await fetchRelease();
    if ('skipped' in fetched) {
      console.log(fetched.skipped);
      skip(fetched.skipped);
    }
    ({ libraryPath, predicate, platform, documentBytes } = fetched);
  }
  expect(platform, 'a platform this binding ships for').not.toBe('');

  // The document's exact bytes, as fetched, to where the verdict job reads them from.
  const documentOut = ENV['CHTYPES_GOLDENS_DOCUMENT'];
  if (documentOut !== undefined && documentOut !== '') {
    mkdirSync(path.dirname(path.resolve(documentOut)), { recursive: true });
    writeFileSync(documentOut, documentBytes);
  }
  const documentPath = path.join(scratch, 'goldens.json');
  writeFileSync(documentPath, documentBytes);
  const doc = JSON.parse(documentBytes.toString('utf8')) as GoldenDoc;

  const handoffPath = path.join(scratch, 'handoff.json');
  const handoff: Handoff = { libraryPath, predicate, platform, documentPath };
  writeFileSync(handoffPath, JSON.stringify(handoff));

  const outputs: ChildOutput[] = [];
  for (const setup of doc.setups) {
    const outPath = path.join(scratch, `setup-${setup.id}.json`);
    spawnChild(setup.id, handoffPath, outPath);
    outputs.push(JSON.parse(readFileSync(outPath, 'utf8')) as ChildOutput);
  }
  const first = outputs[0];
  if (first === undefined) throw new Error('goldens runner: the document has no setups');

  const report = {
    report_version: 1,
    binding: 'ts',
    binding_version: bindingVersion(),
    platform: platform.replace('-', '/'),
    goldens: {
      clickhouse_version: doc.clickhouse_version,
      build: doc.build,
      revision: doc.revision,
      sha256: createHash('sha256').update(documentBytes).digest('hex'),
    },
    artifact: { build_info_b64: first.build_info_b64, abi_fingerprint: first.abi_fingerprint },
    cases: outputs.flatMap((o) => o.cases),
  };
  const reportPath = requiredEnv('CHTYPES_GOLDENS_REPORT');
  mkdirSync(path.dirname(path.resolve(reportPath)), { recursive: true });
  writeFileSync(reportPath, `${JSON.stringify(report, null, 1)}\n`);

  const ran = report.cases.filter((c) => c.result === 'ran').length;
  expect(ran, 'the report ran at least one case').toBeGreaterThan(0);
  console.log(`goldens runner (ts): ${ran} case(s) ran, ${report.cases.length - ran} skipped by platform, across ${doc.setups.length} setup process(es); report: ${reportPath}`);
}

// ------------------------------------------------------------------ the tests

const FIFTEEN_MINUTES = 15 * 60 * 1000;

describe('goldens v1 runner (ts)', () => {
  if (CHILD_SETUP !== undefined && CHILD_SETUP !== '') {
    it(`child: runs setup ${CHILD_SETUP} in its own process`, () => {
      runChild(CHILD_SETUP);
    }, FIFTEEN_MINUTES);
    return;
  }
  if (!configured) {
    console.warn(
      'goldens runner (ts): SKIPPED. Set CHTYPES_GOLDENS_REGISTRY_BASE, CHTYPES_GOLDENS_VERSION and CHTYPES_GOLDENS_PLATFORM (or CHTYPES_GOLDENS_LIBRARY and CHTYPES_GOLDENS_DOCUMENT_IN) to run it.',
    );
  }
  it.skipIf(!configured)(
    'executes every case of the goldens document, one process per setup, and records what the library returned',
    async (ctx) => {
      await runParent((note) => ctx.skip(note));
    },
    FIFTEEN_MINUTES,
  );
});
