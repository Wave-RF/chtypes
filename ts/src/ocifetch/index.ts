/**
 * The v1 fetch layer's public surface: the seam (`docs/guides/fetch-v1.md`
 * §9) and the types a caller needs to use it. Nothing outside this module
 * imports from `./oci.js`, `./dsse.js`, `./http.js`, `./unpack.js`,
 * `./layout.js` or `./lock.js` directly — the FFI/loader lane (not yet
 * built) wires `resolve_installed` and `ensure` in through here once it
 * lands.
 */

export {
  activeChannel,
  type Channel,
  channelName,
  DEV_CACHE_DIR,
  DEV_CHANNEL_BASE,
  DEV_KEY_ID,
  PINNING_REFUSED,
  PinningRefusedError,
  pinningRequested,
  refusePinning,
} from './channel.js';
export { keyIdOfRawKey } from './dsse.js';
export { cacheRoot, isFilesystemError } from './layout.js';
export {
  ensure,
  fetchSigned,
  listInstalled,
  listTags,
  missingNotes,
  probeCache,
  resolveInstalled,
  satisfiesRequest,
  verifyInstalled,
  withNotes,
} from './ensure.js';
export type { FetchSignedResult } from './ensure.js';
export {
  ArtifactCorruptError,
  ArtifactMissingError,
  ArtifactPinnedError,
  ArtifactUnpublishedError,
  ArtifactUntrustedError,
  CacheUnusableError,
  FetchV1Error,
  SourceForbiddenError,
  SourceIncompatibleError,
  SourceUnauthorizedError,
  SourceUnreachableError,
} from './errors.js';
export type { CacheFault, FetchV1ErrorCode } from './errors.js';
export type {
  ArtifactPredicate,
  Clock,
  FetchV1Options,
  PlatformKey,
  Resolved,
  ResolvedDigests,
  TrustedKey,
  VerifyResult,
} from './types.js';
export { hostPlatformKey, isPlatformKey, realClock } from './types.js';
