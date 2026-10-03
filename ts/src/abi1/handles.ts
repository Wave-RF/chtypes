/**
 * D2's handle rules on the TS side: one wrapper class per described handle
 * kind, `close()` idempotent, and a `FinalizationRegistry` PER CLASS (never
 * one shared registry with ordering state) — D2 says any free order is
 * safe, so there is nothing for a shared registry to get wrong, and nothing
 * for it to coordinate either.
 *
 * Data-driven from `./decls.gen.ts`'s `HANDLE_INFO`: one class per entry,
 * freeing through `rawFreeHandle` (which reads `info.free`, a `SYMBOL.*`
 * value) rather than a hand-spelled name.
 */

import type { JsExternal } from 'ffi-rs';
import { HANDLE_INFO } from './decls.gen.js';
import { usageError } from './errors.js';
import { freeHandle, type RawApi } from './raw.js';

/** A live handle over one opened image. `close()` is safe to call more than once and safe to never call explicitly (the registry calls it if this object is abandoned). */
export abstract class Abi1Handle {
  #raw: RawApi;
  #ptr: JsExternal | null;
  readonly kind: string;

  protected constructor(raw: RawApi, ptr: JsExternal, kind: string) {
    this.#raw = raw;
    this.#ptr = ptr;
    this.kind = kind;
  }

  /** The raw pointer, for passing to `rawCall` as a `handle` argument. A closed handle raises a `UsageError` before any C call: a closed handle is never passed to C as NULL. */
  get ptr(): JsExternal {
    if (this.#ptr === null) {
      throw usageError(`this ${HANDLE_INFO[this.kind]?.className ?? this.kind} was already closed`);
    }
    return this.#ptr;
  }

  get isClosed(): boolean {
    return this.#ptr === null;
  }

  close(): void {
    if (this.#ptr === null) return;
    const freeSymbol = HANDLE_INFO[this.kind]?.free;
    if (freeSymbol === undefined) throw new Error(`chtypes abi1: ${this.kind} has no HANDLE_INFO entry`);
    freeHandle(this.#raw, freeSymbol, this.#ptr);
    this.#ptr = null;
  }
}

function makeHandleClass(kind: string): new (raw: RawApi, ptr: JsExternal) => Abi1Handle {
  const registry = new FinalizationRegistry<{ raw: RawApi; ptr: JsExternal; freeSymbol: string }>((held) => {
    freeHandle(held.raw, held.freeSymbol, held.ptr);
  });
  const info = HANDLE_INFO[kind];
  if (info === undefined) throw new Error(`chtypes abi1: no HANDLE_INFO for ${kind}`);
  const freeSymbol: string = info.free;
  const className: string = info.className;
  class Handle extends Abi1Handle {
    constructor(raw: RawApi, ptr: JsExternal) {
      super(raw, ptr, kind);
      registry.register(this, { raw, ptr, freeSymbol }, this);
    }
    override close(): void {
      super.close();
      registry.unregister(this);
    }
  }
  Object.defineProperty(Handle, 'name', { value: `${className}Handle` });
  return Handle;
}

/** Every described handle kind's wrapper class, keyed by its chs_* name (`HANDLE_INFO`'s own keys — never a literal spelled here). */
export const HANDLE_CLASSES: Readonly<Record<string, new (raw: RawApi, ptr: JsExternal) => Abi1Handle>> = Object.fromEntries(
  Object.keys(HANDLE_INFO).map((kind) => [kind, makeHandleClass(kind)]),
);

/** Wrap a freshly minted handle (`RawCallResult`'s `HandleRef`) in its class. */
export function wrapHandle(raw: RawApi, ref: { readonly kind: string; readonly ptr: JsExternal }): Abi1Handle {
  const Class = HANDLE_CLASSES[ref.kind];
  if (Class === undefined) throw new Error(`chtypes abi1: no handle class for kind ${ref.kind}`);
  return new Class(raw, ref.ptr);
}
