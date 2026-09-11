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

This repository is the **SDK half** of chtypes, Apache 2.0 — four bindings, the
C ABI they share, and the normative spec. The other half — the C++ wrapper, the per-version vendoring and build
pipeline, the artifacts themselves, and the differential proof (tens of
thousands of cases scored against real ClickHouse servers on every supported
version) — is the core repository, `chtypes-core`, under its own licence. The
SDKs here contain no ClickHouse code: they load an artifact and speak the ABI.

## Install

Two things, always: the **binding** for your language, and at least one
**artifact** — the per-version native library it loads. The binding is small;
the artifact is real ClickHouse, compiled.

**1. The binding.**

```sh
go get github.com/wave-rf/chtypes/go     # Go
uv add chtypes                           # Python  (or: pip install chtypes)
pnpm add @wavehouse/chtypes              # TypeScript
cargo add chtypes                        # Rust
```

**2. An artifact**, verified into the per-user cache every binding reads by
default. Each binding ships the same command, so you need nothing from this
repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
python -m chtypes fetch 25.8
npx @wavehouse/chtypes fetch 25.8
cargo install chtypes && chtypes fetch 25.8
```

That installs into `~/.cache/chtypes/artifacts/<os>-<arch>/25.8/`
(`$CHTYPES_REGISTRY` overrides), checking an ed25519 signature over the release
and the sha256 of every byte before anything lands. `fetch --all` takes every
published line. From a checkout, [`scripts/fetch.sh`](scripts/fetch.sh) is the
reference implementation of the same contract.

## Quickstart

One artifact, one schema, one row — the same program in each language. It shows
the thing chtypes exists for: the row is **accepted**, and `256` is silently
stored as `0`.

<details open><summary><b>Go</b></summary>

```go
reg, _ := chtypes.NewRegistry(chtypes.DefaultRegistryDir())
lib, _ := reg.For("25.8")                      // a line or an exact patch; never a nearest match
cs, _ := lib.CompileDDL("x UInt8, ts DateTime DEFAULT now()")
defer cs.Close()

r, _ := cs.Row(chtypes.JSONEachRow, []byte(`{"x":256}`))
fmt.Println(r.Outcome)                // accepted
fmt.Println(r.Transformed[0].Reason)  // overflow_wrap — 256 stored as 0, silently
fmt.Println(r.Substituted)            // ts: send it explicitly, or preview != stored
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import Format, Registry

registry = Registry()
library = registry.for_version("25.8")

with library.compile_ddl("x UInt8, ts DateTime DEFAULT now()") as schema:
    r = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')

r.outcome                     # Outcome.ACCEPTED
r.rows[0].value("x").text     # '0'             — what would actually be stored
r.transformed[0].reason       # 'overflow_wrap' — which is the product
r.rows[0].substituted         # ts: send it explicitly, or preview != stored
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Format, Registry } from '@wavehouse/chtypes';

const registry = new Registry();
const lib = registry.for('25.8');
const schema = lib.compileDdl('x UInt8, ts DateTime DEFAULT now()');

const r = schema.row(Format.JSONEachRow, Buffer.from('{"x":256}'));
console.log(r.outcome);                 // accepted
console.log(r.transformed[0]?.reason);  // overflow_wrap — 256 stored as 0, silently
console.log(r.substituted[0]?.column);  // ts — send it explicitly in the real INSERT
schema.close();                         // or `using schema = …` — see below
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{Format, Registry, NO_SETTINGS};

let registry = Registry::from_search_path();
let lib = registry.for_version("25.8")?;
let schema = lib.compile("x UInt8, ts DateTime DEFAULT now()").compile()?;

