# examples/python — the Python tour

Seventeen sections over the whole v1 public surface, matching `../go`, `../ts` and `../rust` section for section. See [`../README.md`](../README.md) for the section list. **Runs entirely offline** once an artifact is installed: no Docker, no ClickHouse server, no network.

## Run it

```bash
uv run demo.py      # or, with prerequisite checks: ../chplay.sh python
```

That is the whole command: `uv` resolves `chtypes` as an editable path dependency and there is nothing to build.

## Prerequisites

- **An installed artifact.** Fetch one with the package's own command: `uv run chtypes fetch 26.8` (or `python -m chtypes fetch 26.8`), or set `CHTYPES_AUTOFETCH=1` and let the first open fetch it. The tour opens the newest build installed in the per-user cache; `chtypes where` prints that cache. One build is enough: section 12's cross-version sweep wants two and says so without them.
- **uv** (or any Python 3.11 or later with `chtypes` installed).

## Knobs

| variable            | effect                                                                                               |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| `CHTYPES_VERSION`   | which build the tour opens: a release line (`26.8`), a three-part or a four-part version. Default: the newest installed |
| `CHTYPES_CACHE`     | the cache directory. Default: the per-user cache, `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`          |
| `CHTYPES_AUTOFETCH` | `1` lets the first open fetch a missing build from the registry                                      |

## What is Python-specific here

Everything in the tour is the same _concept_ in all four SDKs ([`docs/reference/bindings-v1.md`](../../docs/reference/bindings-v1.md)); these are the places where the Python spelling is its own.

- **Keyword-only options.** `compile_table(ddl, settings=..., session_timezone=...)`, `rows(..., columns=..., row_filter=..., export=..., doc_flags=...)`.
- **Context managers.** `with lib.compile_table(...) as schema:` frees the handle; so do `Filter` and `Block`. A `Registry` and a `Library` have nothing to close.
- **Peer error types.** `UnsupportedError` is a PEER of `SchemaError`, not a subclass: `except SchemaError` never catches a decline, and all four call errors share `CallError`. Section 10 demonstrates the idiom.
- **Bytes everywhere a name or a message is.** The tour decodes them for printing only, through one `show()` helper.
