/**
 * The `chs_format` integer codes — the wire format of the bytes handed to
 * `Schema#row` / `Schema#rows`. These numbers are part of the C ABI
 * (the core repository's C ABI specification §Types and schemas) and MUST NOT be renumbered.
 *
 * Name-addressed: `JSONEachRow`. Positional (the k-th field lands in the k-th
 * insertable column; MATERIALIZED / ALIAS / EPHEMERAL occupy no position):
 * `CSV`, `TSV`, `Values`, `JSONCompactEachRow`, the RowBinary family, and
 * `Buffers`. `Native` is column-oriented and name-addressed.
 *
 * The RowBinary family, `Native` and `Buffers` depend on when the loaded
 * ARTIFACT was linked, not on this package's version — probe the artifact
 * (feed it one payload) rather than assuming (docs/reference/bindings.md §Values a
 * binding must accept and reject).
 */
export const Format = {
  /** One JSON object per line, fields matched to columns by name. */
  JSONEachRow: 0,
  /** Comma-separated, positional. A quoted field can contain a newline. */
  CSV: 1,
  /** Tab-separated, positional; `\N` is null. The one text format whose result documents carry `wire` (detector 3). */
  TSV: 2,
  /** The SQL `VALUES` tuple syntax, positional. */
  Values: 3,
  /** One JSON array per line, positional. */
  JSONCompactEachRow: 4,
  /** ClickHouse's binary row encoding, positional. All-or-nothing per batch. */
  RowBinary: 5,
  /** RowBinary where each field is preceded by a use-default flag byte. */
  RowBinaryWithDefaults: 6,
  /**
   * RowBinary prefixed with a names+types header, defaults flagged per field.
   * Parses from 26.6 on; earlier servers (and earlier artifacts) answer 73
   * `UNKNOWN_FORMAT`, exactly as those servers do.
   */
  RowBinaryWithNamesAndTypesAndDefaults: 7,
  /**
   * Column-oriented, self-describing, and what every ClickHouse client library
   * sends on INSERT. Modeled at the revision `INSERT ... FORMAT Native` uses
   * (0): no BlockInfo prefix and no per-column serialization-kind byte. Blocks
   * taken off a live TCP connection carry both and are a different contract
   * (the core repository's C ABI specification §Native). Requires an artifact built at or after the Native
   * exposure — probe it, do not assume it from this package's version.
   */
  Native: 8,
  /**
   * ClickHouse 26.5+. Native's column encoding under a length-prefixed frame
   * that carries NO names and NO types: per block a uint64le column count, a
   * uint64le row count, then per column a uint64le byte size followed by that
   * column's bytes exactly as Native writes them
   * (`src/Formats/BuffersReader.h:13-24`).
   *
   * The missing names and types are the whole point. Native can reconcile a
   * producer against a consumer; Buffers cannot, so a schema disagreement of
   * EQUAL width is undetectable in band and lands as a silently reinterpreted
   * value. Only a WIDTH disagreement is caught. Earlier vendored trees answer
   * 73 UNKNOWN_FORMAT, exactly as their servers do. Requires an artifact built
   * at or after the Buffers exposure — probe it, do not assume it from this
   * package's version.
   */
  Buffers: 9,
} as const;

/** One of the `chs_format` integer codes — see the `Format` constant object. */
export type Format = (typeof Format)[keyof typeof Format];

/**
 * The `chs_rows` export sentinel: no export requested (`CHS_EXPORT_NONE`,
 * revision 3). NOT a member of `Format` — it is legal only as
 * `RowsOptions#exportFormat`'s implicit default, where it means "give me the
 * document shape `docFlags` selects, emit no bytes".
 */
export const EXPORT_NONE = -1;

/**
 * The `doc_flags` bits (`CHS_DOC_*`, revision 3) — which document GROUPS the
 * per-row documents carry (the core repository's C ABI specification §Document flags). The verdict
 * channel (batch and per-row outcome/code/err, rows_read, rows_skipped,
 * unsupported_settings, engine_rows, storage_transforms) is ALWAYS emitted
 * and is not a flag. `DOC_ALL` is today's full document; `0` is "lean"
 * (verdicts only). A bit outside `DOC_ALL` is refused loudly by the library —
 * pass flags through, never pre-validate them here.
 *
 * The cost asymmetry, so callers can reason: `DOC_VALUES` without
 * `DOC_TRANSFORMS` skips the reference second-parse and the wire round trip
 * C-side — real compute saved, and the precise detectors have nothing to run
 * on (the caller's explicit choice, not data loss). `DOC_TRANSFORMS` without
 * `DOC_VALUES` still computes both and saves only bytes: `cols[]` keeps
 * exactly the entries a change detector could fire on, each with its full
 * field set, so `transformed` is derived by the same detectors as always.
 */
export const DOC_VALUES = 0x1;
/** The change-detection channel — see `DOC_VALUES` for the asymmetry. */
export const DOC_TRANSFORMS = 0x2;
/** `computed[]` and (without `DOC_VALUES`) the `default_substituted` entries. */
export const DOC_DEFAULTS = 0x4;
/** Today's full document — what a plain `Schema#rows` always requests. */
export const DOC_ALL = DOC_VALUES | DOC_TRANSFORMS | DOC_DEFAULTS;

/**
 * The name of a format code, for error messages and logs.
 *
 * @param f - a `Format` code.
 * @returns the constant's name (`"JSONEachRow"`), or `"format(<n>)"` for a
 *   number that is not a defined code. Never throws.
 */
export function formatName(f: Format): string {
  for (const [name, code] of Object.entries(Format)) {
    if (code === f) return name;
  }
  return `format(${String(f)})`;
}
