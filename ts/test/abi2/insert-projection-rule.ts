/**
 * The projection rule of `chs_preview_insert` as a function over this binding's
 * decoded results: `scripts/abi-v2/check-insert-projection.py`'s `evaluate`, rule
 * by rule and in the same order. A test helper, not public API.
 *
 * `projectionVerdict(insert, outputBytes, batch, exportBytes, outputFormat, rawInsert, tRows)`
 * returns the FIRST violated rule, or the carve-out the case is equal by, or
 * equal. The output and the export bytes are passed on their own because a
 * decoded result hides the buffer of an output that does not exist (as it
 * should), while the rule is about exactly the buffers a broken library could
 * hand out anyway. `rawInsert` is the insert document as parsed JSON: the public
 * `InsertResult` has no `export_declined*` member, and the rule must still see
 * one that a broken library wrote. `tRows` is a case's `batch_of_t_rows`,
 * decoded: the indexes of the body records the insert keeps, the batch over
 * those records alone, with its export, the case's `body_records` and its
 * `failing_t_record`; whether that batch is accepted decides which of
 * `post_where_cut` and `post_where_refusal` the case is held to.
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
  'export_decline_is_call_decline',
  'post_where_cut',
  'post_where_refusal',
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

export interface BodyRecord {
  readonly off: number;
  readonly len: number;
}

export interface BatchOfTRows {
  readonly rows: readonly number[];
  readonly batch: BatchResult;
  readonly exp: Buffer | undefined;
  /** The case's body_records: one span per body record, in body order; `rows` index these. */
  readonly records: readonly BodyRecord[];
  /** The case's failing_t_record: the first failing kept record's body index; undefined where the t rows' batch is accepted. */
  readonly failing?: number;
}

const INSERT_NEVER = ['export_declined', 'export_declined_b64', 'export_declined_reason'];

const EQUAL: ProjectionVerdict = { kind: 'equal' };
const violates = (rule: (typeof RULES)[number]): ProjectionVerdict => ({ kind: 'violates', rule });

// ClickHouse's own row-reader suffix: a colon, a space, `(at row N)` and a line feed. Matched on latin1
// text with no trimming, and `$` without the m flag matches only at the very end in JavaScript.
const SUFFIX = /^: \(at row [0-9]+\)\n$/;

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

const postWhereSite = (s: string | undefined): boolean => s === 'row' || s === 'body_step';

/**
 * The t rows' batch reads exactly the listed records, in order: one row each, each input_span as long as its body record, none
 * skipped. A batch refused at a row stops reading there, so then its rows end at the refused row.
 */
function sameRecords(tRows: BatchOfTRows): boolean {
  const { rows: idx, batch: sub, records } = tRows;
  const stop = sub.outcome === 'rejected' && sub.refusedAt === 'row' && sub.refusedRow !== undefined && sub.refusedRow >= 0 && sub.refusedRow < idx.length;
  const n = stop ? (sub.refusedRow as number) + 1 : idx.length;
  if (sub.rows.length !== n || sub.rowsRead !== n || sub.rowsSkipped !== 0) return false;
  return sub.rows.every((r, j) => r.inputSpan !== undefined && r.inputSpan.len === records[idx[j] as number]?.len);
}

/**
 * The checker's `_post_where_refusal`: the insert is `rejected` with the code of both batches and the t rows' batch's err, and
 * its rows_read is exactly the failing kept record's body index + 1.
 */
