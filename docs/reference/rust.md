# Rust API reference

```toml
[dependencies]
chtypes = "0.1"
```

Unix only — the loader is `dlopen`. `cargo doc --no-deps --open` is the full reference: every public item is documented (`#![deny(missing_docs)]`), including which `Error` variant each call can produce and what it means. This page is the map.

## How to read this

**The verdict is in the `Ok` value.** A row a server would reject is `Ok` with `Outcome::Rejected`. The `Err` arm is for the machinery — loading, marshaling, an unreadable document — and for schema-level answers.

`Error::Unsupported` and `Outcome::Unsupported` (code `-2`) are neither an acceptance nor a rejection. Fall back to the server.

Two accessors on `Error` that are easy to confuse: `Error::code()` returns `Option<i32>`, the ClickHouse error code of a rejection; `Error::artifact_code()` returns the shared `CHTYPES_ARTIFACT_*` / `CHTYPES_SOURCE_UNREACHABLE` string, and `None` for everything else.

## Registry and loading

| Item                                                                                                          | C function                                                             | Takes                                                                   | Returns                                                                                             | Errors                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `Registry::from_search_path()` / `from_search_path_with(RegistryOptions)`                                     | loads nothing until asked                                              | optional explicit dir, timezone, `autofetch` (+ `fetch: EnsureOptions`) | `Registry` — lazy, one line per open                                                                | —                                                                                                                                       |
| `Registry::new(dir)` / `from_env()` / `from_env_or(dir)` / `from_env_or_default()` / `with_timezone(dir, tz)` | per artifact: `chs_clickhouse_version`, `chs_abi_revision`, `chs_init` | a registry directory                                                    | `Registry` — **eager**, every artifact in that one directory                                        | `Registry`, `Load`, `NotAnArtifact`, `CorruptArtifact`, `VersionMismatch`, `Init`, `InitConflict`, `EmptyRegistry`; `NoRegistryEnv`     |
| `Registry::for_version(v)`                                                                                    | the per-artifact load above, on first open                             | minor line or exact patch                                               | `Arc<Library>`                                                                                      | eager: `NoSuchVersion` (names what IS loaded); lazy: `ArtifactMissing`, or the fetch's own error under autofetch. Never a nearest match |
| `Registry::versions()` / `libraries()` / `dir()` / `search_path()` / `autofetch()`                            | derived                                                                | —                                                                       | minor lines in numeric order / `Vec<Arc<Library>>` / path / the directories consulted / the setting | —                                                                                                                                       |
| `Registry::shutdown()`                                                                                        | `chs_shutdown` per library                                             | —                                                                       | —                                                                                                   | —                                                                                                                                       |
| `Library::load(path, tz)`                                                                                     | dlopen + the four mandatory symbols                                    | artifact path, timezone                                                 | `Library`                                                                                           | `Load`, `NotAnArtifact`, `Init`, `InitConflict`, `Nul`                                                                                  |
| `Library::version()` / `minor()` / `path()` / `abi_revision()`                                                | cached at load                                                         | —                                                                       | `"25.8.28.1-lts"` / `"25.8"` / path / revision (`0` = predates the probe)                           | —                                                                                                                                       |

`from_search_path` is the one that walks the search path; `new` stays the single-directory loader by design.

## Library

| Item                                                  | C function                              | Takes                                                                                  | Returns                                                    | Errors                                                                                          |
| ----------------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `Library::compile(ddl)`                               | builder                                 | a column-declaration list, not a `CREATE TABLE`                                        | `CompileRequest`                                           | —                                                                                               |
| `CompileRequest::settings(...)` / `.mode(...)`        | builder                                 | the declared profile (anything `IntoIterator<Item = (K, V)>`); `CompileMode::Declared` | `Self`                                                     | —                                                                                               |
| `CompileRequest::compile()`                           | `chs_schema_compile` + the column group | —                                                                                      | `Schema`                                                   | `Schema` (115 unknown setting, 455/44 declared gate, 691 Enum DEFAULT, …), `Unsupported`, `Nul` |
| `Library::validate_type(expr)`                        | `chs_validate_type`                     | a type expression                                                                      | the canonical spelling                                     | `Schema` (e.g. 50), `Unsupported`, `PredatesFeature`, `Nul`                                     |
| `Library::reference_type(expr)`                       | `chs_reference_type`                    | a type expression                                                                      | `Option<String>` — `None` means no wider type              | `PredatesFeature`, `Nul`                                                                        |
| `Library::registered_families()` / `function_flags()` | the introspection pair                  | —                                                                                      | every type family in this build / the TSV volatility audit | `PredatesFeature`                                                                               |
| `Library::has_compile_settings()`                     | symbol probe                            | —                                                                                      | `true` (a mandatory symbol)                                | —                                                                                               |
| `Library::set_default_settings(&[(k, v)])`            | `chs_set_default_settings`              | the process-wide seed; admission budgets live here                                     | `()`                                                       | `Schema` (115, **wholesale** — nothing committed), `PredatesFeature`, `Nul`                     |
| `Library::shutdown()`                                 | `chs_shutdown`                          | —                                                                                      | idempotent; required before any `dlclose`                  | —                                                                                               |

