# Python API reference

```python
import chtypes
```

Package `chtypes` — pure Python over stdlib `ctypes`, zero dependencies, no build step. Every public symbol is in `chtypes.__all__`, and the package ships `py.typed`.

## How to read this

Row-level verdicts are **returned, never raised**: a rejected row is a `RowResult` with `outcome == Outcome.REJECTED`, not an exception. Exceptions are for schema-level answers and for the machinery.

> **`UnsupportedError` is a peer of `SchemaError`, not a subclass.** `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired because catching one and silently getting the other is exactly the misclassification the three-outcome model exists to prevent.

## Registry and loading

| Symbol                                                                       | C function                                                                 | Returns                                                                                                      | Raises                                                                                                                                 |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `Registry(dir=None, *, timezone="UTC", verify_hashes=False, autofetch=None)` | per line at load: `chs_clickhouse_version`, `chs_abi_revision`, `chs_init` | `Registry` — walks the search path (`.search_path`; `.directory` is where a fetch writes), loads nothing yet | `RegistryError` (an explicit directory that exists but cannot be read)                                                                 |
| `Registry.versions()`                                                        | —                                                                          | `tuple[str, ...]` of minor lines on the search path, release order                                           | —                                                                                                                                      |
| `Registry.libraries()`                                                       | loads every line                                                           | `tuple[Library, ...]`                                                                                        | `RegistryError` (a line with a manifest that will not load)                                                                            |
| `Registry.for_version(v)` / `registry[v]`                                    | loads the line on first use                                                | `Library` — exact patch or minor line, no nearest fallback                                                   | `ArtifactMissingError`; `RegistryError` (broken artifact, hash mismatch, ABI-revision mismatch, failed init); `ChtypesError` if closed |
| `v in registry`, `iter(registry)`, `len(registry)`                           | —                                                                          | whether `for_version` would resolve without fetching / the libraries / the count                             | —                                                                                                                                      |
| `Registry.close()`                                                           | `chs_shutdown` per library, refcounted per image                           | `None`                                                                                                       | —                                                                                                                                      |
| `Library.version` / `.minor` / `.path` / `.manifest` / `.abi_revision`       | at load                                                                    | `str` / `str` / `str` / `Manifest` / `int`                                                                   | —                                                                                                                                      |

`verify_hashes=True` re-hashes each line against its manifest at load. Off by default: once files are in a registry directory the loader trusts the directory, exactly as a runtime trusts `node_modules`.

## Library

| Symbol                                                              | C function                                             | Returns                            | Raises                                                                                                                                               |
| ------------------------------------------------------------------- | ------------------------------------------------------ | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Library.compile_ddl(ddl, *, settings=None, mode=COMPILE_DECLARED)` | `chs_schema_compile` + the `chs_schema_column_*` group | `Schema`                           | `SchemaError` (bad DDL, 115 unknown setting name, 455/44 declared type gate); `UnsupportedError` (refused DEFAULT, admission budget, unknown `mode`) |
| `Library.validate_type(expr)`                                       | `chs_validate_type`                                    | canonical `str`                    | `SchemaError` (e.g. 50 unknown family); `UnsupportedError` (an unsafe family this build refuses to construct)                                        |
| `Library.reference_type(expr)`                                      | `chs_reference_type`                                   | the widened type, `""` if none     | `UnsupportedError` (artifact predates it)                                                                                                            |
| `Library.registered_families()`                                     | `chs_registered_families`                              | `list[str]`                        | `UnsupportedError`                                                                                                                                   |
| `Library.function_flags()`                                          | `chs_function_flags`                                   | the volatility TSV audit, verbatim | `UnsupportedError`                                                                                                                                   |
| `Library.has_compile_settings`                                      | dlsym probe                                            | `bool`                             | —                                                                                                                                                    |
| `Library.set_default_settings(settings)`                            | `chs_set_default_settings`                             | `None`                             | `ChtypesError` — refused **wholesale** with the server's own 115 plus a hint                                                                         |
| `Library.close()`                                                   | `chs_shutdown` on the LAST close of the image          | `None`                             | —                                                                                                                                                    |

