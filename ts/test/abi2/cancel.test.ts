/**
 * The asynchronous, cancelable calls (`../../src/cancel.ts`) over the generated
 * v2 stub (variant `ok`), whose cancel token is real: a fired token makes a call
 * answer `CHS_CANCELED` at entry, and a first byte argument beginning `!B:`
 * blocks the call until its token fires. Every case sequences on the stub's own
 * probe (`chs_type_validate("!C:")`: how many calls sit blocked now, and how
 * many fires reached a freed token), never on a sleep; the 1 ms wait between
 * probe reads only bounds how long a broken stub can hang a case. Without
 * `$CHTYPES_ABI2_STUBS` every case SKIPS LOUDLY by name.
 */

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { setFlagsFromString } from 'node:v8';
import { runInNewContext } from 'node:vm';
import { isNullPointer } from 'ffi-rs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SYMBOL } from '../../src/abi2/decls.gen.js';
import { CallCanceledError, EXPORT_NONE, Format, type RawCallResult, rawCall, Status, UsageError } from '../../src/abi2/index.js';
import { type LoadedImage, openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { rawCallInNewThread, threadedKeepAlive } from '../../src/abi2/raw.js';
import { asyncCallsForTest } from '../../src/cancel.js';
import { type Library, libraryOf } from '../../src/library.js';
import type { Schema } from '../../src/schema.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;
if (!stubsAvailable) {
  console.warn('SKIP: test/abi2/cancel.test.ts — every case: $CHTYPES_ABI2_STUBS is not set (scripts/abi-v1/build-stubs.sh --major 2)');
}

function openStub(): { image: LoadedImage; library: Library } {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { readonly predicate: Predicate }>;
  };
  const v = doc.variants.ok;
  if (v === undefined) throw new Error('no stub variant ok');
  const image = openAbi2({
    libraryPath: path.join(STUBS_DIR as string, 'ok.so'),
    predicate: v.predicate,
    platform: `${v.predicate.os}-${v.predicate.arch}`,
  });
  return { image, library: libraryOf(image, undefined) };
}

const CREATE = 'CREATE TABLE t (x Int32) ENGINE = Memory';
const BODY = Buffer.from('{"x":1}\n');
/** The stub's block directive: a call with a token blocks until it fires. */
const BLOCK = Buffer.from('!B:{"x":1}\n');
/** The id the library mints: `c` and 16 lowercase hexadecimal digits. */
const CANCEL_ID = /^c[0-9a-f]{16}$/;

/** The stub's cancel probe. */
function probe(image: LoadedImage): { inflight: number; fire_after_free: number } {
  const call = rawCall(image.raw, SYMBOL.TYPE_VALIDATE, [Buffer.from('!C:')]);
  if (call.outcome !== 'status' || call.status !== Status.Ok) throw new Error('the cancel probe did not answer CHS_OK');
  return JSON.parse((call.outs.out as Buffer).toString('utf8')) as { inflight: number; fire_after_free: number };
}