## Schema

| Item                                                                    | C function                     | Takes                                                                                                                                        | Returns                                                                                                                                | Errors                                                                                 |
| ----------------------------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `Schema::columns()`                                                     | read at compile                | —                                                                                                                                            | `&[Column]`, canonicalized. The type field is `ty`, and `default_kind` is a `DefaultKind` (with a `None` **variant**, not an `Option`) | —                                                                                      |
| `Schema::library()`                                                     | derived                        | —                                                                                                                                            | `&Arc<Library>`                                                                                                                        | —                                                                                      |
| `Schema::set_engine(engine, order_by, mt_settings)`                     | `chs_schema_engine`            | the `SHOW CREATE` spelling, a sorting key, MergeTree-namespace settings                                                                      | `()`                                                                                                                                   | sign rule: `Schema` (rc > 0), `Unsupported` (rc < 0), `PredatesFeature`, `Nul`         |
| `Schema::set_ttl(sql)`                                                  | `chs_schema_ttl`               | a table-level rows TTL                                                                                                                       | `()`                                                                                                                                   | `Unsupported` on any nonzero, `PredatesFeature`, `Nul`                                 |
| `Schema::row(format, raw)` / `row_with_settings(format, raw, settings)` | `chs_row`                      | format code, counted bytes                                                                                                                   | `RowResult`                                                                                                                            | `PredatesFeature`, `BadDocument`, `Nul`                                                |
| `Schema::rows(format, body, settings)`                                  | `chs_rows`                     | one request body = one clock instant                                                                                                         | `BatchResult`                                                                                                                          | `PredatesFeature`, `BadDocument`, `Nul`                                                |
| `Schema::rows_export(format, body, settings, export, doc_flags)`        | `chs_rows` — the same ONE call | `export: Option<Format>`, `doc_flags: DocFlags`                                                                                              | `BatchResult` with `payload` / `spans` / `export_declined`                                                                             | as `rows`                                                                              |
| `Schema::compile_filter(expr, params)`                                  | `chs_filter_compile`           | one boolean expression; `params` binds `{name:Type}` as a **slice of pairs** (`NO_PARAMS` for none) — values are strings, never hand-escaped | `Filter<'_>`, borrowing the schema                                                                                                     | `Schema` (47, **456**, **457**), `Unsupported` (clock reads), `PredatesFeature`, `Nul` |
| `Schema::parse_block(format, body, settings)`                           | `chs_block_parse`              | parse a body ONCE                                                                                                                            | `Block<'_>`, borrowing the schema                                                                                                      | `Schema` (115, framing, decode fault), `Unsupported`, `PredatesFeature`, `Nul`         |

> **Settings shapes differ between the builder and the call.** `CompileRequest::settings` takes anything iterable, so `[("k", "v")]` is fine. `rows`, `row_with_settings` and `Filter::rows` take `&[(K, V)]` — a **slice reference**, so write `&[("k", "v")]` or `NO_SETTINGS`.

## Filter and Block

| Item                                   | C function                           | Returns                                                                                            | Errors                                           |
| -------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `Filter::rows(format, body, settings)` | `chs_filter_rows`                    | `FilterResult` — per-row `Verdict`s                                                                | `PredatesFeature`, `BadDocument`, `Nul`          |
| `Filter::eval(&block)`                 | `chs_filter_eval`                    | the same `FilterResult` `rows` answers; a cross-schema pair answers `FilterOutcome::Rejected`/1002 | `CrossLibrary`, `BadDocument`, `PredatesFeature` |
| `Verdict::answered()` / `as_char()`    | —                                    | `true` for `True` and `False` only — **fail closed on the other two**                              | —                                                |
| `Filter` / `Block` drop                | `chs_filter_free` / `chs_block_free` | —                                                                                                  | —                                                |

A `Filter` and a `Block` **borrow** their `Schema`, so dropping the schema first does not compile. The lifetime rule the other three bindings enforce at runtime is a compile-time fact here.

## Fetching artifacts — feature `fetch`, on by default

`default-features = false` drops the binary, `ensure`, autofetch and every dependency they bring (`ed25519-dalek`, `sha2`, `ureq`, `base64`, `tar`, `flate2`), leaving the loader alone.