function postWhereRefusal(ins: InsertResult, out: Buffer | undefined, batch: BatchResult, rawInsert: Record<string, unknown>, tRows: BatchOfTRows): boolean {
  const sub = tRows.batch;
  if (batch.outcome !== 'rejected' || !postWhereSite(batch.refusedAt) || !sameRecords(tRows)) return false;
  if (sub.outcome !== 'rejected' || !postWhereSite(sub.refusedAt)) return false;
  const idx = tRows.rows;
  if (idx.length === 0) return false;
  if (sub.refusedAt === 'row' && !(sub.refusedRow !== undefined && sub.refusedRow >= 0 && sub.refusedRow < idx.length)) return false;
  if (ins.outcome !== 'rejected' || INSERT_NEVER.some((k) => k in rawInsert)) return false;
  // rows_read: exact. A refused insert, like the batch, stops at its refusing record, so it has read through the first
  // failing kept record, whose body index the case carries
  if (tRows.failing === undefined) return false;
  return (
    // the outcome class and code stay equal, to the whole batch and to the t rows' batch
    ins.errCode === batch.errCode &&
    ins.errCode === sub.errCode &&
    // err: the server's for the first failing kept row, numbered over the kept rows
    ins.errMsg.equals(sub.errMsg) &&
    batch.reason === ins.reason &&
    batch.refusedAt === ins.refusedAt &&
    batch.partitionCount === ins.partitionCount &&
    ins.rowsRead === tRows.failing + 1 &&
    batch.rowsSkipped === ins.rowsSkipped &&
    buffersEqual(batch.unsupportedSettings, ins.unsupportedSettings) &&
    isDeepStrictEqual(batch.framing, ins.framing) &&
    ins.rowsPassed === 0 &&
    ins.rowsCut === 0 &&
    ins.rowsUndecided === 0 &&
    !('export_columns' in rawInsert) &&
    out === undefined
  );
}

/** The checker's `_post_where_cut`: the verdict when the pair holds, `undefined` when it does not. */
function postWhereCut(
  ins: InsertResult,
  out: Buffer | undefined,
  batch: BatchResult,
  rawInsert: Record<string, unknown>,
  tRows: BatchOfTRows | undefined,
): ProjectionVerdict | undefined {
  if (tRows === undefined) return undefined; // an accepted insert over a refused body, with no t rows' batch
  const { rows: idx, batch: sub, exp: subExp } = tRows;
  const rows = batch.rows;
  if (batch.outcome !== 'rejected' || !postWhereSite(batch.refusedAt)) return undefined;
  if (batch.refusedAt === 'row' && batch.refusedRow !== undefined && idx.includes(batch.refusedRow)) return undefined;
  // the rows the batch read: it stops at its refusal, so a kept row past it is not among them
  if (idx.some((i) => i < rows.length && rows[i]?.outcome !== 'accepted')) return undefined;
  // the t rows' batch: those records, read alone, accepted, every one t
  if (!sameRecords(tRows)) return undefined;
  if (sub.outcome !== 'accepted' || sub.rows.some((r) => r.verdict !== 't')) return undefined;
  // the t rows' batch's rows_passed is the number of t rows, which the insert's equals
  if (sub.rowsPassed !== idx.length) return undefined;
  if (sub.exportDeclined.length > 0 || sub.exportDeclinedReason !== undefined) return undefined;
  // the insert: the t rows' insert, over the whole body's read
  if (INSERT_NEVER.some((k) => k in rawInsert)) return undefined;
  if (
    !(
      sub.outcome === ins.outcome &&
      sub.errCode === ins.errCode &&
      sub.reason === ins.reason &&
      sub.refusedAt === ins.refusedAt &&
      optBuffersEqual(sub.exportColumns, ins.exportColumns) &&
      sub.partitionCount === ins.partitionCount &&
      batch.rowsSkipped === ins.rowsSkipped &&
      buffersEqual(batch.unsupportedSettings, ins.unsupportedSettings) &&
      isDeepStrictEqual(batch.framing, ins.framing)
    )
  ) {
    return undefined;
  }
  // the insert read the whole body, which the batch, stopped at its refusal, did not
  if (ins.rowsRead !== tRows.records.length) return undefined;
  const cut = ins.rowsRead - ins.rowsSkipped - idx.length;
  if (!(ins.rowsPassed === idx.length && ins.rowsCut === cut && ins.rowsUndecided !== undefined && ins.rowsUndecided >= 0 && ins.rowsUndecided <= cut)) {
    return undefined;
  }
  if (!ins.errMsg.equals(sub.errMsg)) return undefined;
  if ((subExp === undefined) !== (out === undefined)) return undefined;
  if (out === undefined || subExp === undefined) return EQUAL;
  if (sub.rows.some((r) => r.columns.some((c) => c.source === 'default_generated'))) {
    return { kind: 'exception', name: 'generator_draw' };
  }
  // every row of the t rows' batch is t, so its whole export is the t rows' output
  return out.equals(subExp) ? EQUAL : undefined;
}

