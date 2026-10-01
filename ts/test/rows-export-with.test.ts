/**
 * Unit-level coverage for issue #54 (`Schema#rows(..., { rowFilter })`): the
 * pure document-decoding path (`batchResultOf` against a hand-built
 * chs_rows-with-attached-filter document, shaped exactly as the C ABI
 * contract §Rows describes it) and the TypeScript-level cross-library
 * refusal `Schema#rows` performs before any C call.
 *
 * The chtypes#298 regression tests at the end of this file are the
 * exception: each needs a revision-5 artifact (a real `Filter` compiled and
 * evaluated through `chs_rows`) and skips loudly without one.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { ChtypesError } from '../src/errors.js';
import type { BlockHandle, FilterHandle, NativeLibrary, SchemaHandle } from '../src/ffi.js';
import { Format } from '../src/format.js';
import { parseDocument } from '../src/json.js';
import type { Library } from '../src/library.js';
import { looksLikeRegistry, Registry, resolveRegistryDir } from '../src/registry.js';
import { batchResultOf, isAnswer, Outcome, Verdict } from '../src/results.js';
import { Filter, Schema } from '../src/schema.js';

// A stand-in for NativeLibrary: just enough surface (`columns`) for
// `Schema`'s constructor, with no real dlopen behind it. `schemaFree` /
// `filterFree` / `blockFree` are also no-ops here, never exercised by these
// tests directly — but `Schema`'s own `FinalizationRegistry` callback (see
// `schema.ts`) can still call them on an abandoned fake `Schema`/`Filter`
// whenever the GC happens to collect one during this file's run, and a
// missing method there throws inside that callback, failing the whole
// suite on a timing accident unrelated to what the test actually checks.
function fakeNative(): NativeLibrary {
  return {
    columns: () => [],
    schemaFree: () => {},
    filterFree: () => {},
    blockFree: () => {},
  } as unknown as NativeLibrary;
}

describe('rows(..., { rowFilter }) verdict decoding', () => {
  it('decodes all four verdict characters plus rows_passed/rows_cut from a hand-built document', () => {
    // Four rows exercising all four verdict characters, one of them ('d')
    // on a row whose own parse outcome is not accepted, plus rows_passed/
    // rows_cut at the batch level. This is the acceptance-bar test for
    // property (3) — bytes only for 't', e/d never collapsed into f — at
    // the decoding layer.
    const raw = `{
      "outcome":"accepted","code":0,"err":"","rows_read":4,"rows_skipped":0,
      "rows_passed":1,"rows_cut":3,
      "rows":[
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"t"},
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"f"},
        {"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"e","verdict_code":386,"verdict_err":"no common type"},
        {"outcome":"skipped","code":117,"err":"bad row","cols":[],"verdict":"d","verdict_code":117,"verdict_err":"bad row"}
      ],
      "row_spans":[{"off":0,"len":5},{"off":0,"len":0},{"off":0,"len":0},{"off":0,"len":0}]
    }`;
    const res = batchResultOf(parseDocument(Buffer.from(raw, 'utf8')));

    expect(res.rowsPassed).toBe(1);
    expect(res.rowsCut).toBe(3);
    expect(res.rowsPassed + res.rowsCut).toBe(4);

    const want: Verdict[] = ['t', 'f', 'e', 'd'];
    expect(res.rows.map((r) => r.verdict)).toEqual(want);

    // Property (3), directly: neither non-answer decoded as 'f'.
    expect(res.rows[2]!.verdict).not.toBe('f');
    expect(res.rows[3]!.verdict).not.toBe('f');
    expect(isAnswer(res.rows[2]!.verdict!)).toBe(false);
    expect(isAnswer(res.rows[3]!.verdict!)).toBe(false);

    // Row 3's own outcome is not accepted, and it still carries
    // verdict_code / verdict_err beside verdict, per the contract.
    expect(res.rows[3]!.outcome).toBe('skipped');
    expect(res.rows[3]!.verdictCode).toBe(117);
    expect(res.rows[3]!.verdictErr).toBe('bad row');
    expect(res.rows[2]!.verdictCode).toBe(386);
    expect(res.rows[2]!.verdictErr).toBe('no common type');
  });

  it('leaves verdict undefined and rowsPassed/rowsCut at 0 for an ordinary document', () => {
    const raw = `{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_skipped":0,
      "rows":[{"outcome":"accepted","code":0,"err":"","cols":[]}]}`;
    const res = batchResultOf(parseDocument(Buffer.from(raw, 'utf8')));
    expect(res.rowsPassed).toBe(0);
    expect(res.rowsCut).toBe(0);
    expect(res.rows[0]!.verdict).toBeUndefined();
  });
});

describe('Schema#rows cross-library filter refusal', () => {
  it('throws before any C call when the filter comes from a different loaded library', () => {
    // Property (8)'s TypeScript-level half: a filter from a DIFFERENT
    // loaded library is refused before any C call — no handle crosses a
    // dlopen'd image boundary, the same rule Filter#eval enforces for a
    // (filter, block) pair. Needs no dlopen'd artifact: the refusal fires
    // on the NativeLibrary identity check, before `this.live()` or any
    // handle is touched.
    //
    // The SAME-library-different-schema half of (8) (code 1002) is the
    // server's own answer and cannot be exercised without a loaded
    // artifact; it is wired (Schema#rows passes the filter handle straight
    // through when the libraries match) but not run here.
    const schema = new Schema(fakeNative(), {} as unknown as SchemaHandle, 'x UInt8');
    const otherNative = fakeNative();
    const otherSchema = new Schema(otherNative, {} as unknown as SchemaHandle, 'x UInt8');
    // A stand-in for Schema's own GC-finalizer coordination state (schema.ts's
    // unexported SchemaNative) — this test never closes anything, so only the
    // shape needs to match, not a real schema's bookkeeping.
    const otherSchemaNative = {
      native: otherNative,
      handle: {} as unknown as SchemaHandle,
      openFilters: new Set<FilterHandle>(),
      openBlocks: new Set<BlockHandle>(),
    };
    const filter = new Filter(otherNative, otherSchema, otherSchemaNative, {} as unknown as FilterHandle, '1');

    expect(() => schema.rows(0, new Uint8Array(), undefined, { rowFilter: filter })).toThrow(ChtypesError);
  });
});

// ------------------------------------------------------- chtypes#298 (G1-G5)
//
// The artifact producer's relink served at chtypes_build 1790845279 changed
// four document fields by design, measured against that build and against
// the immediately preceding build (1790783214) of the SAME ClickHouse patch
// on darwin-arm64 — same ABI revision, only the library rebuilt:
//
// - G2: `verdictCode`/`verdictErr` are now populated on a non-answered
//   verdict whose row is not itself accepted (measured: 0/'' before, the
//   row's own code/message after).
// - G3: `rowsPassed`/`rowsCut` are now 0 for a batch whose own outcome is
//   not `Outcome.Accepted`, even when an individual row before the one
//   that aborted it was itself accepted with a `'t'` verdict (measured:
//   1/0 before, 0/0 after).
// - G5: `exportDeclined` is now populated for every non-accepted batch
//   that requested an export, including a batch with ZERO rows (measured
//   on a `Format.CSVWithNames` body naming an unknown header column under
//   `input_format_skip_unknown_fields=0`: `''` before, a reason after).
//
// G4 (a skipped row counts in neither `rowsPassed` nor `rowsCut`) has no
// library change — it is a docs-only clarification — so it is not measured
// here; see docs/guides/filters.md.
//
// The fix reached only the SUPPORTED lines' artifacts (docs/support.md):
// 26.3, 26.7, 26.8 and 26.9, at chtypes_build 1790845279 or later. A served,
// unsupported (retired) line gets no new build or ABI revision, ever, so its
// existing artifact keeps the pre-relink behavior permanently — measured:
// 26.6's newest served build, 1790767905, predates this relink and was
// never republished. `openRev5` also opens such a line (anything ABI
// revision 5+), so the three tests below run against `relinked`, which
// gates `rev5` on the artifact's OWN chtypes_build (`isRelinkedBuild`)
// rather than a hand-typed line list — a hand-typed list goes stale the
// moment a new supported line ships, and silently stops exercising it.

const CSV_REJECTION_CODE = 117;
// RELINK_BUILD_298 is the chtypes_build the chtypes#298 relink was served
// at. It is the one constant isRelinkedBuild is built from: the relink is a
// property of the BUILD, not of which lines happened to be supported the
// day this file was written.
const RELINK_BUILD_298 = 1790845279;

/** `relinked`'s predicate, factored out so it can be pinned against
 * fabricated build numbers without a loaded artifact (see the threshold
 * test below). A missing or zero chtypesBuild — 0 is what a manifest that
 * predates the field reads as — must never be treated as relinked. */
