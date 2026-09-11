# TypeScript API reference

```ts
import { Format, Registry } from '@wavehouse/chtypes';
```

`@wavehouse/chtypes` — Node ≥ 22, **ESM only**. Native calls go through `ffi-rs`, prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is no build step.

## How to read this

A bad **row** is never an exception: the verdict is `RowResult#outcome`, one of `accepted`, `rejected`, `accepted_poisoned`, `unsupported`. Exceptions are for schema-level answers — `compileDdl`, `setEngine`, `setTtl`, `validateType` — and there the class _is_ the verdict:

- `SchemaError` — the server refused, and `code` is a real ClickHouse code.
- `UnsupportedError` — chtypes declines to guess. Fall back to the server.

The two are **peers**. A decline never satisfies `instanceof SchemaError`.

## Registry and loading

| Symbol                                                  | C function                                                                     | Params                                                                               | Returns                                                       | Errors                                                                                                                                                                                                           |
| ------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `new Registry(dir?, options?)`                          | per artifact: dlopen, `chs_clickhouse_version`, `chs_abi_revision`, `chs_init` | the head of the search path; `{ timezone='UTC', verifyChecksums, autofetch, fetch }` | `Registry` (`.dir`, `.searchPath`, `.platform`)               | `RegistryError` (a named directory that does not exist, no artifacts anywhere, unreadable directory, checksum or manifest mismatch, load failure); `ChtypesError` (ABI-revision mismatch, `chs_init` failure)    |
| `Registry#for(version)`                                 | lookup; loads a line from a later search-path directory on demand              | minor line or exact patch                                                            | `Library`                                                     | `ArtifactMissingError` (a `RegistryError` with `code`, naming every directory looked in); `RegistryError` (a directory holds the line but it will not load). Never a nearest-version fallback, **never a fetch** |
| `Registry#open(version)`                                | `for()` behind a promise; with `autofetch`, `ensure` first                     | minor line or exact patch                                                            | `Promise<Library>`                                            | `ArtifactMissingError` (autofetch off); the `FetchError` verdicts; `RegistryError`                                                                                                                               |
| `Registry#versions()` / `#libraries()` / `#has(v)`      | derived                                                                        | —                                                                                    | minor lines oldest first / every loaded `Library` / `boolean` | never throw                                                                                                                                                                                                      |
| `Registry#close()` / `Symbol.dispose`                   | `chs_shutdown` per library                                                     | —                                                                                    | —                                                             | see `Library#shutdown`                                                                                                                                                                                           |
| `Library#version` / `#minor` / `#path` / `#abiRevision` | at load                                                                        | —                                                                                    | identity fields                                               | —                                                                                                                                                                                                                |

`for()` is synchronous and never fetches; `open()` is the async twin that can. That split is the flag in the other three bindings.

## Library

| Symbol                                              | C function                                                   | Returns                                           | Errors                                                                                                                                                             |
| --------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Library#compileDdl(ddl, { settings, mode }?)`      | `chs_schema_compile` + the six `chs_schema_column_*` getters | `Schema`                                          | `SchemaError` (bad DDL, 115 unknown setting name, 455/44 declared gate, 691 Enum-DEFAULT domain); `UnsupportedError` (mode ≠ 0, refused DEFAULT, admission budget) |
| `Library#validateType(expr)`                        | `chs_validate_type`                                          | the canonical spelling — use it verbatim          | `SchemaError` (e.g. 50); `UnsupportedError`                                                                                                                        |
| `Library#referenceType(expr)`                       | `chs_reference_type`                                         | the widened type, `''` if none                    | `UnsupportedError`                                                                                                                                                 |
| `Library#registeredFamilies()` / `#functionFlags()` | the introspection pair                                       | family names / the volatility TSV audit, verbatim | `UnsupportedError`                                                                                                                                                 |
| `Library#hasCompileSettings()`                      | probe                                                        | `boolean`                                         | passes through unexpected errors                                                                                                                                   |
| `Library#setDefaultSettings(settings)`              | `chs_set_default_settings`                                   | —                                                 | `ChtypesError` (refused **wholesale** with 115 + hint; reentrancy; a JS `number` value); `UnsupportedError`                                                        |
| `Library#shutdown()`                                | `chs_shutdown`                                               | —                                                 | `ChtypesError` (reentrancy); a missing symbol is ignored                                                                                                           |

## Schema