export function projectionVerdict(
  ins: InsertResult,
  out: Buffer | undefined,
  batch: BatchResult,
  exp: Buffer | undefined,
  outputFormat: string,
  rawInsert: Record<string, unknown>,
  tRows?: BatchOfTRows,
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

  // export_decline_is_call_decline: a declined export of an accepted batch is the insert's whole-call decline.
  const declined = batch.outcome === 'accepted' && (batch.exportDeclined.length > 0 || batch.exportDeclinedReason !== undefined);
  if (declined) {
    if (
      !(
        ins.outcome === 'unsupported' &&
        ins.reason === batch.exportDeclinedReason &&
        ins.errCode === -2 &&
        ins.refusedAt === 'body_step' &&
        ins.exportColumns === undefined &&
        out === undefined &&
        ins.errMsg.equals(batch.exportDeclined) &&
        (!filtered || (ins.rowsPassed === 0 && ins.rowsCut === 0 && ins.rowsUndecided === 0))
      )
    ) {
      return violates('export_decline_is_call_decline');
    }
  }

  // post_where_cut and post_where_refusal split on the t rows' batch, accepted or refused.
  if (tRows !== undefined && tRows.batch.outcome !== 'accepted') {
    return postWhereRefusal(ins, out, batch, rawInsert, tRows) ? EQUAL : violates('post_where_refusal');
  }
  if (tRows !== undefined || (filtered && batch.outcome === 'rejected' && ins.outcome === 'accepted')) {
    return postWhereCut(ins, out, batch, rawInsert, tRows) ?? violates('post_where_cut');
  }

  // shared_members: every member both documents may carry, equal and absent where absent.
  // An insert document never carries export_declined*; a declined output replaces the call's decline members.
  if (INSERT_NEVER.some((k) => k in rawInsert)) return violates('shared_members');
  // The batch decoder reads an absent count as 0, so a count is absent on the batch exactly when it had no filter.
  const batchPassed = filtered ? batch.rowsPassed : undefined;
  const batchCut = filtered ? batch.rowsCut : undefined;
  if (
    !(
      (declined || (batch.outcome === ins.outcome && batch.errCode === ins.errCode && batch.reason === ins.reason && batch.refusedAt === ins.refusedAt)) &&
      batch.rowsRead === ins.rowsRead &&
      batch.rowsSkipped === ins.rowsSkipped &&
      buffersEqual(batch.unsupportedSettings, ins.unsupportedSettings) &&
      isDeepStrictEqual(batch.framing, ins.framing) &&
      batch.partitionCount === ins.partitionCount &&
      (declined || optBuffersEqual(batch.exportColumns, ins.exportColumns)) &&
      (declined || (batchPassed === ins.rowsPassed && batchCut === ins.rowsCut))
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
    const want = ins.outcome === 'accepted' ? [t, f + d, d] : [0, 0, 0];
    if (!(good && ins.rowsPassed === want[0] && ins.rowsCut === want[1] && ins.rowsUndecided === want[2])) return violates('counts');
  } else if (ins.rowsPassed !== undefined || ins.rowsCut !== undefined || ins.rowsUndecided !== undefined) {
    return violates('counts');
  }

  // err: the batch's, plus a remainder that is empty or the row-reader suffix (a declined output's err is the decline text, checked above)
  if (!declined) {
    const be = batch.errMsg;
    const ie = ins.errMsg;
    if (ie.length < be.length || !ie.subarray(0, be.length).equals(be)) return violates('err_prefix');
    const rest = ie.subarray(be.length);
    if (rest.length > 0 && !SUFFIX.test(rest.toString('latin1'))) return violates('err_suffix');
  }

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
