# playground/go — the Go tour

Sixteen sections over the whole chtypes surface, matching `../python`,
`../ts` and `../rust` section for section. See [`../README.md`](../README.md)
for the section list. **Runs entirely offline** — no Docker, no ClickHouse
server, no network.

## Run it

```bash
go run -tags chtypes_linked .   # or, with prerequisite checks: ../chplay.sh go
# (the tag adds the statically linked section — §static artifact — which
#  needs a core build tree via CGO_LDFLAGS; the registry sections need only artifacts)
```

## Prerequisites

- **Artifacts.** The tour reads `$CHTYPES_REGISTRY`, defaulting to the repo's
  the per-user cache (`~/.cache/chtypes/artifacts/<os>-<arch>/`) or wherever
  `$CHTYPES_REGISTRY` points. No artifacts? `../../scripts/fetch.sh 25.8`, or
  build one in the core repository. One version is enough; section 12's cross-version
  sweeps want several and degrade gracefully without them.
- **cgo**. Sections 9 and 14 (the statically linked shape) additionally
  need the core repository's `lib/build` on `CGO_LDFLAGS` and the
  `chtypes_linked` tag; `chplay.sh go` sets both when the sibling is present,
  and without the tag those two sections say so and skip. They use the
  static path it provides; everything else goes through the registry.
- Go 1.27+.

## Knobs

| variable | effect |
|---|---|
| `CHTYPES_VERSION` | which artifact the tour uses (`25.8`, `25.8.28.1-lts`, …). Default: the newest line held |
| `CHTYPES_REGISTRY` | the artifact directory. Default: the per-user cache, `~/.cache/chtypes/artifacts/<os>-<arch>` |

## What is Go-specific here

Everything in the tour is the same *concept* in all four SDKs; these are the
places where Go's spelling is its own.

- **Two loaders.** Only Go has the statically linked single-version path
  (section 14) next to the dlopen registry — cgo can link one artifact
  directly. It is also the only place Go exposes `SetDefaultSettings`
  (section 9) and `RegisteredFamilies`; the dlopen'd `Library` structurally
  cannot make the dangerous seed call, on purpose.
- **Functional options.** `CompileDDL(ddl, WithCompileSettings(m))` and
  `SetEngine(engine, orderBy, WithMergeTreeSettings(m))` — the profile is
  optional without a second signature.
- **Two peer error types.** `*SchemaError` carries a real ClickHouse code;
  `*UnsupportedError` carries none, and a decline can never satisfy
  `errors.As(&SchemaError{})`. Section 10 is the idiom.
- **No teardown, deliberately.** Go never `dlclose`s, so its `Registry` owes
  no `chs_shutdown` (`spec/bindings.md` §Teardown). Section 13 explains.

## Also in this directory

`ingest-demo/` — the **optional online** demo against a real ClickHouse.
Not run by `chplay.sh`; see [`ingest-demo/README.md`](ingest-demo/README.md).
