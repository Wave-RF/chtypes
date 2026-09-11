# examples/python — the Python tour

Sixteen sections over the whole chtypes surface, matching `../go`, `../ts`
and `../rust` section for section. See [`../README.md`](../README.md) for the
section list. **Runs entirely offline** — no Docker, no ClickHouse server, no
network.

## Run it

```bash
uv run demo.py      # or, with prerequisite checks: ../chplay.sh python
```

That is the whole command — `uv` resolves `python` as an editable
path dependency and there is nothing to build.

## Prerequisites

- **Artifacts.** The tour reads `$CHTYPES_REGISTRY`, defaulting to the repo's
  the per-user cache (`~/.cache/chtypes/artifacts/<os>-<arch>/`) or wherever
  `$CHTYPES_REGISTRY` points. No artifacts? `../../scripts/fetch.sh 25.8`, or
  build one in the core repository. One version is enough; section 12's cross-version
  sweeps want several and degrade gracefully without them.
- **uv** (or any Python ≥ 3.11 with `python` installed).

## Knobs

| variable | effect |
|---|---|
| `CHTYPES_VERSION` | which artifact the tour uses (`25.8`, `25.8.28.1-lts`, …). Default: the newest line held |
| `CHTYPES_REGISTRY` | the artifact directory. Default: the per-user cache, `~/.cache/chtypes/artifacts/<os>-<arch>` |

## What is Python-specific here

Everything in the tour is the same *concept* in all four SDKs; these are the
places where the Python spelling is its own.

- **Keyword-only options.** `compile_ddl(ddl, settings=..., mode=...)` and
  `set_engine(engine, order_by, merge_tree_settings=...)`.
- **Context managers.** `with lib.compile_ddl(...) as schema:` frees the
  handle; `Registry` is a context manager too.
- **Peer error types.** `UnsupportedError` is a PEER of `SchemaError`
  (`docs/reference/bindings.md` rule 12; the grandfathered subclass was retired
  2026-08-26) — `except SchemaError` never catches a decline, and forgetting
  the decline arm raises loudly instead of silently converting declines into
  rejections. Section 10 demonstrates the two-arm idiom.
- **Convenience predicates.** `RowResult.accepted` / `.poisoned` (section 5).
- The introspection trio is complete here too: `Library.reference_type`,
  `Library.registered_families()`, `Library.function_flags()`
  (`docs/reference/bindings.md` §Introspection).
