/**
 * The ABI v1 loader and generated layer's surface inside this package: what
 * the public API (`../index.ts`, `../registry.ts`, `../library.ts`) is built
 * over. Nothing here is re-exported wholesale: the public barrel names exactly
 * what a caller may use.
 */

export { type BuildInfo, type Capabilities, decodeBuildInfo } from './buildinfo.js';
export { type BlockHandle, Calls, type FilterHandle, type SchemaHandle } from './calls.gen.js';
export { ABI_FINGERPRINT, ABI_VERSION, BUF_HANDLE, type FunctionSpec, type ParamSpec, type ReturnSpec } from './decls.gen.js';
export { errorForStatus, type LoaderErrorClass, loaderErrorClassFor } from './errmap.gen.js';
export {
  ArtifactCorruptError,
  ArtifactError,
  ArtifactIncompatibleError,
  CallError,
  type CallErrorFields,
  ChtypesError,
  InternalError,
  internalError,
  isChtypesError,
  LoaderCorruptError,
  type LoaderErrorFields,
  SchemaError,
  UnsupportedError,
  UsageError,
  usageError,
} from './errors.js';
export { Abi1Handle, HANDLE_CLASSES, wrapHandle } from './handles.js';
export { type ImageSetup, LoadedImage, type LoadInput, openAbi1, openUnverified, type Predicate } from './loader.js';
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
