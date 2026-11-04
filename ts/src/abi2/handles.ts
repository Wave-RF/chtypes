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

/**
 * A live handle over one opened image. `close()` is safe to call more than once and safe to never call explicitly (the registry calls it if this object is abandoned).
 *
 * The close guard. An asynchronous call (`../cancel.ts`) runs on another thread while the event loop goes on, so a
 * `close()` can arrive while a call that was given this handle is still in the library. Such a call LEASES the handle
 * (`lease`, then `release` once the native call has returned). `close()` marks the handle closing, which refuses every
 * later call, synchronous or not, at once; the free itself waits until the last lease is released. `close()` never
 * fires the cancel token of a call in flight.
 */
export abstract class Abi2Handle {
  #raw: RawApi;
  #ptr: JsExternal | null;
  #inflight = 0;
  #closing = false;
  readonly kind: string;

  protected constructor(raw: RawApi, ptr: JsExternal, kind: string) {
    this.#raw = raw;
    this.#ptr = ptr;
    this.kind = kind;
  }

  /** The raw pointer, for passing to `rawCall` as a `handle` argument. A closed (or closing) handle raises a `UsageError` before any C call: a closed handle is never passed to C as NULL. */
  get ptr(): JsExternal {
    if (this.#closing || this.#ptr === null) {
      throw usageError(`this ${HANDLE_INFO[this.kind]?.className ?? this.kind} was already closed`);
    }
    return this.#ptr;
  }

  /** True once `close()` was called, whether or not the deferred free has happened yet. */
  get isClosed(): boolean {
    return this.#closing || this.#ptr === null;
  }

  /** True once the library's reference was actually released. */
  get isFreed(): boolean {
    return this.#ptr === null;
  }

  /** How many asynchronous calls hold this handle now. */
  get inflight(): number {
    return this.#inflight;
  }

  /** Take a lease for one asynchronous call: the pointer, valid until the matching `release`. A closed or closing handle is a `UsageError`. */
  lease(): JsExternal {
    const p = this.ptr;
    this.#inflight++;
    return p;
  }

  /** Return a lease; after `close()`, the last one returned does the free. */
  release(): void {
    if (this.#inflight === 0) throw new Error(`chtypes abi2: internal — a ${this.kind} lease was released twice`);
    this.#inflight--;
    if (this.#inflight === 0 && this.#closing) this.#free();
  }

  close(): void {
    if (this.#closing || this.#ptr === null) return;
    this.#closing = true;
    if (this.#inflight === 0) this.#free();
  }

  #free(): void {
    if (this.#ptr === null) return;
    const freeSymbol = HANDLE_INFO[this.kind]?.free;
    if (freeSymbol === undefined) throw new Error(`chtypes abi2: ${this.kind} has no HANDLE_INFO entry`);
    freeHandle(this.#raw, freeSymbol, this.#ptr);
    this.#ptr = null;
  }
}

function makeHandleClass(kind: string): new (raw: RawApi, ptr: JsExternal) => Abi2Handle {
  const registry = new FinalizationRegistry<{ raw: RawApi; ptr: JsExternal; freeSymbol: string }>((held) => {
    freeHandle(held.raw, held.freeSymbol, held.ptr);
  });
  const info = HANDLE_INFO[kind];
  if (info === undefined) throw new Error(`chtypes abi2: no HANDLE_INFO for ${kind}`);
  const freeSymbol: string = info.free;
  const className: string = info.className;
  class Handle extends Abi2Handle {
    constructor(raw: RawApi, ptr: JsExternal) {
      super(raw, ptr, kind);
      registry.register(this, { raw, ptr, freeSymbol }, this);
    }
    // Unregistering at close is safe while the free is deferred: every lease
    // holder references this object until it releases, which does the free.
    override close(): void {
      super.close();
      registry.unregister(this);
    }
  }
  Object.defineProperty(Handle, 'name', { value: `${className}Handle` });
  return Handle;
}

/** Every described handle kind's wrapper class, keyed by its chs_* name (`HANDLE_INFO`'s own keys — never a literal spelled here). */
export const HANDLE_CLASSES: Readonly<Record<string, new (raw: RawApi, ptr: JsExternal) => Abi2Handle>> = Object.fromEntries(
  Object.keys(HANDLE_INFO).map((kind) => [kind, makeHandleClass(kind)]),
);

/** Wrap a freshly minted handle (`RawCallResult`'s `HandleRef`) in its class. */
export function wrapHandle(raw: RawApi, ref: { readonly kind: string; readonly ptr: JsExternal }): Abi2Handle {
  const Class = HANDLE_CLASSES[ref.kind];
  if (Class === undefined) throw new Error(`chtypes abi2: no handle class for kind ${ref.kind}`);
  return new Class(raw, ref.ptr);
}