let r = schema.rows(Format::JsonEachRow, br#"{"x":256}"#, NO_SETTINGS)?;
assert_eq!(r.outcome, chtypes::Outcome::Accepted);
assert_eq!(r.rows[0].values[0].text, "0");                      // what would be stored
assert_eq!(r.transformed[0].reason, chtypes::reason::OVERFLOW_WRAP);
```

</details>

Each binding frees the schema its own way: Go `defer Close()`, Python the
context manager, Rust on drop. TypeScript has both — `schema.close()` works
everywhere, and `using schema = lib.compileDdl(…)` is the nicer form once
TypeScript downlevels it for you or you are on Node ≥ 24 (it is a syntax error
in plain JavaScript on Node 22, this package's floor).

A bad row is a **verdict, not an error**: `outcome` becomes `rejected` with
ClickHouse's own error code and message. Exceptions (or the `Err` arm) are for
the machinery — a missing artifact, an unreadable document — and for
schema-level answers. Each binding's README has the full API table and its
language's idioms; [`examples/`](examples/README.md) is the same guided
tour, section for section, in all four.

## What is supported

Three axes — the language you call from, the platform you run on, and the
ClickHouse line you want answers for. **[`docs/support.md`](docs/support.md)**
has the full matrix, generated from the four manifests in this tree and from
the release's own index, so it cannot drift from what actually ships.

The short version: Go, Python, TypeScript and Rust; `linux-amd64`,
`linux-arm64` and `darwin-arm64` (Unix only — both loaders are `dlopen`); and
every ClickHouse line with a committed run of record in the core repository,
which today spans 24.8 through 26.8 and grows as core certifies releases.

## Artifacts

An SDK answers nothing by itself. It `dlopen`s one **artifact** per ClickHouse
release — a self-contained shared library, 160–300 MB, that names itself
(`chs_clickhouse_version()`) and carries its own `manifest.json` — and can hold
several versions in one process, each with its own ClickHouse. The supported
lines are those with a committed run of record in the core repository, on
`linux-amd64`, `linux-arm64` and `darwin-arm64` (macOS is a development floor,
not an oracle: its `long double` makes float parses diverge from a real
server). Which lines those are is a question
[`index.json`](https://artifacts.wavehouse.dev/artifacts/index.json) answers —
`scripts/fetch.sh --all` reads it rather than restating a list — because core
adds a line whenever it certifies one, and a list written here goes stale.

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

## What is in this repository

| | |
|---|---|
| [`go/`](go/README.md) | `github.com/wave-rf/chtypes/go` — package `chtypes`, cgo `dlopen`, dlopen-only by default |
| [`python/`](python/README.md) | `chtypes` — stdlib `ctypes`, zero dependencies |
| [`ts/`](ts/README.md) | `@wavehouse/chtypes` — `ffi-rs`, Node ≥ 22 |
| [`rust/`](rust/README.md) | `chtypes` — `libloading` |
| [`include/chtypes.h`](include/chtypes.h) | the C ABI every binding is written against — 28 `chs_*` functions, ABI revision 4 |
| [`spec/`](spec/README.md) | the normative contract: [`c-abi.md`](docs/reference/c-abi.md), [`bindings.md`](docs/reference/bindings.md) (the shape every SDK implements), [`artifact.md`](docs/reference/artifact.md) (what ships, how a registry is laid out) |
| [`goldens/`](docs/reference/goldens.md) | the public golden set — served by core, not tracked here; one answer in four languages |
| [`examples/`](examples/README.md) | four side-by-side runnable tours, same sections in every language |
| [`docs/artifacts.md`](docs/artifacts.md) | how a consumer obtains and verifies artifacts ([`docs/fetch.md`](docs/fetch.md): the fetch/verify/signing contract every SDK implements) |
| [`scripts/fetch.sh`](scripts/fetch.sh) | the verified download into the per-user cache |

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
contract against `tests/fixtures/fetch`, the CLIs and the pure units (result
documents, transform classification, error shaping, the search path) run in
full, and every test that needs an artifact is *skipped by name* with the one
command that would fill the gap — a suite that skipped everything fails,
because `scripts/check-suite.sh` and `scripts/check-standalone.sh` read the
runners' own summary lines. Then once more with two published lines (the
newest `-lts` and `-stable` for `linux-amd64`, fetched by `scripts/fetch.sh`
exactly as a consumer would and cached until the release changes), where the
golden set and the registry tests must run. The bulk proof — the server-truth
suites, the oracle, every supported line — is the core repository's `sdk-suites`
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
