/**
 * The TS leg of `v1-abi-conformance`: every case in
 * `tests/fixtures/abi-v1/cases.json`, run through `../../src/abi1/loader.ts`
 * and `../../src/abi1/raw.ts`'s generic `rawCall` dispatcher against the
 * stub libraries `scripts/abi-v1/build-stubs.sh` builds
 * (`$CHTYPES_ABI1_STUBS`), with the result written to
 * `$CHTYPES_ABI1_REPORT` (`spec/abi-v1/schema/report.schema.json`).
 *
 * Without `$CHTYPES_ABI1_STUBS` (every v0 CI leg, and a developer running
 * `pnpm test` locally with no stubs built), every case SKIPS LOUDLY by name
 * — it never passes silently and never fails (the rule every other
 * no-registry test in this repository already follows).
 *
 * Run directly: `scripts/abi-v1/conformance/ts.sh` (what
 * `.github/workflows/v1-abi.yml`'s `v1-abi-conformance` job for `ts` calls).
 */

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import type { JsExternal } from 'ffi-rs';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ArtifactCorruptError, ArtifactIncompatibleError } from '../../src/abi1/errors.js';
import { Library, type LoadInput, openAbi1, type Predicate } from '../../src/abi1/loader.js';
import { NULL_EXTERNAL, type RawApi, type RawCallResult, rawCall } from '../../src/abi1/raw.js';

const STUBS_DIR = process.env.CHTYPES_ABI1_STUBS;
const REPORT_PATH = process.env.CHTYPES_ABI1_REPORT;
const TOOLCHAIN = process.env.CHTYPES_ABI1_TOOLCHAIN ?? 'unknown';
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

const CASES_PATH = path.resolve(import.meta.dirname, '../../../tests/fixtures/abi-v1/cases.json');

// ----------------------------------------------------------------- cases.json

type ArgExpr =
  | { readonly int: number }
  | { readonly bytes_hex: string }
  | { readonly handle: { readonly fn: string; readonly args: readonly ArgExpr[] } }
  | { readonly null_handle: true };

interface CaseEntry {
  readonly id: string;
  readonly kind: 'handshake' | 'echo' | 'status' | 'loader';
  readonly fn?: string;
  readonly variant?: string;
  readonly args?: readonly ArgExpr[];
  readonly expect: any;
}

interface CasesDoc {
  readonly schema: number;
  readonly cases: readonly CaseEntry[];
}

interface StubVariant {
  readonly path: string;
  readonly predicate: Predicate;
  readonly reason: string;
}

interface StubsDoc {
  readonly schema: number;
  readonly variants: Readonly<Record<string, StubVariant>>;
}

// Loaded eagerly (not in beforeAll): it.each needs the real arrays at
// REGISTRATION time, which happens before any lifecycle hook runs.
// cases.json is always in the tree; stubsDoc only when stubsAvailable.
const casesDoc: CasesDoc = JSON.parse(readFileSync(CASES_PATH, 'utf8')) as CasesDoc;
const stubsDoc: StubsDoc | null = stubsAvailable
  ? (JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as StubsDoc)
  : null;

function casesOfKind(kind: CaseEntry['kind']): CaseEntry[] {
  return casesDoc.cases.filter((c) => c.kind === kind);
}

/**
 * Canonical JSON (sorted object keys, no whitespace) — byte-for-byte the
 * same derivation `scripts/abi-v1/parity.py`'s `compute_cases_hash` uses
 * (`json.dumps(cases, sort_keys=True, separators=(",", ":"))`), so this
 * report's `cases_sha256` is the SAME hash parity.py computes from the SAME
 * file, not a second guess at it.
 */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((v) => canonicalJson(v)).join(',')}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(obj[k])}`).join(',')}}`;
}

function platformOf(predicate: Predicate): string {
  return `${predicate.os}-${predicate.arch}`;
}

// ------------------------------------------------------------- arg resolution

/** Resolve one `ArgExpr` (cases.json's input-argument shape) into a value `rawCall` accepts, minting a handle recursively through `rawCall` itself when the expression names one. */
function resolveArg(raw: RawApi, expr: ArgExpr): unknown {
  if ('int' in expr) return expr.int;
  if ('bytes_hex' in expr) return Buffer.from(expr.bytes_hex, 'hex');
  if ('null_handle' in expr) return NULL_EXTERNAL;
  const { fn, args } = expr.handle;
  const resolved = args.map((a) => resolveArg(raw, a));
  const call = rawCall(raw, fn, resolved);
  if (call.outcome !== 'status') throw new Error(`conformance: minting a handle via ${fn} did not return a status`);
  if (call.statusName !== 'CHS_OK') throw new Error(`conformance: minting a handle via ${fn} failed: ${call.statusName}`);
  const handleOut = Object.values(call.outs).find((v) => !Buffer.isBuffer(v));
  if (handleOut === undefined) throw new Error(`conformance: ${fn} produced no handle output`);
  return (handleOut as { readonly ptr: JsExternal }).ptr;
}

