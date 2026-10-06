/**
 * ABI v2's reader rules (`spec/abi-v2/docs.md`), through the public API, over
 * the generated v2 stub (`$CHTYPES_ABI2_STUBS`):
 *
 *   - r2: a member the description does not name is ignored at every object
 *     level, `_b64` members included, in every result document and in
 *     build_info (stub variant `r2-unknown-members`);
 *   - r3: a value a vocabulary does not list is kept as that vocabulary's
 *     unknown(n), for that field alone, and never fails the document, the row
 *     or the batch; the fallback's fail-closed reading stays (stub variant
 *     `r3-unknown-values`, one planted value per `!E:<id>` body, and
 *     `r3-unknown-capabilities`); a call status outside the closed set is an
 *     internal error naming unknown(n).
 *
 * The documents and the planted values are `scripts/abi-v1/emit/_stubshared.py`'s
 * R2_DOCS and R3_MUTATIONS, the shapes public pull request #509 measured on the
 * released 1.0.4 bindings; Go's `go/chtypes/reader_rules_test.go` is the same
 * test. The last block covers every enum the description defines (no document
 * carries chs_format or discover_query_param) from the generated
 * DESCRIBED_VOCABULARIES, checked against `spec/abi-v2/abi.json` itself, and
 * needs no stub. Without `$CHTYPES_ABI2_STUBS` the stub tests SKIP LOUDLY by
 * name.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import {
  BatchOutcome,
  batchOutcomeKnown,
  DESCRIBED_VOCABULARIES,
  DefaultKind,
  defaultKindKnown,
  DiscoverQueryParam,
  discoverQueryParamKnown,
  FilterOutcome,
  filterOutcomeKnown,
  Format,
  formatKnown,
  InternalError,
  Outcome,
  outcomeKnown,
  Reason,
  reasonKnown,
  reasonLossy,
  Source,
  sourceKnown,
  Status,
  statusKnown,
  statusName,
  Verdict,
  verdictAnswered,
  verdictKnown,
} from '../../src/abi2/index.js';
import { type Library, libraryOf } from '../../src/library.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

function openStub(variant: string): Library {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`stubs.json has no ${variant} variant: the v2 stubs carry the r2 and r3 variants`);
  const image: LoadedImage = openAbi2({
    libraryPath: path.join(STUBS_DIR as string, `${variant}.so`),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  return libraryOf(image, undefined);
}

const body = (text: string): Buffer => Buffer.from(text, 'utf8');

if (!stubsAvailable) {
  console.warn('reader rules (r2, r3) over the stub: SKIPPED. Set CHTYPES_ABI2_STUBS (scripts/abi-v1/build-stubs.sh --out DIR) to run them.');
}

describe.skipIf(!stubsAvailable)('r2: unknown members are ignored at every level (stub r2-unknown-members)', () => {
  it('build_info, live_handles and error_code_table decode around their unknown members', () => {
    const lib = openStub('r2-unknown-members');
    expect(lib.version).toBe('26.8.15.10');
    expect(lib.buildInfo.channel).toBe('lts');
    expect(lib.buildInfo.capabilities.features).toContain('default_generators');
    expect(lib.liveHandles()).toHaveProperty('chs_schema');
    expect(lib.errorCodes().name(53)).toBe('TYPE_MISMATCH');
  });

  it('schema_description, row, batch, filter_result and discovery decode around their unknown members', () => {
    const lib = openStub('r2-unknown-members');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const d = schema.describe();
    expect(d.columns).toHaveLength(1);
    expect(d.columns[0]?.name.toString()).toBe('x');
    expect(d.columns[0]?.type.toString()).toBe('Int32');

    const r = schema.row(Format.JSONEachRow, body('{"x":1}'));
    expect(r.outcome).toBe(Outcome.Accepted);
    expect(r.columns).toHaveLength(1);
    expect(r.columns[0]?.text.toString()).toBe('abc');
    expect(r.columns[0]?.value?.toString()).toBe('abc');
    expect(r.inputSpan).toEqual({ off: 0, len: 3 });
    expect(r.computed).toHaveLength(1);
    expect(r.transformed).toHaveLength(1);
    expect(r.unknownFields.map((b) => b.toString())).toEqual(['u']);
    expect(r.unsupportedSettings.map((b) => b.toString())).toEqual(['st']);

    const b = schema.rows(Format.JSONEachRow, body('{"x":1}'), { exportFormat: Format.JSONEachRow });
    expect(b.outcome).toBe(Outcome.Accepted);
    expect(b.rowsRead).toBe(1);
    expect(b.rows).toHaveLength(1);
    expect(b.rows[0]?.columns).toHaveLength(1);
    expect(b.engineRows).toHaveLength(1);
    expect(b.spans).toHaveLength(1);
    expect(b.unconsumed).toHaveLength(1);
    expect(b.framing?.header?.names.map((n) => n.toString())).toEqual(['s']);
    expect(b.payload?.toString()).toBe('{"s":"abc"}\n');

    const filter = schema.compileFilter('x > 1');
    const f = filter.rows(Format.JSONEachRow, body('{"x":1}'));
    expect(f.outcome).toBe(FilterOutcome.Ok);
    expect(f.rowsRead).toBe(2);
    expect(f.verdicts).toEqual([Verdict.True, Verdict.False]);
    expect(f.errors).toHaveLength(1);
    expect(f.unsupportedSettings).toHaveLength(1);

    const disc = lib.discoverColumns(body('{}'));
    expect(disc.columns).toHaveLength(1);
    expect(disc.columns[0]?.declaration.toString()).toBe('c String');
    expect(disc.columnsSql.toString()).toBe('c String');
    filter.close();
    schema.close();
  });
});

describe.skipIf(!stubsAvailable)('r3: an unlisted value is kept as its unknown(n), for that field alone (stub r3-unknown-values)', () => {
  const planted = (id: string): Buffer => body(`!E:${id}`);

  it('the controls: the clean documents decode with every value listed', () => {
    const lib = openStub('r3-unknown-values');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const r = schema.row(Format.JSONEachRow, body('{}'));
    expect(r.outcome).toBe(Outcome.Accepted);
    expect(sourceKnown(r.columns[0]?.source ?? '')).toBe(true);
    schema.close();
  });

  it('the row: outcome, a column source, a transform reason and the verdict', () => {
    const lib = openStub('r3-unknown-values');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');

    const outcome = schema.row(Format.JSONEachRow, planted('row.outcome'));
    expect(outcome.outcome).toBe('x_future_outcome');
    expect(outcomeKnown(outcome.outcome)).toBe(false);
    expect(outcome.outcome).not.toBe(Outcome.Accepted);
    expect(outcome.columns).toHaveLength(1);

    const src = schema.row(Format.JSONEachRow, planted('row.cols.src'));
    expect(src.columns[0]?.source).toBe('x_future_src');
    expect(sourceKnown(src.columns[0]?.source ?? '')).toBe(false);
    expect(src.columns[0]?.isStored).toBe(false); // value_src names no fallback: unknown(n) reads not stored
    expect(src.columns[0]?.text.toString()).toBe('abc');
    expect(src.outcome).toBe(Outcome.Accepted);

    const reason = schema.row(Format.JSONEachRow, planted('row.transformed.reason'));
    expect(reason.transformed[0]?.reason).toBe('x_future_reason');
    expect(reasonKnown(reason.transformed[0]?.reason ?? '')).toBe(false);
    expect(reason.transformed[0]?.lossy).toBe(reasonLossy(Reason.ValueChanged)); // the fallback's fact

    const verdict = schema.row(Format.JSONEachRow, planted('row.verdict'));
    expect(verdict.verdict).toBe('x');
    expect(verdictKnown(verdict.verdict ?? '')).toBe(false);
    expect(verdictAnswered(verdict.verdict ?? '')).toBe(false); // never an answer
    schema.close();
  });

  it('the batch: its outcome, a row outcome, a column source, a transform reason, a reason it does not read, and the framing container', () => {
    const lib = openStub('r3-unknown-values');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const batch = (id: string) => schema.rows(Format.JSONEachRow, planted(id), { exportFormat: Format.JSONEachRow });

    const outcome = batch('batch.outcome');
    expect(outcome.outcome).toBe('x_future_outcome');
    expect(batchOutcomeKnown(outcome.outcome)).toBe(false);
    expect(outcome.outcome).not.toBe(BatchOutcome.Accepted);
    expect(outcome.rows).toHaveLength(1);

    const rowOutcome = batch('batch.rows.outcome');
    expect(rowOutcome.rows[0]?.outcome).toBe('x_future_outcome');
    expect(outcomeKnown(rowOutcome.rows[0]?.outcome ?? '')).toBe(false);
    expect(rowOutcome.outcome).toBe(BatchOutcome.Accepted); // the batch's own value untouched

    const src = batch('batch.rows.cols.src');
    expect(src.rows).toHaveLength(1);
    expect(src.rows[0]?.columns[0]?.source).toBe('x_future_src');
    expect(sourceKnown(src.rows[0]?.columns[0]?.source ?? '')).toBe(false);

    const reason = batch('batch.transformed.reason');
    expect(reason.transformed[0]?.reason).toBe('x_future_reason');
    expect(reasonKnown(reason.transformed[0]?.reason ?? '')).toBe(false);

    // A member this binding does not read carries the unlisted value: the batch must not fail.
    const storage = batch('batch.storage_transforms.reason');
    expect(storage.outcome).toBe(BatchOutcome.Accepted);
    expect(storage.rows).toHaveLength(1);

    // The schema's enum constrains the writer, never the reader (r3).
    expect(batch('batch.framing.container').framing?.container).toBe('x_future_container');
    schema.close();
  });

  it('the filter result: its outcome and a verdict character', () => {
    const lib = openStub('r3-unknown-values');
    const schema = lib.compileTable('CREATE TABLE t (x Int32)');
    const filter = schema.compileFilter('x > 1');

    const outcome = filter.rows(Format.JSONEachRow, planted('filter.outcome'));
    expect(outcome.outcome).toBe('x_future_outcome');
    expect(filterOutcomeKnown(outcome.outcome)).toBe(false);
    expect(outcome.outcome).not.toBe(FilterOutcome.Ok);

    const verdicts = filter.rows(Format.JSONEachRow, planted('filter.verdicts'));
    expect(verdicts.verdicts).toEqual([Verdict.True, 'x']);
    expect(verdictKnown(verdicts.verdicts[1] ?? '')).toBe(false);
    expect(verdictAnswered(verdicts.verdicts[1] ?? '')).toBe(false);
    filter.close();
    schema.close();
  });

  it('the schema description and discovery: a default kind', () => {
    const lib = openStub('r3-unknown-values');
    // chs_schema_describe has no body: the stub answers the mutation the schema's own statement named.
    const mutated = lib.compileTable('!E:describe.default_kind');
    const d = mutated.describe();
    expect(d.columns[0]?.defaultKind).toBe('X_FUTURE');
    expect(defaultKindKnown(d.columns[0]?.defaultKind ?? '')).toBe(false);
    expect(d.columns[0]?.name.toString()).toBe('x');
    mutated.close();

    const disc = lib.discoverColumns(body('!E:discovery.default_kind'));
    expect(disc.columns).toHaveLength(1);
    expect(disc.columns[0]?.declaration.toString()).toBe('c String');
  });

  it('a call status outside the closed set is unknown(n), and the call still fails as an internal error naming it', () => {
    const lib = openStub('r3-unknown-values');
    let caught: unknown;
    try {
      lib.validateType('!U:');
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(InternalError);
    const e = caught as InternalError;
    expect(e.status).toBe(99);
    expect(statusKnown(e.status)).toBe(false);
    expect(statusName(e.status)).toBe('unknown(99)');
    expect(e.message).toContain('unknown(99)');
  });
});

describe.skipIf(!stubsAvailable)('r3: build_info capabilities keep values no vocabulary lists (stub r3-unknown-capabilities)', () => {
  it('keeps every unlisted value beside the listed ones', () => {
    const c = openStub('r3-unknown-capabilities').buildInfo.capabilities;
    expect(c.inputFormats).toContain('XFutureFormat');
    expect(c.exportFormats).toContain('XFutureFormat');
    expect(c.docFlags).toContain('x_future_flag');
    expect(c.features).toContain('x_future_feature');
    expect(c.features).toContain('default_generators');
  });
});

describe('r3: every enum the description defines has its unknown(n) member', () => {
  const ABI_JSON = path.resolve(import.meta.dirname, '../../../spec/abi-v2/abi.json');

  it('the generated list names exactly the enums spec/abi-v2/abi.json defines, with exactly their listed values', () => {
    // Derived from the description itself, never from the generated table: a
    // table that dropped an enum or a value would otherwise agree with itself.
    const enums = (JSON.parse(readFileSync(ABI_JSON, 'utf8')) as {
      enums: Record<string, { repr: string; values: { name?: string; value: number | string }[] }>;
    }).enums;
    expect(Object.keys(DESCRIBED_VOCABULARIES).sort()).toEqual(Object.keys(enums).sort());
    expect(Object.keys(enums).length).toBeGreaterThanOrEqual(10);
    for (const [name, e] of Object.entries(enums)) {
      const v = DESCRIBED_VOCABULARIES[name];
      expect(v?.repr, name).toBe(e.repr);
      expect([...(v?.listed ?? [])], name).toEqual(e.values.map((x) => x.value));
    }
  });

  it('an unlisted value reads as that vocabulary\'s unknown(n), carrying the raw value, and every listed value is known', () => {
    for (const [name, v] of Object.entries(DESCRIBED_VOCABULARIES)) {
      if (v.repr === 'int32') {
        for (const raw of [2147483000, -7, 12345]) {
          expect(v.known(raw), `${name}(${raw})`).toBe(false);
          expect(v.of(raw), `${name}(${raw})`).toBe(raw);
        }
        for (const listed of v.listed) expect(v.known(listed), `${name}(${listed})`).toBe(true);
      } else {
        for (const raw of [`x_unlisted_${name}`, 'X_FUTURE', ' ']) {
          expect(v.known(raw), `${name}(${JSON.stringify(raw)})`).toBe(false);
          expect(v.of(raw), `${name}(${JSON.stringify(raw)})`).toBe(raw);
        }
        for (const listed of v.listed) expect(v.known(listed), `${name}(${JSON.stringify(listed)})`).toBe(true);
      }
    }
  });

  it('the exported spellings are listed values, and the facts of an unknown(n) are the fallback\'s, or fail closed', () => {
    expect(statusKnown(Status.Ok)).toBe(true);
    expect(statusName(Status.Rejected)).toBe('CHS_REJECTED');
    expect(formatKnown(Format.JSONEachRow)).toBe(true);
    expect(reasonKnown(Reason.ValueChanged)).toBe(true);
    expect(sourceKnown(Source.Input)).toBe(true);
    expect(outcomeKnown(Outcome.Accepted)).toBe(true);
    expect(batchOutcomeKnown(BatchOutcome.Accepted)).toBe(true);
    expect(filterOutcomeKnown(FilterOutcome.Ok)).toBe(true);
    expect(verdictKnown(Verdict.True)).toBe(true);
    expect(discoverQueryParamKnown(DiscoverQueryParam.Database)).toBe(true);
    expect(defaultKindKnown(DefaultKind.None)).toBe(true);
    // transform_reason's fallback is value_changed (lossy); filter_verdict's is d (not answered).
    expect(reasonLossy('x_future_reason')).toBe(true);
    expect(verdictAnswered('x')).toBe(false);
  });
});