## Schema

| Symbol                                                                  | C function                             | Returns                                                            | Raises                                                                                                                             |
| ----------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `Schema.columns`                                                        | `chs_schema_column_*`, read at compile | `tuple[Column, ...]`, canonicalized                                | —                                                                                                                                  |
| `Schema.set_engine(engine, order_by, *, merge_tree_settings=None)`      | `chs_schema_engine`                    | `None`                                                             | `SchemaError` (rc > 0, server refusal); `UnsupportedError` (rc < 0, unmodeled engine or key, non-default MergeTree setting)        |
| `Schema.set_ttl(ttl_sql)`                                               | `chs_schema_ttl`                       | `None`                                                             | `UnsupportedError` (any refused TTL form)                                                                                          |
| `Schema.row(fmt, raw, settings=None)`                                   | `chs_row`                              | `RowResult`                                                        | `ChtypesError` if closed; `TypeError` on non-bytes, or a `float` setting value                                                     |
| `Schema.rows(fmt, body, settings=None, *, export=None, doc_flags=None)` | `chs_rows`                             | `BatchResult`                                                      | same as `row`                                                                                                                      |
| `Schema.compile_filter(expr, *, params=None)`                           | `chs_filter_compile`                   | `Filter`                                                           | `SchemaError` (47, **456** unbound param, **457** unparseable value); `UnsupportedError` (clock reads, artifact predates the trio) |
| `Schema.parse_block(fmt, body, settings=None)`                          | `chs_block_parse`                      | `Block`                                                            | `SchemaError` (115, framing, decode fault); `UnsupportedError`                                                                     |
| `Schema.close()`, or the context manager                                | `chs_schema_free`                      | `None` — frees open filters and blocks FIRST, the C-required order | —                                                                                                                                  |

The export channel is keyword-only on `rows`: `export=Format.JSON_COMPACT_EACH_ROW` and `doc_flags=DOC_VALUES | DOC_TRANSFORMS`. There is no separate `rows_export` method.

`Schema`, `Filter` and `Block` are all context managers.

## Filter and Block

| Symbol                                  | C function                           | Returns                                                                             | Raises                                              |
| --------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------- | --------------------------------------------------- |
| `Filter.rows(fmt, body, settings=None)` | `chs_filter_rows`                    | `FilterResult` — per-row `Verdict`s                                                 | `ChtypesError` if closed                            |
| `Filter.eval(block)`                    | `chs_filter_eval`                    | the same `FilterResult` `rows` answers; a cross-schema pair answers `REJECTED`/1002 | `ChtypesError` (closed handles, cross-library pair) |
| `Filter.close()` / `Block.close()`      | `chs_filter_free` / `chs_block_free` | `None`                                                                              | —                                                   |

`Verdict` has no `answered()` helper — test membership: `v in (Verdict.TRUE, Verdict.FALSE)`. `ERROR` and `DECLINE` are **not answers**; fail closed on both.

## Fetching artifacts

