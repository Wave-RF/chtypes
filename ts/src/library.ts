/**
 * One loaded ClickHouse build. It names itself — `chs_clickhouse_version()` —
 * and nothing is ever inferred from the directory or the file name.
 */

import { schemaErrorFor } from './errors.js';
import type { NativeLibrary } from './ffi.js';
import { Schema } from './schema.js';
import { encodeSettings, type Settings } from './settings.js';

/**
 * The minor line: the first two dot-separated components of the reported
 * version. `25.10` is a *later* minor than `25.8`, so string comparison of minor
 * lines is meaningless and must not be used for ordering.
 *
 * @param version - an exact patch (`"25.8.28.1-lts"`) or already a minor line.
 * @returns the minor line (`"25.8"`); a string with fewer than two components
 *   is returned unchanged. Never throws.
 */
export function minorOf(version: string): string {
  const parts = version.split('.');
  if (parts.length < 2) return version;
  return `${parts[0]}.${parts[1]}`;
}

/**
 * The compile MODE — how the profile handed to `compileDdl` relates to the
 * settings this build compiles under. Numeric values are part of the C ABI
 * (bindings pass an int), exactly like `Format`.
 */
export const CompileMode = {
  /**
   * The only defined mode: every setting the profile names takes the
   * caller's value, every setting it does not name keeps the library's own
   * permissive compile base. A partial profile can admit a schema the server
   * might refuse, but can never fabricate a rejection. Any other value is
   * refused loudly (an `UnsupportedError`), reserved for a future
   * COMPLETE-profile mode.
   */
  Declared: 0,
} as const;

export type CompileMode = (typeof CompileMode)[keyof typeof CompileMode];

/**
 * Options for `Library#compileDdl` — the DECLARED settings profile a schema is
 * compiled under (the core repository's C ABI specification §Compile-time vs per-call settings;
 * docs/reference/bindings.md "Compile under a declared settings profile").
 */
export interface CompileOptions {
  /**
   * The settings the deployment's server runs, fixed into the compiled schema
   * exactly as a real CREATE TABLE fixes them into the table. Values are
   * strings (or bigints), as everywhere on this ABI. Absent or empty is
   * structurally identical to the plain compile — `chs_schema_compile` takes
   * "{}" plus mode 0 either way. An unknown setting name fails the compile
   * with the server's own code 115 (`chtypes_*` per-call keys included), and
   * nothing is compiled.
   */
  readonly settings?: Settings | undefined;
  /**
   * `CompileMode.Declared` (0), the default and the only defined mode — see
   * `CompileMode`.
   */
  readonly mode?: CompileMode | undefined;
}

/**
 * One loaded ClickHouse build — the entry point for compiling schemas and
 * canonicalizing types under that release's exact semantics. Obtained from
 * `Registry#for`; never constructed directly.
 *
 * Thread-safety: every call here is synchronous on the JS thread, so ordinary
 * single-threaded Node code needs no locking. `setDefaultSettings` and
 * `shutdown` mutate per-library process state and refuse loudly if called from
 * inside another chtypes call (a reentrancy guard, not a lock). Two
 * `worker_threads` share one dlopen'd image and one set of C globals, which no
 * per-isolate guard can see — seed settings before starting workers, or
 * serialize the seed yourself (docs/reference/bindings.md §Concurrency).
 */
export class Library {
  /** The exact patch this build is, e.g. "25.8.28.1-lts". */
  readonly version: string;
  /** The line callers and the rigs ask in, e.g. "25.8". */
  readonly minor: string;
  /** The shared library that was loaded. */
  readonly path: string;
  /**
   * The chs_* ABI revision this ARTIFACT was built from, or 0 when it predates
   * `chs_abi_revision`. A `Library` that exists reports either `ABI_REVISION`
   * or 0 — a different nonzero revision is refused at load (the core repository's C ABI specification
   * §ABI identity).
   */
  readonly abiRevision: number;

  /** @internal — obtained from a `Registry`. */
  constructor(private readonly native: NativeLibrary) {
    this.version = native.version;
    this.minor = minorOf(native.version);
    this.path = native.path;
    this.abiRevision = native.abiRevision;
  }

