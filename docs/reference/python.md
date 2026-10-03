# Python API reference

```python
import chtypes
```

Package `chtypes`: pure Python over stdlib `ctypes`, no compiler and no build step. The one runtime dependency is a zstd decompressor for the artifact layer: the standard library's own on Python 3.14 and later, the `backports.zstd` package before that. Every public symbol is in `chtypes.__all__`, and the package ships `py.typed`.

This page lists what the Python binding ships and how each name is spelled. The contract (what each operation does, the documents it decodes, the error table, the thread-safety rules) is [`bindings-v1.md`](bindings-v1.md), and this page does not restate it; the fetch wire contract is [`fetch-v1.md`](../guides/fetch-v1.md) and the C layer is [`abi-v1.md`](abi-v1.md). The names follow `bindings-v1.md` §1 mechanically: Python `snake_case`.

## How to read this

A bad **row** is a verdict, returned and never raised: a rejected row is a `RowResult` with `outcome == Outcome.REJECTED`. Exceptions are for the call and for the machinery. Names, SQL, messages and renderings come back as `bytes`; nothing here decodes one for you, and `str(error)` is the one lossy display form. Settings and query parameters map `str` to `str` and are written verbatim: a non-string value is a `TypeError`.

## Setup, registry and opening

| Symbol                                                                                 | What it is                                                                                                                                                                                      |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `setup(*, timezone=None, defaults=None)`                                               | The image zone and the default settings, chosen once, before the first open. A second call with the same arguments is a no-op; a different one is a `UsageError` naming both ([§6](bindings-v1.md)) |
| `Registry(*, fetch=None, autofetch=None, preload=())`                                  | Opens nothing at construction. `autofetch` defaults to `CHTYPES_AUTOFETCH`, and is off when that is unset. `preload` opens each listed request at construction and never fetches                |
| `Registry.for_version(request)`                                                        | `Library`. `request` is `26.8`, `26.8.15` or `26.8.15.10`: no `v` prefix, no channel suffix (a refused spelling is a `UsageError`). A request opened before returns the same `Library`          |
| `Registry.installed()`, `Registry.libraries()`                                         | The fetch layer's list of installed builds, as a tuple of `Resolved`; and the libraries this registry has opened, in order                                                                     |
| `FetchOptions(bases, cache_dir, system_dirs, token, trusted_keys, allow_unsigned, offline, frozen, lock_path, lock_write, update)` | A frozen dataclass of everything the fetch layer configures. A field left at its default falls back to the fetch layer's own defaults and environment                                |
| `Resolved`, `TrustedKey`                                                               | The fetch layer's record of an installed build (`Library.resolved` is the one that first opened an image), and a trusted ed25519 key                                                           |
| `open_unverified(path, *, allow=False)`                                                | A local, unpublished build for the artifact producer's own suites. It needs `allow=True` and `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`, and its `Library` has no `resolved`                           |

A library is never unloaded and never closed: there is no `Registry.close` or `Library.close`, and a loaded image lives until the process exits. Two registries, two spellings or a hardlink of one artifact share one image.

## Library

| Symbol                                                                  | C entry point                                  | Returns                                                             |
| ----------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------- |
| `Library.build_info`, `.version`, `.minor`, `.path`, `.resolved`        | `chs_build_info`, read once by the loader      | `BuildInfo`, the four-part version, the release line, a `Path`, `Resolved \| None` |
| `Library.compile_table(create_table, *, settings=None, session_timezone=None)` | `chs_schema_create`                      | `Schema`. Exactly one `CREATE TABLE` statement: the engine, TTL and partition key are part of it |
| `Library.validate_type(type_expr)`                                      | `chs_type_validate`                            | the canonical type, `bytes`                                         |
| `Library.quote_identifier(name)`, `.quote_identifier_if_needed(name)`, `.quote_literal(text)` | `chs_back_quote`, `chs_back_quote_if_needed`, `chs_quote_string` | `bytes`                              |
| `Library.error_codes()`                                                 | `chs_error_codes`                              | this build's `ErrorCodeTable`: `.name(code)`, `.code(name)`, `.all()` |
| `Library.discover_query()`, `Library.discover_columns(rows)`            | `chs_discover_query`, `chs_discover_columns`   | the SQL to run against your server, `bytes`; a `Discovery`          |
| `Library.live_handles()`                                                | `chs_live_handles`                             | `dict[str, int]`, a diagnostic                                      |

## Schema, Filter and Block

| Symbol                                                                                                                                              | C entry point           | Returns                                    |
| --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------ |
| `Schema.describe()`                                                                                                                                 | `chs_schema_describe`   | `SchemaDescription`                        |
| `Schema.row(format, body, *, settings=None, session_timezone=None, columns=None)`                                                                   | `chs_preview_row`       | `RowResult`                                |
| `Schema.rows(format, body, *, settings=None, session_timezone=None, columns=None, row_filter=None, export=None, doc_flags=DocFlags.ALL)`            | `chs_preview_batch`     | `BatchResult`                              |
| `Schema.compile_filter(expr, *, params=None, settings=None, session_timezone=None)`                                                                 | `chs_filter_create`     | `Filter`                                   |
| `Schema.parse_block(format, body, *, settings=None, session_timezone=None, columns=None)`                                                           | `chs_block_create`      | `Block`                                    |
| `Filter.rows(format, body, *, settings=None, session_timezone=None)`, `Filter.eval(block)`                                                          | `chs_filter_eval_body`, `chs_filter_eval_block` | `FilterResult`                |
| `Schema.close()`, `Filter.close()`, `Block.close()`                                                                                                 | `chs_*_free`            | `None`; idempotent, any order is safe      |

