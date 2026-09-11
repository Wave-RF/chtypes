# Go API reference

```go
import "github.com/wave-rf/chtypes/go/chtypes"
```

Module path `github.com/wave-rf/chtypes/go` — lowercase, the Go norm, frozen together with the function signatures at 1.0. `go doc github.com/wave-rf/chtypes/go/chtypes` is the same surface with the full prose; every exported symbol carries its contract.

## How to read the Errors column

|       |                                                                                                        |
| ----- | ------------------------------------------------------------------------------------------------------ |
| **S** | `*SchemaError` — the server refused. `Code` is a real ClickHouse error code.                           |
| **U** | `*UnsupportedError` — this build declines. Fall back to the server; never report it as a rejection.    |
| **E** | a plain `error` — usage or environment: a closed handle, a version mismatch, a bad artifact directory. |

Row and batch verdicts are **never** Go errors. They are `Outcome` in the result.

## Two builds, one package

The default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing, includes no header and needs no build tree. That is what a consumer's `go get` produces, and everything in this reference except the section marked _linked build_ is available there.

```sh
go build ./...                        # dlopen-only: what a consumer gets
go build -tags chtypes_linked ./...   # + the linked path (needs a core build tree via CGO_LDFLAGS)
```

The `chtypes_linked` tag adds a statically linked path — package-level `CompileDDL`, `ValidateType`, `ParseSchema`, `BuiltVersion`, `SetDefaultSettings` — which answers for exactly one artifact and is a development and rig instrument, not a consumer path. `undefined: chtypes.CompileDDL` means the tag is missing.

The frozen numbers the default build hardcodes (`Format`, `DocFlags`, `CodeUnsupported`, `ExportNone`, `ABIRevision`) are pinned to `chtypes.h` by compile-time assertions the tagged build sees, so drift is caught before a consumer could meet it.

## Registry and loading

| Symbol                                        | C function                                   | Params → returns                                                                                                                                  | Errors                                                                                    |
| --------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `NewRegistry(dir, opts...)`                   | dlopen + `manifest.json` scan                | artifact dir (loaded now) or `""` (the search path, loaded on demand) → `*Registry`; options `WithAutoFetch`, `WithFetchOptions`                  | E                                                                                         |
| `Registry.Load(path)`                         | dlopen, `chs_abi_revision`, `chs_init`       | one artifact → registered                                                                                                                         | E (ABI mismatch, missing core symbols)                                                    |
| `Registry.For(v)` / `ForContext(ctx, v)`      | —                                            | minor line or exact patch → `*Library`, from the loaded set, then the first search-path directory holding the line, then (with AutoFetch) a fetch | `ErrArtifactMissing` (use `errors.Is`); a fetch's `*ArtifactError`; never a nearest match |
| `Registry.Versions()`                         | —                                            | minor lines, numerically sorted — loaded plus discovered on the search path                                                                       | —                                                                                         |
| `Library.{Version, Minor, Path, ABIRevision}` | `chs_clickhouse_version`, `chs_abi_revision` | identity fields                                                                                                                                   | —                                                                                         |
| `Library.HasCompileSettings()`                | dlsym probe                                  | does this artifact export settings-aware compile?                                                                                                 | —                                                                                         |

## Schemas and rows

| Symbol                                                                  | C function                                   | Params → returns                                                                                                                                          | Errors                                                                                                         |
| ----------------------------------------------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `Library.CompileDDL(ddl, opts...)`                                      | `chs_schema_compile` + `chs_schema_column_*` | a column list, not a `CREATE TABLE` (+ `WithCompileSettings`, `WithCompileMode`) → `*LoadedSchema`                                                        | S (incl. 115 unknown setting, 455/44 declared gates), U (admission budget, mode ≠ 0), E                        |
| `Library.ValidateType(expr)`                                            | `chs_validate_type`                          | type expression → canonical spelling                                                                                                                      | S, U, E                                                                                                        |
| `Library.{RegisteredFamilies, FunctionFlags, ReferenceType}`            | the introspection trio                       | family names / the volatility TSV audit, verbatim / the widened reference type (`""` if none)                                                             | U (artifact predates the symbol)                                                                               |
| `LoadedSchema.Columns`                                                  | read at compile                              | `[]Column`, canonicalized, declaration order                                                                                                              | —                                                                                                              |
| `LoadedSchema.SetEngine(e, orderBy, opts...)`                           | `chs_schema_engine`                          | engine + sorting key (+ `WithMergeTreeSettings`)                                                                                                          | S when rc > 0 (server refusal, e.g. 115), U when rc < 0 (unmodeled engine or key, non-default MergeTree value) |
| `LoadedSchema.SetTTL(ttl)`                                              | `chs_schema_ttl`                             | a table rows-TTL expression                                                                                                                               | U (every refusal), E                                                                                           |
| `LoadedSchema.Row(f, raw)` / `RowWithSettings(f, raw, settings)`        | `chs_row`                                    | format, raw bytes → `RowResult`                                                                                                                           | E only; the verdict is in `Outcome`                                                                            |
| `LoadedSchema.Rows(f, body, settings)`                                  | `chs_rows`                                   | format, body bytes, settings → `BatchResult`                                                                                                              | E only; verdict in `Outcome` / `EngineRows`                                                                    |
| `LoadedSchema.RowsExport(f, body, settings, exportFormat, docFlags...)` | `chs_rows` — the same ONE call               | adds `Payload` + `Spans` + `ExportDeclined`, and the document groups `DocValues` / `DocTransforms` / `DocDefaults` (none = lean). `ExportNone` = no bytes | E only; a bad export format or flag bit answers `Unsupported` in the result                                    |
| `LoadedSchema.Close()`                                                  | `chs_schema_free`                            | releases the handle — open filters and blocks are closed first (finalizer backup)                                                                         | —                                                                                                              |

