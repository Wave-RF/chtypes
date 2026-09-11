/**
 * Level 1 (ABI) and Level 2 (surface) conformance, run against a real artifact
 * registry. Nothing here asserts on an exit code or a self-report: every case
 * asserts on the document the library produced.
 *
 * Level 3 (semantic) conformance is the rigs' job — `tests/acceptance` and
 * `tests/arbiter` score an implementation against ground truth captured from real
 * ClickHouse servers — and no unit test can stand in for it.
 *
 * Float expectations are deliberately absent: macOS's `long double` is 53-bit, so
 * float parses diverge from a real server (Linux matches 395/395 of the float
 * corpus, macOS 0/395). Any float expectation must come from a Linux artifact.
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { DataType, define, isNullPointer, open as openLibrary, type JsExternal } from 'ffi-rs';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  ArtifactMissingError,
  CODE_UNSUPPORTED,
  Format,
  Registry,
  RegistryError,
  SchemaError,
  UnsupportedError,
  encodeSettings,
  isValidUtf8,
  looksLikeRegistry,
  nativeStats,
  parseDocument,
  parseJsonValue,
  rawText,
  repairBareDenormals,
  resolveRegistryDir,
  type Library,
  type Transform,
} from '../src/index.js';
import { field, items } from '../src/json.js';
import { rowResultOf } from '../src/results.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

if (!HAVE_REGISTRY) {
  console.warn(
    [
      '',
      '[chtypes] Native tests SKIPPED: no artifact registry on the search path.',
      '  Fetch one into the per-user cache with scripts/fetch.sh 25.8 (docs/fetch.md),',
      '  or point CHTYPES_REGISTRY at a registry directory.',
      '',
    ].join('\n'),
  );
}

const utf8 = (s: string): Uint8Array => Buffer.from(s, 'utf8');
const buf = (s: string): Buffer => Buffer.from(s, 'utf8');
const reasons = (ts: readonly Transform[]): string[] => ts.map((t) => t.reason);
/** The document is bytes; a fixture written as text is spelled through this. */
const repaired = (s: string): string => repairBareDenormals(buf(s)).toString('utf8');

// ---------------------------------------------------------------- pure units

describe('result-document JSON', () => {
  it('quotes the bare denormals ClickHouse writes, and nothing else', () => {
    expect(repaired('{"stored":inf}')).toBe('{"stored":"inf"}');
    expect(repaired('{"a":[nan,-inf,1]}')).toBe('{"a":["nan","-inf",1]}');
    // Never inside a string, and never a token that merely starts with one.
    expect(repaired('{"s":"inf"}')).toBe('{"s":"inf"}');
    expect(repaired('{"s":"nan and inf"}')).toBe('{"s":"nan and inf"}');
    expect(repaired('{"info":1}')).toBe('{"info":1}');
    expect(repaired('{"x":1}')).toBe('{"x":1}');
  });

  it('keeps 64-bit and 256-bit values exact', () => {
    // JSON.parse alone turns the first of these into 18446744073709552000 and the
    // second into -5.78960446186581e+76 — differences no scorer can tell from a
    // real coercion defect.
    const doc = parseDocument(
      buf(
        '{"cols":[{"stored":18446744073709551615},' +
          '{"stored":-57896044618658097711785492504343953926634992332820282019728792003956564819968}]}',
      ),
    );
    const cols = items(field(doc, 'cols')).map((c) => field(c, 'stored'));
    expect(rawText(cols[0])).toBe('18446744073709551615');
    expect(rawText(cols[1])).toBe(
      '-57896044618658097711785492504343953926634992332820282019728792003956564819968',
    );
  });

  it('keeps duplicate keys, escape spellings and non-UTF-8 bytes verbatim', () => {
    // All three are bytes ClickHouse really wrote and `JSON.parse` cannot hold:
    // it collapses the duplicate key, rewrites the escape \\u000B as \\u000b,
    // and replaces the 0xff with U+FFFD — each of which reads downstream as a
    // silent transformation that never happened.
    const doc = parseDocument(
      Buffer.concat([buf('{"cols":[{"stored":{"a":1,"a":2},"input":"\\u000B"},{"stored":"'), Buffer.from([0xff, 0xfe]), buf('"}]}')]),
    );
    const cols = items(field(doc, 'cols'));
    expect(rawText(field(cols[0], 'stored'))).toBe('{"a":1,"a":2}');
    expect(rawText(field(cols[0], 'input'))).toBe('"\\u000B"');
    const stored = field(cols[1], 'stored');
    expect(isValidUtf8(stored!.raw)).toBe(false);
    expect([...stored!.raw]).toEqual([0x22, 0xff, 0xfe, 0x22]);
    // The text spelling is the lossy one, and says so by construction.
    expect(rawText(stored)).toBe('"\ufffd\ufffd"');
  });

  it('decides "is this text one JSON value?" by the spec rule, not by trim()', () => {
    // spec/bindings.md §detectors: a strict parse must consume ALL of it, with
    // whitespace being exactly JSON's four. Both edges cost false transforms —
    // a numeric prefix (Go's streaming decoder read `1.2.3.4` as 1.2) and U+FEFF
    // as whitespace (JS `trim()` strips <ZWNBSP>, so `<BOM>42` read as 42).
    expect(parseJsonValue(buf('\ufeff42'))).toBeNull();
    expect(parseJsonValue(buf('1.2.3.4'))).toBeNull();
    expect(parseJsonValue(buf('4,2'))).toBeNull();
    expect(parseJsonValue(buf('42abc'))).toBeNull();
    expect(parseJsonValue(buf('+42'))).toBeNull();
    expect(parseJsonValue(buf('\u00a042'))).toBeNull(); // nor is NBSP
    // JSON's own four are whitespace, and a complete value is a value.
    expect(rawText(parseJsonValue(buf(' \t42\n\r')) ?? undefined)).toBe('42');
    expect(rawText(parseJsonValue(buf('{"a":1,"a":2}')) ?? undefined)).toBe('{"a":1,"a":2}');
  });
});

