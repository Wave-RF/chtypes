/**
 * The ABI v1 loader and generated layer's public surface within this
 * package (internal to `@wavehouse/chtypes` until wave C builds the real
 * public API over it; this directory is not re-exported from `../index.ts`
 * yet).
 */

export { ABI_FINGERPRINT, ABI_VERSION, BUF_HANDLE, type FunctionSpec, type ParamSpec, type ReturnSpec } from './decls.gen.js';
export { errorForStatus, type LoaderErrorClass, loaderErrorClassFor } from './errmap.gen.js';
export {
  ArtifactCorruptError,
  ArtifactIncompatibleError,
  type CallErrorFields,
  ChtypesAbi1Error,
  InternalError,
  type LoaderErrorFields,
  SchemaError,
  UnsupportedError,
  UsageError,
} from './errors.js';
export { Abi1Handle, HANDLE_CLASSES, wrapHandle } from './handles.js';
export { Library, type LoadInput, openAbi1, openUnverified, type Predicate } from './loader.js';
export { defineRawFunctions, type HandleRef, NULL_EXTERNAL, type RawApi, type RawCallResult, rawCall, readOwnedBuf } from './raw.js';