const isRelinkedBuild = (build: number): boolean => build >= RELINK_BUILD_298;

/** Reads library's own manifest.json for its chtypes_build field, the same
 * file and the same place scripts/lib/provenance.py reads it from: next to
 * the loaded library. There is no public accessor for this field — the
 * registry's own Manifest interface does not carry it — so this reads the
 * manifest directly rather than guessing. A manifest that cannot be read or
 * parsed throws: this gate must never default a line it could not actually
 * measure to "relinked". */
const chtypesBuildOf = (library: Library): number => {
  const manifestPath = path.join(path.dirname(library.path), 'manifest.json');
  const doc = JSON.parse(readFileSync(manifestPath, 'utf8')) as { chtypes_build?: number };
  return doc.chtypes_build ?? 0;
};

describe('chtypes#298: isRelinkedBuild threshold', () => {
  it('keeps the relink build, rejects one short of it, and rejects a build of 0', () => {
    expect(isRelinkedBuild(RELINK_BUILD_298)).toBe(true);
    expect(isRelinkedBuild(RELINK_BUILD_298 - 1)).toBe(false);
    expect(isRelinkedBuild(0)).toBe(false);
  });
});

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

/** Every line the test registry holds that loads at ABI revision 5 — the
 * same selection as csv-reader.test.ts's own `openRev5`. */