/**
 * The reference-ladder branches of `classify`, pinned per leaf family.
 *
 * These are pure units and stay that way on purpose. A row document is an
 * INPUT here, not an expectation: the assertion is on how a (`base`, `stored`,
 * `ref`) triple is CLASSIFIED, never on what a parse produced — so the float
 * cases hold on a dev floor whose `long double` is 53-bit, where a native
 * BFloat16 expectation could not (see this file's header).
 *
 * Every document below was captured verbatim from a real artifact (`chs_row`
 * on 25.3 / 26.6 / 26.7) except the one marked hand-written, and every expected
 * reason is the reference oracle's own reply for the same case line
 * (`chtypes-core/tests/conformance/go/cmd/chtypes-oracle`, asked at 26.7).
 *
 * They exist because the extension-8 differential measured 22 divergences from
 * that oracle over the six versions the registry held at the time (the rig
 * set is seven today), in exactly three shapes, all of them a
 * reference-ladder branch this port did not have:
 *
 *   BFloat16     answered `value_changed` where the reference says `float_precision`
 *   Time64(3)    answered `value_changed` where the reference says `reformat`
 *   Time64(3)    INVENTED a transform on a scale-widened reference the
 *                reference collapses to no transform at all — a column-set
 *                difference, which moves the arbiter's transformed_recall axis
 *
 * The first two are `reasonFor` missing `BFloat16` and `Time`; the third is
 * `equivalent` not giving `Time`/`Time64` the fractional-scale trim DateTime64
 * already had, and not giving BFloat16 the numeric comparison Float32/Float64
 * already had.
 */
describe('transform classification: the reference ladder', () => {
  /** One column document -> the transforms `classify` reports for it. */
  const classifyCol = (col: string): readonly Transform[] =>
    rowResultOf(
      parseDocument(
        buf(
          '{"outcome":"accepted","code":0,"err":"","unknown_fields":[],' +
            `"unsupported_settings":[],"computed":[],"cols":[${col}]}`,
        ),
      ),
    ).transformed;

  /** A leaf column document in the shape the C layer writes it. */
  const col = (base: string, refType: string, input: string, stored: string, ref: string): string =>
    `{"name":"x","type":"${base}","base":"${base}","src":"input","input":${input},` +
    `"stored":${stored},"ref":${ref},"ref_type":"${refType}","nullable":false,` +
    '"poison":false,"null_input":false,"dup_dropped":false}';

  const bf16 = (input: string, stored: string, ref: string): string =>
    col('BFloat16', 'Float64', input, stored, ref);
  const time64 = (input: string, stored: string, ref: string): string =>
    col('Time64(3)', 'Time64(9)', input, stored, ref);

  const one = (ts: readonly Transform[]): { stored: string; reason: string; lossy: boolean } => {
    expect(ts.length).toBe(1);
    return { stored: ts[0]!.stored, reason: ts[0]!.reason, lossy: ts[0]!.lossy };
  };

  it('names a BFloat16 rounding float_precision, not value_changed', () => {
    // BFloat16 keeps 8 bits of mantissa, so the Float64 reference disagrees with
    // the stored value for almost every literal a tenant sends. The reason has to
    // say WHICH loss it was: `value_changed` is the classifier's "I could not
    // tell", and the reference implementation never emits it here.
    expect(one(classifyCol(bf16('"1e-45"', '0', '1e-45')))).toEqual({
      stored: '0',
      reason: 'float_precision',
      lossy: true,
    });
    expect(one(classifyCol(bf16('"0.1"', '0.099609375', '0.1')))).toEqual({
      stored: '0.099609375',
      reason: 'float_precision',
      lossy: true,
    });
    expect(one(classifyCol(bf16('"1.00390625"', '1', '1.00390625')))).toEqual({
      stored: '1',
      reason: 'float_precision',
      lossy: true,
    });
    // Near the top of the range, where the gap between two BFloat16 values is
    // 1.3e36 — the largest silent numeric loss in the type system.
    expect(one(classifyCol(bf16('"3.3895314e38"', '3.3762391e38', '3.3895314e38')))).toEqual({
      stored: '3.3762391e38',
      reason: 'float_precision',
      lossy: true,
    });
  });

  it('reports nothing for a BFloat16 the type holds exactly', () => {
    // Coverage must never rise on values that did not change: 0.5 and a denormal
    // are exact in 8 bits of mantissa, and the reference oracle reports no
    // transform for either.
    expect(classifyCol(bf16('"0.5"', '0.5', '0.5'))).toEqual([]);
    expect(classifyCol(bf16('"\\"nan\\""', '"nan"', '"nan"'))).toEqual([]);
    // Hand-written, not captured: the same value spelled with more digits by the
    // wider reference type. Only `equivalent`'s numeric comparison collapses
    // this — byte equality does not — and without it every such column would be
    // reported as a change that never happened.
    expect(classifyCol(bf16('"1"', '1', '1.000'))).toEqual([]);
  });

  it('gives Time64 the fractional-scale trim, so a widened reference is not a change', () => {
    // The Time64(3) column stores ".000"; its Time64(9) reference renders
    // ".000000000". Identical instants, and the reference implementation reports
    // NO transform — inventing one here is worse than a wrong reason, because it
    // changes the column set the arbiter scores recall over.
    expect(classifyCol(time64("\"'10:30'\"", '"00:10:30.000"', '"00:10:30.000000000"'))).toEqual([]);
    expect(classifyCol(time64("\"'10:60:00'\"", '"11:00:00.000"', '"11:00:00.000000000"'))).toEqual([]);
    expect(classifyCol(time64("\"'999:59:59'\"", '"999:59:59.000"', '"999:59:59.000000000"'))).toEqual([]);
    // Time (whole seconds) rides the same branch against its Time64(0) reference.
    expect(classifyCol(col('Time', 'Time64(0)', "\"'10:30:00'\"", '"10:30:00"', '"10:30:00"'))).toEqual([]);
  });

  it('still reports the reformat and the truncation a Time64 really made', () => {
    // Trimming the reference's scale must not silence detector 1: the row said
    // "999:59:59" and the table holds "999:59:59.000", which a subscriber reading
    // the preview would see. Nothing was lost, so it is `reformat` — the reason
    // the reference gives — and never `value_changed`.
    expect(one(classifyCol(time64('"\\"999:59:59\\""', '"999:59:59.000"', '"999:59:59.000000000"')))).toEqual({
      stored: '"999:59:59.000"',
      reason: 'reformat',
      lossy: false,
    });
    // And a real sub-second truncation still names its mechanism: seven digits
    // supplied, three stored, the same truncation DateTime64 makes.
    expect(one(classifyCol(time64('"\\"10:30:00.1234567\\""', '"10:30:00.123"', '"10:30:00.123456700"')))).toEqual({
      stored: '"10:30:00.123"',
      reason: 'datetime_wrap',
      lossy: true,
    });
  });
});

