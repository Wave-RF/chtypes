# Rust API reference

```toml
[dependencies]
chtypes = "1"
```

Unix only: the loader is `dlopen`. Minimum supported Rust version 1.87. `cargo doc --no-deps --open` is the full reference: every public item is documented (`#![deny(missing_docs)]`). The language-neutral contract, with the C entry point under every operation, is [`bindings-v1.md`](bindings-v1.md); this page is the Rust spelling of it.

## How to read this

**The verdict is in the `Ok` value.** A row a server would reject is `Ok` with `Outcome::Rejected`. The `Err` arm is for the machinery (loading, marshaling) and for the library's own refusals.

**Four call classes, peers of each other.** `Error::Schema` (ClickHouse itself would refuse), `Error::Unsupported` (this build declines; a server might accept), `Error::Usage` (misuse) and `Error::Internal` (a library bug) each carry a `CallError`: `status`, `ch_code`, `ch_name`, `message` and `column`, verbatim from the library. A decline is neither an acceptance nor a rejection: fall back to the server. `Error::call_error()` reads the five fields from any of the four.

**One family with the artifact errors.** `Error::ArtifactIncompatible` and `Error::ArtifactCorrupt` carry a `Refusal` (`reason`, `path`, `want`, `got`); the fetch layer's codes are one variant each (`ArtifactMissing`, `ArtifactUntrusted`, `ArtifactPinned`, `ArtifactUnpublished`, `SourceUnreachable`, `SourceUnauthorized`, `SourceForbidden`, `SourceIncompatible`), and `Error::code()` names the `CHTYPES_*` code.

**Bytes are bytes.** A column name, a statement, a message and a rendered value come back as `RawText`: `as_bytes()` is authoritative, `as_str()` is `None` for bytes that are not UTF-8, and `Display` is the one lossy form. Inputs are `impl AsRef<[u8]>`, so `&str`, `&[u8]` and `Vec<u8>` all work. An export is a `Vec<u8>`.

## The operations

| Operation                       | Rust                                                                                                                                                    |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| set the image zone and defaults | `chtypes::setup(SetupOptions { timezone, defaults })`, once per process, before the first open                                                          |
| construct a registry            | `Registry::new(RegistryOptions { fetch, autofetch, preload })`; it opens nothing                                                                        |
| open a version                  | `registry.for_version("26.8") -> Result<Arc<Library>>`                                                                                                  |
| what is installed, what is open | `registry.installed()`, `registry.libraries()`                                                                                                          |
| open a local build              | `Library::open_unverified(path, allow)`; also needs `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`                                                                |
| what the library is             | `lib.build_info()`, `lib.version()`, `lib.minor()`, `lib.path()`, `lib.resolved()`                                                                      |
| helpers                         | `validate_type`, `quote_identifier`, `quote_identifier_if_needed`, `quote_literal`, `error_codes`, `discover_query`, `discover_columns`, `live_handles` |
| compile a table                 | `lib.compile_table(create_table, &CompileOptions)`: exactly one `CREATE TABLE` statement                                                                |
| use a schema                    | `schema.describe()`, `row`, `rows`, `compile_filter`, `parse_block`                                                                                     |
| use a filter                    | `filter.rows(format, body, &EvalOptions)`, `filter.eval(&block)`                                                                                        |

## The fetch options

`RegistryOptions::fetch` is a `FetchOptions` carrying everything [`fetch-v1.md`](../guides/fetch-v1.md) configures, each field defaulting to the fetch layer's own default (the environment, then the built-in value): `platform`, `bases`, `cache_dir`, `system_dirs`, `offline`, `frozen`, `lock_path`, `lock_write`, `update`, `allow_unsigned`, `trusted_keys` and `token`.