`Row` is sugar for a call site that semantically expects one row. On a multi-row body it returns the first and ignores the rest, so it is the wrong call for anything wire-facing — see [batches](../guides/batches.md).

## Filters and blocks

| Symbol                                       | C function                           | Params → returns                                                                                                                                                | Errors                                                                                                |
| -------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `LoadedSchema.CompileFilter(expr, opts...)`  | `chs_filter_compile`                 | one boolean expression over the schema's physical columns (+ `WithFilterParams` binding `{name:Type}` — values are **strings**, never hand-escaped) → `*Filter` | S (47 unknown identifier, **456** unbound param, **457** unparseable param value), U (clock reads), E |
| `Filter.Rows(f, body, settings)`             | `chs_filter_rows`                    | per-row `Verdicts` in a `FilterResult`                                                                                                                          | E only; the call verdict is `FilterResult.Outcome`                                                    |
| `LoadedSchema.ParseBlock(f, body, settings)` | `chs_block_parse`                    | parse a body ONCE → `*Block`, evaluable by K filters with no re-parse                                                                                           | S (115, framing, decode fault), U, E                                                                  |
| `Filter.Eval(block)`                         | `chs_filter_eval`                    | the same `FilterResult` `Rows` answers; a cross-schema pair answers `FilterRejected`/1002                                                                       | E only (closed handles, cross-library pair)                                                           |
| `Filter.Close()` / `Block.Close()`           | `chs_filter_free` / `chs_block_free` | releases the handle                                                                                                                                             | —                                                                                                     |
| `Verdict.Answered()`                         | —                                    | `true` for `VerdictTrue` and `VerdictFalse` only — **fail closed on the other two**                                                                             | —                                                                                                     |

`FilterOutcome` is `FilterOK`, `FilterRejected` or `FilterUnsupported`; on anything but `FilterOK` the verdict list is not populated. `FilterOK` is the zero value and prints as `0`, so compare against the constant rather than formatting it.

## Fetching artifacts

