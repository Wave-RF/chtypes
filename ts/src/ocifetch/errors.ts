/**
 * The v1 fetch layer's own error model (`docs/guides/fetch-v1.md` §8). This
 * module is isolated from the v0 fetch code (`../errors.ts`): v1 replaces v0
 * outright at the switch (plan §2.4), so nothing here imports from there, and
 * nothing in `../errors.ts` is edited by this lane.
 */

import {
  CODE_ARTIFACT_CORRUPT,
  CODE_ARTIFACT_MISSING,
  CODE_ARTIFACT_PINNED,
  CODE_ARTIFACT_UNPUBLISHED,
  CODE_ARTIFACT_UNTRUSTED,
  CODE_CACHE_UNUSABLE,
  CODE_SOURCE_FORBIDDEN,
  CODE_SOURCE_INCOMPATIBLE,
  CODE_SOURCE_RETIRED,
  CODE_SOURCE_UNAUTHORIZED,
  CODE_SOURCE_UNREACHABLE,
  ERROR_EXIT_CODES,
  type ErrorCode,
} from './constants.gen.js';

/**
 * The shared error codes and their type are generated from
 * `spec/fetch-v1/constants.json` (`./constants.gen.ts`; public issue #500).
 * `CODE_ARTIFACT_INCOMPATIBLE` is the loader's: the fetch layer never raises
 * it. `CODE_CACHE_UNUSABLE` is a cache fault (guide §1; public issue #486), and
 * `CODE_SOURCE_RETIRED` a source that answered 410 Gone, never retried and
 * never sent to the next base (guide §2; public issue #571).
 */
export {
  CODE_ARTIFACT_CORRUPT,
  CODE_ARTIFACT_INCOMPATIBLE,
  CODE_ARTIFACT_MISSING,
  CODE_ARTIFACT_PINNED,
  CODE_ARTIFACT_UNPUBLISHED,
  CODE_ARTIFACT_UNTRUSTED,
  CODE_CACHE_UNUSABLE,
  CODE_SOURCE_FORBIDDEN,
  CODE_SOURCE_INCOMPATIBLE,
  CODE_SOURCE_RETIRED,
  CODE_SOURCE_UNAUTHORIZED,
  CODE_SOURCE_UNREACHABLE,
  type ErrorCode,
} from './constants.gen.js';

/** One of the shared error codes: the generated `ErrorCode`, under the name this module has always used. */
export type FetchV1ErrorCode = ErrorCode;

/** A code's process exit status (`docs/guides/fetch-v1.md` §8's table, generated into `ERROR_EXIT_CODES`): the command line's, never the library's. */
export function exitStatusOf(code: FetchV1ErrorCode): number {
  // Every `ErrorCode` is one of `ERROR_EXIT_CODES`'s own keys (both are
  // generated from the same table); the `| undefined` is only
  // `noUncheckedIndexedAccess`'s caution, not a real possibility here.
  return ERROR_EXIT_CODES[code]!;
}

/** A loader refusal's fields (`docs/reference/bindings-v1.md` §4): `sdk.json`'s reason (with its suffix), the library path, and what was wanted and what was found where the refusal names them. */
export interface RefusalFields {
  readonly reason: string;
  readonly path: string;
  readonly want?: string | undefined;
  readonly got?: string | undefined;
}

/**
 * The base of every error this package throws on purpose: the fetch and
 * loader errors (`FetchV1Error`, exported as `ArtifactError`) and the call
 * errors (`CallError`, `abi2/errors.ts`). It lives here, under the fetch
 * layer, so `FetchV1Error` can extend it without the two modules importing
 * each other (public issue #487).
 */
export abstract class ChtypesError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = new.target.name;
  }
}

/**
 * Base of every error this module throws, exported as `ArtifactError`: the
 * code, and a loader refusal's fields (`reason`, `path`, `want`, `got`), as
 * Go's `*ArtifactError` and Python's `ArtifactError` carry them
 * (`docs/reference/bindings-v1.md` §4). They are `undefined` for a fetch
 * error; a cache fault sets `path` and `reason`.
 */