// -------------------------------------------------------------- echo matching

/** One echoed argument (cases.json's expected shape) against the stub's actual JSON: a bare number exact, `null` exact, `{len,sha256,head_hex}` exact on those three keys, `{kind}` matching ONLY `kind` (the stub's own `id` field is a serial number no case can predict — scripts/abi-v1/emit/cases.py's own documented rule). */
function echoArgMatches(expected: unknown, actual: unknown): boolean {
  if (expected === null) return actual === null;
  if (typeof expected === 'number') return actual === expected;
  if (typeof expected === 'object' && expected !== null && typeof actual === 'object' && actual !== null) {
    const e = expected as Record<string, unknown>;
    const a = actual as Record<string, unknown>;
    if ('kind' in e) return a.kind === e.kind;
    if ('len' in e && 'sha256' in e && 'head_hex' in e) {
      return a.len === e.len && a.sha256 === e.sha256 && a.head_hex === e.head_hex;
    }
  }
  return false;
}

function echoOutputMatches(
  expected: { readonly fn: string; readonly out: string; readonly args: readonly unknown[] },
  actualParsed: unknown,
): boolean {
  if (typeof actualParsed !== 'object' || actualParsed === null) return false;
  const a = actualParsed as Record<string, unknown>;
  if (a.fn !== expected.fn || a.out !== expected.out) return false;
  const aArgs = a.args;
  if (!Array.isArray(aArgs) || aArgs.length !== expected.args.length) return false;
  return expected.args.every((e, i) => echoArgMatches(e, aArgs[i]));
}

// -------------------------------------------------------------------- report

interface ReportResult {
  readonly id: string;
  readonly pass: boolean;
  readonly detail?: string;
}

const results: ReportResult[] = [];

function record(id: string, run: () => void): void {
  try {
    run();
    results.push({ id, pass: true });
  } catch (err) {
    results.push({ id, pass: false, detail: err instanceof Error ? err.message : String(err) });
    throw err;
  }
}

