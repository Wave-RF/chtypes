/**
 * The generic, data-driven call engine over the typed table `defineRawFunctions`
 * builds for one opened ABI v1 image (plan §3.3(d) happens in `./loader.ts`,
 * which calls `./libc.ts`'s `ffiOpen` then this file's `defineRawFunctions`).
 *
 * This is the "invoke by name" dispatcher the plan asks of this emitter
 * (scripts/abi-v1/emit/ts.py): given a `SYMBOL.*` name and a resolved
 * argument array, it marshals, calls and decodes GENERICALLY, reading only
 * `./decls.gen.ts`'s `FUNCTION_SPECS` to know a function's shape. See
 * emit/ts.py's module docstring for why this is ONE generic routine rather
 * than 38 generated near-duplicates: ffi-rs 1.3.7 can only call a symbol
 * already known BY NAME (there is no "call this resolved pointer" API), so
 * every chs_* call already goes through one mechanism (`define`/`load` by
 * name) — a per-function body would only restate the same few
 * parameter-kind branches 38 times.
 *
 * Every chs_* name this file ever sees comes from `./decls.gen.ts` (as a
 * `FUNCTION_SPECS`/`SYMBOL` value — `FUNCTION_SPECS`'s own keys, read
 * through `Object.entries`, double as the `define()` table's keys; see
 * `defineRawFunctions`'s own comment for why that must be the REAL chs_*
 * name) or from a caller's own runtime data (cases.json, parsed at
 * runtime); this file's own source never spells one.
 */

import {
  createExternalBuffer,
  createPointer,
  DataType,
  define,
  freePointer,
  isNullPointer,
  type JsExternal,
  PointerType,
  restorePointer,
} from 'ffi-rs';
import { BUF_HANDLE, FUNCTION_SPECS, type FunctionSpec, type ParamSpec, STATUS_NAMES, SYMBOL } from './decls.gen.js';
import type { CallErrorFields } from './errors.js';
import { readCString } from './libc.js';

const { External, I32, U32, I64, U64, Void, U8Array } = DataType;

function scalarDataType(type: string | null): DataType {
  switch (type) {
    case 'int32':
    case 'int':
      return I32;
    case 'uint32':
      return U32;
    case 'int64':
      return I64;
    case 'uint64':
    case 'size':
      return U64;
    case 'cstr_static':
    case 'u8ptr_const':
      return External;
    default:
      throw new Error(`chtypes abi1: raw.ts does not know the ffi-rs type for scalar ${String(type)}`);
  }
}

function retDataType(spec: FunctionSpec): DataType {
  switch (spec.returns.kind) {
    case 'status':
      return I32;
    case 'void':
      return Void;
    case 'enum':
      return I32;
    case 'handle':
      return External;
    case 'scalar':
      return scalarDataType(spec.returns.type);
    default:
      throw new Error(`chtypes abi1: unknown return kind ${spec.returns.kind}`);
  }
}

function paramDataTypes(p: ParamSpec): DataType[] {
  switch (p.kind) {
    case 'scalar':
      return [scalarDataType(p.type)];
    case 'enum':
      return [I32];
    case 'bytes_in':
      return [U8Array, U64];
    case 'handle':
    case 'out_handle':
    case 'out_error':
      return [External];
    case 'out_scalar':
      // Reserved: no FIRM or provisional function uses it today
      // (spec/abi-v1/abi.json); nothing to marshal yet.
      throw new Error('chtypes abi1: out_scalar has no described use yet');
    default:
      throw new Error(`chtypes abi1: unknown param kind ${p.kind}`);
  }
}

type RawFn = (args: unknown[]) => unknown;
export type RawApi = Readonly<Record<string, RawFn>>;

/**
 * `define()` the whole described surface against one already-opened image
 * (`libraryKey`, via `./libc.ts`'s `ffiOpen` — call that FIRST). One call
 * builds every chs_* function this image exposes; `rawCall` below is the
 * only thing that ever invokes the result.
 *
 * The table is keyed by each function's OWN chs_* name, never
 * `CAMEL_NAMES`: `define()`'s per-entry type
 * (`Omit<FFIParams, 'paramsValue' | 'funcName'>`) OMITS `funcName`
 * entirely (measured against the installed ffi-rs 1.3.7 package) — the
 * symbol `define()` resolves IS the table entry's own key, so a
 * `funcName` alongside a differently-spelled key is silently ignored and
 * the real call fails at run time ("Cannot find `<key>` function"), not
 * at compile time (TypeScript's excess-property check does not see it
 * through this `table` variable). Every chs_* name here comes from
 * `Object.entries(FUNCTION_SPECS)` — this file's own source never spells
 * one (scripts/abi-v1/check-no-hand-decls.py's rule 3 matches a LITERAL
 * `chs_x: {`, never a key built from a loop variable).
 */