export class FetchV1Error extends ChtypesError {
  readonly code: FetchV1ErrorCode;
  /** A loader refusal's reason (`sdk.json`'s `loader.refusals`, with `:<symbol>` or `:<field>` appended where it gives a suffix), or a cache fault's. */
  readonly reason: string | undefined;
  /** The library a loader refusal names, or the path a cache fault names. */
  readonly path: string | undefined;
  /** What a loader refusal's check wanted, where it names both sides. */
  readonly want: string | undefined;
  /** What a loader refusal's check found: beside `want`, or alone. */
  readonly got: string | undefined;

  constructor(code: FetchV1ErrorCode, message: string, options?: { cause?: unknown; refusal?: RefusalFields }) {
    super(message, options);
    this.code = code;
    this.reason = options?.refusal?.reason;
    this.path = options?.refusal?.path;
    this.want = options?.refusal?.want;
    this.got = options?.refusal?.got;
  }
}

export class ArtifactMissingError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_ARTIFACT_MISSING, message, options);
  }
}

/** The signature did not verify under any trusted key (no referrer bundle of the signature artifactType verified). */
export class ArtifactUntrustedError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_ARTIFACT_UNTRUSTED, message, options);
  }
}

/** A hash mismatch, a malformed object, a duplicate JSON key, or a tar rule violation. */
export class ArtifactCorruptError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown; refusal?: RefusalFields }) {
    super(CODE_ARTIFACT_CORRUPT, message, options);
  }
}

/** `--frozen` refused: the lock names no entry for this platform, or fails schema validation. */
export class ArtifactPinnedError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_ARTIFACT_PINNED, message, options);
  }
}

/** The index offers nothing for this platform, or every base 404s the tag. */
export class ArtifactUnpublishedError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_ARTIFACT_UNPUBLISHED, message, options);
  }
}

/** A source could not be reached, exhausted its retry budget, or `offline` forbade reaching it. */
export class SourceUnreachableError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_SOURCE_UNREACHABLE, message, options);
  }
}

/** A 401 from a configured base host. */
export class SourceUnauthorizedError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_SOURCE_UNAUTHORIZED, message, options);
  }
}

/** A 403 from a configured base host. */
export class SourceForbiddenError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_SOURCE_FORBIDDEN, message, options);
  }
}

/** An index, manifest or layer media type this fetch layer does not recognize (§3 of the guide). */
export class SourceIncompatibleError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_SOURCE_INCOMPATIBLE, message, options);
  }
}

/**
 * A source answered 410 Gone: a retired repository, which is permanent, so the
 * request was never retried and never sent to the next base (guide §2, "A
 * retired repository"; public issue #571). The message names the URL that
 * answered and carries the registry's own message, made safe to print.
 */
export class SourceRetiredError extends FetchV1Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(CODE_SOURCE_RETIRED, message, options);
  }
}

/** What a `CacheUnusableError` names: the exact path that failed, the reason, and the errno name when there is one. */
export interface CacheFault {
  readonly path: string;
  /** `unreadable_root`, `not_a_directory`, `unreadable_entry`, `unacceptable_record`, `layout_0x` or `unwritable`. */
  readonly reason: string;
  /** The errno name (`EACCES`), or `undefined` when there is none. */
  readonly osError?: string | undefined;
}

/**
 * A cache directory or entry the fetch layer could not read or write, or one
 * strict mode refuses: never "not installed", never the network's
 * `SourceUnreachableError`, never a raw Node error (guide §1, the cache
 * faults; public issue #486).
 */
export class CacheUnusableError extends FetchV1Error implements CacheFault {
  /** The exact path that failed (set by the base, from the fault). */
  declare readonly path: string;
  /** The fault's reason (set by the base, from the fault). */
  declare readonly reason: string;
  readonly osError: string | undefined;

  constructor(message: string, fault: CacheFault, options?: { cause?: unknown }) {
    super(CODE_CACHE_UNUSABLE, message, { ...options, refusal: { reason: fault.reason, path: fault.path } });
    this.osError = fault.osError;
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export { errorText };
