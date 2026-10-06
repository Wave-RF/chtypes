# examples/rust: the Rust tour

Seventeen sections over the public surface of the v1 API ([`docs/reference/bindings-v1.md`](../../docs/reference/bindings-v1.md)), matching `../go`, `../python` and `../ts` section for section. See [`../README.md`](../README.md) for the section list. A section whose v0 feature the v1 API deletes (bindings-v1.md section 7) keeps its number and says which deletion removed it.

## Run it

```bash
cargo run           # or, with prerequisite checks: ../chplay.sh rust
```

## Prerequisites

- **A real artifact.** The tour opens a release through the v1 fetch layer. Fetch one first with the crate's own CLI (`cargo run --manifest-path ../../rust/Cargo.toml --bin chtypes -- fetch 26.8`), or set `CHTYPES_AUTOFETCH=1` and let the registry fetch what it lacks. The cache is `~/.cache/chtypes/v1` (`CHTYPES_CACHE` overrides it).
- Rust 1.87 or newer (the binding is edition 2024), and a **Unix** host: the loader is `dlopen`.

## Knobs

| variable            | effect                                                             |
| ------------------- | ------------------------------------------------------------------ |
| `CHTYPES_VERSION`   | which line the tour opens (`26.8`, `26.8.15`, ...). Default `26.8` |
| `CHTYPES_AUTOFETCH` | `1` lets the registry fetch a version nothing installed answers    |
| `CHTYPES_CACHE`     | the cache directory. Default: the per-user cache                   |

## What is Rust-specific here

Everything in the tour is the same _concept_ in all four SDKs; these are the places where the Rust spelling is its own.

- **Option structs.** Every call takes an options struct with public fields and `Default` (`RowsOptions { export: Some(Format::JsonCompactEachRow), ..Default::default() }`); settings are `Vec<(String, String)>`.
- **Sibling enum variants.** `Error::Schema` and `Error::Unsupported` are variants of one `#[non_exhaustive]` enum, never a subtype relationship (section 10).
- **Bytes out are `RawText`.** The bytes are authoritative and the UTF-8 view is fallible; the tour prints the lossy `Display` form.
- **Drop frees.** Schema handles free on `Drop`; there is no shutdown (section 13).