`Schema`, `Filter` and `Block` are context managers and are freed by a finalizer when abandoned. Using a closed object is a `UsageError`. `BytesIn` is `bytes | str` (a `str` is encoded as UTF-8); a body is `bytes` and never accepts `str`. The per-call zone is the `session_timezone` argument, which is written into `settings` as that one key, and giving both is a `UsageError`. The export channel is the keyword-only `export` of `rows`; there is no separate method.

## Results and vocabularies

**Result documents**, all frozen dataclasses decoded one to one from the library's documents ([§5](bindings-v1.md)): `RowResult`, `BatchResult`, `FilterResult`, `FilterRowError`, `Value`, `Transform`, `Computed`, `EngineCell`, `Span`, `Header`, `Framing`, `SchemaDescription`, `Column`, `Discovery`, `DiscoveredColumn`, `ErrorCodeTable`, `ErrorCodeEntry`, `BuildInfo` and `Capabilities`.

`Value.is_stored` and `Transform.lossy` are the description's own facts, read from the generated tables. `BatchResult.engine_rows` is `None` or a tuple of rows, each a tuple of `EngineCell`; `RowResult.partition_id` and `BatchResult.partition_count` are `None` unless the statement declared a partition key.

**Vocabularies**, generated from the description ([§3](bindings-v1.md)): `Format` (an `IntEnum`, with `.ch_name`), `Status`, `Outcome` and `FilterOutcome` (`StrEnum`), `Verdict` (with `.answered`), `DefaultKind`, `DocFlags`, and the plain-string vocabularies `Reason` and `Source` (a `Source` of `DEFAULT_GENERATED` means insert the library's export, never the original body). `Reason.lossy(reason)` and `Source.is_stored(source)` are the same facts the result fields read.

## Errors

One table ([§4](bindings-v1.md)). The four call errors are peers under `CallError`, which carries `status`, `ch_code`, `ch_name`, `message` (bytes) and `column` (bytes):

| Class                 | Raised for                                                                                                         |
| --------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `SchemaError`         | ClickHouse's own refusal, which a server would also give                                                           |
| `UnsupportedError`    | this build declines to answer, and a server might accept: fall back to the server                                  |
| `UsageError`          | a misuse: a closed object, a refused version spelling, a conflicting setup, a zone given twice                     |
| `InternalError`       | a library bug, or a document that does not decode                                                                  |
| `ArtifactError` and one subclass per code | the loader's refusals and the fetch layer's errors, each with `.code`: `ArtifactMissingError`, `ArtifactUntrustedError`, `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`, `ArtifactIncompatibleError`, `SourceUnreachableError`, `SourceUnauthorizedError`, `SourceForbiddenError`, `SourceIncompatibleError` |

`UnsupportedError` is a peer of `SchemaError`, not a subclass: `except SchemaError` never catches a decline. The `CODE_*` constants are the codes; `chtypes.ChtypesError` is the root of everything this package raises.

## The command

`python -m chtypes` and the `chtypes` console script run the same four commands over the fetch layer ([`fetch-v1.md`](../guides/fetch-v1.md)):

| Command                                                  | What it does                                                                                                                                          |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chtypes fetch <spelling>... \| --all [--lock FILE] [--frozen] [--offline]` | Resolve, verify and install; prints each installed library path on stdout. `--all` is every release line the registry publishes; `--lock FILE` writes the lock, and `--frozen` reads `FILE` (default `chtypes.lock`) and fetches only what it pins, by digest |
| `chtypes verify`                                         | Re-hash every installed library against its verified record                                                                                           |
| `chtypes list [--offline]`                               | What is installed, and the version tags the registry publishes (its `tags/list`, kept to version spellings)                                           |
| `chtypes where`                                          | The v1 cache root                                                                                                                                     |

Exit statuses come from the `errors` table of [`spec/fetch-v1/constants.json`](../../spec/fetch-v1/constants.json) (the table in `fetch-v1.md` §8): the status of the error's code, `2` for a usage error and `0` for success. Configuration is the environment the fetch layer reads: `CHTYPES_ARTIFACTS_URL`, `CHTYPES_CACHE`, `CHTYPES_DOWNLOAD_TOKEN`, `CHTYPES_TRUSTED_KEYS` (which replaces the default trust list, never extends it), `CHTYPES_ALLOW_UNSIGNED` and `CHTYPES_AUTOFETCH`.

## Concurrency

Safe across threads at every level ([§3](bindings-v1.md)). `ctypes` releases the GIL for the whole of a foreign call, so calls on compiled handles run in parallel. The package takes no lock around a call: it keeps a close guard per handle (so `close` waits for the calls already inside it) and the setup guard.

## Known gaps in 1.0

What the 1.0 binding does not do, what happens instead, and the workaround for each are in [`limitations.md`](../limitations.md#known-gaps-in-10).
