/**
 * The error model, which is three outcomes that must never be conflated
 * (the core repository's C ABI specification §Error model):
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
 * incompatibility (docs/reference/artifact.md §Loading).
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
 * is undefined behavior, which is exactly what the gate exists to prevent.
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
 * (docs/reference/bindings.md rule 12).
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
 * The SIGN decides (docs/reference/bindings.md rule 12, the core repository's C ABI specification §Error model): a
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

// ---------------------------------------------------------------- artifacts
//
// The fetch/verify contract (docs/guides/fetch.md §7) shares six codes across the
// four SDKs. Five of them are verdicts of the verification chain or of the
// source and are raised by `ensure` / the CLI; the sixth,
// `CHTYPES_ARTIFACT_MISSING`, is the loader's — raised when a registry is
// asked for a line no directory on the search path holds.

/**
 * The codes docs/guides/fetch.md §6 shares across every SDK, as named constants.
 *
 * Each error below carries its own `code`, and a caller matches on it. Naming
 * them here is what lets that caller write `err.code === CODE_ARTIFACT_PINNED`
 * instead of retyping the string — which is exactly why the Go, Python and Rust
 * bindings all export them, and why this one already exported
 * `CODE_UNSUPPORTED`. A typo in a hand-written literal is a comparison that is
 * silently always false.
 */
export const CODE_ARTIFACT_MISSING = 'CHTYPES_ARTIFACT_MISSING';
/** docs/guides/fetch.md §6: the signature did not verify against the trust list. */
export const CODE_ARTIFACT_UNTRUSTED = 'CHTYPES_ARTIFACT_UNTRUSTED';
/** docs/guides/fetch.md §6: a hash mismatch, or a release that disagrees with itself. */
export const CODE_ARTIFACT_CORRUPT = 'CHTYPES_ARTIFACT_CORRUPT';
/** docs/guides/fetch.md §6: `--frozen` refused an asset the lock file does not pin. */
export const CODE_ARTIFACT_PINNED = 'CHTYPES_ARTIFACT_PINNED';
/** docs/guides/fetch.md §6: the release offers nothing for this platform or line. */
export const CODE_ARTIFACT_UNPUBLISHED = 'CHTYPES_ARTIFACT_UNPUBLISHED';
/** docs/guides/fetch.md §6: the source could not be reached or does not serve the release. */
export const CODE_SOURCE_UNREACHABLE = 'CHTYPES_SOURCE_UNREACHABLE';

/** The codes docs/guides/fetch.md §7 shares across every SDK. */
export type ArtifactErrorCode =
  | typeof CODE_ARTIFACT_MISSING
  | typeof CODE_ARTIFACT_UNTRUSTED
  | typeof CODE_ARTIFACT_CORRUPT
  | typeof CODE_ARTIFACT_PINNED
  | typeof CODE_ARTIFACT_UNPUBLISHED
  | typeof CODE_SOURCE_UNREACHABLE;

/** The command this SDK's §7 message tells a user to run. */
export const FETCH_COMMAND = 'npx @wavehouse/chtypes fetch';

/**
 * The §7 message, verbatim apart from the bracketed parts:
 *
 *     chtypes: no artifact for ClickHouse <line> (<os>-<arch>). Looked in: <dir1>, <dir2>, ….
 *     Install it:  npx @wavehouse/chtypes fetch <line>
 *     or set CHTYPES_AUTOFETCH=1 to fetch on first use.
 */
export function artifactMissingMessage(line: string, platform: string, lookedIn: readonly string[]): string {
  return (
    `chtypes: no artifact for ClickHouse ${line} (${platform}). Looked in: ${lookedIn.join(', ')}.\n` +
    `Install it:  ${FETCH_COMMAND} ${line}\n` +
    'or set CHTYPES_AUTOFETCH=1 to fetch on first use.'
  );
}

/**
 * The one identifiable error for a missing artifact (docs/guides/fetch.md §7):
 * a `Registry` was asked for a line that no directory on its search path
 * holds. `code` is `'CHTYPES_ARTIFACT_MISSING'`; the message is the
 * contract's, and names every directory that was looked in and the command
 * that installs the line.
 *
 * A subclass of `RegistryError`, so a `catch` written against `for()`'s
 * documented error keeps working; the `code` is what a caller matches on.
 */
export class ArtifactMissingError extends RegistryError {
  readonly code = 'CHTYPES_ARTIFACT_MISSING' as const;
  /** The minor line that was asked for, e.g. `25.8`. */
  readonly line: string;
  /** The platform key the registry serves, e.g. `darwin-arm64`. */
  readonly platform: string;
  /** Every directory of the search path, in order. */
  readonly lookedIn: readonly string[];

  constructor(line: string, platform: string, lookedIn: readonly string[]) {
    super(artifactMissingMessage(line, platform, lookedIn));
    this.line = line;
    this.platform = platform;
    this.lookedIn = [...lookedIn];
  }
}

/**
 * Base of the fetch-time verdicts (docs/guides/fetch.md §3–§7). `code` is one of the
 * shared codes; the subclasses exist so `instanceof` reads as well as `code`.
 */
export class FetchError extends ChtypesError {
  readonly code: Exclude<ArtifactErrorCode, 'CHTYPES_ARTIFACT_MISSING'>;

  constructor(
    code: Exclude<ArtifactErrorCode, 'CHTYPES_ARTIFACT_MISSING'>,
    message: string,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.code = code;
  }
}

/**
 * `CHTYPES_ARTIFACT_UNTRUSTED`: the release's `SHA256SUMS.sig` is absent,
 * malformed, or does not verify with any trusted key. Nothing is downloaded
 * around it (§3 step 0).
 */
export class ArtifactUntrustedError extends FetchError {
  constructor(message: string, options?: { cause?: unknown }) {
    super('CHTYPES_ARTIFACT_UNTRUSTED', message, options);
  }
}

/**
 * `CHTYPES_ARTIFACT_CORRUPT`: any hash mismatch along the chain, a release
 * that disagrees with itself (`index.json` vs `SHA256SUMS`, manifest vs
 * index), or an archive that cannot be read safely.
 */
export class ArtifactCorruptError extends FetchError {
  constructor(message: string, options?: { cause?: unknown }) {
    super('CHTYPES_ARTIFACT_CORRUPT', message, options);
  }
}

/**
 * `CHTYPES_ARTIFACT_PINNED`: a `--frozen` lock file names a different asset
 * or sha256 for this line — or none at all — and the fetch refused it (§5).
 */
export class ArtifactPinnedError extends FetchError {
  constructor(message: string, options?: { cause?: unknown }) {
    super('CHTYPES_ARTIFACT_PINNED', message, options);
  }
}

/** `CHTYPES_ARTIFACT_UNPUBLISHED`: the release has nothing for this platform, line or exact patch. */
export class ArtifactUnpublishedError extends FetchError {
  constructor(message: string, options?: { cause?: unknown }) {
    super('CHTYPES_ARTIFACT_UNPUBLISHED', message, options);
  }
}

/**
 * `CHTYPES_SOURCE_UNREACHABLE`: the source could not be reached or does not
 * serve the release files — or `offline` forbade reaching it and the line is
 * not installed and verified.
 */
export class SourceUnreachableError extends FetchError {
  constructor(message: string, options?: { cause?: unknown }) {
    super('CHTYPES_SOURCE_UNREACHABLE', message, options);
  }
}