  /**
   * Parse and canonicalize one type expression, e.g. `DECIMAL(18,4)` ->
   * `Decimal(18, 4)`. The library's spelling is authoritative: pass it through
   * verbatim and never normalize its whitespace.
   *
   * Note that this alone is insufficient for a schema — `x Int64 DEFAULT NULL`
   * canonicalizes the *column* to `Nullable(Int64)`, which only `compileDdl` sees.
   *
   * @param typeExpr - a ClickHouse type expression, e.g. `"Nullable(Decimal(18,4))"`.
   * @returns the canonical spelling, e.g. `"Nullable(Decimal(18, 4))"`.
   * @throws {SchemaError} when ClickHouse itself refuses the expression —
   *   `code` is its own code (`50` `Unknown data type family` for a typo) and
   *   the message its own text.
   * @throws {UnsupportedError} when this build declines to construct the type
   *   (an unsafe family), or the artifact predates `chs_validate_type`.
   */
  validateType(typeExpr: string): string {
    const r = this.native.validateType(typeExpr);
    if (!r.ok) throw schemaErrorFor(r.code, r.message);
    return r.canonical;
  }

  /**
   * Compile a ClickHouse column-declaration list — not a CREATE TABLE:
   * `"a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()"`.
   * It is parsed by ClickHouse's own `ParserColumnDeclarationList`, so DEFAULT
   * expressions are validated as real SQL. Column-level TTL clauses belong here.
   *
   * With `options.settings` the schema is compiled under that DECLARED profile,
   * exactly as a real CREATE TABLE under those settings: `{ flatten_nested: '0' }`
   * keeps `n Nested(a,b)` ONE column `n` of type `Nested(…)` where the default
   * flattens it to the `n.a`/`n.b` Array columns, and every downstream shape —
   * `columns`, JSONEachRow name lookup, positional arity, the RowBinary wire —
   * follows the compiled shape. The profile is compile-time only and immutable
   * for the schema's life; per-call settings still govern row parsing, and only
   * row parsing.
   *
   * Absent/empty settings are structurally the plain compile: `chs_schema_compile`
   * takes settings "{}" and `mode` `CompileMode.Declared` (0) either way. An
   * unknown setting name fails the compile with the server's own code 115.
   *
   * @param ddl - a column-declaration list, e.g. `"x UInt8, s String DEFAULT 'x'"`.
   * @param options - the declared settings profile and compile mode.
   * @returns the compiled `Schema`. Release it with `close()` (or `using`).
   * @throws {SchemaError} when the compile is REFUSED with a positive code: a
   *   bad type or DEFAULT carries ClickHouse's own code and message; an
   *   unknown setting name in `options.settings` carries the server's own 115
   *   (with its did-you-mean hint), and nothing is compiled; a type gate
   *   declared at a refusing value fails with the server's own 455/44, exactly
   *   as that server's CREATE; an Enum DEFAULT outside the declared domain is
   *   691 (schema-level poisoning, refused on every version by design).
   * @throws {UnsupportedError} when this build DECLINES rather than guesses —
   *   a `mode` other than `CompileMode.Declared`, or a DEFAULT it refuses to
   *   evaluate (server-property functions like `hostName()`, `sleep`, an
   *   admission-budget overrun). Validate cautiously; do not blame the tenant.
   */
  compileDdl(ddl: string, options?: CompileOptions): Schema {
    const settingsJson = encodeSettings(options?.settings);
    const mode = options?.mode ?? CompileMode.Declared;
    return new Schema(this.native, this.native.schemaCompile(ddl, settingsJson, mode), ddl);
  }

  /**
   * Does this artifact export the consolidated, settings-aware
   * `chs_schema_compile`? True on every artifact this repo builds — the
   * symbol is one of the mandatory four. Kept for a `Registry` that may
   * someday load a third-party-built artifact that lacks it, in which case
   * `compileDdl` with a non-empty `settings` and `setEngine` with non-empty
   * `mergeTreeSettings` throw an `UnsupportedError` instead of
   * crashing.
   */
  hasCompileSettings(): boolean {
    return this.native.hasCompileSettings();
  }

