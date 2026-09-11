# examples/ts — the TypeScript tour

Sixteen sections over the whole chtypes surface, matching `../go`, `../python` and `../rust` section for section. See [`../README.md`](../README.md) for the section list. **Runs entirely offline** — no Docker, no ClickHouse server, no network.

## Run it

```bash
pnpm install && pnpm demo    # or, with prerequisite checks: ../chplay.sh ts
```

## Prerequisites

- **Artifacts.** The tour reads `$CHTYPES_REGISTRY`, defaulting to the repo's the per-user cache (`~/.cache/chtypes/artifacts/<os>-<arch>/`) or wherever `$CHTYPES_REGISTRY` points. No artifacts? `../../scripts/fetch.sh 25.8`, or build one in the core repository. One version is enough; section 12's cross-version sweeps want several and degrade gracefully without them.
- **A built binding.** This package depends on `ts` via `file:`, and that package's entry point is `dist/index.js`. If it is missing or stale:

  ```bash
  pnpm --dir ../../ts install && pnpm --dir ../../ts build
  ```

- Node 22+ (the binding declares `engines.node >= 22`) and `pnpm`.

## Knobs

| variable           | effect                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `CHTYPES_VERSION`  | which artifact the tour uses (`25.8`, `25.8.28.1-lts`, …). Default: the newest line held      |
| `CHTYPES_REGISTRY` | the artifact directory. Default: the per-user cache, `~/.cache/chtypes/artifacts/<os>-<arch>` |

## What is TypeScript-specific here

Everything in the tour is the same _concept_ in all four SDKs; these are the places where the TypeScript spelling is its own.

- **A trailing options object.** `compileDdl(ddl, { settings, mode })` and `setEngine(engine, orderBy, { mergeTreeSettings })`.
- **Peer classes.** `SchemaError` carries a real ClickHouse `code`; `UnsupportedError` has **no `code` property at all**, and a decline never satisfies `instanceof SchemaError`. Section 10 is the idiom.
- **Settings values are `string | bigint`** — a JS `number` is a RUNTIME error, because a 19-digit nanosecond epoch does not survive an IEEE double (section 9's comment).
- **Disposal.** `close()` everywhere, plus `Symbol.dispose` on `Schema` and `Registry` for `using`; `nativeStats()` in section 13 shows the string-ownership counters balancing.
