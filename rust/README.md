# chtypes — Rust SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through `libloading`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Unix only — the loader is `dlopen`.

## Install

Two things: this crate, and at least one **artifact**, the per-version native library it `dlopen`s at runtime. The registry fetches, verifies and caches artifacts through the v1 fetch layer (an OCI registry, a signed statement per build), and `CHTYPES_AUTOFETCH=1` lets it fetch a version nothing installed answers.

```sh
cargo add chtypes
cargo install chtypes     # the `chtypes` command: fetch, verify, list, where
chtypes fetch 26.8
```

## Quickstart

```rust
use chtypes::{CompileOptions, Format, Registry, RegistryOptions, RowsOptions};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Optional, and first: the image zone and default settings are fixed once per process.
    chtypes::setup(chtypes::SetupOptions::default())?;

    let registry = Registry::new(RegistryOptions::default())?;
    let lib = registry.for_version("26.8")?;       // a line or an exact patch; never a nearest match
    let schema = lib.compile_table(
        "CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x",
        &CompileOptions::default(),
    )?;

    let batch = schema.rows(Format::JsonEachRow, br#"{"x":256}"#, &RowsOptions::default())?;
    let row = &batch.rows[0];
    println!("{}", batch.outcome);                // accepted
    println!("{}", row.values[0].text);           // 0             what would actually be stored
    println!("{}", row.transformed[0].reason);    // overflow_wrap which is the product
    Ok(())
}
```

The row is **accepted** and `256` is silently stored as `0`. That report, `transformed`, is the reason the product exists, and it is computed by the library, never by this crate.

## Four call classes, and conflating any two is a bug

**The verdict is in the `Ok` value.** A row a server would reject is `Ok` with `Outcome::Rejected`, carrying ClickHouse's own code and message. The `Err` arm is for the machinery and for the library's own refusals.

- `Error::Schema`: the server itself would refuse this. `ch_code` is a real ClickHouse code.
- `Error::Unsupported`: this build declines to guess, and a real server might well have accepted. **Fall back to the server**; a decline is neither an acceptance nor a rejection.
- `Error::Usage` is misuse (a closed or refused input); `Error::Internal` is a library bug.

## Documentation

`cargo doc --no-deps --open` is the full reference — every public item is documented (`#![deny(missing_docs)]`), including which `Error` variant each call can produce.

- [`docs/reference/bindings-v1.md`](https://github.com/wave-rf/chtypes/blob/v1/docs/reference/bindings-v1.md): the contract, every operation with its C entry point.
- [`docs/reference/rust.md`](https://github.com/wave-rf/chtypes/blob/v1/docs/reference/rust.md): the Rust spelling of it.
- [`docs/guides/fetch-v1.md`](https://github.com/wave-rf/chtypes/blob/v1/docs/guides/fetch-v1.md): getting an artifact, and what is verified.

Where this crate and the normative spec disagree, **the spec wins**.

## Three things specific to this binding

**Handles are cheap to clone and free themselves.** `Schema`, `Filter` and `Block` are `Clone + Send + Sync + 'static`: a clone shares one native handle, freed when the last clone drops, and any number of threads may call one handle at once. `Library` and `Registry` are `Send + Sync`. A library is never unloaded.

**Bytes out are `RawText`.** The bytes are authoritative and the UTF-8 view is fallible, so a column name or a stored value is never silently repaired.

**Option structs, not builders.** Every call takes an options struct with public fields and `Default` (`RowOptions { session_timezone: Some(z), ..Default::default() }`), and settings are `Vec<(String, String)>`: values are strings, and only strings.

## Tests

`cargo test`. The suite that needs the stub libraries (`CHTYPES_ABI1_STUBS`), the fetch fixtures (`CHTYPES_V1_CONFORMANCE`) and the goldens runner (`CHTYPES_GOLDENS_REGISTRY_BASE`) each skips loudly without its inputs; `scripts/abi-v1/build-stubs.sh --out DIR` builds the stubs.

`cargo run --example demo` is the product in one screen.

## License

Apache 2.0. The artifacts this crate loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
