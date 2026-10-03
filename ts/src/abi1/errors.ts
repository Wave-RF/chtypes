/**
 * The ABI v1 error model, local to this `abi1` directory (plan §3.4: "Errors
 * stay abi1-local until wave C. Waves A and B touch NO v0 file; the public
 * mapping lands with the public API."). These classes are deliberately NOT
 * the v0 error hierarchy in `../errors.ts` — independent until the cutover
 * unifies them — so nothing in this directory needs an edit to any v0 file.
 *
 * Two shapes (spec/abi-v1/sdk.json's two tables):
 *
 *   - a CALL error (`CallErrorFields`): the four fields a `chs_error`
 *     carries, plus the status name that produced it, per sdk.json
 *     `errors.fields` — `errorForStatus` in `./errmap.gen.ts` picks the
 *     class from `status`.
 *   - a LOADER error (`LoaderErrorFields`): a refusal reason, the path, and
 *     the want/got pair where the reason has one (spec/abi-v1/sdk.json
 *     `loader.refusals`).
 *
 * `message` and `column` are raw bytes, never assumed UTF-8 (the header's
 * own words for `chs_error_message`/`chs_error_column`: "may carry any
 * byte" / "NUL and invalid UTF-8 are legal"). `chName` is a plain string:
 * `chs_error_ch_name`'s content is guaranteed ASCII.
 */

/** Base of every error this directory throws. */
export abstract class ChtypesAbi1Error extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** The four `chs_error` fields plus the status that produced them. */
export interface CallErrorFields {
  readonly status: string;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;
}

function renderCallMessage(prefix: string, f: CallErrorFields): string {
  const msg = f.messageBytes.toString('utf8');
  const col = f.column.length > 0 ? ` (column ${JSON.stringify(f.column.toString('utf8'))})` : '';
  return f.chCode !== 0 ? `chtypes: ${prefix}: [${f.chCode}] ${f.chName}: ${msg}${col}` : `chtypes: ${prefix}: ${msg}${col}`;
}

/** `CHS_REJECTED`: ClickHouse's own refusal, carrying its own code and name. */
export class SchemaError extends ChtypesAbi1Error implements CallErrorFields {
  readonly status: string;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;

  constructor(f: CallErrorFields) {
    super(renderCallMessage('rejected', f));
    this.status = f.status;
    this.chCode = f.chCode;
    this.chName = f.chName;
    this.messageBytes = f.messageBytes;
    this.column = f.column;
  }
}

/** `CHS_DECLINED`: this build will not answer; a server might accept. */
export class UnsupportedError extends ChtypesAbi1Error implements CallErrorFields {
  readonly status: string;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;

  constructor(f: CallErrorFields) {
    super(renderCallMessage('declined', f));
    this.status = f.status;
    this.chCode = f.chCode;
    this.chName = f.chName;
    this.messageBytes = f.messageBytes;
    this.column = f.column;
  }
}

/** `CHS_INVALID_ARGUMENT`: caller misuse (a bad handle, a malformed input). */
export class UsageError extends ChtypesAbi1Error implements CallErrorFields {
  readonly status: string;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;

  constructor(f: CallErrorFields) {
    super(renderCallMessage('invalid argument', f));
    this.status = f.status;
    this.chCode = f.chCode;
    this.chName = f.chName;
    this.messageBytes = f.messageBytes;
    this.column = f.column;
  }
}

/** `CHS_INTERNAL`, or any status outside the closed set: a library bug. */
export class InternalError extends ChtypesAbi1Error implements CallErrorFields {
  readonly status: string;
  readonly chCode: number;
  readonly chName: string;
  readonly messageBytes: Buffer;
  readonly column: Buffer;

  constructor(f: CallErrorFields) {
    super(renderCallMessage('internal error', f));
    this.status = f.status;
    this.chCode = f.chCode;
    this.chName = f.chName;
    this.messageBytes = f.messageBytes;
    this.column = f.column;
  }
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
 * A loader step 1-4/6 refusal: the artifact is not one this binding can
 * speak to (wrong ABI generation, a missing symbol, an unresolved link).
 */
export class ArtifactIncompatibleError extends ChtypesAbi1Error implements LoaderErrorFields {
  readonly reason: string;
  readonly path: string;
  readonly want: string | undefined;
  readonly got: string | undefined;

  constructor(f: LoaderErrorFields) {
    super(renderLoaderMessage('incompatible artifact', f));
    this.reason = f.reason;
    this.path = f.path;
    this.want = f.want;
    this.got = f.got;
  }
}

/**
 * A loader step 4 (malformed `chs_build_info`) or step 5 (a cross-check
 * mismatch) refusal: the artifact's own signed statement and its bytes
 * disagree, so the artifact is internally inconsistent.
 */
export class ArtifactCorruptError extends ChtypesAbi1Error implements LoaderErrorFields {
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
