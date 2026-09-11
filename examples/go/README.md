# examples/go — the Go tour

Sixteen sections over the whole chtypes surface, matching `../python`, `../ts` and `../rust` section for section. See [`../README.md`](../README.md) for the section list. **Runs entirely offline** — no Docker, no ClickHouse server, no network.

## Run it

```bash
go run .            # 16/16 sections, dlopen-only — what a public clone runs
../chplay.sh go     # the same tour with prerequisite checks; this is what CI runs
```

`chplay.sh` is the tested path: CI runs `chplay.sh --require-all --locked`, so the command above is verified on every pull request rather than merely written down.

## Prerequisites

- **Artifacts.** The tour reads `$CHTYPES_REGISTRY`, defaulting to the per-user cache (`~/.cache/chtypes/artifacts/<os>-<arch>/`) or wherever `$CHTYPES_REGISTRY` points. No artifacts? `../../scripts/fetch.sh 25.8`. One version is enough; section 12's cross-version sweeps want several and degrade gracefully without them.
- **cgo — optional, and not available to a public clone.** Sections 9 and 14 (the statically linked shape) need a core build tree on `CGO_LDFLAGS` plus the `chtypes_linked` tag. That tree is not public: adding the tag without one fails at link time with `library 'chtypes' not found`, which is why it is not in the command above. Without the tag those two sections say so and skip, and the other fourteen run against the registry. `chplay.sh go` adds the tag only when `CHTYPES_CORE_DIR` or `CHTYPES_LIB_BUILD` points at such a tree.
- Go 1.27+.

## Knobs

| variable           | effect                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `CHTYPES_VERSION`  | which artifact the tour uses (`25.8`, `25.8.28.1-lts`, …). Default: the newest line held      |
| `CHTYPES_REGISTRY` | the artifact directory. Default: the per-user cache, `~/.cache/chtypes/artifacts/<os>-<arch>` |

## What is Go-specific here

Everything in the tour is the same _concept_ in all four SDKs; these are the places where Go's spelling is its own.

- **Two loaders.** Only Go has the statically linked single-version path (section 14) next to the dlopen registry — cgo can link one artifact directly. It is also the only place Go exposes `SetDefaultSettings` (section 9) and `RegisteredFamilies`; the dlopen'd `Library` structurally cannot make the dangerous seed call, on purpose.
- **Functional options.** `CompileDDL(ddl, WithCompileSettings(m))` and `SetEngine(engine, orderBy, WithMergeTreeSettings(m))` — the profile is optional without a second signature.
- **Two peer error types.** `*SchemaError` carries a real ClickHouse code; `*UnsupportedError` carries none, and a decline can never satisfy `errors.As(&SchemaError{})`. Section 10 is the idiom.
- **No teardown, deliberately.** Go never `dlclose`s, so its `Registry` owes no `chs_shutdown` (`docs/reference/bindings.md` §Teardown). Section 13 explains.

## Also in this directory

`ingest-demo/` — the **optional online** demo against a real ClickHouse. Not run by `chplay.sh`; see [`ingest-demo/README.md`](ingest-demo/README.md).