export function defineRawFunctions(libraryKey: string): RawApi {
  const table: Record<string, { library: string; retType: DataType; paramsType: DataType[] }> = {};
  for (const [name, spec] of Object.entries(FUNCTION_SPECS)) {
    table[name] = { library: libraryKey, retType: retDataType(spec), paramsType: spec.params.flatMap((p) => paramDataTypes(p)) };
  }
  return define(table) as unknown as RawApi;
}

// ------------------------------------------------------------- pointer slots

type Slot = JsExternal[];

function u64Slot(): Slot {
  return createPointer({ paramsType: [U64], paramsValue: [0] });
}

function readExternalSlot(slot: Slot): JsExternal {
  // The generic parameter is a DataType VALUE (ffi-rs maps it through
  // DataTypeToType), not the JS type we want back — `External` here, not
  // `JsExternal`; passing the latter silently yields `never[]`, so a
  // noUncheckedIndexedAccess `[0]` reads as `undefined` always, which only
  // surfaces as a cast error rather than a wrong runtime value (measured
  // against ffi-rs 1.3.7's own .d.ts).
  return restorePointer<DataType.External>({ retType: [External], paramsValue: slot })[0] as JsExternal;
}

function dropSlot(slot: Slot): void {
  freePointer({ paramsType: [U64], paramsValue: slot, pointerType: PointerType.RsPointer });
}

/**
 * The null `External` value ffi-rs accepts for an optional (nullable)
 * handle parameter: a zeroed 8-byte slot read back as a pointer. `Str`
 * cannot carry a null (an input parameter typed `Str` throws before the
 * native call is made for a JS `null`/`undefined`), but `External` can —
 * the same technique v0 `ts/src/ffi.ts`'s `NULL_PTR` uses, re-derived here
 * rather than imported (this directory touches no v0 file; plan §3.4).
 */
export const NULL_EXTERNAL: JsExternal = (() => {
  const slot = u64Slot();
  const p = readExternalSlot(slot);
  dropSlot(slot);
  if (!isNullPointer(p)) {
    throw new Error('chtypes abi1: internal — the null External constant did not come back null');
  }
  return p;
})();

// --------------------------------------------------------------- buf reading

function rawFn(raw: RawApi, chsName: string): RawFn {
  if (FUNCTION_SPECS[chsName] === undefined) throw new Error(`chtypes abi1: ${chsName} is not a described symbol`);
  const fn = raw[chsName];
  if (fn === undefined) throw new Error(`chtypes abi1: ${chsName} was not declared on this image`);
  return fn;
}

/** Read `buf`'s bytes (via the raw `chs_buf_data`/`chs_buf_len` accessors) into an owned copy, then free `buf`. Returns an empty Buffer for a null handle (never dereferenced). */
export function readOwnedBuf(raw: RawApi, buf: JsExternal): Buffer {
  if (isNullPointer(buf)) return Buffer.alloc(0);
  const len = Number(rawFn(raw, SYMBOL.BUF_LEN)([buf]));
  const out = len === 0 ? Buffer.alloc(0) : Buffer.from(createExternalBuffer(rawFn(raw, SYMBOL.BUF_DATA)([buf]) as JsExternal, len));
  rawFn(raw, SYMBOL.BUF_FREE)([buf]);
  return out;
}

/** Release a handle directly through its `SYMBOL.*_FREE` function (for a handle this caller minted but never passed through `rawCall`'s own out-handle decoding, or to close early). Freeing a null handle is a documented no-op (D2). */
export function freeHandle(raw: RawApi, chsFreeSymbol: string, ptr: JsExternal): void {
  rawFn(raw, chsFreeSymbol)([ptr]);
}

/** A NUL-terminated, guaranteed-ASCII static string (`chs_build_info`/`chs_clickhouse_version`'s `cstr_static` return): null for a null pointer (a malformed or hostile build; never normative, but must not crash the reader). `strlen` lives on the libc library key (`./libc.ts`), which is always open by the time a chs_* image has been loaded at all. */
function readStaticCString(ptr: JsExternal): string | null {
  if (isNullPointer(ptr)) return null;
  return readCString(ptr);
}

// ------------------------------------------------------------------- rawCall

export type HandleRef = { readonly kind: string; readonly ptr: JsExternal };

export type RawCallResult =
  | { readonly outcome: 'void' }
  | { readonly outcome: 'value'; readonly value: number | string | null }
  | { readonly outcome: 'handle'; readonly handle: HandleRef }
  | {
      readonly outcome: 'status';
      readonly statusName: string;
      readonly outs: Readonly<Record<string, Buffer | HandleRef>>;
      readonly error: CallErrorFields | null;
    };

function inputParams(spec: FunctionSpec): ParamSpec[] {
  return spec.params.filter((p) => p.kind !== 'out_handle' && p.kind !== 'out_error');
}

