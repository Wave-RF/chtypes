# chtypes — Python SDK

> **2.0.0-dev: UNSTABLE, staging only, not for production.** This is the ABI v2 development binding (public issue #511). It speaks ABI v2's unstable description, pins its dev fingerprint and refuses a library with any other ("update your dev SDK"). It fetches only from the staging dev channel (`https://registry-staging.wavehouse.dev/chtypes/v2-dev`) and trusts only the staging key; `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED`, and the `bases`, `trusted_keys` and `allow_unsigned` fetch options, are ignored, each with one warning; `--lock`, `--frozen` and `--update`, and the `frozen`, `lock_path`, `lock_write` and `update` options, are refused, because a dev build is replaceable and a superseded one expires after 14 days. Its cache is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`, or `<CHTYPES_CACHE>/v2-dev` under an explicit cache, which no 1.x SDK reads. Every vocabulary keeps a value it does not list as its `unknown(n)` member (`Outcome("x")`, whose `known` is `False`) instead of failing the document. The rules are r1 to r6 of [`docs/reference/abi-v2.md`](../docs/reference/abi-v2.md). For production, use the 1.x package: `pip install "chtypes<2"`.
>
> An explicit cache directory gets the `v2-dev` subroot whether it comes from `CHTYPES_CACHE` or from `FetchOptions(cache_dir=...)`: the layout is under `<cache_dir>/v2-dev`, never in the directory itself, so code that inspects or pre-populates a cache looks there; `chtypes.cache_root` and `chtypes.search_dirs` return the resolved paths. `CHTYPES_OFFLINE=1` is the environment twin of `FetchOptions(offline=True)` and `--offline`: the cache only, no request.

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through stdlib `ctypes`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Pure ctypes: no compiler, no build step. The only dependency is a zstd decompressor for the artifact layer, which is the standard library's own from Python 3.14 and the `backports.zstd` package before that.

## Install

Two things: this package, and at least one **artifact** — the per-version native library it `dlopen`s at runtime. The registry fetches the artifact on first use when autofetch is on (`Registry(autofetch=True)` or `CHTYPES_AUTOFETCH=1`), after verifying its signature and every byte against the signed statement; with autofetch off it answers only from what is already installed.

```sh
uv add "chtypes>=2.0.0.dev0"            # or: pip install "chtypes>=2.0.0.dev0"
```

`uv add chtypes` and `pip install chtypes` never select a pre-release on their own: the specifier names a dev version, which is what allows one.

## Quickstart

```python
import chtypes
from chtypes import Format, Registry

chtypes.setup(timezone="UTC")  # once, before the first open (optional)
registry = Registry(autofetch=True)
library = registry.for_version("26.8")  # a release line or an exact version

ddl = b"CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = Memory"
with library.compile_table(ddl) as schema:
    batch = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')

row = batch.rows[0]
print(batch.outcome)  # accepted
print(row.values[0].text)  # b'0'          what would actually be stored
print(row.transformed[0].reason)  # overflow_wrap which is the product
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
| [Support matrix](https://github.com/wave-rf/chtypes/blob/main/docs/support-v1.md) · [Limitations](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md)         | what works where; what chtypes declines to answer                            |

## Fetching from the command line

`chtypes fetch 26.8` (or `python -m chtypes fetch 26.8`) resolves, verifies and installs a build into the per-user cache; `chtypes verify`, `chtypes list` and `chtypes where` complete the four commands. `--lock FILE` records what was installed and `--frozen` fetches only what the lock pins. See the [Python API reference](https://github.com/wave-rf/chtypes/blob/main/docs/reference/python.md#the-command).

## Things specific to this binding

**A settings value is a string, and only a string.** `settings={"flatten_nested": "0"}`, never `0`: a non-string value is a `TypeError`, and nothing rewrites one for you.

**`ctypes` releases the GIL for the whole of a foreign call**, so calls on compiled handles run in parallel across threads. The package takes no lock around a call; it keeps a close guard per handle and one setup guard.

**The INSERT column list is a keyword-only `columns`** on `schema.row`, `schema.rows` and `schema.parse_block`: `schema.row(Format.JSON_EACH_ROW, body, columns=["id", "e"])`. `None` or an empty sequence is the no-list shape; a list makes the data supply exactly those columns, with a listed `EPHEMERAL` value read and in scope for the DEFAULTs that reference it but never stored.

**Known gaps in 1.0** are listed, with workarounds, in [`docs/limitations.md`](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md#known-gaps-in-10).

## Tests

`uv run pytest -q`. Tests that need an artifact **skip loudly by name** without a registry on the search path, and a suite that ran nothing fails. The fetch conformance suite runs offline against the repository's `tests/fixtures/fetch-v1/` cases, served by a loopback registry the suite starts itself, under the fixture test key (`docs/guides/fetch-v1.md` §10).

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