/** Wait, on the probe, until `pred` holds; false if it never did. */
async function waitFor(pred: () => boolean): Promise<boolean> {
  for (let i = 0; i < 5000; i++) {
    if (pred()) return true;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  return pred();
}

/** Record every native return, by call name, for the case's own reading. */
function recordReturns(): { name: string; args: readonly unknown[]; result: RawCallResult }[] {
  const seen: { name: string; args: readonly unknown[]; result: RawCallResult }[] = [];
  asyncCallsForTest.onReturn = (name, args, result) => seen.push({ name, args, result });
  return seen;
}

/** The options document (the last argument) of one recorded call. */
function optionsDoc(s: { args: readonly unknown[] } | undefined): Buffer {
  if (s === undefined) throw new Error('no call was recorded');
  return s.args.at(-1) as Buffer;
}

function statusOf(r: RawCallResult): number {
  if (r.outcome !== 'status') throw new Error('not a status call');
  return r.status;
}

async function expectCanceled(p: Promise<unknown>, reason: unknown): Promise<CallCanceledError> {
  const err = await p.then(
    () => {
      throw new Error('the call resolved; it was expected to be canceled');
    },
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(CallCanceledError);
  const c = err as CallCanceledError;
  expect(c.name).toBe('AbortError');
  expect(c.status).toBe(Status.Canceled);
  expect(c.reason).toBe('call-canceled');
  expect(c.cause).toBe(reason);
  return c;
}

/** The six cancelable calls, each given a signal and a body (or statement) whose first byte argument is `first`. */
function cancelables(library: Library, schema: Schema): Record<string, (first: Buffer, signal: AbortSignal) => Promise<unknown>> {
  const filter = schema.compileFilter('x > 0');
  return {
    row: (b, signal) => schema.rowAsync(Format.JSONEachRow, b, { signal }),
    rows: (b, signal) => schema.rowsAsync(Format.JSONEachRow, b, { signal }),
    insert: (b, signal) => schema.insertAsync(Format.JSONEachRow, b, EXPORT_NONE, { signal }),
    parseBlock: (b, signal) => schema.parseBlockAsync(Format.JSONEachRow, b, { signal }),
    'filter.rows': (b, signal) => filter.rowsAsync(Format.JSONEachRow, b, { signal }),
    compileTable: (b, signal) => library.compileTableAsync(Buffer.concat([b.subarray(0, b.indexOf('{')), Buffer.from(CREATE)]), { signal }),
  };
}

describe.skipIf(!stubsAvailable)('cancelable async calls over the stub library', () => {
  afterEach(async () => {
    await asyncCallsForTest.drain();
    asyncCallsForTest.onReturn = undefined;
    threadedKeepAlive.enabled = true;
  });

  it('an already-aborted signal rejects at once with CallCanceledError, and the library still answered CHS_CANCELED at entry with no output', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const calls = cancelables(library, schema);
    const baseline = library.liveHandles();
    const seen = recordReturns();
    for (const [name, call] of Object.entries(calls)) {
      const ac = new AbortController();
      const reason = new Error(`stop ${name}`);
      ac.abort(reason);
      await expectCanceled(call(BODY, ac.signal), reason);
    }
    await asyncCallsForTest.drain();
    expect(seen.map((s) => s.name).sort()).toEqual(
      [SYMBOL.PREVIEW_ROW, SYMBOL.PREVIEW_BATCH, SYMBOL.PREVIEW_INSERT, SYMBOL.BLOCK_CREATE, SYMBOL.FILTER_EVAL_BODY, SYMBOL.SCHEMA_CREATE].sort(),
    );
    for (const s of seen) {
      expect(statusOf(s.result), s.name).toBe(Status.Canceled);
      if (s.result.outcome !== 'status') continue;
      expect(s.result.error?.reason, s.name).toBe('call-canceled');
      for (const [k, out] of Object.entries(s.result.outs)) {
        expect(Buffer.isBuffer(out) ? out.length === 0 : isNullPointer(out.ptr), `${s.name} ${k} is NULL`).toBe(true);
      }
    }
    expect(library.liveHandles()).toEqual(baseline);
    expect(probe(image).fire_after_free).toBe(0);
  });

  it('an abort during the call: each of the six blocks in the stub, the abort rejects it, and the library answers CHS_CANCELED', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const seen = recordReturns();
    const calls = cancelables(library, schema);
    const baseline = library.liveHandles();
    for (const [name, call] of Object.entries(calls)) {
      const before = probe(image).inflight;
      const ac = new AbortController();
      const p = call(BLOCK, ac.signal);
      expect(await waitFor(() => probe(image).inflight > before), `${name} reached the block directive`).toBe(true);
      const reason = `stop ${name}`;
      ac.abort(reason);
      await expectCanceled(p, reason);
      await asyncCallsForTest.drain();
      const last = seen.at(-1);
      expect(last === undefined ? undefined : statusOf(last.result), name).toBe(Status.Canceled);
    }
    expect(library.liveHandles()).toEqual(baseline);
    expect(probe(image).fire_after_free).toBe(0);
  });

  it('cancel after the last check point', async () => {
    // The stub's `!L:` body passes the library's last check point and then waits for the token, so the native call
    // goes on to return a complete answer. The TypeScript call still rejects promptly with CallCanceledError (cause =
    // the signal's reason); the late native result is freed and never surfaced, and its buffers stay alive until the
    // native call returns.
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const seen = recordReturns();
    const late = Buffer.from('!L:{"x":1}\n');
    const baseline = library.liveHandles();
    // parseBlock returns a handle, so a late result that leaked would show in the live count.
    for (const [name, call] of [
      ['row', (signal: AbortSignal) => schema.rowAsync(Format.JSONEachRow, late, { signal })],
      ['parseBlock', (signal: AbortSignal) => schema.parseBlockAsync(Format.JSONEachRow, late, { signal })],
    ] as const) {
      const before = probe(image).inflight;
      const ac = new AbortController();
      const reasonValue = { why: `stop ${name}` };
      const p = call(ac.signal);
      expect(await waitFor(() => probe(image).inflight > before), `${name} reached the wait`).toBe(true);
      ac.abort(reasonValue);
      await expectCanceled(p, reasonValue);
      // Rejected while the native call is still waiting inside the library.
      expect(probe(image).inflight, name).toBe(before + 1);
      expect(await waitFor(() => probe(image).inflight === before), `${name} native call returned`).toBe(true);
      await asyncCallsForTest.drain();
      const last = seen.at(-1);
      expect(last === undefined ? undefined : statusOf(last.result), `${name}: the library answered`).toBe(Status.Ok);
      expect(library.liveHandles(), `${name}: the late result was freed`).toEqual(baseline);
    }
    expect(probe(image).fire_after_free).toBe(0);
  });

  it('never aborted: the same answer as the synchronous call, with the token id in the options document', async () => {
    const { library } = openStub();
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x > 0');
    const signal = new AbortController().signal;
    const seen = recordReturns();
    expect(await schema.rowAsync(Format.JSONEachRow, BODY, { signal })).toEqual(schema.row(Format.JSONEachRow, BODY));
    // (No export or output rows here: the stub's export echoes the options document, which carries the id.)
    expect(await schema.rowsAsync(Format.JSONEachRow, BODY, { signal, docFlags: 1 })).toEqual(
      schema.rows(Format.JSONEachRow, BODY, { docFlags: 1 }),
    );
    expect(await schema.insertAsync(Format.JSONEachRow, BODY, EXPORT_NONE, { signal, rowFilter: filter })).toEqual(
      schema.insert(Format.JSONEachRow, BODY, EXPORT_NONE, { rowFilter: filter }),
    );
    expect(await filter.rowsAsync(Format.JSONEachRow, BODY, { signal })).toEqual(filter.rows(Format.JSONEachRow, BODY));
    const block = await schema.parseBlockAsync(Format.JSONEachRow, BODY, { signal });
    expect(filter.eval(block)).toEqual(filter.eval(schema.parseBlock(Format.JSONEachRow, BODY)));
    const compiled = await library.compileTableAsync(CREATE, { signal });
    expect(compiled.describe()).toEqual(schema.describe());
    // Each call's options document (its last argument) is exactly {"cancel": <a fresh id>}, merged with the others.
    for (const s of seen) {
      const doc = JSON.parse((s.args.at(-1) as Buffer).toString('utf8')) as { cancel: string };
      expect(Object.keys(doc), s.name).toEqual(['cancel']);
      expect(doc.cancel, s.name).toMatch(CANCEL_ID);
    }
    expect(new Set(seen.map((s) => (s.args.at(-1) as Buffer).toString())).size).toBe(seen.length);
  });

  it('the cancel id merges with the options the call already builds, and replaces a cancel key in insertOptionsExtra', async () => {
    const { library } = openStub();
    const schema = library.compileTable(CREATE);
    const seen = recordReturns();
    await schema.rowAsync(Format.JSONEachRow, BODY, {
      signal: new AbortController().signal,
      memoryLimitBytes: 7,
      insertOptionsExtra: { cancel: '"c0000000000000000"' },
    });
    const doc = JSON.parse(optionsDoc(seen[0]).toString('utf8')) as Record<string, string>;
    expect(doc.memory_limit_bytes).toBe('7');
    expect(doc.cancel).toMatch(CANCEL_ID);
    expect(doc.cancel).not.toBe('c0000000000000000');
  });

  it('with no signal: no token is made and no extra call, and the options document is the synchronous one', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const spy = vi.spyOn(image.calls, 'cancelCreate');
    const seen = recordReturns();
    expect(await schema.rowAsync(Format.JSONEachRow, BODY)).toEqual(schema.row(Format.JSONEachRow, BODY));
    expect(spy).not.toHaveBeenCalled();
    expect(optionsDoc(seen[0]).length).toBe(0);
    spy.mockRestore();
  });

  it('one signal across concurrent calls cancels all of them', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const baseline = library.liveHandles();
    const seen = recordReturns();
    const before = probe(image).inflight;
    const ac = new AbortController();
    const ps = [
      schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: ac.signal }),
      schema.rowsAsync(Format.JSONEachRow, BLOCK, { signal: ac.signal }),
      schema.insertAsync(Format.JSONEachRow, BLOCK, EXPORT_NONE, { signal: ac.signal }),
    ];
    expect(await waitFor(() => probe(image).inflight === before + 3)).toBe(true);
    ac.abort('all');
    for (const p of ps) await expectCanceled(p, 'all');
    await asyncCallsForTest.drain();
    expect(seen.map((s) => statusOf(s.result))).toEqual([Status.Canceled, Status.Canceled, Status.Canceled]);
    expect(library.liveHandles()).toEqual(baseline);
  });

  it('independent signals cancel only their own call', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const before = probe(image).inflight;
    const a = new AbortController();
    const b = new AbortController();
    const pa = schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: a.signal });
    let bSettled = false;
    const pb = schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: b.signal }).finally(() => {
      bSettled = true;
    });
    expect(await waitFor(() => probe(image).inflight === before + 2)).toBe(true);
    a.abort('a');
    await expectCanceled(pa, 'a');
    // A's call has left the stub; B's is still blocked in it, and its promise is pending.
    expect(await waitFor(() => probe(image).inflight === before + 1)).toBe(true);
    expect(bSettled).toBe(false);
    b.abort('b');
    await expectCanceled(pb, 'b');
  });

  it('close() while a call is in flight: later calls are refused at once, the free waits for the call, and close does not cancel it', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x > 0');
    const baseline = library.liveHandles();
    const before = probe(image).inflight;
    const ac = new AbortController();
    const p = schema.rowsAsync(Format.JSONEachRow, BLOCK, { signal: ac.signal, rowFilter: filter });
    expect(await waitFor(() => probe(image).inflight === before + 1)).toBe(true);
    schema.close();
    filter.close();
    expect(() => schema.row(Format.JSONEachRow, BODY)).toThrow(UsageError);
    expect(() => filter.rows(Format.JSONEachRow, BODY)).toThrow(UsageError);
    await expect(schema.rowAsync(Format.JSONEachRow, BODY)).rejects.toBeInstanceOf(UsageError);
    // The library still holds both: the free is deferred, and the call is still blocked (close fired nothing).
    expect(library.liveHandles().chs_schema).toBe(baseline.chs_schema);
    expect(library.liveHandles().chs_filter).toBe(baseline.chs_filter);
    expect(probe(image).inflight).toBe(before + 1);
    ac.abort('done');
    await expectCanceled(p, 'done');
    await asyncCallsForTest.drain();
    expect(library.liveHandles().chs_schema).toBe((baseline.chs_schema ?? 0) - 1);
    expect(library.liveHandles().chs_filter).toBe((baseline.chs_filter ?? 0) - 1);
    expect(library.liveHandles().chs_cancel).toBe(baseline.chs_cancel);
  });

  it('a call queued behind a full threadpool rejects at once on abort, and the library answers it at entry', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const pool = Number(process.env.UV_THREADPOOL_SIZE ?? 4);
    const before = probe(image).inflight;
    const ac = new AbortController();
    const blockers = Array.from({ length: pool }, () => schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: ac.signal }));
    expect(await waitFor(() => probe(image).inflight === before + pool)).toBe(true);
    const queuedAc = new AbortController();
    const queued = schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: queuedAc.signal });
    queuedAc.abort('queued');
    await expectCanceled(queued, 'queued');
    // It never reached the stub: the pool is full.
    expect(probe(image).inflight).toBe(before + pool);
    ac.abort('blockers');
    for (const p of blockers) await expectCanceled(p, 'blockers');
  });

  it('a soak of racing aborts and returns: live handles back at baseline, and no fire ever reaches a freed token', async () => {
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const filter = schema.compileFilter('x > 0');
    const baseline = library.liveHandles();
    const fafBefore = probe(image).fire_after_free;
    const ROUNDS = 300;
    const ps: Promise<unknown>[] = [];
    for (let i = 0; i < ROUNDS; i++) {
      const ac = new AbortController();
      const body = i % 3 === 0 ? BLOCK : BODY;
      const call =
        i % 4 === 0
          ? schema.rowAsync(Format.JSONEachRow, body, { signal: ac.signal })
          : i % 4 === 1
            ? schema.parseBlockAsync(Format.JSONEachRow, body, { signal: ac.signal }).then((b) => b.close())
            : i % 4 === 2
              ? filter.rowsAsync(Format.JSONEachRow, body, { signal: ac.signal })
              : schema.insertAsync(Format.JSONEachRow, body, Format.CSV, { signal: ac.signal });
      ps.push(call.catch((e: unknown) => (e instanceof CallCanceledError ? 'canceled' : Promise.reject(e))));
      // Abort at a spread of moments: at once, after a turn, after a timer, or (for an unblocked call) after it returned.
      const when = i % 5;
      if (when === 0) ac.abort();
      else if (when === 1) queueMicrotask(() => ac.abort());
      else if (when === 2) setImmediate(() => ac.abort());
      else if (when === 3) setTimeout(() => ac.abort(), 0);
      else void call.then(() => ac.abort(), () => ac.abort());
      // A blocked call whose signal is aborted only after it returns would wait out the stub's cap: abort those soon.
      if (body === BLOCK && when === 4) setTimeout(() => ac.abort(), 1);
    }
    await Promise.all(ps);
    await asyncCallsForTest.drain();
    expect(library.liveHandles()).toEqual(baseline);
    expect(probe(image).fire_after_free).toBe(fafBefore);
  });

  it('keep-alive: a queued call whose caller dropped every reference still reads its own body (and, with CHTYPES_TS_PLANTED_RED=1, the planted red: with it off, a use-after-free)', async () => {
    // A call whose arguments nobody references once it is dispatched: the
    // frame's keep-alive (rawCallInNewThread holding its arguments until the
    // native call returns) is the only thing that holds the body. Queue the
    // call behind a full threadpool, collect, reuse the memory, then let it
    // run: the stub echoes the sha256 of the bytes it read. With the keep-alive
    // off (the test-only switch `threadedKeepAlive`) the body is collected
    // while the call waits, and the stub hashes freed memory. That half reads
    // freed memory, so it runs only on request (CHTYPES_TS_PLANTED_RED=1).
    setFlagsFromString('--expose-gc');
    const gc = runInNewContext('gc') as () => void;
    const { image, library } = openStub();
    const schema = library.compileTable(CREATE);
    const pool = Number(process.env.UV_THREADPOOL_SIZE ?? 4);
    const handle = image.calls.schemaCreate(null, Buffer.from(CREATE), Buffer.alloc(0), Buffer.alloc(0));

    async function bodyAsEchoed(keepAlive: boolean): Promise<{ collected: boolean; intact: boolean }> {
      threadedKeepAlive.enabled = keepAlive;
      const before = probe(image).inflight;
      const ac = new AbortController();
      const blockers = Array.from({ length: pool }, () => schema.rowAsync(Format.JSONEachRow, BLOCK, { signal: ac.signal }).catch(() => undefined));
      expect(await waitFor(() => probe(image).inflight === before + pool)).toBe(true);
      const SIZE = 64 << 10;
      const make = (): Buffer => Buffer.alloc(SIZE, 'a');
      let collected = false;
      const registry = new FinalizationRegistry(() => {
        collected = true;
      });
      const pending = (() => {
        const body = make();
        registry.register(body, 0);
        const args: unknown[] = [handle.ptr, Format.JSONEachRow, body, Buffer.alloc(0), Buffer.alloc(0), Buffer.alloc(0)];
        const p = rawCallInNewThread(image.raw, image.threaded, SYMBOL.PREVIEW_ROW, args);
        // The caller's own array is the caller's reference: drop it too, so only the frame's keep-alive is left.
        args.length = 0;
        return p;
      })();
      for (let i = 0; i < 8; i++) {
        gc();
        await new Promise((resolve) => setImmediate(resolve));
        const fill: Buffer[] = Array.from({ length: 16 }, () => Buffer.alloc(SIZE, 'Z'));
        void fill;
      }
      ac.abort();
      await Promise.all(blockers);
      const r = await pending;
      threadedKeepAlive.enabled = true;
      const echo = JSON.parse(((r.outcome === 'status' ? r.outs.out : undefined) as Buffer).toString('utf8')) as { args: unknown[] };
      return { collected, intact: JSON.stringify(echo.args[2]).includes(createHash('sha256').update(make()).digest('hex')) };
    }

    expect(await bodyAsEchoed(true), 'with the keep-alive on, the body is held and the stub reads it').toEqual({ collected: false, intact: true });
    if (process.env.CHTYPES_TS_PLANTED_RED === '1') {
      // The red is the collection itself: the body's memory was released while the call still had to read it. Whether
      // the stub then hashes changed bytes depends on whether the allocator reused that memory first (usually it has).
      const red = await bodyAsEchoed(false);
      expect(red.collected, 'with the keep-alive off, the body is collected while the call waits to read it').toBe(true);
      console.warn(`planted red: body collected while queued; the stub read ${red.intact ? 'stale but unchanged' : 'reused'} memory`);
    }
    handle.close();
  });
});
