# examples/go: the Go tour

Seventeen sections over the whole v1 public API, matching `../python`, `../ts` and `../rust` section for section. See [`../README.md`](../README.md) for the section list. **Runs entirely offline** (no Docker, no ClickHouse server, no network) against an installed artifact.

## Run it

```bash
go run ../../go/cmd/chtypes fetch 26.8   # install a library for this host, verified
go run .                                  # 17 of 17 sections, dlopen-only: what a public clone runs
../chplay.sh go                           # the same tour with prerequisite checks
```

`chplay.sh` is the tested path: CI runs it with `--require-all --locked`, so the command above is verified rather than merely written down.

## Prerequisites

- **An installed library.** The tour reads the v1 cache, `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/` (or `$CHTYPES_CACHE`). One line is enough; section 12's cross-version sweeps want several and degrade gracefully without them.
- **cgo is optional, and not available to a public clone.** Section 14 (the linked image) needs the artifact producer's build tree on `CGO_LDFLAGS` plus the `chtypes_linked` tag. That tree is not public: adding the tag without one fails at link time, which is why it is not in the commands above. Without the tag that section says so and skips, and the other sixteen run against the registry.
- Go 1.27+.

## Knobs

| variable          | effect                                                                                   |
| ----------------- | ---------------------------------------------------------------------------------------- |
| `CHTYPES_VERSION` | which line the tour uses (`26.8`, `26.8.15.10`, ...). Default: the newest line installed |
| `CHTYPES_CACHE`   | the cache root. Default: `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`                        |

## What is Go-specific here

Everything in the tour is the same _concept_ in all four SDKs; these are the places where Go's spelling is its own.

- **Two loaders.** Only Go has the linked image (section 14) next to the registry: cgo can link one library directly, and `OpenLinked` opens it as the same `Library` type.
- **Functional options.** `CompileTable(statement, WithSettings(m))` and `Rows(format, body, WithExport(f))`: every option is optional without a second signature.
- **Peer error types.** `*SchemaError` carries a real ClickHouse code; `*UnsupportedError` carries none, and a decline can never satisfy `errors.As(&SchemaError{})`. Section 10 is the idiom.
- **No teardown, deliberately.** No library is ever unloaded, so a registry owes nothing. Section 13 explains.
- **One process setup.** `chtypes.Setup` fixes the default settings once, first; section 9 uses it to show the precedence layers.

## Also in this directory

`ingest-demo/` is the **optional online** demo against a real ClickHouse. Not run by `chplay.sh`; see [`ingest-demo/README.md`](ingest-demo/README.md).
