/**
 * The GC-finalizer backstop for `Schema`/`Filter`/`Block` (issue #302):
 * TypeScript shared Go's gap (no `FinalizationRegistry` anywhere, so a
 * never-closed handle leaked its native memory for the life of the process).
 * `close()` stays the primary, deterministic path — these tests only cover
 * what happens when a caller forgets it, which is what a finalizer is for.
 *
 * Needs `global.gc()` to force a collection deterministically rather than
 * polling and hoping one happens inside the test's timeout; `vitest.config.ts`
 * passes `--expose-gc` to the worker for exactly this file, mirroring Python's
 * own GC-timing-dependent `__del__` safety-net tests.
 */

import { describe, expect, it } from 'vitest';
import type { BlockHandle, FilterHandle, NativeLibrary, SchemaHandle } from '../src/ffi.js';
import { Schema } from '../src/schema.js';

declare const gc: (() => void) | undefined;

/** A NativeLibrary stand-in that records every free call, in order, by name —
 * enough surface for Schema/Filter/Block's construction and close() paths,
 * with no real dlopen behind it. */
function trackingNative(): NativeLibrary & { readonly freed: string[] } {
  const freed: string[] = [];
  let nextFilter = 0;
  let nextBlock = 0;
  return {
    columns: () => [],
    schemaFree: () => freed.push('schema'),
    filterCompile: () => ({ id: `f${nextFilter++}` }) as unknown as FilterHandle,
    filterFree: () => freed.push('filter'),
    blockParse: () => ({ id: `b${nextBlock++}` }) as unknown as BlockHandle,
    blockFree: () => freed.push('block'),
    freed,
  } as unknown as NativeLibrary & { readonly freed: string[] };
}

/** Force a GC pass and give any scheduled FinalizationRegistry callback a
 * chance to run — Node schedules them after collection, not synchronously
 * inside `gc()`. Retries because a single pass is not always enough to
 * reclaim everything made unreachable just before it. */
async function waitForFinalizer(ran: () => boolean, attempts = 20): Promise<void> {
  for (let i = 0; i < attempts && !ran(); i++) {
    gc?.();
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

describe('Schema GC finalizer (issue #302)', () => {
  it('requires --expose-gc, wired through vitest.config.ts', () => {
    // If this fails, the config is not reaching the worker and every other
    // case in this file would otherwise skip the thing it claims to prove.
    expect(typeof gc).toBe('function');
  });

  it('frees a never-closed schema once nothing references it', async () => {
    const native = trackingNative();
    (() => {
      // Scoped so nothing outside this IIFE can keep the Schema reachable.
      new Schema(native, {} as unknown as SchemaHandle, 'x UInt8');
    })();

    await waitForFinalizer(() => native.freed.includes('schema'));

    expect(native.freed).toEqual(['schema']);
  });

  it('frees a still-open filter BEFORE the schema when both are abandoned', async () => {
    // The C layer does not refcount: freeing the schema under a live filter
    // handle is a use-after-free, so the order here is load-bearing, not
    // cosmetic — see SchemaNative in schema.ts for how it is kept correct
    // regardless of which object's finalizer the GC happens to run first.
    const native = trackingNative();
    (() => {
      const schema = new Schema(native, {} as unknown as SchemaHandle, 'x UInt8');
      schema.compileFilter('x = 1');
      // Neither schema nor filter is closed, and neither escapes this scope.
    })();

    await waitForFinalizer(() => native.freed.includes('schema'));

    expect(native.freed).toEqual(['filter', 'schema']);
  });

  it('does not double-free a filter the caller already closed explicitly', async () => {
    const native = trackingNative();
    (() => {
      const schema = new Schema(native, {} as unknown as SchemaHandle, 'x UInt8');
      const filter = schema.compileFilter('x = 1');
      filter.close();
      // schema itself is abandoned unclosed; the already-closed filter must
      // not be freed a second time by the schema's own finalizer sweep.
    })();

    await waitForFinalizer(() => native.freed.includes('schema'));

    expect(native.freed).toEqual(['filter', 'schema']);
  });
});