function decodeError(raw: RawApi, statusName: string, errSlot: Slot): CallErrorFields | null {
  const errPtr = readExternalSlot(errSlot);
  if (isNullPointer(errPtr)) return null;
  const chCode = Number(rawFn(raw, SYMBOL.ERROR_CH_CODE)([errPtr]));
  const chName = readOwnedBuf(raw, rawFn(raw, SYMBOL.ERROR_CH_NAME)([errPtr]) as JsExternal).toString('utf8');
  const messageBytes = readOwnedBuf(raw, rawFn(raw, SYMBOL.ERROR_MESSAGE)([errPtr]) as JsExternal);
  const column = readOwnedBuf(raw, rawFn(raw, SYMBOL.ERROR_COLUMN)([errPtr]) as JsExternal);
  rawFn(raw, SYMBOL.ERROR_FREE)([errPtr]);
  return { status: statusName, chCode, chName, messageBytes, column };
}

/**
 * Call `name` (a `SYMBOL.*` value) with `args`, one JS value per INPUT
 * parameter in declaration order — `number` for a scalar/enum, `Buffer` for
 * bytes_in, `JsExternal` (or `NULL_EXTERNAL`) for a handle. Marshals every
 * `out_handle`/`out_error` automatically and decodes the result generically
 * from `FUNCTION_SPECS[name]`.
 *
 * Refuses a function whose return is a borrowed `u8ptr_const`
 * (`chs_buf_data`): its length lives in a SEPARATE call (`chs_buf_len`),
 * which this generic engine has no way to know about for an arbitrary
 * function. `./raw.ts`'s own `readOwnedBuf` calls that pair directly; no
 * described function other than `chs_buf_data` has this shape today.
 */
export function rawCall(raw: RawApi, name: string, args: readonly unknown[]): RawCallResult {
  const spec = FUNCTION_SPECS[name];
  if (spec === undefined) throw new Error(`chtypes abi1: no such described function ${name}`);
  const inputs = inputParams(spec);
  if (inputs.length !== args.length) {
    throw new Error(`chtypes abi1: ${name} takes ${inputs.length} input argument(s), got ${args.length}`);
  }
  const paramsValue: unknown[] = [];
  const outSlots: { param: ParamSpec; slot: Slot }[] = [];
  let i = 0;
  for (const p of spec.params) {
    if (p.kind === 'out_handle' || p.kind === 'out_error') {
      const slot = u64Slot();
      // `slot` is the length-1 JsExternal[] createPointer returns: SPREAD it
      // into paramsValue (one positional arg), never pushed as the array
      // itself — measured: pushing the array gives ffi-rs an Object where it
      // expects an External ("expect External, got: Object"). v0's own
      // ts/src/ffi.ts calls every slot this same way (`...errSlot`).
      paramsValue.push(...slot);
      outSlots.push({ param: p, slot });
      continue;
    }
    const v = args[i++];
    if (p.kind === 'bytes_in') {
      const b = v as Buffer;
      // DataType.U64 maps to a plain JS `number` (ffi-rs's own DataTypeToType),
      // not `bigint` — measured: passing a BigInt here throws "NumberExpected".
      paramsValue.push(b, b.length);
    } else {
      paramsValue.push(v);
    }
  }
  const call = rawFn(raw, name);
  const result = call(paramsValue);

  try {
    switch (spec.returns.kind) {
      case 'void':
        return { outcome: 'void' };
      case 'status': {
        const statusName = STATUS_NAMES[Number(result)] ?? `UNKNOWN(${String(result)})`;
        const outs: Record<string, Buffer | HandleRef> = {};
        let error: CallErrorFields | null = null;
        for (const { param, slot } of outSlots) {
          if (param.kind === 'out_error') {
            error = decodeError(raw, statusName, slot);
            continue;
          }
          const ptr = readExternalSlot(slot);
          outs[param.name] = param.type === BUF_HANDLE ? readOwnedBuf(raw, ptr) : { kind: param.type as string, ptr };
        }
        return { outcome: 'status', statusName, outs, error };
      }
      case 'scalar':
        if (spec.returns.type === 'cstr_static') {
          return { outcome: 'value', value: readStaticCString(result as JsExternal) };
        }
        if (spec.returns.type === 'u8ptr_const') {
          throw new Error(
            `chtypes abi1: ${name} returns a borrowed u8ptr_const (its length lives in a different ` +
              'described call); call it directly through the raw table, not through rawCall',
          );
        }
        return { outcome: 'value', value: Number(result) };
      case 'enum':
        return { outcome: 'value', value: Number(result) };
      case 'handle':
        return { outcome: 'handle', handle: { kind: spec.returns.type as string, ptr: result as JsExternal } };
      default:
        throw new Error(`chtypes abi1: unknown return kind ${spec.returns.kind}`);
    }
  } finally {
    for (const { slot } of outSlots) dropSlot(slot);
  }
}
