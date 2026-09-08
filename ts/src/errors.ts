/**
 * The error model, which is three outcomes that must never be conflated
 * (spec/c-abi.md §Error model):
 *
 *   - ClickHouse rejects        -> a real ClickHouse error code
 *   - this build refuses        -> CODE_UNSUPPORTED (-2), never a ClickHouse code
 *   - ClickHouse accepts but the value cannot be read back -> code 691, and it
 *     is an ACCEPTED insert, surfaced through the row outcome rather than here.
 *
 * Mapping `unsupported` onto a rejection manufactures an over-reject the product
 * never made; mapping it onto an acceptance manufactures an over-accept, which
 * is the cardinal sin. So it gets its own sentinel on the wire and, since
 * 2026-08-26, its own ERROR CLASS here — `UnsupportedError`, a peer of
 * `SchemaError` rather than a subclass, so that a `catch` which handles only
 * `SchemaError` cannot silently swallow a decline as a rejection.
 */

/** "This build refuses to answer." Never a real ClickHouse error code. */
export const CODE_UNSUPPORTED = -2;

/**
 * The chs_* ABI revision this binding was written against — `CHS_ABI_REVISION`
 * in `include/chtypes.h`.
 *
 * This package dlopens artifacts rather than compiling against the header, so
 * this is a hand-kept mirror and MUST be bumped in the same cycle the header
 * is. A `Registry` refuses an artifact reporting a different nonzero revision;
 * 0 means the artifact predates the probe, which is ignorance rather than
 * incompatibility (spec/artifact.md §Loading).
 *
 * Revision 3 (2026-08-31): `chs_rows` gained `export_format` / `doc_flags` /
 * `out_bytes`, and the `chs_filter_compile` / `chs_filter_free` /
 * `chs_filter_rows` trio joined the surface.
 *
 * Revision 4 (2026-08-31, the filter phase-2 cycle): `chs_filter_compile`
 * gained `params_json` (`{name:Type}` query parameters), and the block twin
 * joined — `chs_block_parse` / `chs_block_free` / `chs_filter_eval`. This
 * binding therefore speaks 4 and refuses revision-3 artifacts — calling the
 * 5-argument `chs_filter_compile` through the 4-argument revision-3 artifact
 * is undefined behaviour, which is exactly what the gate exists to prevent.
 */
export const ABI_REVISION = 4;

/** Base class for everything this package throws. */
export class ChtypesError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** A registry could not be loaded, or a version could not be resolved. */
export class RegistryError extends ChtypesError {}

/**
 * A REFUSAL: ClickHouse itself refused a type, a column list, an engine or a
 * TTL.
 *
 * `code` is ALWAYS a real ClickHouse error code. "This build declines to
 * answer" is a DIFFERENT CLASS — `UnsupportedError` — never this one carrying
 * a sentinel, so no `SchemaError` ever holds `CODE_UNSUPPORTED`
 * (spec/bindings.md rule 12).
 */
export class SchemaError extends ChtypesError {
  readonly code: number;
  readonly detail: string;
  readonly column: string | undefined;

  constructor(code: number, detail: string, column?: string) {
    super(
      column
        ? `chtypes: column ${JSON.stringify(column)}: [${code}] ${detail}`
        : `chtypes: [${code}] ${detail}`,
    );
    this.code = code;
    this.detail = detail;
    this.column = column;
  }
}

/**
 * A DECLINE: "a real server might well have accepted this; I will not guess."
 * Never ClickHouse rejecting anything, so it carries no `code` property at all
 * — there is no code to carry.
 *
 * It is a PEER of `SchemaError`, deliberately NOT a subclass and deliberately
 * NOT matched by `err instanceof SchemaError`. A decline that still satisfied
 * "is a SchemaError" would be the sentinel problem wearing a class hierarchy:
 * every `catch` that forgot the predicate would keep silently turning declines
 * into rejections, which is a manufactured over-reject — data loss the product
 * never made, budgeted at zero. As a peer, forgetting throws on past the
 * handler, which is loud.
 *
 * ```ts
 * try {
 *   schema.setEngine(engine, orderBy);
 * } catch (err) {
 *   // validate cautiously; do NOT blame the tenant
 *   if (err instanceof UnsupportedError) return degradeToCarefulPath(err);
 *   // ClickHouse refused: err.code is its own code
 *   if (err instanceof SchemaError) return rejectWith(err.code, err.detail);
 *   throw err;
 * }
 * ```
 *
 * The MESSAGE still renders the ABI's `CODE_UNSUPPORTED` sentinel in the shape
 * `SchemaError` renders its code. The sentinel is not a property of this class
 * — it is the wire value the C ABI returned — but the rendering is frozen: the
 * conformance driver puts this exact string on the protocol wire as an
 * `unsupported` scope, and Python's `UnsupportedError` already spells it this
 * way. Changing the text would move rig records without changing a verdict.
 */
export class UnsupportedError extends ChtypesError {
  readonly detail: string;
  readonly column: string | undefined;

  constructor(detail: string, column?: string) {
    super(
      column
        ? `chtypes: column ${JSON.stringify(column)}: [${CODE_UNSUPPORTED}] ${detail}`
        : `chtypes: [${CODE_UNSUPPORTED}] ${detail}`,
    );
    this.detail = detail;
    this.column = column;
  }
}

/**
 * The ONE place an ABI error code becomes an error object, so the
 * refusal/decline split cannot be decided differently in two files.
 *
 * The SIGN decides (spec/bindings.md rule 12, spec/c-abi.md §Error model): a
 * positive code is the server's own refusal and rides through verbatim; any
 * negative code is this library declining (`-2` "I will not guess", `-1` a
 * guarded exception, and a binding's own missing-symbol sentinel) and becomes
 * an `UnsupportedError`. Keying on the sign rather than on `=== CODE_UNSUPPORTED`
 * means a negative sentinel a later era adds can never become "a SchemaError
 * with a negative code", which the class's own contract forbids.
 *
 * Internal: not re-exported from the package index.
 */
export function schemaErrorFor(
  code: number,
  detail: string,
  column?: string,
): SchemaError | UnsupportedError {
  return code < 0 ? new UnsupportedError(detail, column) : new SchemaError(code, detail, column);
}