| Item                                                                                                                                                                                            | Takes                                                          | Returns                                                                                                   | Errors                                                                                                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `ensure(line, &EnsureOptions)`                                                                                                                                                                  | line or exact patch                                            | `Installed { dir, line, version, library, library_sha256, platform, action, asset }`                      | `ArtifactUntrusted`, `ArtifactCorrupt`, `ArtifactPinned`, `ArtifactUnpublished`, `SourceUnreachable`, `Fetch` |
| `fetch::ensure_all` / `verify_installed(dir)` / `release_info(&opts)` / `install_dir(&opts)` / `search_path(&opts)` / `parse_line`                                                              | as `--all` / `verify` / `list` / `where`                       | `Vec<Installed>` / `Vec<Verification>` / `ReleaseInfo` / paths / a minor line                             | as `ensure`                                                                                                   |
| `fetch::TrustPolicy`, `LockFile`, `IndexRow`, `verify_signature`, `sha256_file`, `RELEASE_PUBLIC_KEY[_HEX]`, `RELEASE_KEY_ID`                                                                   | the pieces of the chain, for a host that wants them separately | —                                                                                                         | `Fetch` (a malformed key list)                                                                                |
| `registry_search_path(explicit)` / `search_path_for(platform, explicit)` / `install_dir[_for]` / `locate[_in]` / `installed_lines` / `host_platform` / `cache_dir_for` / `default_registry_dir` | —                                                              | the search path, where a fetch writes, `<dir>/<minor>` of the first hit, what is installed, `<os>-<arch>` | —                                                                                                             |
| `Error::artifact_code()`                                                                                                                                                                        | —                                                              | the shared code string, `None` otherwise                                                                  | —                                                                                                             |

`EnsureOptions` carries the flags (`dest`, `platform`, `url` / `tag`, `lock`, `frozen`, `force`, `offline`, `progress`) and the trust policy (`trusted_keys`, `allow_unsigned`; `None` reads the environment). `Action` is `AlreadyInstalled`, `Installed` or `Replaced`.

With an explicit `--dest`, "already installed" means installed _in that directory_: a container build's `--dest /opt/chtypes/artifacts` is never satisfied by the builder's own cache.

```sh
cargo install chtypes && chtypes fetch 25.8    # also verify, list, where
```

## Discovery

| Item                                                                              | Takes               | Returns                                                      | Errors      |
| --------------------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------ | ----------- |
| `QUERY_SERVER_VERSION`, `QUERY_CHANGED_SETTINGS`, `QUERY_TABLE_COLUMNS`           | —                   | the three SQL constants                                      | —           |
| `parse_version_result` / `parse_changed_settings_result` / `parse_columns_result` | `JSONEachRow` bytes | `String` / `Vec<(String, String)>` / `Vec<DiscoveredColumn>` | `Discovery` |
| `reconstruct_ddl(&cols)`                                                          | discovered columns  | a column-declaration list for `compile`                      | `Discovery` |
| `ServerProfile { version, settings }`, `DiscoveredColumn`                         | —                   | the result types                                             | —           |

`parse_changed_settings_result` hands back a `Vec<(String, String)>` rather than a map, which is the shape the settings channels take anyway. The parsers keep integer fields exact through both quoted and bare spellings, never through a float.

## Types and constants

**Results** — `RowResult`, `BatchResult` (`engine_rows_bytes()` is the byte-exact stored truth), `Value`, `Transform` (+ `lossy()`), `Substitution`, `Computed`, `Span`, `Outcome`, `Verdict`, `FilterOutcome`, `FilterResult`, `FilterRowError`, `DocFlags`.

`RawText` is the byte-exact stored rendering: `as_str()` is fallible and `to_lossy()` is explicit, because a stored value is bytes before it is a string.

**`DocFlags`** — `NONE`, `VALUES`, `TRANSFORMS`, `DEFAULTS`, `ALL`, plus `bits()`.

**Constants** — `Format` (codes 0–9), `CompileMode::Declared` = 0, `CODE_UNSUPPORTED` = `-2`, `ABI_REVISION`, `DEFAULT_TIMEZONE`, `NO_SETTINGS`, `NO_PARAMS`, `REGISTRY_ENV`, `AUTOFETCH_ENV`, `SYSTEM_ARTIFACT_ROOTS`, `CODE_ARTIFACT_*` / `CODE_SOURCE_UNREACHABLE`, `FETCH_COMMAND`, the `SETTING_*` keys, and `reason::*` (the 24 stable spellings).

**`DefaultKind`** — `None`, `Default`, `Materialized`, `Alias`, `Ephemeral`, and `Other(String)`: the ABI grows additively, so an unknown spelling is passed through rather than rejected.

## Thread-safety

One mutex per loaded image serializes every call into a `Library`, including `set_default_settings`, which the ABI requires be excluded against everything else on that image. `Library` and `Registry` are `Send + Sync` — share them freely.

`Schema` is `Send` and deliberately **not** `Sync`: one native handle must not be used from two threads at once, and the type system enforces it. Parallelism comes from more schemas, not shared ones.

This crate never `dlclose`s — the handle is intentionally leaked to the process — so `chs_init`'s `atexit` registration covers an ordinary program and `shutdown()` is needed only when the host controls its own teardown order.

## Deeper

- [`bindings.md`](bindings.md) — the normative shape all four bindings implement. Where this crate and the spec disagree, **the spec wins**.
- the core repository's C ABI specification — the `chs_*` contract, the error model and the result documents.