const openRev5 = (): { registry: Registry; libraries: Library[] } => {
  const dir = REGISTRY ?? undefined;
  const registry = new Registry(dir);
  const libraries: Library[] = [];
  for (const line of registry.versions()) {
    let library: Library;
    try {
      library = registry.for(line);
    } catch (err) {
      console.warn(`[chtypes] line ${line} not exercised: ${(err as Error).message}`);
      continue;
    }
    if (library.abiRevision < 5) {
      console.warn(`[chtypes] line ${line} not exercised: ABI revision ${library.abiRevision}, needs 5`);
      continue;
    }
    libraries.push(library);
  }
  return { registry, libraries };
};

let rev5Registry: Registry | undefined;
let rev5: Library[] = [];
const relinked: Library[] = [];
if (HAVE_REGISTRY) {
  const opened = openRev5();
  rev5Registry = opened.registry;
  rev5 = opened.libraries;
  if (rev5.length === 0) {
    console.warn('[chtypes] chtypes#298 tests SKIPPED: the registry holds no ABI revision-5 artifact.');
  }
  for (const library of rev5) {
    const build = chtypesBuildOf(library);
    if (isRelinkedBuild(build)) {
      relinked.push(library);
    } else {
      console.warn(`[chtypes] skipping ${library.minor} at build ${build}: predates the relink ${RELINK_BUILD_298}`);
    }
  }
  if (relinked.length === 0) {
    console.warn(
      `[chtypes] chtypes#298 tests SKIPPED: the registry holds no artifact at chtypes_build ${RELINK_BUILD_298} or later (the chtypes#298 relink).`,
    );
  }
} else {
  console.warn('[chtypes] chtypes#298 tests SKIPPED: no artifact registry on the search path.');
}

afterAll(() => {
  rev5Registry?.close();
});

