/**
 * The error model (`docs/reference/bindings-v1.md` §4), over the fetch
 * layer's own error family.
 *
 * Two shapes (spec/abi-v2/sdk.json's two tables):
 *
 *   - a CALL error: the five fields of the library's error object, read
 *     verbatim. `errorForStatus` in `./errmap.gen.ts` picks the class from the
 *     raw status. The four classes are PEERS, never one a subtype of another:
 *     a decline that satisfied `instanceof SchemaError` would let a handler
 *     that forgot the distinction turn every decline into a rejection. They
 *     share one abstract base, `CallError`, under `ChtypesError`, which is the
 *     explicit way to catch all four.
 *   - a LOADER error: a refusal reason, the path, and the want/got pair where
 *     the reason has one (sdk.json `loader.refusals`). These extend the fetch
 *     layer's own classes, so a caller catching `ArtifactCorruptError` catches
 *     the fetch layer's corruption and the loader's step 5 alike, and every
 *     artifact error (fetch or loader) is an `ArtifactError`.
 *
 * `messageBytes` and `column` are raw bytes, never assumed UTF-8. `chName` is
 * a plain string: its content is ASCII by the ABI's own promise. `message`
 * (the string every `Error` has) is the one lossy display form.
 */

import { ArtifactCorruptError, FetchV1Error } from '../ocifetch/index.js';
import { ABI_STABILITY } from './decls.gen.js';
import { Status } from './vocab.gen.js';

/** The base of every call error. The fetch and loader errors live under `ArtifactError` instead; `isChtypesError` is true for all of them. */
export abstract class ChtypesError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** The base of every fetch error and every loader refusal: the fetch layer's own base class, re-exported under the name the rest of the family uses. */
export { FetchV1Error as ArtifactError };

/** Re-exported unchanged: the fetch layer's corruption error, which the loader's step-5 refusal extends. */
export { ArtifactCorruptError };

/** True for any error this package throws on purpose: a call error, a fetch error or a loader refusal. */
export function isChtypesError(err: unknown): err is ChtypesError | FetchV1Error {
  return err instanceof ChtypesError || err instanceof FetchV1Error;
}

/** The five fields of a library error object, read verbatim. */
export interface CallErrorFields {
  /** The raw status value; compare with `Status`. */
  readonly status: number;
  /** ClickHouse's own code; nonzero only for a refusal. */
  readonly chCode: number;
  /** The name this build's vendored table gives the code; ASCII; empty if none. */
  readonly chName: string;
  /** ClickHouse's own message for a refusal, the library's otherwise; bytes. */
  readonly messageBytes: Buffer;
  /** The column concerned, empty if none; bytes. */
  readonly column: Buffer;
}

function renderCallMessage(label: string, f: CallErrorFields): string {
  const msg = f.messageBytes.toString('utf8');
  const col = f.column.length > 0 ? ` (column ${JSON.stringify(f.column.toString('utf8'))})` : '';
  return f.chCode !== 0 ? `chtypes: ${label}: [${f.chCode}] ${f.chName}: ${msg}${col}` : `chtypes: ${label}: ${msg}${col}`;
}

/** The abstract base of the four call classes, under `ChtypesError`: catching it is the explicit way to catch all four. */
export abstract class CallError extends ChtypesError implements CallErrorFields {
  readonly status: number;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;

  protected constructor(label: string, f: CallErrorFields) {
    super(renderCallMessage(label, f));
    this.status = f.status;
    this.chCode = f.chCode;
    this.chName = f.chName;
    this.messageBytes = f.messageBytes;
    this.column = f.column;
  }
}

/** The library answered `rejected`: ClickHouse's own refusal, which a server would also give, carrying its own code and name. */
export class SchemaError extends CallError {
  constructor(f: CallErrorFields) {
    super('rejected', f);
  }
}

/** The library answered `declined`: this build will not answer; a server might accept. Never a refusal. */
export class UnsupportedError extends CallError {
  constructor(f: CallErrorFields) {
    super('declined', f);
  }
}

