# chtypes — Python SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through stdlib `ctypes`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Pure ctypes: no compiler, no build step. The only dependency is a zstd decompressor for the artifact layer, which is the standard library's own from Python 3.14 and the `backports.zstd` package before that.

## Install

Two things: this package, and at least one **artifact** — the per-version native library it `dlopen`s at runtime. The registry fetches the artifact on first use when autofetch is on (`Registry(autofetch=True)` or `CHTYPES_AUTOFETCH=1`), after verifying its signature and every byte against the signed statement; with autofetch off it answers only from what is already installed.

```sh
uv add chtypes            # or: pip install chtypes
```

## Quickstart

```python
import chtypes
from chtypes import Format, Registry

chtypes.setup(timezone="UTC")             # once, before the first open (optional)
registry = Registry(autofetch=True)
library = registry.for_version("26.8")    # a release line or an exact version

ddl = b"CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = Memory"
with library.compile_table(ddl) as schema:
    batch = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')

row = batch.rows[0]
print(batch.outcome)                  # accepted
print(row.values[0].text)             # b'0'          what would actually be stored
print(row.transformed[0].reason)      # overflow_wrap which is the product
```

The row is **accepted** and `256` is silently stored as `0`. That report — `transformed` — is why chtypes exists. Names, SQL, messages and renderings come back as `bytes`, never decoded for you: a column name that is not valid UTF-8 round-trips exactly.

A column whose DEFAULT calls a random or UUID generator is drawn by the library (`Source.DEFAULT_GENERATED`). Ask `rows` for an `export` and insert its `payload`, never the original body, or the server draws a different value than the one previewed.

## Errors

A bad **row** is a verdict, not an exception: `outcome` becomes `Outcome.REJECTED` with ClickHouse's own code. Exceptions are for the call and for the machinery. Every call error carries `status`, `ch_code`, `ch_name`, `message` (bytes) and `column` (bytes).

- `SchemaError` — the server itself would refuse this, and `.ch_code` is a real ClickHouse code.
- `UnsupportedError` — this build declines to answer, and a real server might well have accepted. **Fall back to the server**; never tell a user they are wrong on the strength of a decline.
- `UsageError` — a misuse: a closed object, a conflicting `setup`, a zone given twice. `InternalError` — a library bug.
- **`UnsupportedError` is a peer of `SchemaError`, not a subclass**, so `except SchemaError` never catches a decline. Catch `CallError` for all four, deliberately.
- `ArtifactError` and its subclasses cover fetching and loading: `ArtifactCorruptError`, `ArtifactIncompatibleError`, `ArtifactMissingError` and the rest, one class per code.

## Documentation

|                                                                                                                                                                             |                                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| [Quickstart](https://github.com/wave-rf/chtypes/blob/main/docs/quickstart.md)                                                                                               | the same program in all four languages                                       |
| [Python API reference](https://github.com/wave-rf/chtypes/blob/main/docs/reference/python.md)                                                                               | every symbol, the C entry point under it, what it returns and what it raises |
| [Artifacts](https://github.com/wave-rf/chtypes/blob/main/docs/guides/artifacts.md)                                                                                          | getting one, where it lands, verifying and pinning it                        |
| [Batches](https://github.com/wave-rf/chtypes/blob/main/docs/guides/batches.md)                                                                                              | always `rows`, and the two bad-row policies                                  |
| [Transformations](https://github.com/wave-rf/chtypes/blob/main/docs/guides/transformations.md)                                                                              | the silent-change report, and the DEFAULTs you must echo back                |
| [Settings](https://github.com/wave-rf/chtypes/blob/main/docs/guides/settings.md) · [Discovery](https://github.com/wave-rf/chtypes/blob/main/docs/guides/discovery.md)       | the four channels; asking a real server what profile to validate under       |
| [Filters](https://github.com/wave-rf/chtypes/blob/main/docs/guides/filters.md) · [Multi-version](https://github.com/wave-rf/chtypes/blob/main/docs/guides/multi-version.md) | boolean expressions over rows; several ClickHouse versions in one process    |
| [Support matrix](https://github.com/wave-rf/chtypes/blob/main/docs/support.md) · [Limitations](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md)            | what works where; what chtypes declines to answer                            |

## Four things specific to this binding

**A settings value must never be a `float`.** `encode_settings` stringifies an `int` exactly and raises `TypeError` on a `float`: a 19-digit `chtypes_now_epoch_nanos` does not survive an IEEE double, and as a JSON number the setting would be silently ignored.

**`substituted` is on `RowResult`, not on `BatchResult`.** Reach it through `batch.rows[i].substituted`. `BatchResult` does carry a batch-level `transformed`, which folds in the storage layer's own verdicts.

**`ctypes` releases the GIL for the whole duration of a foreign call**, so the GIL is not the exclusion. The package uses a writer-preferring readers-writer lock per loaded image plus one plain lock per `Schema`; `set_default_settings` and `close` take it exclusively, as the ABI requires. Measured under contention: 27,770 batch reads across 8 threads against 566 concurrent settings swaps, every answer byte-identical to the uncontended one.

**The INSERT column list (ABI revision 5) is a keyword-only `columns` on the same calls** — `schema.row(fmt, raw, columns=["id", "e"])`, and the same argument on `Schema.rows` and `Schema.parse_block`. `None` or an empty sequence is the no-list behavior of every earlier revision; a list makes the data supply exactly those columns, with a listed `EPHEMERAL` value read and in scope for the DEFAULTs that reference it but never stored. The column list needs an artifact of revision 5 or later, and this binding loads only artifacts at its own `chtypes.ABI_REVISION` — any other is refused at load, naming both revisions.

## Tests

`uv run pytest -q`. Tests that need an artifact **skip loudly by name** without a registry on the search path, and a suite that ran nothing fails. The fetch suite runs offline against the miniature releases in `tests/fixtures/fetch/` through `file://` sources and the test key.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
