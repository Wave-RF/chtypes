//! ABI v1's Rust FFI layer, behind the `abi-v1` feature.
//!
//! `decls` and `invoke_gen` are generated from `spec/abi-v1/abi.json` by
//! `scripts/abi-v1/emit/rust.py` — see that module's own docstring for what
//! each half owns and why they are two files. `loader` is hand-written: the
//! plan's "only three things stay hand-written" rule (the loader, the
//! conformance runner, and — outside this module's v1 FFI scope — the public
//! API).
//!
//! Nothing here is part of this crate's public API: `src/lib.rs` declares
//! this as `mod abi1;`, not `pub mod abi1;`. The conformance runner
//! (`rust/tests/abi1_conformance.rs`) therefore does not reach these modules
//! through `chtypes::abi1::*` — an integration test is a separate crate with
//! only this crate's PUBLIC items visible, the same as every other test
//! under `rust/tests/` (`rust/tests/abi_revision.rs` reaches only
//! `chtypes::Registry`, for instance). Instead it re-declares this same
//! module tree with `#[path = "../src/abi1/<file>.rs"]`, so `decls.rs`,
//! `invoke_gen.rs` and `loader.rs` are each compiled twice — once here,
//! unreached until wave C wires a public `Library` onto it (hence this
//! module's `#![allow(dead_code)]`s), and once inside the test binary, where
//! the conformance runner actually calls them. Nothing is duplicated BY
//! HAND: both compilations read the identical three files.

#![allow(dead_code)]

mod decls;
mod invoke_gen;
mod loader;
