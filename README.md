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
  <a href="docs/support-v2.md">Supported versions</a> ·
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

Two things, always: the **binding** for your language, and at least one **library** — the per-version native library it loads. The binding is small; the library is real ClickHouse, compiled.

```sh
go get github.com/wave-rf/chtypes/go     # Go
uv add chtypes                           # Python  (or: pip install chtypes)
pnpm add @wavehouse/chtypes              # TypeScript
cargo add chtypes                        # Rust
```

Then fetch a library: resolved over OCI, verified, and unpacked into the per-user cache every binding reads by default. Each binding ships the same command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 26.8
python -m chtypes fetch 26.8
npx @wavehouse/chtypes fetch 26.8
cargo install chtypes && chtypes fetch 26.8
```

A signature over the library's statement and the sha256 of every byte are checked before anything is unpacked, and the library's own build record is checked again when it loads. `chtypes list` shows the lines the registry publishes, and [docs/support-v2.md](docs/support-v2.md) is generated from the registry's signed statements, each verified before anything is read from it.

## Quickstart

One library, one schema, one row — the same program in each language. The row is **accepted**, and `256` is silently stored as `0`.

<details open><summary><b>Go</b></summary>

```go
reg, _ := chtypes.NewRegistry()
lib, _ := reg.For("26.8")                      // a line or an exact patch; never a nearest match
cs, _ := lib.CompileTable("CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY x")
defer cs.Close()

r, _ := cs.Row(chtypes.JSONEachRow, []byte(`{"x":256}`))
fmt.Println(r.Outcome)                // accepted
fmt.Println(r.Transformed[0].Reason)  // overflow_wrap — 256 stored as 0, silently
fmt.Println(r.Columns[1].Source)      // ts: default_substituted — send it explicitly, or preview != stored
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import Format, Registry

registry = Registry()
library = registry.for_version("26.8")
ddl = b"CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY x"

with library.compile_table(ddl) as schema:
    r = schema.row(Format.JSON_EACH_ROW, b'{"x":256}\n')

r.outcome                     # Outcome.ACCEPTED
r.values[0].text              # b'0'            — what would actually be stored
r.transformed[0].reason       # 'overflow_wrap' — which is the product
r.columns[1].source           # ts: 'default_substituted' — send it explicitly, or preview != stored
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Format, Registry } from '@wavehouse/chtypes';

const registry = await Registry.open();
const lib = await registry.for('26.8');
const schema = lib.compileTable('CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY x');

const r = schema.row(Format.JSONEachRow, Buffer.from('{"x":256}'));
console.log(r.outcome);                 // accepted
console.log(r.transformed[0]?.reason);  // overflow_wrap — 256 stored as 0, silently
console.log(r.columns[1]?.source);      // default_substituted — send ts explicitly in the real INSERT
schema.close();                         // or `using schema = …` on Node >= 24
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{CompileOptions, Format, Registry, RegistryOptions, RowOptions};

let registry = Registry::new(RegistryOptions::default())?;
let lib = registry.for_version("26.8")?;
let ddl = "CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY x";
let schema = lib.compile_table(ddl, &CompileOptions::default())?;

let r = schema.row(Format::JsonEachRow, br#"{"x":256}"#, &RowOptions::default())?;
assert_eq!(r.outcome, chtypes::Outcome::Accepted);
assert_eq!(r.values[0].text, b"0");                            // what would be stored
assert_eq!(r.transformed[0].reason, chtypes::reason::OVERFLOW_WRAP);
```

</details>

A bad row is a **verdict, not an error**: `outcome` becomes `rejected`, carrying ClickHouse's own error code and message. Exceptions (or the `Err` arm) are for the machinery — a missing library, an unreadable document. [Full quickstart](docs/quickstart.md) · [the same tour, runnable, in all four languages](examples/)

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
- **Over-accepts and over-rejects have no budget** — a non-zero count is refused unless a person has named that case and recorded why, with a tracking reference. Known cases exist and are registered individually rather than absorbed into an allowance; what that does and does not promise: [`docs/limitations.md`](docs/limitations.md#the-error-model-is-normative).
- **`unsupported` is an answer, never a guess.** A binding surfaces the library's decline; it never papers over one.
- **The four bindings give one answer.** The golden set — a published file of expected answers every binding must reproduce — is run by all of them, and each is scored against real ClickHouse servers at the same agreement as the reference.

## What is supported

Three axes — the language you call from, the platform you run on, and the ClickHouse line you want answers for. **[docs/support-v2.md](docs/support-v2.md)** is generated from the registry's verified statements: Go, Python, TypeScript and Rust; `linux-amd64`, `linux-arm64` and `darwin-arm64` (Unix only — the loaders are `dlopen`); and, per line and platform, the exact ClickHouse version, build and glibc floor the registry publishes. Where the page does not know something it says **support unknown**, never unsupported. The 1.x line's page, written by hand, is [docs/support-v1.md](docs/support-v1.md).

## Known gaps in 1.0

A short list of places where 1.0 does less than you might expect, each with what happens, a workaround and a note that a fix is planned: nested `String` values carry no raw bytes, binary `settings` and `query_params` values must be UTF-8, zone names follow the host's zone files, `SHOW CREATE` of a Memory table is declined, a batch preview with a filter in another zone is declined, per-call settings values are not validated like `SET`, `lossy` on a raw NUL in TSV, and no call for a server's version or settings. The full text is in **[docs/limitations.md](docs/limitations.md#known-gaps-in-10)**.

## Documentation

|                                                               |                                                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| [Install](docs/install.md) · [Quickstart](docs/quickstart.md) | getting a binding and a library, and the first program                          |
| [Guides](docs/guides/)                                        | fetching, settings, batches, filters, discovery, multi-version, transformations |
| [Reference](docs/reference/)                                  | per-language API, the C ABI contract, the binding contract                      |
| [Supported versions](docs/support-v2.md)                      | languages, platforms, ClickHouse lines                                          |
| [Examples](examples/)                                         | four side-by-side runnable tours, same sections in every language               |

## Project status

The `v1` line is the 1.0 release in progress, and the ABI stays provisional until it is confirmed. The badges above read the live version from each registry. What that means for you is in [`docs/support-v1.md`](docs/support-v1.md#pre-10).

This repository is the **SDK half** of chtypes, Apache 2.0. The other half — the C++ wrapper, the per-version vendoring and build pipeline, the libraries themselves, and the differential proof (tens of thousands of cases scored against real ClickHouse servers on every supported version) — belongs to the artifact producer, under its own license. The bindings here contain no ClickHouse code: they load a library and speak the ABI. Libraries carry their own license; see the `LICENSE` inside each one.

## Contributing

Issues and pull requests are welcome — start with [CONTRIBUTING.md](CONTRIBUTING.md). A change to one binding's behavior lands in all four in the same cycle; that is the contract, not a preference. Report security issues privately: [SECURITY.md](SECURITY.md).

## AI-assisted development

Much of this repository was written with AI assistance and reviewed by a human before merge. The review gate is the same regardless of who or what authored a change, and the test suites are deliberately built to refuse a silent pass — every artifact-dependent test skips loudly by name, and a suite that ran nothing fails. If you find documentation that drifted from the code, please open an issue; that is the failure mode we most want reported.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
