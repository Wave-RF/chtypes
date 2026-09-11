/**
 * The native layer: one `dlopen`'d chtypes artifact behind the frozen `chs_*` C
 * ABI (the core repository's C ABI specification). Everything about ownership and lifetime lives here so no
 * caller ever has to remember it.
 *
 * ------------------------------------------------------------------ memory
 * Every `char *` a `chs_*` function returns is malloc'd by the library and MUST
 * be released with **that same library's** `chs_free` — in a multi-version
 * process the frees must never cross. There is exactly one place in this package
 * that turns a returned pointer into a JS string, `NativeLibrary#takeString`,
 * and it frees in a `finally`. Nothing else may touch an owned pointer.
 *
 * Borrowed `const char *` returns (`chs_clickhouse_version`, the column getters)
 * are declared as `DataType.String`, which copies without freeing — correct,
 * because freeing them would be a bug.
 *
 * -------------------------------------------------------------------- FFI
 * This binds through **ffi-rs** (libffi, prebuilt N-API per platform) rather
 * than koffi, and the reason is measured rather than aesthetic:
 *
 *   koffi runs every native call on its own preallocated stack — its arm64
 *   trampoline does `add sp, x1, #136` before `blr` (see
 *   koffi/src/koffi/src/abi_arm64_asm.S). ClickHouse's `checkStackSize()` reads
 *   the *pthread* stack bounds and compares them against the current frame, so
 *   with koffi every code path that calls it throws TOO_DEEP_RECURSION (306):
 *   "Stack size too large. Stack address: 0x16a6dc000, frame address:
 *   0x1065fce10, stack size: 1687024112, maximum stack size: 8388608".
 *   Measured on darwin-arm64/25.8: `chs_schema_compile("b Nullable(String)
 *   DEFAULT 'x'")` fails under koffi (sync and async, at every
 *   `sync_stack_size`) and succeeds under ffi-rs, which calls on the caller's
 *   own stack. Every DEFAULT, TTL and expression path is affected, i.e. the
 *   product's headline behavior, so the FFI had to change.
 *
 * ffi-rs ships prebuilt binaries for darwin-arm64/x64 and linux arm64/x64
 * (gnu and musl) — the shipping platform included — and needs no build step.
 */

import {
  DataType,
  PointerType,
  createExternalBuffer,
  createPointer,
  define,
  freePointer,
  isNullPointer,
  load,
  open,
  restorePointer,
  wrapPointer,
  type JsExternal,
} from 'ffi-rs';
import { realpathSync } from 'node:fs';
import { ABI_REVISION, ChtypesError, schemaErrorFor, UnsupportedError } from './errors.js';
import { DOC_ALL, EXPORT_NONE } from './format.js';

const { External, String: Str, I32, U64, Void, U8Array } = DataType;

/** A compiled-schema handle. Opaque; only `NativeLibrary` may pass it around. */
export type SchemaHandle = JsExternal;

/** A compiled-filter handle. Opaque; only `NativeLibrary` may pass it around. */
export type FilterHandle = JsExternal;

/** A parsed-block handle (revision 4). Opaque, same rule. */
export type BlockHandle = JsExternal;

/** Counters a test can assert on: every taken string must be freed. */
const stats = { stringsTaken: 0, stringsFreed: 0 };

/**
 * Leak-audit counters over every owned `char *` this process has taken from
 * any loaded artifact. `stringsTaken === stringsFreed` when no chtypes call is
 * on the stack; a test asserts exactly that (Level 1 conformance: zero growth
 * over a few thousand calls). Diagnostic only — a snapshot, never live state.
 *
 * @returns a copy of the counters at the moment of the call. Never throws.
 */
export function nativeStats(): { stringsTaken: number; stringsFreed: number } {
  return { ...stats };
}

const MISSING_SYMBOL = /Cannot find "(.+?)" function in shared library/;

function missingSymbol(err: unknown): string | null {
  const m = err instanceof Error ? MISSING_SYMBOL.exec(err.message) : null;
  return m === null ? null : (m[1] ?? '');
}

/**
 * A symbol the artifact does not export means "this artifact predates the
 * feature" and MUST degrade to `unsupported` at call time — never to a load
 * failure (docs/reference/artifact.md §Loading, step 5).
 */
function unsupportedIfMissing<T>(err: unknown, message: string): T {
  if (missingSymbol(err) !== null) throw new UnsupportedError(message);
  throw err;
}