| Symbol                                                      | C function                     | Params                                                                                            | Returns                                          | Errors                                                                                        |
| ----------------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `Schema#columns`                                            | the column getters, at compile | —                                                                                                 | `ColumnInfo[]`, canonicalized, declaration order | —                                                                                             |
| `Schema#setEngine(engine, orderBy, { mergeTreeSettings }?)` | `chs_schema_engine`            | engine, sorting key, MergeTree-namespace `SETTINGS`                                               | —                                                | the sign of rc decides: `SchemaError` (rc > 0, today 115); `UnsupportedError` (rc < 0)        |
| `Schema#setTtl(ttl)`                                        | `chs_schema_ttl`               | a rows-TTL expression                                                                             | —                                                | `UnsupportedError` on any nonzero rc                                                          |
| `Schema#row(format, raw, settings?)`                        | `chs_row`                      | format code, row **bytes**                                                                        | `RowResult`                                      | `ChtypesError` (closed schema, `number` setting)                                              |
| `Schema#rows(format, body, settings?, options?)`            | `chs_rows`                     | format code, body **bytes**, per-call settings, and `RowsOptions`                                 | `BatchResult`                                    | `ChtypesError` (closed schema, `number` setting)                                              |
| `Schema#compileFilter(expr, { params }?)`                   | `chs_filter_compile`           | one boolean expression; `params` binds `{name:Type}` — values are **strings**, never hand-escaped | `Filter`                                         | `SchemaError` (47, **456**, **457**); `UnsupportedError` (clock reads)                        |
| `Schema#parseBlock(format, body, settings?)`                | `chs_block_parse`              | parse a body ONCE                                                                                 | `Block`                                          | `SchemaError` (115, framing, decode fault — no block, no partial answers); `UnsupportedError` |
| `Schema#close()` / `Symbol.dispose`                         | `chs_schema_free`              | —                                                                                                 | —                                                | never throws; idempotent                                                                      |

`RowsOptions` is the export channel — there is no separate `rowsExport` method:

```ts
schema.rows(Format.JSONEachRow, body, undefined, {
  exportFormat: Format.JSONCompactEachRow,  // the one format this revision serializes
  docFlags: DOC_VALUES | DOC_TRANSFORMS,    // default: DOC_ALL with no export, 0 (lean) with one
});
```

## Filter and Block

| Symbol                                                | C function                           | Returns                                                                               | Errors                                              |
| ----------------------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------- | --------------------------------------------------- |
| `Filter#rows(format, body, settings?)`                | `chs_filter_rows`                    | `FilterResult` — per-row verdicts                                                     | `ChtypesError` (closed filter, `number` setting)    |
| `Filter#eval(block)`                                  | `chs_filter_eval`                    | the same `FilterResult` `rows` answers; a cross-schema pair answers `'rejected'`/1002 | `ChtypesError` (closed handles, cross-library pair) |
| `Filter#close()` / `Block#close()` / `Symbol.dispose` | `chs_filter_free` / `chs_block_free` | —                                                                                     | never throw; idempotent                             |
| `isAnswer(v)`                                         | —                                    | `true` for `'true'` and `'false'` only — **fail closed on the other two**             | never throws                                        |

`Schema#close` frees open filters and blocks first, the C-required order, and `using` nests naturally: block, filter, schema.

## Fetching artifacts