  /**
   * Seed the settings every later call starts from. ClickHouse gates several type
   * families at column-creation time (`allow_suspicious_low_cardinality_types`,
   * `allow_experimental_json_type`, …); those are properties of the server the
   * table lives on, not of the row, so a gateway sets them once here. Anything a
   * per-call settings map sets still wins.
   *
   * Prefer `compileDdl(ddl, { settings })` for a gate that belongs to a
   * particular tenant's table: a gate declared in the compile profile binds
   * where a real server binds it — once, at CREATE — and then outranks the
   * per-call map for that handle (measured on live 25.10.7.6 and 26.7.3.19;
   * the core repository's C ABI specification, "Server-level type gates"). This process-wide seed stays the
   * right channel only for gateway-uniform policy.
   *
   * @param settings - the seed. An unknown name refuses the WHOLE payload with
   *   the server's own 115 and nothing is committed — a gateway can never
   *   believe a default profile is in force when part of it never applied.
   * @throws {ChtypesError} when the payload is refused (the message carries
   *   the server's own text, did-you-mean hint included), when a value is a JS
   *   `number`, or when called from inside another chtypes call — it replaces
   *   process-global state the row path reads by reference, so it must never
   *   overlap another call on this library. In a `worker_threads` setup, seed
   *   BEFORE starting workers; the guard cannot see across isolates.
   * @throws {UnsupportedError} when the artifact predates
   *   `chs_set_default_settings`.
   */
  setDefaultSettings(settings: Settings): void {
    this.native.setDefaultSettings(encodeSettings(settings));
  }

  /**
   * Diagnostic: the widened reference type this build compares against —
   * `UInt8` → `Int256`, `DateTime` → `DateTime64(0, 'UTC')`. Not needed to
   * function (the reference parse is already applied inside `row`/`rows` and
   * reported per column); it makes transformation findings explainable.
   *
   * @param typeExpr - a ClickHouse type expression.
   * @returns the reference type, or `''` for a type with no wider type to
   *   compare against (`String`, `Float64`).
   * @throws {UnsupportedError} when the artifact predates `chs_reference_type`.
   */
  referenceType(typeExpr: string): string {
    return this.native.referenceType(typeExpr);
  }

  /**
   * Every type family in this build's own runtime registry (139 entries on
   * 25.8) — the answer to "does this build track upstream type families?"
   * without a hand-maintained table. Part of the three-question introspection
   * surface every SDK exposes (docs/reference/bindings.md §Introspection).
   *
   * @returns the family names, one per registry entry.
   * @throws {UnsupportedError} when the artifact predates
   *   `chs_registered_families`.
   */
  registeredFamilies(): string[] {
    return this.native.registeredFamilies();
  }

  /**
   * The TSV audit of every registered function's volatility
   * (`chs_function_flags`), VERBATIM: one function per line, six
   * tab-separated fields — `name`, `deterministic`, `deterministic_in_query`,
   * `server_constant`, `stateful`, `resolver_error_code`. ClickHouse's own
   * answers off this build's own registry, and the input to the statelessness
   * gate (`lib/tools/gen_function_flags.py`). Part of the three-question
   * introspection surface every SDK exposes (docs/reference/bindings.md
   * §Introspection).
   *
   * @returns the audit text, verbatim.
   * @throws {UnsupportedError} when the artifact predates
   *   `chs_function_flags`.
   */
  functionFlags(): string {
    return this.native.functionFlags();
  }

  /**
   * Join the DEFAULT evaluator's background threads. `chs_init` registers this
   * with `atexit`, so an ordinary process needs no call; it is REQUIRED before
   * an explicit unload, and useful in tests that must not depend on `atexit`.
   * Idempotent.
   *
   * @throws {ChtypesError} when called from inside another chtypes call on
   *   this library (the same reentrancy guard as `setDefaultSettings`).
   */
  shutdown(): void {
    this.native.shutdown();
  }
}
