# chtypes — Python SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through stdlib `ctypes`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Pure Python: **zero dependencies, no build step, no compiler.**

## Install

Two things: this package, and at least one **artifact** — the per-version native library it `dlopen`s at runtime.

```sh
uv add chtypes            # or: pip install chtypes
python -m chtypes fetch 25.8
```

The fetch lands in `~/.cache/chtypes/artifacts/<os>-<arch>/25.8/` — the per-user cache every chtypes binding reads by default — after checking an ed25519 signature over the release and the sha256 of every byte. `$CHTYPES_REGISTRY` overrides it. The ed25519 verifier is pure stdlib too.

## Quickstart

```python
from chtypes import Format, Registry

registry = Registry()                   # walks the search path
library = registry.for_version("25.8")  # a line or an exact patch; never a nearest match

with library.compile_ddl("x UInt8, ts DateTime DEFAULT now()") as schema:
    batch = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')

row = batch.rows[0]
print(batch.outcome)              # accepted
print(row.value("x").text)        # 0             — what would actually be stored
print(row.transformed[0].reason)  # overflow_wrap — which is the product
print(row.substituted)            # ts: send it explicitly, or preview != stored
```

The row is **accepted** and `256` is silently stored as `0`. That report — `transformed` — is the one derived answer in the system and the reason it exists.

`ts` was substituted rather than stored: send every substituted column as an explicit value in the real INSERT, or the server re-evaluates `now()` at its own instant and your preview is not what landed. Pin the instant in tests with `settings={"chtypes_now_epoch_nanos": "1700000000000000000"}`.

## Three outcomes, and conflating any two is a bug

A bad **row** is a verdict, not an exception: `outcome` becomes `Outcome.REJECTED` with ClickHouse's own code. Exceptions are for schema-level answers and for the machinery.

- `SchemaError` — the server itself would refuse this, and `.code` is a real ClickHouse code.
- `UnsupportedError` — this build declines to answer, and a real server might well have accepted. **Fall back to the server**; never tell a user they are wrong on the strength of a decline.
- **`UnsupportedError` is a peer of `SchemaError`, not a subclass.** `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both.

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

## Three things specific to this binding

**A settings value must never be a `float`.** `encode_settings` stringifies an `int` exactly and raises `TypeError` on a `float`: a 19-digit `chtypes_now_epoch_nanos` does not survive an IEEE double, and as a JSON number the setting would be silently ignored.

**`substituted` is on `RowResult`, not on `BatchResult`.** Reach it through `batch.rows[i].substituted`. `BatchResult` does carry a batch-level `transformed`, which folds in the storage layer's own verdicts.

**`ctypes` releases the GIL for the whole duration of a foreign call**, so the GIL is not the exclusion. The package uses a writer-preferring readers-writer lock per loaded image plus one plain lock per `Schema`; `set_default_settings` and `close` take it exclusively, as the ABI requires. Measured under contention: 27,770 batch reads across 8 threads against 566 concurrent settings swaps, every answer byte-identical to the uncontended one.

## Tests

`uv run pytest -q`. Tests that need an artifact **skip loudly by name** without a registry on the search path, and a suite that ran nothing fails. The fetch suite runs offline against the miniature releases in `tests/fixtures/fetch/` through `file://` sources and the test key.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
