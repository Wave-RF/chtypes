/**
 * The projection rule of `chs_preview_insert` as a function over this binding's
 * decoded results: `scripts/abi-v2/check-insert-projection.py`'s `evaluate`, rule
 * by rule and in the same order. A test helper, not public API.
 *
 * `projectionVerdict(insert, outputBytes, batch, exportBytes, outputFormat)`
 * returns the FIRST violated rule, or the carve-out the case is equal by, or
 * equal. The output and the export bytes are passed on their own because a
 * decoded result hides the buffer of a declined output (as it should), while the
 * rule is about exactly the buffers a broken library could hand out anyway.
 *
 * Whether the batch carried a filter is read off its rows: a verdict is present
 * on every row exactly when the call had a filter (`batch.verdict_iff_filter`),
 * and `BatchResult.rowsPassed` defaults to 0, so it cannot say. A filtered batch
 * of no rows would read as unfiltered here; the fixture holds none.
 */

import { isDeepStrictEqual } from 'node:util';
import type { BatchResult, InsertResult } from '../../src/documents.js';

export const RULES = [
  'e_row_rejects',
  'shared_members',
  'counts',
  'err_prefix',
  'err_suffix',
  'output_presence',
  'non_t_row',
  'line_output',
  'native_output',
] as const;
export const EXCEPTIONS = ['generator_draw'] as const;

export type ProjectionVerdict =
  | { readonly kind: 'equal' }
  | { readonly kind: 'exception'; readonly name: (typeof EXCEPTIONS)[number] }
  | { readonly kind: 'violates'; readonly rule: (typeof RULES)[number] };

const EQUAL: ProjectionVerdict = { kind: 'equal' };
const violates = (rule: (typeof RULES)[number]): ProjectionVerdict => ({ kind: 'violates', rule });

// ClickHouse's own suffix. `\n?` mirrors Python's `$`, which also matches before a trailing newline.
const SUFFIX = /^ \(at row [0-9]+\)\n?$/;

function buffersEqual(a: readonly Buffer[], b: readonly Buffer[]): boolean {
  return a.length === b.length && a.every((x, i) => x.equals(b[i] as Buffer));
}

function optBuffersEqual(a: readonly Buffer[] | undefined, b: readonly Buffer[] | undefined): boolean {
  return a === undefined || b === undefined ? a === b : buffersEqual(a, b);
}

/** The number of lines `bytes.splitlines(keepends=True)` yields: a terminator is `\n`, `\r` or `\r\n`. */
function lineCount(b: Buffer): number {
  let n = 0;
  let start = 0;
  for (let i = 0; i < b.length; i++) {
    if (b[i] === 0x0a || b[i] === 0x0d) {
      if (b[i] === 0x0d && b[i + 1] === 0x0a) i++;
      n++;
      start = i + 1;
    }
  }
  return start < b.length ? n + 1 : n;
}

export function projectionVerdict(
  ins: InsertResult,
  out: Buffer | undefined,
  batch: BatchResult,
  exp: Buffer | undefined,
  outputFormat: string,
): ProjectionVerdict {
  const rows = batch.rows;
  const verdicts = rows.map((r) => r.verdict);
  const filtered = rows.length > 0 && verdicts.every((v) => v !== undefined);

  // e_row_rejects
  if (batch.outcome === 'accepted' && verdicts.includes('e')) {
    const first = rows[verdicts.indexOf('e')];
    if (
      !(
        first !== undefined &&
        ins.outcome === 'rejected' &&
        ins.errCode === first.verdictCode &&
        ins.errMsg.equals(first.verdictErr) &&
        ins.refusedAt === 'row' &&
        ins.rowsPassed === 0 &&
        ins.rowsCut === 0 &&
        ins.rowsUndecided === 0 &&
        ins.rowsRead === batch.rowsRead &&
        out === undefined
      )
    ) {
      return violates('e_row_rejects');
    }
    return EQUAL;
  }

  // shared_members: every member both documents may carry, equal and absent where absent.
  // The batch decoder reads an absent count as 0, so a count is absent on the batch exactly when it had no filter.
  const batchPassed = filtered ? batch.rowsPassed : undefined;
  const batchCut = filtered ? batch.rowsCut : undefined;
  if (
    !(
      batch.outcome === ins.outcome &&
      batch.errCode === ins.errCode &&
      batch.reason === ins.reason &&
      batch.refusedAt === ins.refusedAt &&
      batch.rowsRead === ins.rowsRead &&
      batch.rowsSkipped === ins.rowsSkipped &&
      buffersEqual(batch.unsupportedSettings, ins.unsupportedSettings) &&
      isDeepStrictEqual(batch.framing, ins.framing) &&
      batch.partitionCount === ins.partitionCount &&
      optBuffersEqual(batch.exportColumns, ins.exportColumns) &&
      batch.exportDeclined.equals(ins.exportDeclined) &&
      batch.exportDeclinedReason === ins.exportDeclinedReason &&
      batchPassed === ins.rowsPassed &&
      batchCut === ins.rowsCut
    )
  ) {
    return violates('shared_members');
  }

  // counts
  const t = verdicts.filter((v) => v === 't').length;
  const f = verdicts.filter((v) => v === 'f').length;
  const d = verdicts.filter((v) => v === 'd').length;
  if (filtered) {
    const good = batch.outcome === 'accepted' ? batch.rowsPassed === t && batch.rowsCut === f + d : batch.rowsPassed === 0 && batch.rowsCut === 0;
    if (!(good && ins.rowsUndecided === d)) return violates('counts');
  } else if (ins.rowsPassed !== undefined || ins.rowsCut !== undefined || ins.rowsUndecided !== undefined) {
    return violates('counts');
  }

  // err
  const be = batch.errMsg;
  const ie = ins.errMsg;
  if (ie.length < be.length || !ie.subarray(0, be.length).equals(be)) return violates('err_prefix');
  const rest = ie.subarray(be.length);
  if (rest.length > 0 && !SUFFIX.test(rest.toString('latin1'))) return violates('err_suffix');

  // output
  if ((exp === undefined) !== (out === undefined)) return violates('output_presence');
  if (out === undefined || exp === undefined) return EQUAL;
  if (rows.some((r) => r.columns.some((c) => c.source === 'default_generated'))) {
    return { kind: 'exception', name: 'generator_draw' };
  }
  if (outputFormat === 'Native') return out.equals(exp) ? EQUAL : violates('native_output');
  const spans = batch.spans ?? [];
  const tLines: Buffer[] = [];
  verdicts.forEach((v, i) => {
    const s = spans[i];
    if ((v === undefined || v === 't') && s !== null && s !== undefined) tLines.push(exp.subarray(s.off, s.off + s.len));
  });
  // The spans of non-t rows are null, so a stray row is seen as a line beyond the t rows.
  if (lineCount(out) > tLines.length) return violates('non_t_row');
  return out.equals(Buffer.concat(tLines)) ? EQUAL : violates('line_output');
}
