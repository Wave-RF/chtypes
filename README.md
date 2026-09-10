# chtypes — ClickHouse's own type system, as a library you can call

Exact ClickHouse semantics for **validating, coercing and previewing rows
before they reach a server**: ClickHouse's real C++ (`DataTypeFactory`,
`ISerialization`, `ReadHelpers`, `evaluateMissingDefaults`, the MergeTree
insert-time merge) compiled per ClickHouse release into a native artifact
behind a small, frozen C ABI, and reached from Go, Python, TypeScript and Rust.
Nothing semantic is reimplemented, so *"what does ClickHouse do with `256` into
a `UInt8`?"* is answered by ClickHouse's own code, not by a model of it.

```
row + schema + ClickHouse version  ─▶  accepted / rejected / poisoned
                                       the stored value, byte for byte
                                       every silent change, named
```

This repository is the **SDK half** of chtypes, Apache 2.0:

| | |
|---|---|
| [`go/`](go/README.md) | `github.com/wave-rf/chtypes/go` — package `chtypes`, cgo `dlopen`, dlopen-only by default |
| [`python/`](python/README.md) | `chtypes` — stdlib `ctypes`, zero dependencies |
| [`ts/`](ts/README.md) | `@wavehouse/chtypes` — `ffi-rs`, Node ≥ 22 |
| [`rust/`](rust/README.md) | `chtypes` — `libloading` |
| [`include/chtypes.h`](include/chtypes.h) | the C ABI every binding is written against — 28 `chs_*` functions, ABI revision 4 |
| [`spec/`](spec/README.md) | the normative contract: [`c-abi.md`](spec/c-abi.md), [`bindings.md`](spec/bindings.md) (the shape every SDK implements), [`artifact.md`](spec/artifact.md) (what ships, how a registry is laid out) |
| [`goldens/`](goldens/README.md) | the public golden set — 31 cases, one answer in four languages |
| [`playground/`](playground/README.md) | four side-by-side runnable tours, same sections in every language |
| [`docs/artifacts.md`](docs/artifacts.md) | how a consumer obtains and verifies artifacts ([`docs/fetch.md`](docs/fetch.md): the fetch/verify/signing contract every SDK implements) |
| [`scripts/fetch.sh`](scripts/fetch.sh) | the verified download into the per-user cache |

The other half — the C++ wrapper, the per-version vendoring and build
pipeline, the artifacts themselves, and the differential proof (tens of
thousands of cases scored against real ClickHouse servers on every supported
version) — is the core repository, `chtypes-core`, under its own licence. The
SDKs here contain no ClickHouse code: they load an artifact and speak the ABI.

## Artifacts

An SDK answers nothing by itself. It `dlopen`s one **artifact** per ClickHouse
release — a self-contained shared library, 160–300 MB, that names itself
(`chs_clickhouse_version()`) and carries its own `manifest.json` — and can hold
several versions in one process, each with its own ClickHouse. The supported
lines are those with a committed run of record in the core repository; today
that is 24.8, 25.3, 25.8, 25.10, 26.5, 26.6 and 26.7, on `linux-amd64`,
`linux-arm64` and `darwin-arm64` (macOS is a development floor, not an oracle:
its `long double` makes float parses diverge from a real server).

```sh
scripts/fetch.sh 25.8                 # one line, verified, into the per-user cache
scripts/fetch.sh --all                # every published line for this host
```

Every SDK defaults to the same registry directory —
`${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/` (`$CHTYPES_REGISTRY`
overrides it) — which is where `scripts/fetch.sh` installs and where a core
build lands, so a machine set up once serves all four. The fetch-and-verify
contract, the asset names and the versioning rule are in
[`docs/artifacts.md`](docs/artifacts.md).

Artifacts carry their own licence, separate from this repository's; see the
`LICENSE` file inside each artifact release.

## Quickstart

```go
reg, _ := chtypes.NewRegistry(chtypes.DefaultRegistryDir())
lib, _ := reg.For("25.8")                       // minor line or exact patch; never a nearest match
cs, _ := lib.CompileDDL("x UInt8, ts DateTime DEFAULT now()")
defer cs.Close()
r, _ := cs.Row(chtypes.JSONEachRow, []byte(`{"x":256}`))
r.Outcome                // accepted
r.Transformed[0].Reason  // overflow_wrap — 256 stored as 0, silently
r.Substituted            // ts: send it explicitly, or preview != stored
```

The same program in the other three languages, section for section, is
[`playground/`](playground/README.md). Each binding's README has the full API
table and the language's own idioms.

## The guarantees every binding is held to

- **`Transformed` is the product.** ClickHouse never says *"I changed your
  value"*; chtypes derives that report (`overflow_wrap`, `date_clamp`,
  `poisoned`, `ttl_expired`, …) and it is not optional.
- **Over-accepts and over-rejects are both budgeted at zero** in the proof
  behind the artifacts: a row accepted here and rejected by the server ships
  before the insert fails; a row rejected here and accepted by the server is
  silent data loss.
- **`unsupported` is an answer, never a guess.** A binding surfaces the
  library's decline; it never papers over one.
- **The four bindings give one answer.** The golden set is run by all of them,
  and each is a scored column in the core repository's arbiter at the same
  agreement as the reference.

## Developing here

Each language directory is self-contained: its own tests (loader, isolation,
error model, the golden set), its own lint and its own CI job
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). `scripts/check-standalone.sh`
proves the Go package builds from a bare copy with nothing beside it — the tree a
consumer's `go get` produces. The SDKs' server-truth suites (fixture replays,
version-specific behaviour) live with the proof in the core repository and run
against this one as a sibling checkout; a change to an SDK is finished when
both are green.

**Testing, with and without artifacts.** CI here runs on GitHub's hosted
runners with no repository variable and no secret, and runs every suite
twice. First with *no* artifact on the search path: the fetch/verify/install
contract against `spec/fixtures/fetch`, the CLIs and the pure units (result
documents, transform classification, error shaping, the search path) run in
full, and every test that needs an artifact is *skipped by name* with the one
command that would fill the gap — a suite that skipped everything fails,
because `scripts/check-suite.sh` and `scripts/check-standalone.sh` read the
runners' own summary lines. Then once more with two published lines (the
newest `-lts` and `-stable` for `linux-amd64`, fetched by `scripts/fetch.sh`
exactly as a consumer would and cached until the release changes), where the
golden set and the registry tests must run. The bulk proof — the server-truth
suites, the oracle, every supported line — is the core repository's `certify`
workflow against this same tree. Locally, `scripts/fetch.sh 25.8` fills the
per-user cache and the plain commands then run everything;
`scripts/check-suite.sh --no-artifacts <lang>` reproduces the artifact-free
runner.

The C header is owned here. An ABI change lands in `include/chtypes.h` and the
four bindings together (the frozen numbers — `enum chs_format`, `CHS_DOC_*`,
`CHS_ABI_REVISION` — are pinned in each binding and cross-checked by CI), and the
core repository pulls the header from here.

Pre-1.0: the artifact name `libchtypes`, the `chs_` prefix and the `enum
chs_format` numbers are frozen; function signatures freeze at the first tag.