describe('settings', () => {
  it('crosses the boundary as strings, and refuses a JS number', () => {
    expect(encodeSettings({ input_format_null_as_default: '0' })).toBe('{"input_format_null_as_default":"0"}');
    // A bigint is stringified exactly — 19 digits survive.
    expect(encodeSettings({ chtypes_now_epoch_nanos: 1700000000123456789n })).toBe(
      '{"chtypes_now_epoch_nanos":"1700000000123456789"}',
    );
    expect(encodeSettings()).toBe('{}');
    // @ts-expect-error a number cannot carry a nanosecond epoch, so the type forbids it
    expect(() => encodeSettings({ chtypes_now_epoch_nanos: 1700000000123456789 })).toThrow(/must be a string or a bigint/);
  });
});

// ------------------------------------------------------------- native surface

describe.skipIf(!HAVE_REGISTRY)('chtypes over a real artifact registry', () => {
  // Loaded in beforeAll, not in the suite body: a skipped suite still evaluates
  // its body, and a missing registry must skip rather than explode.
  let registry: Registry;
  let versions: string[];
  let newest: string;
  let preferred: string;
  const lib = (): Library => registry.for(preferred);

  beforeAll(() => {
    registry = new Registry(REGISTRY ?? undefined);
    versions = registry.versions();
    newest = versions[versions.length - 1]!;
    preferred = registry.has('25.8') ? '25.8' : newest;
  });

  // chs_init registers chs_shutdown with atexit, but a test must not depend on
  // atexit: the DEFAULT evaluator's reload thread is what hangs a process that
  // never joins it.
  afterAll(() => {
    registry?.close();
  });

  it('loads every artifact and lets the library name itself', () => {
    expect(versions.length).toBeGreaterThan(0);
    for (const library of registry.libraries()) {
      // The minor line is derived from the version the library reports, and the
      // registry cross-checked that against the manifest while loading.
      expect(library.version.startsWith(`${library.minor}.`)).toBe(true);
      expect(registry.for(library.minor)).toBe(library);
      expect(registry.for(library.version)).toBe(library);
    }
    // An unknown patch inside a loaded minor line resolves to that line.
    expect(registry.for(`${preferred}.999.999`).minor).toBe(preferred);
    // A line no directory on the search path holds is the one §7 error
    // (docs/fetch.md), a RegistryError carrying the shared code.
    expect(() => registry.for('19.1')).toThrow(/^chtypes: no artifact for ClickHouse 19\.1 \(/);
    expect(() => registry.for('19.1')).toThrow(RegistryError);
    try {
      registry.for('19.1');
    } catch (err) {
      expect(err).toBeInstanceOf(ArtifactMissingError);
      expect((err as ArtifactMissingError).code).toBe('CHTYPES_ARTIFACT_MISSING');
    }
  });

  describe('the process-state mutators are serialized against everything else', () => {
    // `chs_set_default_settings` REPLACES a process-global that `chs_row` /
    // `chs_rows` / `chs_schema_compile` read BY REFERENCE, so spec/c-abi.md
    // §Thread-safety requires it be serialized against every other call. Go
    // enforces that with an RWMutex and Python with an _RWLock, because both
    // have real threads that can be inside a foreign call. Node cannot: every
    // chs_* call in this binding is synchronous, nothing awaits, and the
    // library never calls back into JS, so a single JS thread is never inside
    // chs_rows when it reaches setDefaultSettings — the exposure is nil.
    //
    // The guard is the PROOF of that rather than the assumption: a counter
    // that only a genuine re-entry can raise. These tests pin both halves —
    // the ordinary path is unaffected, and a re-entry is refused loudly rather
    // than corrupting the row underneath it.
    it('accepts a seed on the ordinary path, and the seed is honoured', () => {
      const library = lib();
      library.setDefaultSettings({ chtypes_default_eval_wall_nanos: '2000000000' });
      library.setDefaultSettings({});
      const schema = library.compileDdl('a UInt8');
      try {
        const got = schema.rows(Format.JSONEachRow, utf8('{"a":1}\n'));
        expect(got.rows[0]!.values[0]!.text).toBe('1');
      } finally {
        schema.close();
      }
    });

    it('the guard is exact: it fires only while a library call is on the stack', () => {
      // Direct proof, without needing a re-entry vector: drive the counter the
      // way a re-entry would and check both states. `entered` is private, so
      // this reaches it the way a re-entry would — through the public surface
      // of the same object.
      const native = (lib() as unknown as { native: { inCall: number } }).native;
      expect(native.inCall).toBe(0);
      native.inCall = 1;
      try {
        expect(() => lib().setDefaultSettings({})).toThrow(/must be serialized against every other call/);
        expect(() => lib().shutdown()).toThrow(/must be serialized against every other call/);
      } finally {
        native.inCall = 0;
      }
      // And back to normal once nothing is on the stack.
      expect(() => lib().setDefaultSettings({})).not.toThrow();
    });
  });

  describe('multi-version coexistence in one process', () => {
    it('keeps at least two versions loaded, each answering as itself', () => {
      // A single-version registry (this mid-cycle's 25.8-only staging) cannot
      // exercise coexistence at all — skip loudly rather than fail, exactly
      // as the Python twin does ("holds only 1 artifact").
      if (versions.length < 2) {
        console.warn(`[chtypes] coexistence proof skipped: registry holds only ${versions.length} artifact`);
        return;
      }
      const oldest = registry.for(versions[0]!);
      const latest = registry.for(newest);
      expect(oldest.version).not.toBe(latest.version);

      // Both answer the same case through their own machinery.
      for (const library of [oldest, latest]) {
        const schema = library.compileDdl('x UInt8');
        try {
          const r = schema.rows(Format.JSONEachRow, utf8('{"x":1}\n'));
          expect(r.outcome).toBe('accepted');
          expect(r.rows[0]!.values[0]!.text).toBe('1');
        } finally {
          schema.close();
        }
      }
    });

    it('keeps each artifact answering as itself: two loaded versions can disagree on one probe', (ctx) => {
      // The proof that two builds are genuinely separate: ONE case, asked of
      // every loaded version in one process, must not come back identical from
      // all of them. If their symbols had collided every version would answer
      // alike — the failure mode that "exits 0, reports zero duplicate symbols,
      // runs, and answers with one version's semantics for both"
      // (spec/artifact.md).
      //
      // WHICH version says what is ClickHouse's answer; it lives in the served
      // golden set and is named nowhere here. This asserts only that the answers
      // CAN differ, and it finds a discriminating probe at run time rather than
      // hardcoding one — a probe that discriminates today may be unanimous once
      // the oldest supported line moves.
      if (versions.length < 2) {
        // A SKIP, not a pass: the census must be able to see that this proved
        // nothing. Returning quietly would let an isolation proof that never ran
        // read as an isolation proof that held.
        ctx.skip(`needs two artifacts to compare, this registry has ${versions.length}`);
      }
      // Each probe is a schema some ClickHouse releases admit and others refuse.
      // Answers are reduced to a coarse verdict: what matters is difference, not
      // which difference.
      // Each probe is a schema-and-row pair some ClickHouse releases admit and
      // others refuse. It must go all the way to a ROW: a release can compile a
      // type it will not then accept a value for, and an earlier version of this
      // test that stopped at compileDdl was unanimous across seven artifacts —
      // it passed while proving nothing.
      const probes: readonly (readonly [string, string])[] = [
        ['j JSON', '{"j":{"a":1}}'],
        ['v Variant(UInt8, String)', '{"v":1}'],
        ['d Dynamic', '{"d":1}'],
        ['t Time', '{"t":"12:00:00"}'],
      ];
      const answerOf = (v: string, ddl: string, body: string): string => {
        let schema;
        try {
          schema = registry.for(v).compileDdl(ddl);
        } catch (err) {
          return err instanceof SchemaError ? `compile-refused:${err.code}` : 'compile-error';
        }
        try {
          const r = schema.rows(Format.JSONEachRow, utf8(`${body}\n`));
          const row = r.rows[0];
          const code = r.errCode !== 0 ? r.errCode : (row?.errCode ?? 0);
          return `${row?.outcome ?? r.outcome}:${code}`;
        } catch (err) {
          return err instanceof SchemaError ? `rows-refused:${err.code}` : 'rows-error';
        } finally {
          schema.close();
        }
      };
      const tried: string[] = [];
      for (const [ddl, body] of probes) {
        const answers = versions.map((v) => answerOf(v, ddl, body));
        tried.push(`${ddl} -> ${[...new Set(answers)].join(' | ')}`);
        if (new Set(answers).size > 1) return; // two builds, two answers: isolated
      }
      // Never a silent pass. Every probe was unanimous, so this registry cannot
      // prove isolation — which is a skip the census shows, not a green tick.
      ctx.skip(
        `no probe discriminated across ${versions.join(', ')}; isolation is unproven here — ${tried.join('; ')}`,
      );
    });

    it('loads each artifact into its own symbol scope (RTLD_LOCAL)', () => {
      // Direct proof rather than a claim: ask the dynamic loader whether the
      // artifacts' exported symbols reached the process-global scope. With
      // RTLD_LOCAL they must not have. RTLD_GLOBAL "will appear to work and then
      // answer with the wrong version's semantics" (spec/c-abi.md).
      const candidates =
        process.platform === 'darwin'
          ? ['/usr/lib/libSystem.B.dylib']
          : ['libc.so.6', 'libc.musl-aarch64.so.1', 'libc.musl-x86_64.so.1'];
      const RTLD_DEFAULT = process.platform === 'darwin' ? -2 : 0;
      let dlsym: ((args: unknown[]) => JsExternal) | null = null;
      for (const path of candidates) {
        try {
          openLibrary({ library: 'chtypes-test-libc', path });
          dlsym = define({
            dlsym: {
              library: 'chtypes-test-libc',
              retType: DataType.External,
              paramsType: [DataType.I64, DataType.String],
            },
          }).dlsym as (args: unknown[]) => JsExternal;
          break;
        } catch {
          // try the next spelling of libc
        }
      }
      if (dlsym === null) {
        // Never silently: the behavioural test above still stands, but say so.
        console.warn('[chtypes] RTLD_LOCAL probe skipped: no libc handle on this platform');
        return;
      }
      // Control: a symbol that is certainly global.
      expect(isNullPointer(dlsym([RTLD_DEFAULT, 'malloc']))).toBe(false);
      // The artifacts' own entry point must be invisible in the global scope.
      expect(isNullPointer(dlsym([RTLD_DEFAULT, 'chs_clickhouse_version']))).toBe(true);
    });
  });

  describe('types and schemas', () => {
    it('canonicalises a type expression and hands back the library spelling verbatim', () => {
      const l = lib();
      expect(l.validateType('DECIMAL(18,4)')).toBe('Decimal(18, 4)');
      expect(l.validateType('Decimal64(4)')).toBe('Decimal(18, 4)');
      expect(l.validateType('Nullable(Decimal(18,4))')).toBe('Nullable(Decimal(18, 4))');
      expect(l.validateType("Enum8('a'=1,'b'=2)")).toBe("Enum8('a' = 1, 'b' = 2)");
      expect(l.validateType('Map(String,Array(UInt8))')).toBe('Map(String, Array(UInt8))');
      expect(l.validateType('LowCardinality( String )')).toBe('LowCardinality(String)');
      expect(l.validateType('BIGINT')).toBe('Int64');
      expect(l.validateType('Int8(3)')).toBe('Int8'); // surplus parameters dropped, not rejected
      if (preferred === '25.8') {
        expect(l.validateType('Variant(UInt8, String)')).toBe('Variant(String, UInt8)'); // members sorted
      }
    });

    it('reports an unknown family as a ClickHouse error, not as unsupported', () => {
      try {
        lib().validateType('NotAType');
        expect.unreachable('NotAType must not validate');
      } catch (err) {
        expect(err).toBeInstanceOf(SchemaError);
        expect(err).not.toBeInstanceOf(UnsupportedError);
        const e = err as SchemaError;
        expect(e.code).toBe(50);
        expect(e.detail).toMatch(/Unknown data type family: NotAType/);
      }
    });

    it('canonicalises schema-aware: a DEFAULT can rewrite the declared type', () => {
      const schema = lib().compileDdl('x Int64 DEFAULT NULL');
      try {
        expect(schema.columns).toEqual([
          { name: 'x', type: 'Nullable(Int64)', defaultKind: 'DEFAULT', defaultExpr: 'NULL', defaultIsLiteral: true },
        ]);
      } finally {
        schema.close();
      }
    });

    it('exposes DEFAULT / MATERIALIZED as declared', () => {
      const schema = lib().compileDdl("a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()");
      try {
        expect(schema.columns.map((c) => [c.name, c.type, c.defaultKind, c.defaultExpr, c.defaultIsLiteral])).toEqual([
          ['a', 'UInt8', '', '', false],
          ['b', 'Nullable(String)', 'DEFAULT', "'x'", true],
          ['c', 'DateTime', 'MATERIALIZED', 'now()', false],
        ]);
      } finally {
        schema.close();
      }
    });

    it('refuses a DEFAULT that exceeds the admission budget, at compile, as unsupported', () => {
      // A schema whose DEFAULT exceeds a ceiling is refused at chs_schema_compile,
      // naming the column and the budget — never as a fabricated rejection, since
      // a real server might well have accepted it.
      try {
        lib().compileDdl('b UInt8 DEFAULT range(400000000)[1]');
        expect.unreachable('an unbounded DEFAULT must be refused at admission');
      } catch (err) {
        // A DECLINE — a real server might well have accepted this schema —
        // so its own peer class, never the SchemaError a refusal uses.
        expect(err).toBeInstanceOf(UnsupportedError);
        expect(err).not.toBeInstanceOf(SchemaError);
        const e = err as UnsupportedError;
        expect(e).not.toHaveProperty('code');
        expect(e.message).toContain(`[${CODE_UNSUPPORTED}]`);
        expect(e.detail).toMatch(/admission (memory )?budget/);
        // No column is attributed on a compile decline, and the rendered
        // message therefore carries no `column "b":` prefix. That is the
        // spelling Go's dlopen'd path, Python and Rust all use — three of the
        // four SDKs — and this binding used to be the odd one out, GUESSING
        // the column by looking for a declared name inside the server's text
        // (playground/README.md finding 5). The library's own message already
        // names the column when it knows one, which is asserted here, so the
        // guess added nothing.
        expect(e.column).toBeUndefined();
        expect(e.message).toBe(`chtypes: [${CODE_UNSUPPORTED}] ${e.detail}`);
        expect(e.detail).toContain('column b');
      }
    });

    it('declines a server-property DEFAULT per row, as unsupported and not as a rejection', () => {
      // Measured on darwin-arm64: this schema COMPILES and the decline arrives per
      // row, with src `default_expr_unsupported` and no transformation reported.
      const schema = lib().compileDdl('h String DEFAULT hostName()');
      try {
        const r = schema.rows(Format.JSONEachRow, utf8('{"z":1}\n'));
        expect(r.outcome).toBe('unsupported');
        const row = r.rows[0]!;
        expect(row.outcome).toBe('unsupported');
        expect(row.values[0]!.source).toBe('default_expr_unsupported');
        expect(row.errMsg).toMatch(/property of the ClickHouse server/);
        expect(row.transformed).toEqual([]);
      } finally {
        schema.close();
      }
    });
  });

  describe('rows', () => {
    it('reports the canonical silent change: 256 into UInt8', () => {
      const schema = lib().compileDdl('x UInt8, s String');
      try {
        const r = schema.row(Format.JSONEachRow, utf8('{"x":256,"s":"hi"}'));
        expect(r.outcome).toBe('accepted');
        expect(r.errCode).toBe(0);
        expect(r.values.map((v) => [v.column, v.text, v.source])).toEqual([
          ['x', '0', 'input'],
          ['s', '"hi"', 'input'],
        ]);
        expect(r.transformed).toEqual([
          {
            column: 'x',
            input: '256',
            stored: '0',
            inputBytes: buf('256'),
            storedBytes: buf('0'),
            reason: 'overflow_wrap',
            row: 0,
            lossy: true,
          },
        ]);
      } finally {
        schema.close();
      }
    });

    // ------------------------------------------------------------ byte fidelity
    //
    // Four classes of bytes a `JSON.parse`-based reading of the result document
    // cannot hold. Every expectation below is the reference driver's own answer to
    // the same case on 25.8 (`chtypes-core/lib/build/chtypes-oracle`), so "more correct" here
    // means "equal to the oracle", not "reads better".
    //
    //   duplicate Map keys    {"a":1,"a":2,"a":3}  -> {"a":3}
    //   ClickHouse's escapes  \u000B               -> \u000b
    //   a non-UTF-8 value     0xff                 -> U+FFFD
    //   a BOM-prefixed field  reported as reformat -> nothing happened

    it('keeps a stored Map with duplicate keys exactly as ClickHouse wrote it', () => {
      const schema = lib().compileDdl('x Map(String, Int64)');
      try {
        // ClickHouse really stores both: a Map is a pair of arrays, not a hash.
        const r = schema.row(Format.JSONEachRow, utf8('{"x":{"a":1,"a":2,"a":3}}'));
        expect(r.outcome).toBe('accepted');
        // Reference: value `[{"x": {"a":1,"a":2,"a":3}}]`, and no transform.
        expect(r.values[0]!.text).toBe('{"a":1,"a":2,"a":3}');
        expect(r.transformed).toEqual([]);
      } finally {
        schema.close();
      }
    });

    it('crosses a non-UTF-8 stored value as bytes, not as U+FFFD', () => {
      const schema = lib().compileDdl('x FixedString(2)');
      try {
        // The body is bytes and the two 0xff are the whole point: no JS string can
        // hold them, and the C-string crossing used to replace them before any
        // caller could see the value.
        const r = schema.row(
          Format.JSONEachRow,
          Buffer.concat([buf('{"x":"'), Buffer.from([0xff, 0xff]), buf('"}')]),
        );
        expect(r.outcome).toBe('accepted');
        // Reference: body_b64 decodes to the four bytes 0x22 0xff 0xff 0x22.
        expect([...r.values[0]!.bytes]).toEqual([0x22, 0xff, 0xff, 0x22]);
        expect(isValidUtf8(r.values[0]!.bytes)).toBe(false);
        // `text` is the lossy spelling, which is exactly why it is not the answer.
        expect(r.values[0]!.text).toBe('"\ufffd\ufffd"');
        expect(r.transformed).toEqual([]);
      } finally {
        schema.close();
      }
    });


    it('does not read a BOM-prefixed field as a number (JSON whitespace is four bytes)', () => {
      const schema = lib().compileDdl('x String');
      try {
        const r = schema.row(Format.TSV, utf8('\ufeff42'));
        expect(r.outcome).toBe('accepted');
        expect(r.values[0]!.text).toBe('"\ufeff42"');
        // Input bytes == stored bytes, so there is nothing to report. Reading
        // <BOM>42 as the number 42 — which `String.prototype.trim()` invites,
        // since its WhiteSpace set includes <ZWNBSP> — reported 12 false
        // `reformat`s across the arbiter's test set.
        expect(r.transformed).toEqual([]);
      } finally {
        schema.close();
      }
    });

    it('tags every transform with its row index inside the batch', () => {
      const schema = lib().compileDdl('x UInt8');
      try {
        const r = schema.rows(Format.JSONEachRow, utf8('{"x":1}\n{"x":256}\n{"x":2}\n'));
        expect(r.outcome).toBe('accepted');
        expect(r.rowsRead).toBe(3);
        expect(r.rowsSkipped).toBe(0);
        expect(r.transformed).toEqual([
          {
            column: 'x',
            input: '256',
            stored: '0',
            inputBytes: buf('256'),
            storedBytes: buf('0'),
            reason: 'overflow_wrap',
            row: 1,
            lossy: true,
          },
        ]);
      } finally {
        schema.close();
      }
    });

    it('reads RowBinary from counted bytes, NUL included', () => {
      const schema = lib().compileDdl('x UInt8, y UInt8');
      try {
        const r = schema.row(Format.RowBinary, new Uint8Array([1, 0]));
        expect(r.outcome).toBe('accepted');
        expect(r.values.map((v) => v.text)).toEqual(['1', '0']);
      } finally {
        schema.close();
      }
    });

    it('reports a rejection with ClickHouse\'s own error code', () => {
      const schema = lib().compileDdl('x UInt8');
      try {
        const r = schema.rows(Format.JSONEachRow, utf8('{"x":"abc"}\n'));
        expect(r.outcome).toBe('rejected');
        expect(r.errCode).toBe(27); // CANNOT_PARSE_INPUT_ASSERTION_FAILED
        expect(r.errMsg).toMatch(/Cannot parse/);
      } finally {
        schema.close();
      }
    });

    it('reports accept-then-poison as ACCEPTED with the poison flag', () => {
      const schema = lib().compileDdl("e Enum8('a'=1,'b'=2)");
      try {
        const r = schema.row(Format.JSONEachRow, utf8('{"e":null}'), {
          input_format_defaults_for_omitted_fields: '0',
        });
        // The insert returns rc=0 and every later SELECT fails with code 691.
        expect(r.outcome).toBe('accepted_poisoned');
        expect(r.errCode).toBe(691);
        expect(reasons(r.transformed)).toEqual(['poisoned']);
        expect(r.transformed[0]!.lossy).toBe(true);
      } finally {
        schema.close();
      }
    });

    it('substitutes a volatile DEFAULT from a pinned clock, exactly', () => {
      const schema = lib().compileDdl('a UInt8, ts DateTime DEFAULT now()');
      try {
        // The setting value is a STRING: as a JSON number the 19-digit epoch is
        // silently ignored and the batch keeps stamping the real wall clock.
        const r = schema.rows(Format.JSONEachRow, utf8('{"a":1}\n'), {
          chtypes_now_epoch_nanos: '1700000000000000000',
        });
        expect(r.outcome).toBe('accepted');
        const row = r.rows[0]!;
        expect(row.values.map((v) => [v.column, v.text, v.source])).toEqual([
          ['a', '1', 'input'],
          ['ts', '"2023-11-14 22:13:20"', 'default_substituted'],
        ]);
        // The caller MUST send this column as an explicit value in the INSERT.
        expect(row.substituted).toEqual([
          // `bytes` is the form that goes back into the INSERT.
          { column: 'ts', expr: 'now()', text: '"2023-11-14 22:13:20"', bytes: buf('"2023-11-14 22:13:20"') },
        ]);
        expect(r.transformed).toEqual([
          {
            column: 'ts',
            input: '',
            stored: '"2023-11-14 22:13:20"',
            inputBytes: buf(''),
            storedBytes: buf('"2023-11-14 22:13:20"'),
            reason: 'default_materialised',
            row: 0,
            lossy: false,
          },
        ]);

        // A bigint pins identically; a number would not, which is why it cannot
        // be passed at all.
        const viaBigint = schema.rows(Format.JSONEachRow, utf8('{"a":1}\n'), {
          chtypes_now_epoch_nanos: 1700000000000000000n,
        });
        expect(viaBigint.rows[0]!.values[1]!.text).toBe('"2023-11-14 22:13:20"');
      } finally {
        schema.close();
      }
    });

    it('declines to substitute past the clock-skew budget', () => {
      const schema = lib().compileDdl('a UInt8, ts DateTime DEFAULT now()');
      try {
        const r = schema.rows(Format.JSONEachRow, utf8('{"a":1}\n'), {
          chtypes_clock_offset_nanos: '5000000000',
          chtypes_max_clock_skew_nanos: '1',
        });
        const row = r.rows[0]!;
        expect(row.outcome).toBe('unsupported');
        expect(row.errCode).toBe(CODE_UNSUPPORTED);
        expect(row.values.find((v) => v.column === 'ts')!.source).toBe('default_volatile_unresolved');
        // A declined volatile DEFAULT is never reported as a transformation.
        expect(reasons(row.transformed)).not.toContain('default_materialised');
      } finally {
        schema.close();
      }
    });

    it('folds storage_transforms in: an expired row is reported, and engineRows is where "not stored" lives', () => {
      // SHAPE, not a verdict. What ClickHouse *decides* about a TTL-expired row
      // is ClickHouse's answer and belongs in the served golden set; asserting
      // it here would put a second copy of a ClickHouse rule in the SDK. What
      // this pins is the binding's own contract: a ttl_expired transform is
      // well-formed and carries its row index, and engineRows — not `rows` — is
      // the stored truth it is absent from. A binding that read only `rows`
      // would preview a row the table silently deletes at merge time.
      //
      // The probe is arithmetic rather than a version's behaviour: a 2020
      // timestamp under a 1-day TTL against a clock pinned to 2023 is expired
      // on any version that models TTL at all.
      const schema = lib().compileDdl('ts DateTime, v UInt8');
      try {
        schema.setEngine('MergeTree', 'ts');
        schema.setTtl('ts + INTERVAL 1 DAY');
        const r = schema.rows(Format.JSONEachRow, utf8('{"ts":"2020-01-01 00:00:00","v":9}\n'), {
          chtypes_now_epoch_nanos: '1700000000000000000',
        });
        const ttl = r.transformed.find((t) => t.reason === 'ttl_expired');
        expect(ttl).toBeDefined();
        expect(ttl!.row).toBe(0);
        expect(ttl!.lossy).toBe(true);
        // Reported expired and therefore absent from the stored view.
        expect(r.engineRows).toEqual([]);
      } finally {
        schema.close();
      }
    });

    it('applies the engine insert-time merge and prefers engineRows as stored truth', () => {
      const schema = lib().compileDdl('day Date, key UInt32, v UInt32');
      try {
        schema.setEngine('SummingMergeTree', '(day, key)');
        const r = schema.rows(
          Format.JSONEachRow,
          utf8('{"day":"2026-01-01","key":1,"v":5}\n{"day":"2026-01-01","key":1,"v":7}\n'),
        );
        expect(r.outcome).toBe('accepted');
        expect(r.rows.length).toBe(2);
        expect(r.engineRows).toEqual(['{"day":"2026-01-01","key":1,"v":12}']);
      } finally {
        schema.close();
      }
    });

    it('declines a TTL form it does not model, as unsupported and not as a rejection', () => {
      const schema = lib().compileDdl('ts DateTime, v UInt8');
      try {
        schema.setTtl('now() + INTERVAL 1 DAY');
        expect.unreachable('a clock-reading TTL must be declined');
      } catch (err) {
        expect(err).toBeInstanceOf(UnsupportedError);
        expect(err).not.toBeInstanceOf(SchemaError);
        expect((err as UnsupportedError).message).toContain(`[${CODE_UNSUPPORTED}]`);
      } finally {
        schema.close();
      }
    });

    it('reports MATERIALIZED separately from the stored row', () => {
      const schema = lib().compileDdl('a UInt8, m UInt8 MATERIALIZED a + 1, b String');
      try {
        // MATERIALIZED is durable but is not part of `SELECT *`, so it must never
        // be mixed into the stored row a subscriber sees.
        const named = schema.row(Format.JSONEachRow, utf8('{"a":7,"b":"hey"}'));
        expect(named.values.map((v) => v.column)).toEqual(['a', 'b']);
        expect(named.computed).toEqual([{ column: 'm', kind: 'MATERIALIZED', text: '8', bytes: buf('8') }]);

        // Positional formats address the k-th INSERTABLE column, so MATERIALIZED
        // occupies no field position. Measured on darwin-arm64/25.8: the CSV path
        // computes `m` as 8, matching live servers on every format. The
        // positional path used to compute MATERIALIZED from the type zero
        // (`1`) — a wrapper bug fixed 2026-08-17; spec/c-abi.md §Positional
        // formats records the confirmed ground truth.
        const positional = schema.row(Format.CSV, utf8('7,"hey"'));
        expect(positional.outcome).toBe('accepted');
        expect(positional.values.map((v) => [v.column, v.text])).toEqual([
          ['a', '7'],
          ['b', '"hey"'],
        ]);
        expect(positional.computed).toEqual([{ column: 'm', kind: 'MATERIALIZED', text: '8', bytes: buf('8') }]);
      } finally {
        schema.close();
      }
    });

    it('accepts an empty body as zero rows', () => {
      const schema = lib().compileDdl('x UInt8');
      try {
        const r = schema.rows(Format.JSONEachRow, new Uint8Array(0));
        expect(r.outcome).toBe('accepted');
        expect(r.rowsRead).toBe(0);
        expect(r.rows).toEqual([]);
      } finally {
        schema.close();
      }
    });

    it('refuses to use a closed schema', () => {
      const schema = lib().compileDdl('x UInt8');
      schema.close();
      schema.close(); // idempotent
      expect(() => schema.rows(Format.JSONEachRow, utf8('{"x":1}\n'))).toThrow(/schema is closed/);
    });
  });

  describe('artifact integrity', () => {
    it('verifies library_sha256 before loading, and shares one loaded library', () => {
      // A registry of one version, assembled by symlink, so the hash check runs
      // over a single 200-300 MB library rather than the whole matrix. The loader
      // must follow the symlink: dist/out itself is one into ~/.cache/chtypes.
      const tmp = mkdtempSync(path.join(tmpdir(), 'chtypes-reg-'));
      try {
        symlinkSync(path.join(registry.dir, preferred), path.join(tmp, preferred), 'dir');
        const verified = new Registry(tmp, { verifyChecksums: true });
        expect(verified.versions()).toEqual([preferred]);
        // dlopen is refcounted, so the same artifact must resolve to the same
        // loaded library rather than being initialised a second time.
        expect(verified.for(preferred).version).toBe(registry.for(preferred).version);
        const schema = verified.for(preferred).compileDdl('x UInt8');
        try {
          expect(schema.rows(Format.JSONEachRow, utf8('{"x":7}\n')).rows[0]!.values[0]!.text).toBe('7');
        } finally {
          schema.close();
        }
      } finally {
        rmSync(tmp, { recursive: true, force: true });
      }
    });

    it('refuses an artifact whose bytes do not match its manifest', () => {
      const tmp = mkdtempSync(path.join(tmpdir(), 'chtypes-bad-'));
      try {
        const src = path.join(registry.dir, preferred);
        const dst = path.join(tmp, preferred);
        mkdirSync(dst);
        const manifest = JSON.parse(readFileSync(path.join(src, 'manifest.json'), 'utf8')) as Record<string, unknown>;
        // Absent fields must not be required, so this copy carries no library_bytes.
        delete manifest['library_bytes'];
        // A tiny stand-in for the library: the point is that the hash is checked
        // before anything is dlopen'd, so no real load happens here.
        writeFileSync(path.join(dst, manifest['library'] as string), 'not a shared library');
        writeFileSync(path.join(dst, 'manifest.json'), JSON.stringify(manifest));
        expect(() => new Registry(tmp, { verifyChecksums: true })).toThrow(/sha256 .* does not match manifest/);
      } finally {
        rmSync(tmp, { recursive: true, force: true });
      }
    });
  });

  describe('memory discipline', () => {
    it('frees every string it takes, with the owning library\'s chs_free', () => {
      const schema = lib().compileDdl('x UInt8, s String');
      try {
        const body = utf8('{"x":256,"s":"hi"}\n');
        const before = nativeStats();
        for (let i = 0; i < 2000; i++) schema.rows(Format.JSONEachRow, body);
        const after = nativeStats();
        expect(after.stringsTaken - before.stringsTaken).toBe(2000);
        // Nothing may be left owned: taken and freed must move in lockstep.
        expect(after.stringsFreed).toBe(after.stringsTaken);
      } finally {
        schema.close();
      }
    });
  });
});
