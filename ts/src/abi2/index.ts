/**
 * The ABI v2 loader and generated layer's surface inside this package: what
 * the public API (`../index.ts`, `../registry.ts`, `../library.ts`) is built
 * over. Nothing here is re-exported wholesale: the public barrel names exactly
 * what a caller may use.
 */

export { type BuildInfo, type Capabilities, decodeBuildInfo } from './buildinfo.js';
export { type BlockHandle, Calls, type FilterHandle, type SchemaHandle, type ServerHandle } from './calls.gen.js';
export { ABI_FINGERPRINT, ABI_STABILITY, ABI_VERSION, BUF_HANDLE, type FunctionSpec, type ParamSpec, type ReturnSpec } from './decls.gen.js';
export { errorForStatus, type LoaderErrorClass, loaderErrorClassFor } from './errmap.gen.js';
export {
  ArtifactCorruptError,
  ArtifactError,
  ArtifactIncompatibleError,
  CallError,
  type CallErrorFields,
  ChtypesError,
  corruptRefusal,
  devFingerprintMessage,
  InternalError,
  internalError,
  SchemaError,
  UnsupportedError,
  UsageError,
  usageError,
} from './errors.js';
export { Abi2Handle, HANDLE_CLASSES, wrapHandle } from './handles.js';
export { checkUnverifiedAllowed, type ImageSetup, LoadedImage, type LoadInput, openAbi2, openUnverified, type Predicate } from './loader.js';
export {
  asBuffer,
  checkStatus,
  defineRawFunctions,
  type HandleRef,
  NULL_EXTERNAL,
  type RawApi,
  type RawCallResult,
  rawCall,
  readOwnedBuf,
} from './raw.js';
export { DuplicateKeyError, firstDuplicateKey, isAsciiOnly, parseStrictJson } from './strictjson.js';
export * from './vocab.gen.js';