function asBuffer(bytes: Uint8Array): Buffer {
  if (typeof bytes === 'string') {
    throw new ChtypesError(
      'chtypes: row bodies must be a byte sequence, not a string: binary formats ' +
        'contain NUL bytes and text rows can carry invalid UTF-8 on purpose ' +
        '(docs/reference/bindings.md §Values a binding must accept and reject).',
    );
  }
  return Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

// ---------------------------------------------------------------- out params
//
// ffi-rs models an out parameter as a pointer created by `createPointer`, passed
// straight through (not unwrapped — unwrapping peels a level and would hand the
// callee the value instead of its address). A `char **` slot is an 8-byte slot
// created as U64 so it starts out NULL: `chs_validate_type` and
// `chs_schema_compile` only *write* `*out_err` on failure, so a slot seeded with
// anything else would be read back as a bogus error string on the happy path.

type Slot = JsExternal[];

const ptrSlot = (): Slot => createPointer({ paramsType: [U64], paramsValue: [0] });
const intSlot = (): Slot => createPointer({ paramsType: [I32], paramsValue: [0] });

const readPtr = (s: Slot): JsExternal => restorePointer<JsExternal>({ retType: [External], paramsValue: s })[0]!;
const readInt = (s: Slot): number => Number(restorePointer<number>({ retType: [I32], paramsValue: s })[0] ?? 0);

const dropPtrSlot = (s: Slot): void =>
  freePointer({ paramsType: [U64], paramsValue: s, pointerType: PointerType.RsPointer });
const dropIntSlot = (s: Slot): void =>
  freePointer({ paramsType: [I32], paramsValue: s, pointerType: PointerType.RsPointer });

// ------------------------------------------------------------ the symbol table

function declare(library: string) {
  const d = (retType: DataType, paramsType: DataType[]) => ({ library, retType, paramsType });
  return define({
    // Mandatory four (docs/reference/artifact.md §Loading, step 4). chs_schema_compile is
    // the ONE consolidated compile entry point — settings_json and mode are
    // always part of its signature now, not a separate `_v2` overload.
    chs_clickhouse_version: d(Str, []),
    chs_abi_revision: d(I32, []),
    chs_init: d(I32, [Str, Str, External]),
    chs_schema_compile: d(External, [Str, Str, I32, External, External]),
    // Revision 3: export_format (int), doc_flags (unsigned — I32 carries the
    // three defined bits and any refused ones identically), out_bytes
    // (chs_bytes * — passed as a 16-byte scratch buffer, see `rows`).
    chs_rows: d(External, [External, I32, U8Array, U64, Str, I32, I32, U8Array]),
    // Everything else is optional and degrades to `unsupported` at call time.
    chs_free: d(Void, [External]),
    chs_shutdown: d(Void, []),
    chs_set_default_settings: d(I32, [Str, External]),
    chs_validate_type: d(I32, [Str, External, External, External]),
    chs_schema_free: d(Void, [External]),
    // Also consolidated: merge_tree_settings_json is always part of the
    // signature, not a separate `_v2` overload.
    chs_schema_engine: d(I32, [External, Str, Str, Str, External]),
    chs_schema_ttl: d(I32, [External, Str, External]),
    chs_schema_column_count: d(I32, [External]),
    chs_schema_column_name: d(Str, [External, I32]),
    chs_schema_column_type: d(Str, [External, I32]),
    chs_schema_column_default_kind: d(Str, [External, I32]),
    chs_schema_column_default_expr: d(Str, [External, I32]),
    chs_schema_column_default_is_literal: d(I32, [External, I32]),
    chs_row: d(External, [External, I32, U8Array, U64, Str]),
    chs_reference_type: d(External, [Str]),
    chs_registered_families: d(External, []),
    chs_function_flags: d(External, []),
    // Revision 3: the filter trio. Optional like everything above — a missing
    // symbol degrades to `unsupported` at call time.
    // Revision 4: chs_filter_compile carries params_json ({name:Type} query
    // parameters — a JSON object of name -> value STRING, "{}" for none). The
    // ABI-revision gate is what guarantees this 5-argument declaration
    // describes the loaded artifact: a rev-3 artifact (4-argument shape) is
    // refused at load, and a rev-0 artifact predates the symbol entirely.
    chs_filter_compile: d(External, [External, Str, Str, External, External]),
    chs_filter_free: d(Void, [External]),
    chs_filter_rows: d(External, [External, I32, U8Array, U64, Str]),
    // Revision 4: the block twin (the core repository's C ABI specification §Blocks) — parse a body once,
    // evaluate K filters against the block. Optional, same degradation rule.
    chs_block_parse: d(External, [External, I32, U8Array, U64, Str, External, External]),
    chs_block_free: d(Void, [External]),
    chs_filter_eval: d(External, [External, External]),
    // Reached through the artifact's own dependency graph (it links libc), and
    // only used to bound the copy of an owned C string.
    strlen: d(U64, [External]),
    // Also libc, via the same graph: the copy out of the export channel's
    // library-owned buffer, which arrives as a raw address inside the
    // chs_bytes struct rather than as a JsExternal (see `rows`).
    memcpy: d(Void, [U8Array, U64, U64]),
  });
}

type Fns = ReturnType<typeof declare>;

const loaded = new Map<string, NativeLibrary>();

/** One loaded artifact. Loading the same path twice returns the same object. */
export class NativeLibrary {
  private readonly fns: Fns;
  private readonly haveStrlen: boolean;
  private initTimezone: string | null = null;
  /** Cached answer of `hasCompileSettings` — one probe per loaded library. */
  private compileSettingsProbe: boolean | null = null;
  /**
   * How many calls into the library are on the stack right now — the reentrancy
   * tripwire for the ABI's one genuinely dangerous entry point.
   *
   * `chs_set_default_settings` REPLACES a process-global that `chs_row` /
   * `chs_rows` / `chs_schema_compile` read **by reference**, so
   * the core repository's C ABI specification §Thread-safety requires it be serialized against every
   * other call. Go enforces that with an `RWMutex` and Python with an
   * `_RWLock`, because both have real threads inside a foreign call.
   *
   * **Node's exposure is nil, and this counter is the proof rather than the
   * assumption.** Every `chs_*` call in this file is synchronous — ffi-rs is
   * called without `runInNewThread`, nothing here awaits, and the library
   * never calls back into JS — so one JS thread cannot be inside `chs_rows`
   * when it enters `setDefaultSettings`. The only way it could is a genuine
   * re-entry, and that is exactly what this counts. A tripwire that never
   * fires is the point: if a later change makes a call async, or hands the
   * library a JS callback, the seed stops being safe and this says so loudly
   * instead of corrupting a row.
   *
   * Not a lock. It cannot make a `worker_threads` setup safe — separate JS
   * threads share one dlopen'd image and one set of C globals, and only one of
   * them would see this counter. `docs/reference/bindings.md` §Concurrency says so.
   */
  private inCall = 0;
  readonly path: string;
  readonly version: string;
  /**
   * The chs_* ABI revision this ARTIFACT was built from, or 0 when it predates
   * `chs_abi_revision`. A library that exists reports either `ABI_REVISION` or
   * 0 — a different nonzero revision is refused in the constructor.
   */
  readonly abiRevision: number;

  /** The realpath `open()` registered the image under — the `library` name for
   * raw `load` calls that step outside the declared symbol table (the
   * free-by-address in `rows`'s export path). */
  private readonly key: string;

  private constructor(path: string, key: string) {
    this.path = path;
    this.key = key;
    this.fns = declare(key);

    // The library names itself; nothing is inferred from the path or file name.
    this.version = this.fns.chs_clickhouse_version([]) as string;
    if (typeof this.version !== 'string' || this.version === '') {
      throw new ChtypesError(`chtypes: ${path} did not report a ClickHouse version`);
    }

    // The ABI identity gate (the core repository's C ABI specification §ABI identity). ffi-rs resolves
    // symbols at call time, so absence surfaces as its missing-symbol throw —
    // which here means "the artifact predates the probe" (revision 0), keeping
    // the per-symbol degradation rules. A DIFFERENT nonzero revision is a
    // positive statement that these declarations do not describe this artifact,
    // and calling through them would be undefined.
    let abiRevision = 0;
    try {
      abiRevision = Number(this.fns.chs_abi_revision([]));
    } catch (err) {
      if (missingSymbol(err) === null) throw err;
    }
    this.abiRevision = abiRevision;
    if (abiRevision !== 0 && abiRevision !== ABI_REVISION) {
      throw new ChtypesError(
        `chtypes: ${path} reports ABI revision ${abiRevision}, this binding speaks ` +
          `${ABI_REVISION}; refusing to call through mismatched declarations`,
      );
    }

    // Assert the string-taking path against a known answer before trusting it on
    // owned pointers: `chs_clickhouse_version` returns a borrowed static string,
    // so probing it costs nothing and frees nothing.
    this.haveStrlen = this.probeStrlen(key);
  }

  private probeStrlen(key: string): boolean {
    try {
      const ptr = load<DataType.External>({
        library: key,
        funcName: 'chs_clickhouse_version',
        retType: External,
        paramsType: [],
        paramsValue: [],
      });
      if (isNullPointer(ptr)) return false;
      const n = Number(this.fns.strlen([ptr]));
      return n === Buffer.byteLength(this.version, 'utf8') && createExternalBuffer(ptr, n).toString('utf8') === this.version;
    } catch {
      return false;
    }
  }

  static open(path: string): NativeLibrary {
    // dlopen is refcounted, and `chs_init` must run exactly once per loaded
    // library, so two Registry instances over one directory must share one
    // NativeLibrary rather than initializing it twice.
    const key = realpathSync(path);
    const existing = loaded.get(key);
    if (existing !== undefined) return existing;
    open({ library: key, path });
    const lib = new NativeLibrary(path, key);
    loaded.set(key, lib);
    return lib;
  }

  static isLoaded(path: string): boolean {
    try {
      return loaded.has(realpathSync(path));
    } catch {
      return false;
    }
  }

  /**
   * Copy an owned `char *` into **bytes** and release it with **this** library's
   * `chs_free`. The only place that ever owns a returned pointer, and the only
   * place that decides what a returned pointer becomes.
   *
   * It must be bytes, not a string. A result document carries ClickHouse's own
   * rendering of stored values, and a `String` / `FixedString` /
   * `AggregateFunction` column holds arbitrary bytes: a UTF-8 decode here would
   * replace every invalid byte with U+FFFD before any caller could see the
   * value, and the binding would then report bytes the table does not hold
   * (the core repository's C ABI specification §Per-column fields; the reference implementation crosses this
   * boundary with `C.GoString`, which is a byte copy, and keeps
   * `json.RawMessage` from there on). The copy is bounded by the `strlen` this
   * class already probes — bytes, not a latin1 round trip, because a `Buffer`
   * needs no round trip at all.
   */
  private takeBytes(ptr: JsExternal): Buffer | null {
    if (isNullPointer(ptr)) return null;
    stats.stringsTaken++;
    try {
      if (this.haveStrlen) {
        const n = Number(this.fns.strlen([ptr]));
        // A zero-length C string still has to come back as empty rather than
        // null: an empty error message is not the absence of an error message.
        if (n === 0) return Buffer.alloc(0);
        // createExternalBuffer WRAPS the library's own allocation, so the copy is
        // not an optimization to skip: `chs_free` runs in the finally below and
        // the caller would be reading freed memory.
        return Buffer.from(createExternalBuffer(ptr, n));
      }
      // Degraded mode, only for an artifact whose dependency graph gives no
      // `strlen`: ffi-rs's own C-string reader copies through UTF-8, so a
      // non-UTF-8 stored value cannot survive it. No artifact lands
      // here (every artifact links libc), and `probeStrlen` proves the fast path
      // against a known answer before it is trusted.
      const text = restorePointer<string>({ retType: [Str], paramsValue: wrapPointer([ptr]) })[0] ?? '';
      return Buffer.from(text, 'utf8');
    } finally {
      this.fns.chs_free([ptr]);
      stats.stringsFreed++;
    }
  }

  /**
   * `takeBytes` decoded as UTF-8, for the paths whose payload is a message, a
   * canonical type or a family list rather than a stored value.
   *
   * The reference implementation reaches the same place by a different route: it
   * keeps ClickHouse's bytes in a Go string and lets `encoding/json` substitute
   * U+FFFD when the message is marshaled out. Decoding one step earlier here
   * gives the same text at the surface, and no result-document value passes
   * through this method.
   */
  private takeString(ptr: JsExternal): string | null {
    const bytes = this.takeBytes(ptr);
    return bytes === null ? null : bytes.toString('utf8');
  }

  /**
   * Run one library call with the reentrancy counter raised. See `inCall`.
   * Counting rather than flagging so nested calls unwind correctly if a future
   * change ever introduces one.
   */
  private entered<T>(call: () => T): T {
    this.inCall++;
    try {
      return call();
    } finally {
      this.inCall--;
    }
  }

  /**
   * The guard the two process-state mutators take. Throws rather than waiting:
   * there is nothing to wait FOR on a single JS thread — a call on the stack
   * below this one can only be a re-entry, and re-entering the library to
   * replace the settings list a caller further down is still reading is the
   * use-after-free the ABI's serialization rule exists to prevent.
   */
  private refuseIfInCall(what: string): void {
    if (this.inCall > 0) {
      throw new ChtypesError(
        `chtypes: ${what} was called from inside another chtypes call on ${this.path}; ` +
          `it replaces process-global state the row path reads by reference and ` +
          `must be serialized against every other call (the core repository's C ABI specification §Thread-safety)`,
      );
    }
  }

  // ------------------------------------------------------------- process setup

  /**
   * `chs_init(timezone, unsafe_families, out_err)`, **exactly once** per loaded
   * library — dlopen is refcounted, so two registries over one directory share
   * this object and the second `init` must be a no-op rather than a second
   * process setup.
   *
   * The timezone defaults to UTC and is never read from the environment: without
   * it the host's TZ leaks into results. `unsafeFamilies` is the artifact's own
   * generated refuse-list, and empty is a valid list rather than a missing file.
   * `out_err` is ClickHouse's own message on the one reachable failure — an
   * unknown timezone — and is folded into the thrown error's message; a bare
   * code cannot say which name was rejected.
   */
  init(timezone: string, unsafeFamilies: string): void {
    if (this.initTimezone !== null) {
      if (this.initTimezone !== timezone) {
        throw new ChtypesError(
          `chtypes: ${this.path} is already loaded with timezone ${this.initTimezone}; ` +
            `one loaded library gets one chs_init, so ${timezone} cannot be honored`,
        );
      }
      return;
    }
    const errSlot = ptrSlot();
    try {
      const rc = Number(this.fns.chs_init([timezone, unsafeFamilies, ...errSlot]));
      if (rc !== 0) {
        // The one reachable failure is an unknown timezone; out_err is the only
        // way to say which name was rejected (the core repository's C ABI specification chs_init).
        const message = this.takeString(readPtr(errSlot)) ?? '';
        throw new ChtypesError(
          `chtypes: chs_init failed for ${this.path} (rc ${rc})${message === '' ? '' : `: ${message}`}`,
        );
      }
      this.initTimezone = timezone;
    } finally {
      dropPtrSlot(errSlot);
    }
  }

  /**
   * `chs_set_default_settings(settings_json, out_err)`. On refusal, `out_err`
   * carries the server's own message — including its did-you-mean hint on an
   * unknown-name 115, the whole diagnostic value of that code — folded into
   * the thrown error's message.
   */
  setDefaultSettings(settingsJson: string): void {
    this.refuseIfInCall('setDefaultSettings');
    const errSlot = ptrSlot();
    try {
      let rc: number;
      try {
        rc = Number(this.fns.chs_set_default_settings([settingsJson, ...errSlot]));
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates chs_set_default_settings (rebuild it)');
      }
      if (rc !== 0) {
        // out_err carries the server's own message here, including its
        // did-you-mean hint on a 115 — the whole diagnostic value of that code.
        const message = this.takeString(readPtr(errSlot)) ?? '';
        throw new ChtypesError(
          `chtypes: chs_set_default_settings failed for ${this.path} (rc ${rc})${message === '' ? '' : `: ${message}`}`,
        );
      }
    } finally {
      dropPtrSlot(errSlot);
    }
  }

  /**
   * Joins the DEFAULT evaluator's background threads. `chs_init` registers it
   * with `atexit`, so an ordinary process needs no call; a test that must not
   * depend on `atexit` does. Idempotent.
   */
  shutdown(): void {
    this.refuseIfInCall('shutdown');
    try {
      this.fns.chs_shutdown([]);
    } catch {
      // An artifact without chs_shutdown predates the hang it fixes.
    }
  }

  // -------------------------------------------------------------------- types

  validateType(expr: string): { canonical: string; code: number; message: string; ok: boolean } {
    const canonSlot = ptrSlot();
    const codeSlot = intSlot();
    const errSlot = ptrSlot();
    try {
      let rc: number;
      try {
        rc = this.entered(() =>
          Number(this.fns.chs_validate_type([expr, ...canonSlot, ...codeSlot, ...errSlot])),
        );
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates chs_validate_type (rebuild it)');
      }
      const canonical = this.takeString(readPtr(canonSlot)) ?? '';
      const message = this.takeString(readPtr(errSlot)) ?? '';
      return { canonical, code: readInt(codeSlot), message, ok: rc === 0 };
    } finally {
      dropPtrSlot(canonSlot);
      dropIntSlot(codeSlot);
      dropPtrSlot(errSlot);
    }
  }

  referenceType(expr: string): string {
    try {
      return this.takeString(this.fns.chs_reference_type([expr]) as JsExternal) ?? '';
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_reference_type (rebuild it)');
    }
  }

  registeredFamilies(): string[] {
    let text: string | null;
    try {
      text = this.takeString(this.fns.chs_registered_families([]) as JsExternal);
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_registered_families (rebuild it)');
    }
    return (text ?? '').split('\n').filter((line) => line !== '');
  }

  /** The function-volatility TSV audit, verbatim. Requires `chs_init`. */
  functionFlags(): string {
    try {
      return this.takeString(this.fns.chs_function_flags([]) as JsExternal) ?? '';
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_function_flags (rebuild it)');
    }
  }

  // ------------------------------------------------------------------ schemas

  /**
   * Compile a column-declaration list, optionally under a DECLARED settings
   * profile fixed into the handle exactly as a real CREATE TABLE fixes its
   * settings into the table (the core repository's C ABI specification §Compile-time vs per-call
   * settings). `settingsJson` "{}" plus `mode` 0 (`CHS_COMPILE_DECLARED`) is
   * structurally the plain compile — the settings-free path this build has
   * always taken. Throws `SchemaError` when the SERVER refuses — an unknown
   * setting name carries its own code 115 — and `UnsupportedError` when this
   * build declines, e.g. a mode other than 0.
   */
  schemaCompile(ddl: string, settingsJson: string, mode: number): SchemaHandle {
    const codeSlot = intSlot();
    const errSlot = ptrSlot();
    try {
      const handle = this.entered(
        () => this.fns.chs_schema_compile([ddl, settingsJson, mode, ...codeSlot, ...errSlot]) as JsExternal,
      );
      if (!isNullPointer(handle)) return handle;
      const message = this.takeString(readPtr(errSlot)) ?? '';
      // No column is attributed here, and that is deliberate. Go's `dlopen`'d
      // path (`multiversion.go`), Python and Rust all pass no column on this
      // call, so `chtypes: [-2] …` is the spelling three of the four SDKs
      // render and this one used to be alone in printing
      // `chtypes: column "b": [-2] …`. It was a GUESS — the longest declared
      // name that happened to appear in the server's text — and the library's
      // own message already names the column when it knows one ("DEFAULT for
      // column b exceeded the admission memory budget"), so the guess added no
      // information and cost uniformity. Measured before removing it: the
      // conformance wire is unaffected, because the driver puts `err.detail`
      // (the bare message) in `scope`, never `err.message` — 6,411 recorded
      // `unsupported` records across 7 versions, zero scope differences from
      // the reference column, and zero column-attributed renders anywhere in
      // the recorded output. See examples/README.md finding 5.
      throw schemaErrorFor(readInt(codeSlot), message);
    } finally {
      dropIntSlot(codeSlot);
      dropPtrSlot(errSlot);
    }
  }

  /**
   * Does this artifact export the consolidated, settings-aware
   * `chs_schema_compile`? True on every artifact this repo builds — the
   * symbol is one of the mandatory four (docs/reference/artifact.md §Loading, step 4).
   * The probe stays for a Registry that may someday load a third-party-built
   * artifact that lacks it.
   *
   * The reference loader answers this with a load-time `dlsym`
   * (`go/chtypes/multiversion.go`, `HasCompileSettings`); ffi-rs
   * resolves symbols at call time, so this asks with a call the ABI defines as
   * refused loudly and cheaply: `mode 1` with a non-empty profile answers `-2`
   * and creates no handle on every artifact that exports the symbol
   * (the core repository's C ABI specification §Compile-time vs per-call settings, rule 2), while an
   * artifact without the symbol throws ffi-rs's missing-symbol error. Cached:
   * one refused compile per loaded library.
   *
   * False means `compileDdl` with a non-empty `settings` and `setEngine` with
   * non-empty `mergeTreeSettings` throw an `UnsupportedError` —
   * never a crash and never a silent settings-free compile of a
   * differently-shaped table.
   */
  hasCompileSettings(): boolean {
    if (this.compileSettingsProbe !== null) return this.compileSettingsProbe;
    const codeSlot = intSlot();
    const errSlot = ptrSlot();
    try {
      const handle = this.fns.chs_schema_compile([
        'x UInt8',
        '{"flatten_nested":"1"}',
        1,
        ...codeSlot,
        ...errSlot,
      ]) as JsExternal;
      // Refused as designed — free the message it came with. Defensively free
      // the handle too: an artifact that ever accepted this call would still
      // have proven the symbol, and must not leak a schema doing it.
      if (!isNullPointer(handle)) this.fns.chs_schema_free([handle]);
      this.takeString(readPtr(errSlot));
      this.compileSettingsProbe = true;
    } catch (err) {
      if (missingSymbol(err) === null) throw err;
      this.compileSettingsProbe = false;
    } finally {
      dropIntSlot(codeSlot);
      dropPtrSlot(errSlot);
    }
    return this.compileSettingsProbe;
  }

  schemaFree(handle: SchemaHandle): void {
    try {
      this.fns.chs_schema_free([handle]);
    } catch {
      // Nothing sane to do while releasing; the process is going away anyway.
    }
  }

  /**
   * Declare the table engine, optionally with its MergeTree-namespace
   * settings (the `SETTINGS` clause after the engine — `allow_nullable_key`
   * and friends, a namespace `DB::Settings` cannot carry). `mergeTreeSettingsJson`
   * "{}" is structurally the plain engine declaration this build has always
   * made.
   *
   * Error mapping per the core repository's C ABI specification: the SIGN of rc decides the KIND of
   * answer. A positive rc is a real ClickHouse code — the server's own
   * refusal of this DDL, which can therefore never exist — and crosses
   * verbatim as a `SchemaError` with that code and the server's message
   * (`115`, with its "Maybe you meant ..." hint, is today's only instance).
   * A negative rc is this library declining and is an `UnsupportedError`: a
   * decline must never be presented as a rejection the product invented, and
   * a rejection must never be hidden behind a decline. A missing symbol
   * degrades to `UnsupportedError`, never a crash.
   */
  schemaEngine(handle: SchemaHandle, engine: string, orderBy: string, mergeTreeSettingsJson: string): void {
    const errSlot = ptrSlot();
    try {
      let rc: number;
      try {
        rc = this.entered(() =>
          Number(this.fns.chs_schema_engine([handle, engine, orderBy, mergeTreeSettingsJson, ...errSlot])),
        );
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates engine support (rebuild it)');
      }
      if (rc === 0) return;
      const message = this.takeString(readPtr(errSlot)) ?? '';
      // The SIGN of rc is the rule. Positive is a real ClickHouse error code:
      // the server's own engine validation REFUSED this DDL, it can never
      // exist, and the tenant has to be told — a SchemaError carrying the
      // server's code and message (which holds the "Maybe you meant ..."
      // hint). Negative is this library declining (-2 "I will not guess", -1 a
      // guarded exception) and becomes an UnsupportedError. schemaErrorFor
      // keys on the sign, not on the literal 115, so a code a future era
      // returns here cannot silently be demoted to a decline.
      throw schemaErrorFor(rc, message);
    } finally {
      dropPtrSlot(errSlot);
    }
  }

  schemaTtl(handle: SchemaHandle, ttl: string): void {
    const errSlot = ptrSlot();
    try {
      let rc: number;
      try {
        rc = this.entered(() => Number(this.fns.chs_schema_ttl([handle, ttl, ...errSlot])));
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates TTL support (rebuild it)');
      }
      if (rc === 0) return;
      throw new UnsupportedError(this.takeString(readPtr(errSlot)) ?? '');
    } finally {
      dropPtrSlot(errSlot);
    }
  }

  /**
   * Column introspection is all-or-nothing: the six getters shipped together, so
   * a missing one means the whole group is absent and the column list stays empty
   * rather than partially populated.
   */
  columns(handle: SchemaHandle): {
    name: string;
    type: string;
    defaultKind: string;
    defaultExpr: string;
    defaultIsLiteral: boolean;
  }[] {
    try {
      const n = Number(this.fns.chs_schema_column_count([handle]));
      const out = [];
      for (let i = 0; i < n; i++) {
        out.push({
          name: this.fns.chs_schema_column_name([handle, i]) as string,
          type: this.fns.chs_schema_column_type([handle, i]) as string,
          defaultKind: this.fns.chs_schema_column_default_kind([handle, i]) as string,
          defaultExpr: this.fns.chs_schema_column_default_expr([handle, i]) as string,
          defaultIsLiteral: Number(this.fns.chs_schema_column_default_is_literal([handle, i])) !== 0,
        });
      }
      return out;
    } catch (err) {
      if (missingSymbol(err) !== null) return [];
      throw err;
    }
  }

  // --------------------------------------------------------------------- rows

  /**
   * `chs_row`: one row body. Returns the raw result document **as bytes** — a
   * stored value can be any byte sequence, so the document can be too.
   */
  row(handle: SchemaHandle, format: number, raw: Uint8Array, settingsJson: string): Buffer {
    const body = asBuffer(raw);
    let ptr: JsExternal;
    try {
      ptr = this.entered(
        () => this.fns.chs_row([handle, format, body, body.length, settingsJson]) as JsExternal,
      );
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_row (rebuild it)');
    }
    // A NULL return is how the reference reports the same thing through cgo.
    const doc = this.takeBytes(ptr);
    if (doc === null) throw new UnsupportedError('this artifact predates chs_row (rebuild it)');
    return doc;
  }

  /**
   * `chs_rows`: a whole request body — ONE call, whatever the caller asked
   * for (the core repository's C ABI specification §Rows; revision 3). `exportFormat` is `EXPORT_NONE`
   * (-1, the default — no bytes) or an `enum chs_format` value the artifact
   * can serialize; `docFlags` selects the document groups (`DOC_ALL`
   * reproduces the revision-2 document byte-for-byte). Returns the raw result
   * document as bytes, plus the export channel:
   *
   *   `payload === null`  the library emitted nothing — no export requested,
   *                       the export DECLINED (the document's
   *                       `export_declined` says why), or a call-level
   *                       verdict preempted it. The ABI's `{NULL, 0}`.
   *   zero-length Buffer  EMITTED-EMPTY: an accepted batch with zero accepted
   *                       rows — `data` non-NULL, `len` 0. An answer, not a
   *                       decline.
   *   bytes               the batch's accepted rows, serialized once by the
   *                       vendored writer; slice per the document's
   *                       `row_spans`.
   *
   * Ownership: the `chs_bytes` out-param is a 16-byte scratch struct
   * (`{char *data; size_t len}`, both little-endian on every supported
   * platform); the library's malloc'd `data` is COPIED here and immediately
   * freed with THIS library's `chs_free` — no ownership ever escapes this
   * method. The copy runs through libc `memcpy` because the address arrives
   * as a struct field rather than a `JsExternal`, and the free goes through a
   * raw `load` of `chs_free` with the address passed by value — the C ABI
   * passes a `u64` and a pointer identically on darwin-arm64 and
   * linux-arm64/x64.
   *
   * A NULL out-param is legal at the C level only when no export is
   * requested; this binding always passes the scratch struct, which the
   * library initializes to `{NULL, 0}` at entry — same document either way,
   * and one less branch to get wrong.
   *
   * Degrades exactly as `row` does (the asymmetry was a 2026-08-26 fix): a
   * missing symbol and a NULL return are both the ABI's "the loaded artifact
   * does not export the function" (the core repository's C ABI specification §Rows) and surface as the
   * DECLINE type, never a crash and never a generic error a caller cannot
   * handle as the decline it is. `chs_rows` is one of the mandatory four, so
   * with repo-built artifacts the branch is unreachable — the type still has
   * to be the honest one.
   */
  rows(
    handle: SchemaHandle,
    format: number,
    body: Uint8Array,
    settingsJson: string,
    exportFormat: number = EXPORT_NONE,
    docFlags: number = DOC_ALL,
  ): { doc: Buffer; payload: Buffer | null } {
    const buf = asBuffer(body);
    // The chs_bytes out-param: {char *data; size_t len}, 16 bytes, zeroed.
    const outBytes = Buffer.alloc(16);
    let ptr: JsExternal;
    try {
      ptr = this.entered(
        () =>
          this.fns.chs_rows([
            handle,
            format,
            buf,
            buf.length,
            settingsJson,
            exportFormat,
            docFlags,
            outBytes,
          ]) as JsExternal,
      );
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_rows (rebuild it)');
    }
    // Copy-then-free the export buffer FIRST, whatever the document says: the
    // bytes are library-owned malloc'd memory and this is the one place that
    // ever sees the address.
    let payload: Buffer | null = null;
    const dataAddr = outBytes.readBigUInt64LE(0);
    if (dataAddr !== 0n) {
      const len = Number(outBytes.readBigUInt64LE(8));
      if (len > 0) {
        payload = Buffer.alloc(len);
        this.fns.memcpy([payload, Number(dataAddr), len]);
      } else {
        payload = Buffer.alloc(0);
      }
      load({
        library: this.key,
        funcName: 'chs_free',
        retType: DataType.Void,
        paramsType: [DataType.U64],
        paramsValue: [Number(dataAddr)],
      });
      stats.stringsTaken++;
      stats.stringsFreed++;
    }
    const doc = this.takeBytes(ptr);
    if (doc === null) throw new UnsupportedError('this artifact predates chs_rows (rebuild it)');
    return { doc, payload };
  }

  // ------------------------------------------------------------------ filters

  /**
   * `chs_filter_compile`: one boolean expression over a compiled schema's
   * PHYSICAL columns, compiled by the same TreeRewriter + ExpressionAnalyzer
   * pipeline the CONSTRAINT CHECK path runs (the core repository's C ABI specification §Filters).
   *
   * The error split is rule 12's: a NULL handle with a positive code is the
   * server's own refusal (`SchemaError`, code and message verbatim — unknown
   * identifier 47, unknown function, an analyzer-raised NO_COMMON_TYPE, and
   * since revision 4 the server's own parameter refusals: 456 for an unbound
   * `{name:Type}`, 457 for an unparseable value); a negative code is this
   * library declining (`UnsupportedError` — clock reads, server-constants).
   * A missing symbol degrades to the decline type: the artifact predates the
   * trio.
   *
   * `paramsJson` (revision 4) is the `{name:Type}` bindings, a JSON object
   * of name -> value STRING; `"{}"` declares none.
   */
  filterCompile(schema: SchemaHandle, expr: string, paramsJson: string): FilterHandle {
    const codeSlot = intSlot();
    const errSlot = ptrSlot();
    try {
      let handle: JsExternal;
      try {
        handle = this.entered(
          () =>
            this.fns.chs_filter_compile([schema, expr, paramsJson, ...codeSlot, ...errSlot]) as JsExternal,
        );
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates chs_filter_compile (rebuild it)');
      }
      if (!isNullPointer(handle)) return handle;
      const message = this.takeString(readPtr(errSlot)) ?? '';
      throw schemaErrorFor(readInt(codeSlot), message);
    } finally {
      dropIntSlot(codeSlot);
      dropPtrSlot(errSlot);
    }
  }

  /** `chs_filter_free`. Never throws: nothing sane to do while releasing. */
  filterFree(handle: FilterHandle): void {
    try {
      this.fns.chs_filter_free([handle]);
    } catch {
      // An artifact without the symbol never produced a handle to free.
    }
  }

  /**
   * `chs_filter_rows`: evaluate the filter over a body of rows. Returns the
   * raw filter document as bytes; a missing symbol or NULL return degrades to
   * the decline type like every optional entry point.
   */
  filterRows(handle: FilterHandle, format: number, body: Uint8Array, settingsJson: string): Buffer {
    const buf = asBuffer(body);
    let ptr: JsExternal;
    try {
      ptr = this.entered(
        () => this.fns.chs_filter_rows([handle, format, buf, buf.length, settingsJson]) as JsExternal,
      );
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_filter_rows (rebuild it)');
    }
    const doc = this.takeBytes(ptr);
    if (doc === null) throw new UnsupportedError('this artifact predates chs_filter_rows (rebuild it)');
    return doc;
  }

  // ------------------------------------------------------------------ blocks

  /**
   * `chs_block_parse` (revision 4): parse a body ONCE into a block — the
   * parse half of `chs_filter_rows`, exported so K filters can evaluate one
   * event with no re-parse (the core repository's C ABI specification §Blocks). A call-level failure
   * (unknown setting 115, framing, a binary decode fault) returns NULL with
   * ClickHouse's own code/message — a malformed body yields no block and no
   * partial answers; the sign of the code picks the error class, exactly as
   * `filterCompile`. A missing symbol degrades to the decline type.
   */
  blockParse(schema: SchemaHandle, format: number, body: Uint8Array, settingsJson: string): BlockHandle {
    const buf = asBuffer(body);
    const codeSlot = intSlot();
    const errSlot = ptrSlot();
    try {
      let handle: JsExternal;
      try {
        handle = this.entered(
          () =>
            this.fns.chs_block_parse([
              schema,
              format,
              buf,
              buf.length,
              settingsJson,
              ...codeSlot,
              ...errSlot,
            ]) as JsExternal,
        );
      } catch (err) {
        return unsupportedIfMissing(err, 'this artifact predates chs_block_parse (rebuild it)');
      }
      if (!isNullPointer(handle)) return handle;
      const message = this.takeString(readPtr(errSlot)) ?? '';
      throw schemaErrorFor(readInt(codeSlot), message);
    } finally {
      dropIntSlot(codeSlot);
      dropPtrSlot(errSlot);
    }
  }

  /** `chs_block_free`. Never throws: nothing sane to do while releasing. */
  blockFree(handle: BlockHandle): void {
    try {
      this.fns.chs_block_free([handle]);
    } catch {
      // An artifact without the symbol never produced a handle to free.
    }
  }

  /**
   * `chs_filter_eval` (revision 4): evaluate one compiled filter over an
   * already-parsed block — a pure function of (filter, block), no settings.
   * Returns the same raw filter document `chs_filter_rows` returns; a
   * cross-schema (filter, block) pair answers a rejected document (1002)
   * from the C layer, loudly, never undefined behavior.
   */
  filterEval(filter: FilterHandle, block: BlockHandle): Buffer {
    let ptr: JsExternal;
    try {
      ptr = this.entered(() => this.fns.chs_filter_eval([filter, block]) as JsExternal);
    } catch (err) {
      return unsupportedIfMissing(err, 'this artifact predates chs_filter_eval (rebuild it)');
    }
    const doc = this.takeBytes(ptr);
    if (doc === null) throw new UnsupportedError('this artifact predates chs_filter_eval (rebuild it)');
    return doc;
  }
}