`trusted_keys: Option<Vec<String>>` is the trust list: raw 32-byte ed25519 public keys, each as 64 hex digits. A non-empty list REPLACES the default trust (the release key); `None` reads `CHTYPES_TRUSTED_KEYS` (comma-separated), and the release key is used when neither names a key. A list never appends to the default, so trusting the SDK's fixture key means naming it (and the release key too, if both should verify). A key that is not 64 hex digits is `Error::Usage`.

`chtypes::cache_root(&FetchOptions) -> Result<PathBuf>` and `chtypes::search_dirs(&FetchOptions) -> Result<Vec<PathBuf>>` return the resolved cache root and the ordered directories searched (the root, then each system directory). They are the fetch layer's own resolution, read-only, and create nothing (public issue #530).

## The `chtypes` command

The crate ships a `chtypes` binary over the fetch layer (`cargo install chtypes`). Progress and warnings go to stderr and results to stdout. Exit statuses come from the fetch layer's error table ([`fetch-v1.md`](../guides/fetch-v1.md) section 8); a usage error exits 2.

| command                                                                                   | what it does                                                                                                                                            |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chtypes fetch <spelling>... [--platform K] [--lock F] [--frozen] [--offline] [--update]` | resolve, verify and install each version spelling; prints each installed directory                                                                      |
| `chtypes fetch --all [...]`                                                               | the same for every line (two-part tag) the registry publishes                                                                                           |
| `chtypes verify`                                                                          | re-hash every installed library against its verified record                                                                                             |
| `chtypes list [--offline]`                                                                | the installed builds, then, unless `--offline`, the version spellings the registry publishes (`tags/list`, filtered to two-, three- and four-part tags) |
| `chtypes where [--all]`                                                                   | the v1 cache root; `--all` prints every directory searched, the root first, one per line                                                                |

`--lock F` writes the lock after a fetch, `--frozen` fetches exactly what the lock pins (default file `chtypes.lock`) with no discovery, `--offline` reads the cache only, and `--update` re-resolves and rewrites the lock. The environment variables are `CHTYPES_ARTIFACTS_URL`, `CHTYPES_CACHE`, `CHTYPES_DOWNLOAD_TOKEN`, `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED`.

`chtypes --help` and `-h` print the usage to stdout and exit 0 wherever they appear; a usage error prints to stderr and exits 2. `chtypes --version` prints `chtypes <version>` and a newline (`0.0.0-dev` for an untagged build). `list` prints one flat line per entry, with no header: first `installed <version> <platform> <dir>` for each installed build, then, unless `--offline`, `published <spelling> support unknown` for each tag in the registry's `tags/list`. `--platform` belongs to `fetch` alone; on `verify`, `list` or `where` it is a usage error. `scripts/check-cli-parity.sh` holds all four bindings' commands to these rules.

## Objects and threads

`Schema`, `Filter` and `Block` are `Clone + Send + Sync + 'static`: a clone shares one handle, and the handle is freed when the last clone drops. `&self` methods run concurrently with no lock. A filter and a block hold a counted reference to their schema inside the library, so dropping the schema first is fine and the filter keeps working. `Registry` and `Library` (shared as `Arc<Library>`) are `Send + Sync`. A library is never unloaded.

## Options

Every option struct has public fields and `Default`, and is built as `RowOptions { session_timezone: Some(z), ..Default::default() }`. Settings are `Vec<(String, String)>`: values are strings, and only strings, and are never rewritten. The per-call zone is the `session_timezone` key of the settings; passing the option and a settings key of the same name is `Error::Usage`.

A filter's zone is its own, fixed when it is compiled (`FilterOptions::session_timezone`); an evaluation's settings (`EvalOptions`, or a block's `RowOptions`) are the body's parse settings only.

## Generated defaults

A column whose DEFAULT calls a random or UUID generator the build admits reports `Value::source == source::DEFAULT_GENERATED`. It is stored only if the caller inserts the library's own output: ask `rows` for an export (`RowsOptions::export`) in the format you will insert, and insert `BatchResult::payload`. Never insert the original body.
