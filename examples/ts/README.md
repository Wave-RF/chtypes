# examples/ts: the TypeScript tour

Eighteen sections over the whole chtypes surface, matching `../go`, `../python` and `../rust` section for section through section 17; section 18, the ABI v2 server profile, landed in Go first. See [`../README.md`](../README.md) for the section list. **Runs offline** once an artifact is installed: no Docker, no ClickHouse server, no network.

## Run it

```bash
pnpm install && pnpm demo    # or, with prerequisite checks: ../chplay.sh ts
```

`pnpm demo` runs `demo.ts` directly with Node's built-in type stripping (Node 22.21 or later, this package's own floor), so there is no build step for the tour. CI type-checks it with `tsc` as part of the package's `pnpm typecheck`.

## Prerequisites

- **An installed artifact.** The tour opens whatever the v1 fetch layer has installed in its cache (`chtypes where` prints the cache root). Install one with the package's own CLI:

  ```bash
  npx @wavehouse/chtypes fetch 26.8
  ```

  One line is enough; section 12's cross-version sweeps want two and degrade gracefully without them.

- **A built binding.** This package depends on `ts` via `file:`, and that package's entry point is `dist/index.js`. If it is missing or stale:

  ```bash
  pnpm --dir ../../ts install && pnpm --dir ../../ts build
  ```

- Node 22.21+ (the binding declares `engines.node >= 22.21`) and `pnpm`.

## Knobs

| variable            | effect                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------- |
| `CHTYPES_VERSION`   | which installed line the tour uses (`26.8`, `26.8.15`, ...). Default: the newest        |
| `CHTYPES_CACHE`     | the cache root the fetch layer reads. Default: `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1` |
| `CHTYPES_AUTOFETCH` | `1` lets the registry fetch a version that is not installed, instead of refusing        |

## What is TypeScript-specific here

Everything in the tour is the same _concept_ in all four SDKs; these are the places where the TypeScript spelling is its own.

- **A trailing options object.** `compileTable(ddl, { settings, sessionTimezone })`, `row(format, body, { settings, columns })` and `rows(format, body, { exportFormat, docFlags, rowFilter })`.
- **Peer classes.** `SchemaError` carries ClickHouse's own `chCode` and `chName`; `UnsupportedError` is a decline and never satisfies `instanceof SchemaError`. Section 10 is the idiom.
- **Bytes are `Buffer`s.** Every name, message and rendered value is bytes, never assumed to be UTF-8; the tour calls `.toString()` only to print.
- **Settings values are strings, and only strings.** A JS `number` is a `TypeError`, never silently rewritten (section 4d).
- **Disposal.** `close()` everywhere, plus `Symbol.dispose` on `Schema`, `Filter` and `Block` for `using` on Node 24 and later. A `Registry` and a `Library` have nothing to close (section 13).