| Symbol                                                                                                                                                                          | Params → returns                                                                                                                                                          | Errors              |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| `Ensure(ctx, line, opts)`                                                                                                                                                       | fetch/verify/install one line → `*Installed` (`.Dir`, `.AlreadyInstalled`, `.Version`, `.Library`, …)                                                                     | `*ArtifactError`    |
| `FetchAll(ctx, opts)`                                                                                                                                                           | every published line for the platform → `[]*Installed`                                                                                                                    | `*ArtifactError`    |
| `ListRelease(ctx, opts)` / `ListInstalled(dir)` / `VerifyInstalled(dir)`                                                                                                        | the verified `*ReleaseIndex` / `[]Installed` / each re-hashed, `[]VerifyResult`                                                                                           | `*ArtifactError`, E |
| `FetchOptions`                                                                                                                                                                  | `Dest`, `Platform`, `URL`, `Tag`, `LockFile`, `Frozen`, `Force`, `Offline`, `AllowUnsigned`, `TrustedKeys`, `Progress`, `HTTPClient`. The zero value is the default fetch | —                   |
| `ErrArtifactMissing`, `ErrArtifactUntrusted`, `ErrArtifactCorrupt`, `ErrArtifactPinned`, `ErrArtifactUnpublished`, `ErrSourceUnreachable`                                       | sentinels for `errors.Is`, one per shared code                                                                                                                            | —                   |
| `ArtifactError`, `ErrorCode`, `ExitCode(err)`                                                                                                                                   | the typed error, the shared code vocabulary, the CLI exit status                                                                                                          | —                   |
| `LockFile`, `LockEntry`, `ReadLockFile(path)`, `LockFile.Write(path)`, `LockKey(platform, minor)`, `NewLockFile()`                                                              | the pin file, schema 1                                                                                                                                                    | E                   |
| `ReleasePublicKeyHex`, `ReleaseKeyID`, `ReleasePublicKey()`, `KeyID(pub)`, `ParseTrustedKeys(spec)`, `VerifySignature(msg, sig, keys)`, `ParseSignatureFile(b)`                 | the signature layer                                                                                                                                                       | E                   |
| `RegistrySearchPath(explicit)`, `FetchRegistryDir(explicit)`, `DefaultRegistryDir()`, `DefaultRegistryDirFor(p)`, `SystemRegistryDirs(p)`, `HostPlatform()`, `ValidPlatform(p)` | the search path, where a fetch writes, the platform key                                                                                                                   | —                   |
| `GoFetchCommand`, `DefaultArtifactsURL`, `DefaultReleaseTag`, `DefaultLockFile`, `LockSchema`                                                                                   | the spellings the contract fixes                                                                                                                                          | —                   |

The command is `go/cmd/chtypes`, runnable without installing anything:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
```

The implementation is stdlib only — `crypto/ed25519`, `crypto/sha256`, `archive/tar`, `net/http`.

## Discovery

| Symbol                                                                              | Returns                                                                                      |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `QueryServerVersion`, `QueryChangedSettings`, `QueryTableColumns`                   | the three SQL constants; no C call, no connection                                            |
| `ParseVersionResult(b)` / `ParseChangedSettingsResult(b)` / `ParseColumnsResult(b)` | version / `map[string]string` (duplicates: last write wins) / `[]DiscoveredColumn`           |
| `ReconstructDDL(cols)`                                                              | discovered columns → a column-declaration list                                               |
| `QuoteIdentifier(name)`                                                             | ClickHouse DDL identifier spelling: bare where legal, else backticked with backticks doubled |
| `ServerProfile{Version, Settings}`, `DiscoveredColumn`                              | the result types                                                                             |

## Types and constants

**Results** — `RowResult`, `BatchResult` (+ `Payload` / `Spans` / `ExportDeclined` / `EngineRows`), `Value`, `Transform` (+ `Lossy()`), `Substitution`, `Computed`, `Outcome`, `Span`, `DocFlags`, `FilterResult`, `Verdict` (+ `Answered()`), `FilterOutcome`, and the `Reason*` constants. `stored` and `ref` are kept as raw JSON, so numbers stay exact.

**Outcomes** — `Accepted`, `Rejected`, `AcceptedPoisoned`, `Unsupported`, `Skipped`. `Skipped` is only ever seen on a row inside a `BatchResult`, never as a batch verdict and never from `Row`.

**Default kinds** — `KindNone`, `KindDefault`, `KindMaterialized`, `KindAlias`, `KindEphemeral`.

**Inputs** — `Schema`, `Column`, `DefaultKind`, `Format`, `Version`, `CompileMode` / `CompileDeclared`, and the option types. The `Format` and mode integers are the frozen ABI codes.

**Pinned to the header** — `ABIRevision`, `CodeUnsupported` (the wire sentinel `-2`, carried by results and never by an error value), `ExportNone`.

**Process state** — `Timezone` feeds `chs_init`; it is the server TZ used for a bare `DateTime`, defaults to `"UTC"`, and must be set before the first call.

## Linked build only

`CompileDDL(v, ddl, opts...)`, `ValidateType(v, expr)`, `ParseSchema(v, schema)`, `SetDefaultSettings(m)`, `RegisteredFamilies()`, `FunctionFlags()`, `ReferenceType(expr)`, `BuiltVersion()` and `*CompiledSchema` are package-level twins behind `-tags chtypes_linked`, answering for the one artifact linked at compile time.

`SetDefaultSettings` is **only** there. It replaces a process-global that the row path reads by reference, so the ABI requires it to exclude everything else on the image — and a dlopen'd `Library` deliberately does not carry the symbol in its function-pointer table. A Go consumer puts those settings in the compile profile and the per-call map instead; see [settings](../guides/settings.md).

## Deeper

- [`bindings.md`](bindings.md) — the normative shape all four bindings implement.
