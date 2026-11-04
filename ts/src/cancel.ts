/**
 * The asynchronous, cancelable calls (`Schema.rowAsync`, `rowsAsync`, `insertAsync`, `parseBlockAsync`,
 * `Filter.rowsAsync` and `Library.compileTableAsync`): one engine, `callAsync`, that every one of them runs through.
 *
 * Each call runs off the event loop, on a thread of ffi-rs's own (`runInNewThread`, which queues on the libuv
 * threadpool: four threads unless `UV_THREADPOOL_SIZE` says otherwise, so a fifth concurrent call waits for a free
 * thread). The public source of cancellation is the caller's `AbortSignal`; the library's token (`chs_cancel`) is
 * per call and per image, and is never exposed. With a signal, a call:
 *
 *   1. leases every handle it passes (`Abi2Handle.lease`), so a `close()` meanwhile defers its free;
 *   2. creates a token and writes `{"cancel": <id>}` into the call's options document;
 *   3. arranges that the signal's `abort` fires the token, or fires it at once if the signal is already aborted
 *      (the library then answers `CHS_CANCELED` at entry and reads no byte);
 *   4. makes the call;
 *   5. once the native call has returned, removes the arrangement, frees the token, and releases the leases.
 *
 * Fire-before-free: the fire runs on the event loop thread, synchronously, inside the `abort` listener, and the
 * token is freed on that same thread only after the listener is removed, so no fire can reach a freed token.
 *
 * On abort, the promise rejects AT ONCE, queued or running, with a `CallCanceledError` (`name` `'AbortError'`,
 * `cause` the signal's `reason`). The call's buffers, leases and token are still held until the native call
 * returns, and a handle the call made after all (a block, a schema) is freed then. Without an abort, the answer
 * is the library's: `CHS_CANCELED` is the same `CallCanceledError`, and any other status or document is the
 * normal answer. With no signal, no token is created and no extra library call is made.
 *
 * Keep-alive: every argument (the body, every byte document, the out-slots, the token) stays referenced until
 * the native promise settles (`./abi2/raw.ts`: `rawCallInNewThread`); ffi-rs does not hold them itself.
 */

import type { JsExternal } from 'ffi-rs';
import {
  type Abi2Handle,
  CallCanceledError,
  type Calls,
  checkStatus,
  type HandleRef,
  imageOfCalls,
  type RawCallResult,
  rawCall,
  rawCallInNewThread,
  Status,
  wrapHandle,
} from './abi2/index.js';
import { SYMBOL } from './abi2/decls.gen.js';

/** The options every asynchronous call takes besides its own. */
export interface AsyncOptions {
  /** Cancel the call. An aborted signal rejects the call's promise at once with a `CallCanceledError` whose `cause` is `signal.reason`. */
  readonly signal?: AbortSignal | undefined;
}

/** One asynchronous call, as `callAsync` runs it. */
export interface AsyncCall<T> {
  /** The `SYMBOL.*` name of the call. */
  readonly name: string;
  /** Every handle the call passes, leased for the call's duration; `null` for an absent optional handle. */
  readonly handles: readonly (Abi2Handle | null)[];
  /** The input arguments, in the order of the call's input parameters, given each leased pointer (in `handles` order, `undefined` for a `null`) and the token's id (absent with no signal). */
  readonly args: (ptrs: readonly (JsExternal | undefined)[], cancelId: string | undefined) => unknown[];
  /** The answer from the call's outputs; runs only on `CHS_OK`. */
  readonly decode: (outs: Readonly<Record<string, Buffer | HandleRef>>) => T;
}

/** The binding's own `CallCanceledError` for an abort it observed itself, before the library answered. */
function abortedError(signal: AbortSignal): CallCanceledError {
  return new CallCanceledError(
    {
      status: Status.Canceled,
      chCode: 0,
      chName: '',
      messageBytes: Buffer.from('the call was canceled by its AbortSignal', 'utf8'),
      column: Buffer.alloc(0),
      reason: 'call-canceled',
    },
    { cause: signal.reason },
  );
}

/** Free each handle an answer carried that nobody will receive. */
function discardHandles(calls: Calls, outs: Readonly<Record<string, Buffer | HandleRef>>): void {
  for (const out of Object.values(outs)) if (!Buffer.isBuffer(out)) wrapHandle(calls.raw, out).close();
}

/**
 * Test-only: `onReturn` sees each native call's arguments and raw result as it returns (whether or not its promise
 * already settled), and `drain()` resolves once every native call started so far has returned and been cleaned up.
 * Not exported from the package.
 */
export const asyncCallsForTest: {
  onReturn: ((name: string, args: readonly unknown[], result: RawCallResult) => void) | undefined;
  drain: () => Promise<void>;
} = {
  onReturn: undefined,
  drain: async () => {
    while (nativeInFlight.size > 0) await Promise.allSettled([...nativeInFlight]);
  },
};
const nativeInFlight = new Set<Promise<unknown>>();

/** Run one call off the event loop, cancelable through `options.signal`. Every refusal before the call (a closed handle, a malformed option) is a rejection, never a throw. */
export function callAsync<T>(calls: Calls, signal: AbortSignal | undefined, call: AsyncCall<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const image = imageOfCalls(calls);
    const leased: Abi2Handle[] = [];
    let token: Abi2Handle | undefined;
    let settled = false;
    let fired = false;
    const release = (): void => {
      token?.close();
      token = undefined;
      for (const h of leased.splice(0)) h.release();
    };
    const fire = (): void => {
      if (fired || token === undefined) return;
      fired = true;
      rawCall(calls.raw, SYMBOL.CANCEL_FIRE, [token.ptr]);
    };
    const onAbort = (): void => {
      fire();
      if (settled) return;
      settled = true;
      reject(abortedError(signal as AbortSignal));
    };
    let args: unknown[];
    try {
      const ptrs = call.handles.map((h) => {
        if (h === null) return undefined;
        const p = h.lease();
        leased.push(h);
        return p;
      });
      let cancelId: string | undefined;
      if (signal !== undefined) {
        const made = calls.cancelCreate();
        token = made.out;
        cancelId = made.outId.toString('ascii');
      }
      args = call.args(ptrs, cancelId);
    } catch (err) {
      release();
      reject(err);
      return;
    }
    if (signal !== undefined) {
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }
    const native = rawCallInNewThread(calls.raw, image.threaded, call.name, args).then(
      (result) => {
        signal?.removeEventListener('abort', onAbort);
        release();
        asyncCallsForTest.onReturn?.(call.name, args, result);
        if (settled) {
          if (result.outcome === 'status' && result.status === Status.Ok) discardHandles(calls, result.outs);
          return;
        }
        settled = true;
        try {
          resolve(call.decode(checkStatus(result)));
        } catch (err) {
          if (err instanceof CallCanceledError && signal !== undefined) err.cause = signal.reason;
          reject(err);
        }
      },
      (err: unknown) => {
        signal?.removeEventListener('abort', onAbort);
        release();
        if (settled) return;
        settled = true;
        reject(err);
      },
    );
    nativeInFlight.add(native);
    void native.finally(() => nativeInFlight.delete(native));
  });
}