| Symbol                                                                                                                                                     | Params                               | Returns                                                                            | Errors                                                                                                                                                                                          |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ensure(line, opts?)` / `ensureAll(opts?)`                                                                                                                 | line or exact patch; `EnsureOptions` | `Promise<EnsureResult>` (`.dir`, `.installed`, `.version`, `.signed`, `.keyId`, …) | `ArtifactUntrustedError`, `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`, `SourceUnreachableError`; `ChtypesError` (bad spelling, platform key, conflicting options) |
| `verifyInstalled(dest?, platform?)` / `listArtifacts(opts?)`                                                                                               | registry dir / `EnsureOptions`       | `Promise<InstalledArtifact[]>` / `Promise<ListResult>`                             | `verifyInstalled` never throws; `listArtifacts` throws the `FetchError` verdicts unless `offline`                                                                                               |
| `registrySearchPath(explicit?, platform?)` / `fetchDestination(…)` / `hostPlatform()` / `systemRegistryDirs()` / `cacheRegistryDir()` / `isPlatformKey(s)` | path and environment probing         | the search path, where a fetch writes, the platform key                            | never throw                                                                                                                                                                                     |
| `resolveRegistryDir(explicit?)` / `defaultRegistryDir()` / `looksLikeRegistry(dir)`                                                                        | derived                              | resolved dir / `string` / `boolean`                                                | never throw                                                                                                                                                                                     |
| `readLock`, `LOCK_SCHEMA`, `selectArtifact`, `selectAll`, `compareVersions`, `parseVersionSpelling`, `resolvePlatform`                                     | the pieces of the chain              | —                                                                                  | —                                                                                                                                                                                               |
| `RELEASE_PUBLIC_KEYS`, `keyId`, `verifyEd25519`, `parseSignatureFile`, `sha256File`, `trustedKeys`                                                         | the signature layer                  | —                                                                                  | —                                                                                                                                                                                               |
| `DEFAULT_ARTIFACTS_URL`, `DEFAULT_RELEASE_TAG`, `FETCH_COMMAND`, `artifactMissingMessage`                                                                  | the spellings the contract fixes     | —                                                                                  | —                                                                                                                                                                                               |
| `extractTarGz`                                                                                                                                             | a tarball                            | `ExtractedEntry[]`                                                                 | —                                                                                                                                                                                               |

`EnsureOptions`: `dest`, `platform`, `tag`, `url`, `lock`, `frozen`, `force`, `offline`, `trustedKeys`, `allowUnsigned`, `onProgress`, `signal`. Concurrent `ensure`s of one line in one process share a single fetch.

The command is `bin: chtypes`:

```sh
npx @wavehouse/chtypes fetch 25.8    # also verify, list, where
```

## Discovery

| Symbol                                                                     | Returns                                       | Errors                            |
| -------------------------------------------------------------------------- | --------------------------------------------- | --------------------------------- |
| `QUERY_SERVER_VERSION`, `QUERY_CHANGED_SETTINGS`, `QUERY_TABLE_COLUMNS`    | the three SQL constants                       | —                                 |
| `parseVersionResult` / `parseChangedSettingsResult` / `parseColumnsResult` | version / settings map / `DiscoveredColumn[]` | `ChtypesError` on malformed input |
| `reconstructDdl(cols)`                                                     | a column-declaration string                   | `ChtypesError`                    |

`DiscoveredColumn` requires all five fields — `name`, `type`, `defaultKind`, `defaultExpression`, `position` — so feed `reconstructDdl` from `parseColumnsResult` rather than hand-building rows.

The parsers read the `JSONEachRow` bytes through this package's own byte-exact reader: `position` survives quoted and bare spellings without a float round-trip.

## Types and constants

**Results** — `RowResult`, `BatchResult`, `FilterResult`, `FilterRowError`, `Value`, `Transform`, `Substitution`, `Computed`, `Span`, `ColumnDoc`, `ColumnInfo`, `Outcome`, `Verdict`, `FilterOutcome`.

`Value` carries both `text` and the raw `bytes`, plus `isNull` and `source`. `exportDeclined` is `undefined` rather than `''` when nothing was declined.

**Options** — `EngineOptions`, `CompileOptions`, `CompileFilterOptions`, `RowsOptions`, `RegistryOptions`, `Settings`, `SettingValue`.

**Constants** — `Format` (the `chs_format` integers 0–9, frozen), `CompileMode.Declared` (= 0), `CODE_UNSUPPORTED` (`-2`, the wire sentinel, carried by results and never by an error object), `ABI_REVISION`, `DOC_VALUES` / `DOC_TRANSFORMS` / `DOC_DEFAULTS` / `DOC_ALL`, `EXPORT_NONE`.

**Helpers** — `formatName(f)`, `minorOf(v)`, `compareMinor(a, b)`, `encodeSettings(s?)`, `isLossyReason(reason)`, `Reason`, `nativeStats()`.

**The byte-exact JSON kit** — `parseDocument`, `parseJsonValue`, `repairBareDenormals`, `rawBytes`, `rawText`, `isValidUtf8`, `Json`, `JsonKind`.

**Errors** — `ChtypesError` (base), `RegistryError`, `ArtifactMissingError`, `FetchError` and its five verdict subclasses, `SchemaError`, `UnsupportedError`.

## Two Node facts worth knowing

**`using` is a syntax error on Node 22**, which is this package's own floor. Explicit resource management needs TypeScript to downlevel it for you, or plain JavaScript on Node ≥ 24. `close()` works everywhere and every disposable here has both, so portable examples use `close()`.

**A settings value may be a `string` or a `bigint`, never a `number`.** The runtime rejects a `number`, because TypeScript is not present at a JS consumer's call site and a 19-digit nanosecond epoch does not survive an IEEE double. See [settings](../guides/settings.md).

## Thread-safety

Every call is synchronous on the JS thread, so ordinary single-threaded Node needs no locking. `setDefaultSettings` and `shutdown` carry a reentrancy tripwire and refuse loudly if another chtypes call is on the stack.

**`worker_threads` is the boundary**: two JS threads share one dlopen'd image and one set of C globals, which no per-isolate counter can see. Seed default settings before starting workers, and do not share a `Schema` across them.

## Deeper

- [`bindings.md`](bindings.md) — the normative shape all four bindings implement.
- [`c-abi.md`](c-abi.md) — the `chs_*` contract, the error model and the result documents.