| Symbol                                                                                                                                                                      | Returns                                                                     | Raises                                                                                                                                                                          |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ensure(line, *, dest=None, platform=None, url=None, tag=None, lock=None, frozen=False, force=False, offline=False, trusted_keys=None, allow_unsigned=None, progress=None)` | `Path` of `<registry>/<minor>`, installed and verified; idempotent          | `ArtifactUntrustedError`, `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`, `SourceUnreachableError` (each with `.code`); `ValueError` on a bad option |
| `fetch_lines(lines, *, all_lines=False, …)`                                                                                                                                 | `list[Path]`, the release read once                                         | same                                                                                                                                                                            |
| `registry_search_path(explicit=None)`, `fetch_destination(explicit=None)`, `host_platform()`, `default_registry_dir()`                                                      | the search path / where a fetch writes / `<os>-<arch>` / the per-user cache | —                                                                                                                                                                               |
| `ArtifactError` and its subclasses, `CODE_ARTIFACT_*`, `CODE_SOURCE_UNREACHABLE`, `UnsignedArtifactWarning`                                                                 | `RegistryError` subclasses carrying `.code`                                 | —                                                                                                                                                                               |
| `RELEASE_PUBLIC_KEY`, `RELEASE_KEY_ID`, `ENV_AUTOFETCH`, `ENV_REGISTRY`                                                                                                     | the embedded release key and its id; the variable names                     | —                                                                                                                                                                               |
| `Manifest`, `read_manifest`, `verify_library`, `minor_of`                                                                                                                   | loader helpers                                                              | `RegistryError` from `verify_library`                                                                                                                                           |

The command:

```sh
python -m chtypes fetch 25.8     # or the `chtypes` console script; also verify, list, where
```

The ed25519 verifier is pure stdlib, like the rest of the package.

## Discovery

| Symbol                                                                          | Returns                                             | Raises                                          |
| ------------------------------------------------------------------------------- | --------------------------------------------------- | ----------------------------------------------- |
| `QUERY_SERVER_VERSION`, `QUERY_CHANGED_SETTINGS`, `QUERY_TABLE_COLUMNS`         | the three SQL constants                             | —                                               |
| `parse_version_result`, `parse_changed_settings_result`, `parse_columns_result` | `str` / `dict[str, str]` / `list[DiscoveredColumn]` | `ValueError` on malformed caller-supplied bytes |
| `reconstruct_ddl(cols)`                                                         | a column-declaration `str`                          | `ValueError`                                    |
| `ServerProfile(version, settings)`, `DiscoveredColumn`                          | frozen dataclasses                                  | —                                               |

## Types and constants

**Result documents**, all frozen dataclasses: `Column`, `Value`, `Transform`, `Substitution`, `Computed`, `Span`, `RowResult`, `BatchResult`, `FilterResult`, `FilterRowError`.

`RowResult.value(column)` is a convenience accessor other bindings do not have — `row.value("x").text` rather than indexing `values`.

**`substituted` lives on `RowResult`, not on `BatchResult`.** Reach it through `batch.rows[i].substituted`. `BatchResult` does carry a batch-level `transformed`, which folds in the storage layer's verdicts.

**Enums** — `Format`, `Outcome` (a `StrEnum`, so it prints as `accepted` rather than `Outcome.ACCEPTED`), `DefaultKind`, `Verdict`, `FilterOutcome`.

**Vocabulary** — `Reason`, `LOSSLESS_REASONS` (the four lossless spellings, as a frozenset), `COMPILE_DECLARED`.

**Document flags** — `DOC_VALUES`, `DOC_TRANSFORMS`, `DOC_DEFAULTS`, `DOC_ALL`, `EXPORT_NONE`.

**Errors** — `ChtypesError` (base), `RegistryError`, `SchemaError`, `UnsupportedError` (a **peer**), `CODE_UNSUPPORTED` (`-2`).

**Plumbing** — `RawNumber` (exact-text numerics), `quote_bare_denormals`, `ABI_REVISION`.

## Settings values must not be floats

`encode_settings` stringifies an `int` exactly — `str(int)`, never through a float — and **raises `TypeError` on a `float`**. A 19-digit `chtypes_now_epoch_nanos` does not survive an IEEE double, and as a JSON number the setting is silently ignored. See [settings](../guides/settings.md).

## Thread-safety

`ctypes` releases the GIL for the whole duration of a foreign call, so the GIL is not the exclusion. The package uses a writer-preferring readers-writer lock per loaded **image** — row calls, compiles and declarations hold it shared; `set_default_settings` and `close` hold it exclusively — plus one plain lock per `Schema`. Locks are interned on the resolved path, which is the same key `chs_init` deduplicates on. [`multi-version.md`](../guides/multi-version.md) has the rest.

## Deeper

- [`bindings.md`](bindings.md) — the normative shape all four bindings implement.
- the core repository's C ABI specification — the `chs_*` contract, the error model and the result documents.
