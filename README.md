<h1 align="center">chtypes</h1>

<p align="center">
  <strong>ClickHouse's own type system, as a library you can call.</strong>
</p>

<p align="center">
  Validate, coerce and preview rows <em>before</em> they reach a server —<br>
  answered by ClickHouse's real C++, not a model of it.
</p>

<p align="center">
  <a href="https://github.com/Wave-RF/chtypes/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Wave-RF/chtypes/ci.yml?branch=main&label=CI&color=%2306B0BF"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-%23086D77"></a>
  <a href="https://pkg.go.dev/github.com/wave-rf/chtypes/go"><img alt="Go" src="https://img.shields.io/badge/Go-pkg.go.dev-%2306B0BF?logo=go&logoColor=white"></a>
  <a href="https://pypi.org/project/chtypes/"><img alt="PyPI" src="https://img.shields.io/pypi/v/chtypes?label=PyPI&color=%2306B0BF&logo=pypi&logoColor=white"></a>
  <a href="https://www.npmjs.com/package/@wavehouse/chtypes"><img alt="npm" src="https://img.shields.io/npm/v/%40wavehouse%2Fchtypes?label=npm&color=%2306B0BF&logo=npm&logoColor=white"></a>
  <a href="https://crates.io/crates/chtypes"><img alt="crates.io" src="https://img.shields.io/crates/v/chtypes?label=crates.io&color=%2306B0BF&logo=rust&logoColor=white"></a>
</p>

<p align="center">
  <a href="docs/">Docs</a> ·
  <a href="#install">Install</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="docs/support.md">Supported versions</a> ·
  <a href="#how-it-compares">How it compares</a> ·
  <a href="examples/">Examples</a>
</p>

---

_"What does ClickHouse do with `256` into a `UInt8`?"_ has exactly one correct answer, and it is whatever ClickHouse's code does — which changes between releases. chtypes compiles ClickHouse's real C++ (`DataTypeFactory`, `ISerialization`, `ReadHelpers`, `evaluateMissingDefaults`, the MergeTree insert-time merge) per release into a native artifact behind a small frozen C ABI, and hands it to Go, Python, TypeScript and Rust. Nothing semantic is reimplemented in any binding.

```text
row + schema + ClickHouse version  ─▶  accepted / rejected / poisoned
                                       the stored value, byte for byte
                                       every silent change, named
```

That last line is the point. ClickHouse will accept `256` into a `UInt8`, store `0`, and never mention it. chtypes names it.

## Install

Two things, always: the **binding** for your language, and at least one **artifact** — the per-version native library it loads. The binding is small; the artifact is real ClickHouse, compiled.

```sh
go get github.com/wave-rf/chtypes/go     # Go
uv add chtypes                           # Python  (or: pip install chtypes)
pnpm add @wavehouse/chtypes              # TypeScript
cargo add chtypes                        # Rust
```

Then fetch an artifact — verified, into the per-user cache every binding reads by default. Each binding ships the same command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
python -m chtypes fetch 25.8
npx @wavehouse/chtypes fetch 25.8
cargo install chtypes && chtypes fetch 25.8
```

An ed25519 signature over the release and the sha256 of every byte are checked before anything lands. `fetch --all` takes every published line. Full details: [docs/install.md](docs/install.md).

## Quickstart

One artifact, one schema, one row — the same program in each language. The row is **accepted**, and `256` is silently stored as `0`.

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
schema.close();                         // or `using schema = …` on Node >= 24
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

A bad row is a **verdict, not an error**: `outcome` becomes `rejected`, carrying ClickHouse's own error code and message. Exceptions (or the `Err` arm) are for the machinery — a missing artifact, an unreadable document. [Full quickstart](docs/quickstart.md) · [the same tour, runnable, in all four languages](examples/)

## How it compares

|                                 | chtypes                          | hand-rolled validation       | round-trip to a real server    |
| ------------------------------- | -------------------------------- | ---------------------------- | ------------------------------ |
| Exact ClickHouse semantics      | ClickHouse's own C++             | an approximation that drifts | yes                            |
| Answers before the insert       | yes                              | yes                          | no — the row has already gone  |
| Names what was silently changed | yes, every coercion              | no                           | no                             |
| Per-release answers             | one artifact per ClickHouse line | no                           | only that one server's version |
| Cost per row                    | in-process call                  | in-process call              | a network round trip           |
| Needs a running ClickHouse      | no                               | no                           | yes                            |

## The guarantees every binding is held to

- **`Transformed` is the product.** ClickHouse never says _"I changed your value"_; chtypes derives that report (`overflow_wrap`, `date_clamp`, `poisoned`, `ttl_expired`, …) and it is not optional.
- **Over-accepts and over-rejects are both budgeted at zero** in the proof behind the artifacts: a row accepted here and rejected by the server ships before the insert fails; a row rejected here and accepted by the server is silent data loss.
- **`unsupported` is an answer, never a guess.** A binding surfaces the library's decline; it never papers over one.
- **The four bindings give one answer.** The golden set is run by all of them, and each is a scored column in the core repository's arbiter at the same agreement as the reference.

## What is supported

Three axes — the language you call from, the platform you run on, and the ClickHouse line you want answers for. **[docs/support.md](docs/support.md)** carries the full matrix, generated from this tree's manifests and the release's own index so it cannot drift from what actually ships.

The short version: Go, Python, TypeScript and Rust; `linux-amd64`, `linux-arm64` and `darwin-arm64` (Unix only — both loaders are `dlopen`); and every ClickHouse line with a committed run of record, today spanning 24.8 through 26.8 and growing as releases are certified.

## Documentation

|                                                               |                                                                                            |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [Install](docs/install.md) · [Quickstart](docs/quickstart.md) | getting a binding and an artifact, and the first program                                   |
| [Guides](docs/guides/)                                        | artifacts, fetching, settings, batches, filters, discovery, multi-version, transformations |
| [Reference](docs/reference/)                                  | per-language API, the C ABI contract, the binding contract                                 |
| [Supported versions](docs/support.md)                         | languages, platforms, ClickHouse lines                                                     |
| [Examples](examples/)                                         | four side-by-side runnable tours, same sections in every language                          |

## Project status

Pre-1.0, and published: Go, PyPI, npm and crates.io all carry `0.1.1`. The C ABI is frozen at revision 4 (`include/chtypes.h`, 28 `chs_*` functions) and the four bindings pin that number at compile time. Package names, the artifact name `libchtypes`, the `chs_` prefix and the `enum chs_format` numbers are frozen; function signatures froze at the first tag. Anything else may still move — each binding keeps its own CHANGELOG.

This repository is the **SDK half** of chtypes, Apache 2.0. The other half — the C++ wrapper, the per-version vendoring and build pipeline, the artifacts themselves, and the differential proof (tens of thousands of cases scored against real ClickHouse servers on every supported version) — is the core repository, under its own license. The bindings here contain no ClickHouse code: they load an artifact and speak the ABI. Artifacts carry their own license; see the `LICENSE` inside each release.

## Contributing

Issues and pull requests are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md). A change to one binding's behavior lands in all four in the same cycle; that is the contract, not a preference. Report security issues privately: [SECURITY.md](SECURITY.md).

## AI-assisted development

Much of this repository was written with AI assistance and reviewed by a human before merge. The review gate is the same regardless of who or what authored a change, and the test suites are deliberately built to refuse a silent pass — every artifact-dependent test skips loudly by name, and a suite that ran nothing fails. If you find documentation that drifted from the code, please open an issue; that is the failure mode we most want reported.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