/** Caller misuse: the library's invalid-argument answer, or a misuse the binding detected itself before any call. */
export class UsageError extends CallError {
  constructor(f: CallErrorFields) {
    super('invalid argument', f);
  }
}

/** The library answered `internal`, or sent a status outside the closed set, or a document that does not decode: a library bug. */
export class InternalError extends CallError {
  constructor(f: CallErrorFields) {
    super('internal error', f);
  }
}

function binding(status: number, message: string): CallErrorFields {
  return { status, chCode: 0, chName: '', messageBytes: Buffer.from(message, 'utf8'), column: Buffer.alloc(0) };
}

/** A misuse the binding detects itself: it carries the invalid-argument status, ch_code 0 and an empty name and column, so a handler sees one shape whichever side caught it. */
export function usageError(message: string): UsageError {
  return new UsageError(binding(Status.InvalidArgument, message));
}

/** A document or a value that breaks the library's own contract. */
export function internalError(message: string): InternalError {
  return new InternalError(binding(Status.Internal, message));
}

/** A loader refusal's fields: the reason, the artifact path, and want/got where the reason has one. */
export interface LoaderErrorFields {
  readonly reason: string;
  readonly path: string;
  readonly want?: string | undefined;
  readonly got?: string | undefined;
}

function renderLoaderMessage(label: string, f: LoaderErrorFields): string {
  const detail = f.want !== undefined || f.got !== undefined ? ` (want ${f.want ?? '?'}, got ${f.got ?? '?'})` : '';
  return `chtypes: ${f.path}: ${label}: ${f.reason}${detail}`;
}

/**
 * Rule r6's exact refusal of a library whose fingerprint is not this dev SDK's
 * (`spec/abi-v2/docs.md`): `sdk` is this binding's own `ABI_FINGERPRINT`,
 * `library` the library's, both full `sha256:` spellings.
 */
export function devFingerprintMessage(sdk: string, library: string): string {
  return `this SDK speaks dev fingerprint ${sdk}; the library has ${library} — update your dev SDK`;
}

/** An incompatible-artifact refusal's message: rule r6's exact text for a fingerprint refusal while the description is unstable (a 2.0.0-dev SDK), the loader's own otherwise. */
function incompatibleMessage(f: LoaderErrorFields): string {
  if (f.reason === 'fingerprint' && ABI_STABILITY === 'unstable' && f.want !== undefined && f.got !== undefined) {
    return devFingerprintMessage(f.want, f.got);
  }
  return renderLoaderMessage('incompatible artifact', f);
}

/** A loader step 1-4 or 6 refusal: the artifact is not one this binding can speak to (wrong ABI generation, a different fingerprint, a missing symbol, an unresolvable link). A fingerprint refusal by a 2.0.0-dev SDK carries rule r6's exact message. */
export class ArtifactIncompatibleError extends FetchV1Error implements LoaderErrorFields {
  readonly reason: string;
  readonly path: string;
  readonly want: string | undefined;
  readonly got: string | undefined;

  constructor(f: LoaderErrorFields) {
    super('CHTYPES_ARTIFACT_INCOMPATIBLE', incompatibleMessage(f));
    this.reason = f.reason;
    this.path = f.path;
    this.want = f.want;
    this.got = f.got;
  }
}

/**
 * A loader step 4 (malformed build info) or step 5 (a cross-check mismatch)
 * refusal: the artifact's own signed statement and its bytes disagree. It
 * extends the fetch layer's `ArtifactCorruptError`, so the two are one class
 * to a caller who catches it.
 */
export class LoaderCorruptError extends ArtifactCorruptError implements LoaderErrorFields {
  readonly reason: string;
  readonly path: string;
  readonly want: string | undefined;
  readonly got: string | undefined;

  constructor(f: LoaderErrorFields) {
    super(renderLoaderMessage('corrupt artifact', f));
    this.reason = f.reason;
    this.path = f.path;
    this.want = f.want;
    this.got = f.got;
  }
}
