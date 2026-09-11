# chtypes — Rust SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through `libloading`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Unix only — the loader is `dlopen`.

## Install

Two things: this crate, and at least one **artifact** — the per-version native library it `dlopen`s at runtime.

```sh
cargo add chtypes
cargo install chtypes && chtypes fetch 25.8
```

The fetch lands in `~/.cache/chtypes/artifacts/<os>-<arch>/25.8/` — the per-user cache every chtypes binding reads by default — after checking an ed25519 signature over the release and the sha256 of every byte. `$CHTYPES_REGISTRY` overrides it.

The `fetch` feature is on by default and carries the binary, `ensure` and autofetch. `default-features = false` drops it and every dependency it brings (`ed25519-dalek`, `sha2`, `ureq`, `base64`, `tar`, `flate2`), leaving the loader alone.

## Quickstart

```rust
use chtypes::{Format, Registry, NO_SETTINGS};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let registry = Registry::from_search_path();  // walks the search path
    let lib = registry.for_version("25.8")?;      // a line or an exact patch; never a nearest match
    let schema = lib.compile("x UInt8, ts DateTime DEFAULT now()").compile()?;

    let batch = schema.rows(Format::JsonEachRow, br#"{"x":256}"#, NO_SETTINGS)?;
    let row = &batch.rows[0];
    println!("{:?}", batch.outcome);           // Accepted
    println!("{}", row.values[0].text);        // 0             — what would actually be stored
    println!("{}", row.transformed[0].reason); // overflow_wrap — which is the product
    println!("{:?}", row.substituted);         // ts: send it explicitly, or preview != stored
    Ok(())
}
```

The row is **accepted** and `256` is silently stored as `0`. That report — `transformed` — is the one derived answer in the system and the reason it exists.

`ts` was substituted rather than stored: send every substituted column as an explicit value in the real INSERT, or the server re-evaluates `now()` at its own instant and your preview is not what landed.

## Three outcomes, and conflating any two is a bug

**The verdict is in the `Ok` value.** A row a server would reject is `Ok` with `Outcome::Rejected`, carrying ClickHouse's own code and message. The `Err` arm is for the machinery — loading, marshaling, an unreadable document — and for schema-level answers.

- `Error::Schema` — the server itself would refuse this. `Error::code()` is a real ClickHouse code, as an `Option<i32>`.
- `Error::Unsupported` / `Outcome::Unsupported` (code `-2`) — this build declines to guess, and a real server might well have accepted. **Fall back to the server**; a decline is neither an acceptance nor a rejection.

## Documentation

`cargo doc --no-deps --open` is the full reference — every public item is documented (`#![deny(missing_docs)]`), including which `Error` variant each call can produce.

|                                                                                                                                                                             |                                                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| [Quickstart](https://github.com/wave-rf/chtypes/blob/main/docs/quickstart.md)                                                                                               | the same program in all four languages                                    |
| [Rust API reference](https://github.com/wave-rf/chtypes/blob/main/docs/reference/rust.md)                                                                                   | the map of the crate, with the C entry point under each item              |
| [Artifacts](https://github.com/wave-rf/chtypes/blob/main/docs/guides/artifacts.md)                                                                                          | getting one, where it lands, verifying and pinning it                     |
| [Batches](https://github.com/wave-rf/chtypes/blob/main/docs/guides/batches.md)                                                                                              | always `rows`, and the two bad-row policies                               |
| [Transformations](https://github.com/wave-rf/chtypes/blob/main/docs/guides/transformations.md)                                                                              | the silent-change report, and the DEFAULTs you must echo back             |
| [Settings](https://github.com/wave-rf/chtypes/blob/main/docs/guides/settings.md) · [Discovery](https://github.com/wave-rf/chtypes/blob/main/docs/guides/discovery.md)       | the four channels; asking a real server what profile to validate under    |
| [Filters](https://github.com/wave-rf/chtypes/blob/main/docs/guides/filters.md) · [Multi-version](https://github.com/wave-rf/chtypes/blob/main/docs/guides/multi-version.md) | boolean expressions over rows; several ClickHouse versions in one process |
| [Support matrix](https://github.com/wave-rf/chtypes/blob/main/docs/support.md) · [Limitations](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md)            | what works where; what chtypes declines to answer                         |

Where this crate and the normative spec disagree, **the spec wins**.

## Three things specific to this binding

**Two settings shapes, and the difference will catch you once.** The compile builder's `.settings(...)` takes anything iterable, so `[("k", "v")]` is fine. `rows`, `row_with_settings` and `Filter::rows` take `&[(K, V)]` — a **slice reference** — so write `&[("k", "v")]`, or `NO_SETTINGS` for the empty case.

**Lifetimes do the work other bindings do at runtime.** A `Filter` and a `Block` borrow their `Schema`, so freeing the schema first does not compile. `Schema` is `Send` and deliberately **not** `Sync`: one native handle must not reach two threads, and the type system enforces it. Parallelism comes from more schemas, not shared ones. `Library` and `Registry` are `Send + Sync` — share them freely.

**Two constructors, two behaviors.** `Registry::from_search_path()` is lazy and walks the search path, loading one line per open. `Registry::new(dir)` is the single-directory loader: eager, that directory only, answering `Error::NoSuchVersion` for a line it lacks.

## Tests

`cargo test`. Integration tests and the golden set **skip loudly** without a registry; `CHTYPES_REGISTRY=/path/to/registry cargo test` runs them. The fetch suite (`cargo test --test fetch`) needs only `tests/fixtures/fetch/` and runs offline, reading its verdicts from the fixtures' own `expected.json`.

`cargo run --example demo` is the product in one screen: overflow, a pinned `now()`, TTL, a rejection and a decline.

## License

Apache 2.0. The artifacts this crate loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
