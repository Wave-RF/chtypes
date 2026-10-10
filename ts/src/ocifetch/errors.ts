/**
 * The v1 fetch layer's own error model (`docs/guides/fetch-v1.md` §8). This
 * module is isolated from the v0 fetch code (`../errors.ts`): v1 replaces v0
 * outright at the switch (plan §2.4), so nothing here imports from there, and
 * nothing in `../errors.ts` is edited by this lane.
 */

import { ERROR_EXIT_CODES } from './constants.gen.js';

export type FetchV1ErrorCode = keyof typeof ERROR_EXIT_CODES;

export const CODE_ARTIFACT_MISSING = 'CHTYPES_ARTIFACT_MISSING' satisfies FetchV1ErrorCode;
export const CODE_ARTIFACT_UNTRUSTED = 'CHTYPES_ARTIFACT_UNTRUSTED' satisfies FetchV1ErrorCode;
export const CODE_ARTIFACT_CORRUPT = 'CHTYPES_ARTIFACT_CORRUPT' satisfies FetchV1ErrorCode;
export const CODE_ARTIFACT_PINNED = 'CHTYPES_ARTIFACT_PINNED' satisfies FetchV1ErrorCode;
export const CODE_ARTIFACT_UNPUBLISHED = 'CHTYPES_ARTIFACT_UNPUBLISHED' satisfies FetchV1ErrorCode;
export const CODE_SOURCE_UNREACHABLE = 'CHTYPES_SOURCE_UNREACHABLE' satisfies FetchV1ErrorCode;
export const CODE_SOURCE_UNAUTHORIZED = 'CHTYPES_SOURCE_UNAUTHORIZED' satisfies FetchV1ErrorCode;
export const CODE_SOURCE_FORBIDDEN = 'CHTYPES_SOURCE_FORBIDDEN' satisfies FetchV1ErrorCode;
export const CODE_SOURCE_INCOMPATIBLE = 'CHTYPES_SOURCE_INCOMPATIBLE' satisfies FetchV1ErrorCode;
/**
 * Reserved for the FFI/loader layer (not yet built, blocked on the ABI v1
 * design). No case in this module's own conformance suite raises it — the
 * fetch layer never dlopens, checks glibc, or reads a `chs_*` symbol (plan
 * §1.3).
 */
export const CODE_ARTIFACT_INCOMPATIBLE = 'CHTYPES_ARTIFACT_INCOMPATIBLE' satisfies FetchV1ErrorCode;
/** A cache directory or entry the fetch layer could not read or write, or one strict mode refuses (guide §1; public issue #486). */
export const CODE_CACHE_UNUSABLE = 'CHTYPES_CACHE_UNUSABLE' satisfies FetchV1ErrorCode;
/** A source that answered 410 Gone: a retired repository, never retried and never sent to the next base (guide §2; public issue #571). */
export const CODE_SOURCE_RETIRED = 'CHTYPES_SOURCE_RETIRED' satisfies FetchV1ErrorCode;

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

/** Base of every error this module throws. */
export class FetchV1Error extends ChtypesError {
  readonly code: FetchV1ErrorCode;

  constructor(code: FetchV1ErrorCode, message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.code = code;
  }

  /** `docs/guides/fetch-v1.md` §8's table, generated into `ERROR_EXIT_CODES`. */
  get exitStatus(): number {
    // `this.code` is always one of `ERROR_EXIT_CODES`'s own keys (the
    // `FetchV1ErrorCode` union IS `keyof typeof ERROR_EXIT_CODES`); the
    // `| undefined` is only `noUncheckedIndexedAccess`'s generic-index-signature
    // caution, not a real possibility here.
    return ERROR_EXIT_CODES[this.code]!;
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
  constructor(message: string, options?: { cause?: unknown }) {
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
  /** Named value of a `Retry-After` refused as past the remaining budget (§7 of the guide). */
  readonly retryAfterRefused: number | undefined;

  constructor(message: string, options?: { cause?: unknown; retryAfterRefused?: number }) {
    super(CODE_SOURCE_UNREACHABLE, message, options);
    this.retryAfterRefused = options?.retryAfterRefused;
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
  readonly path: string;
  readonly reason: string;
  readonly osError: string | undefined;

  constructor(message: string, fault: CacheFault, options?: { cause?: unknown }) {
    super(CODE_CACHE_UNUSABLE, message, options);
    this.path = fault.path;
    this.reason = fault.reason;
    this.osError = fault.osError;
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export { errorText };