describe.skipIf(!stubsAvailable)('abi v1 conformance (ts)', () => {
  let okRaw: RawApi;
  let okPredicate: Predicate;

  beforeAll(() => {
    const doc = stubsDoc as StubsDoc;
    const ok = doc.variants.ok;
    if (ok === undefined) throw new Error('conformance: stubs.json has no "ok" variant');
    okPredicate = ok.predicate;
    const okPath = path.join(STUBS_DIR as string, 'ok.so');
    const input: LoadInput = { libraryPath: okPath, predicate: okPredicate, platform: platformOf(okPredicate) };
    okRaw = openAbi1(input).raw;
  });

  afterAll(() => {
    if (REPORT_PATH === undefined) return;
    const cases_sha256 = createHash('sha256').update(canonicalJson(casesDoc.cases)).digest('hex');
    const report = {
      schema: 1,
      binding: 'ts',
      toolchain: TOOLCHAIN,
      os: platformOf(okPredicate),
      cases_sha256,
      results,
    };
    writeFileSync(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  });

  describe('handshake', () => {
    it.each(casesOfKind('handshake'))('$id', (c: CaseEntry) => {
      record(c.id, () => {
        const call = rawCall(okRaw, c.fn as string, []);
        if ('int' in c.expect) {
          expect(call.outcome).toBe('value');
          expect(call.outcome === 'value' ? call.value : undefined).toBe(c.expect.int);
        } else if ('contains' in c.expect) {
          expect(call.outcome).toBe('value');
          const v = call.outcome === 'value' ? call.value : null;
          expect(typeof v).toBe('string');
          expect((v as string).includes(c.expect.contains as string)).toBe(true);
        } else if ('non_empty' in c.expect) {
          expect(call.outcome).toBe('value');
          const v = call.outcome === 'value' ? call.value : null;
          expect(typeof v).toBe('string');
          expect((v as string).length > 0).toBe(true);
        } else {
          throw new Error(`conformance: ${c.id}: unrecognized handshake expect shape`);
        }
      });
    });
  });

  describe('echo', () => {
    it.each(casesOfKind('echo'))('$id', (c: CaseEntry) => {
      record(c.id, () => {
        const args = (c.args ?? []).map((a) => resolveArg(okRaw, a));
        const call: RawCallResult = rawCall(okRaw, c.fn as string, args);
        expect(call.outcome).toBe('status');
        if (call.outcome !== 'status') return;
        expect(call.statusName).toBe(c.expect.status);
        const expectedOutputs = c.expect.outputs as Record<string, { fn: string; out: string; args: unknown[] }> | undefined;
        if (expectedOutputs === undefined) return;
        for (const [name, expected] of Object.entries(expectedOutputs)) {
          const actual = call.outs[name];
          expect(Buffer.isBuffer(actual)).toBe(true);
          const parsed: unknown = JSON.parse((actual as Buffer).toString('utf8'));
          expect(echoOutputMatches(expected, parsed)).toBe(true);
        }
      });
    });
  });

  describe('status', () => {
    it.each(casesOfKind('status'))('$id', (c: CaseEntry) => {
      record(c.id, () => {
        const args = (c.args ?? []).map((a) => resolveArg(okRaw, a));
        const call = rawCall(okRaw, c.fn as string, args);
        expect(call.outcome).toBe('status');
        if (call.outcome !== 'status') return;
        expect(call.statusName).toBe(c.expect.status);
        const expectedError = c.expect.error as { ch_code: number; ch_name: string; message: string; column: string };
        expect(call.error).not.toBeNull();
        if (call.error === null) return;
        expect(call.error.chCode).toBe(expectedError.ch_code);
        expect(call.error.chName).toBe(expectedError.ch_name);
        expect(call.error.messageBytes.toString('utf8')).toBe(expectedError.message);
        expect(call.error.column.toString('utf8')).toBe(expectedError.column);
      });
    });
  });

  describe('loader', () => {
    it.each(casesOfKind('loader'))('$id', (c: CaseEntry) => {
      record(c.id, () => {
        const doc = stubsDoc as StubsDoc;
        const variant = doc.variants[c.variant as string];
        if (variant === undefined) throw new Error(`conformance: ${c.id}: no stub variant ${c.variant}`);
        const soPath = path.join(STUBS_DIR as string, `${c.variant}.so`);
        const input: LoadInput = { libraryPath: soPath, predicate: variant.predicate, platform: platformOf(variant.predicate) };
        const expectedReason = c.expect.reason as string;
        if (expectedReason === 'accepted') {
          const lib = openAbi1(input);
          expect(lib).toBeInstanceOf(Library);
          return;
        }
        let caught: unknown;
        try {
          openAbi1(input);
        } catch (err) {
          caught = err;
        }
        expect(caught).toBeDefined();
        const isLoaderError = caught instanceof ArtifactIncompatibleError || caught instanceof ArtifactCorruptError;
        expect(isLoaderError).toBe(true);
        if (!isLoaderError) return;
        const reason = (caught as ArtifactIncompatibleError | ArtifactCorruptError).reason;
        expect(reason).toBe(expectedReason);
      });
    });

    // The headline case (plan: "the stub's `unbound` variant must REFUSE on
    // both Linux and darwin"), asserted again on its own for visibility
    // beyond the generic loop above: ffi-rs's own `open()` (RTLD_LAZY) would
    // let this load; only the loader's own RTLD_NOW dlopen (./libc.ts) can
    // catch it, on EITHER platform.
    it('the unbound stub (an unresolved external symbol) refuses under RTLD_NOW, on this platform', () => {
      const doc = stubsDoc as StubsDoc;
      const variant = doc.variants.unbound;
      if (variant === undefined) throw new Error('conformance: stubs.json has no "unbound" variant');
      const input: LoadInput = {
        libraryPath: path.join(STUBS_DIR as string, 'unbound.so'),
        predicate: variant.predicate,
        platform: platformOf(variant.predicate),
      };
      let caught: unknown;
      try {
        openAbi1(input);
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(ArtifactIncompatibleError);
      expect((caught as ArtifactIncompatibleError).reason).toBe('dlopen');
    });
  });
});

if (!stubsAvailable) {
  describe('abi v1 conformance (ts)', () => {
    it.skip('CHTYPES_ABI1_STUBS is not set — skipping loudly (build-stubs.sh was not run for this leg)', () => {
      // Intentionally empty: the test name itself is the skip's reason, and
      // `it.skip` reports as SKIPPED, never as a silent pass.
    });
  });
}