describe.skipIf(!HAVE_REGISTRY || relinked.length === 0)('chtypes#298: the relinked contract (G1-G5)', () => {
  it('zeroes rowsPassed/rowsCut and carries verdictCode on a non-accepted batch (G2, G3)', () => {
    // The same strict (no input_format_allow_errors_*) body exercises both
    // at once. Row 0 parses and is individually accepted with verdict 't';
    // row 1 is the CSV reader's own trailing-garbage refusal (code 117),
    // which aborts the batch — so the batch's own outcome is `Rejected`
    // even though row 0, in isolation, was accepted and passed the filter.
    let ran = 0;
    for (const library of relinked) {
      const schema = library.compileDdl('id UInt8, n UInt8');
      const filter = schema.compileFilter('id >= 0');
      try {
        const body = Buffer.from('1,2\n1,abc\n', 'utf8'); // row 1: "abc" in a UInt8 column
        const batch = schema.rows(Format.CSV, body, undefined, { rowFilter: filter });
        const where = library.version;

        expect(batch.outcome, `${where}: the aborting row must still reject the whole batch`).toBe(Outcome.Rejected);
        // G3: the batch's own outcome is not Accepted, so BOTH counts must
        // be zero, regardless of row 0's own accepted-and-'t' verdict.
        expect([batch.rowsPassed, batch.rowsCut], `${where}: rowsPassed/rowsCut, want 0/0 for a non-Accepted batch`).toEqual([0, 0]);
        expect(batch.rows.length, `${where}: want 2 row documents`).toBe(2);

        // G2: row 1's own parse outcome is not Accepted (Rejected, code
        // 117), so its verdict is 'd' and must carry that same
        // code/message beside it — never the 0/'' the pre-relink library
        // left there.
        const row1 = batch.rows[1]!;
        expect(row1.verdict, `${where}: row 1's own outcome is not Accepted`).toBe(Verdict.Decline);
        expect(row1.verdictCode, `${where}: verdictCode, want the row's own error code`).toBe(CSV_REJECTION_CODE);
        expect(row1.verdictErr, `${where}: verdictErr must not be empty`).not.toBe('');
        expect(row1.verdictCode, `${where}: verdictCode must equal the row's own errCode`).toBe(row1.errCode);
        ran++;
      } finally {
        filter.close();
        schema.close();
      }
    }
    expect(ran, 'ran the wrong number of cases').toBe(relinked.length);
    expect(ran, 'ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });

  it('names the export decline on a rejected zero-row batch (G5)', () => {
    // A CSVWithNames body naming a header column no schema column matches,
    // read under input_format_skip_unknown_fields=0, is refused before a
    // single row is admitted — Rejected, rowsRead 0, zero row documents —
    // and an export was requested. The pre-relink library left
    // exportDeclined empty here even though bytes were withheld; the
    // relinked one names the batch's own outcome as the reason.
    let ran = 0;
    for (const library of relinked) {
      const schema = library.compileDdl('id UInt8, p String');
      try {
        const body = Buffer.from('id,unknown_col\n1,a\n', 'utf8');
        const batch = schema.rows(Format.CSVWithNames, body, { input_format_skip_unknown_fields: '0' }, { exportFormat: Format.JSONCompactEachRow });
        const where = library.version;

        expect(batch.outcome, `${where}`).toBe(Outcome.Rejected);
        expect(batch.rowsRead, `${where}: want a call-level refusal with no rows`).toBe(0);
        expect(batch.rows.length, `${where}`).toBe(0);
        expect(batch.payload, `${where}: a rejected batch exports nothing`).toBeUndefined();
        expect(batch.exportDeclined, `${where}: exportDeclined must not be empty (chtypes#298, G5)`).toBeTruthy();
        ran++;
      } finally {
        schema.close();
      }
    }
    expect(ran, 'ran the wrong number of cases').toBe(relinked.length);
    expect(ran, 'ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });

  it('declines session_timezone rather than silently ignoring it (G1)', () => {
    // ClickHouse's session_timezone is a real, known setting name — never
    // the server's own code 115 — but this library resolves bare-DateTime
    // timezone once, process-wide, at chs_init, and never re-reads it per
    // call. Before the relink, a per-call session_timezone was silently
    // accepted and had no effect, indistinguishable from agreement. The
    // relinked library declines it the same way an unmodeled MergeTree
    // setting is declined (docs/guides/settings.md): it comes back in
    // unsupportedSettings, which promotes the row's own outcome to
    // Unsupported.
    let ran = 0;
    for (const library of relinked) {
      const schema = library.compileDdl('id UInt8, t DateTime');
      try {
        const result = schema.row(Format.JSONEachRow, Buffer.from('{"id":1,"t":"2024-01-01 00:00:00"}', 'utf8'), {
          session_timezone: 'Europe/Berlin',
        });
        const where = library.version;

        expect(result.unsupportedSettings, `${where}: sent on a per-call map, this is a decline, never a silent admission`).toContain(
          'session_timezone',
        );
        expect(result.outcome, `${where}: a non-empty unsupportedSettings must promote the row (docs/guides/settings.md)`).toBe(
          Outcome.Unsupported,
        );
        ran++;
      } finally {
        schema.close();
      }
    }
    expect(ran, 'ran the wrong number of cases').toBe(relinked.length);
    expect(ran, 'ran ZERO cases — a block that asserts nothing is not a pass').toBeGreaterThan(0);
  });
});
