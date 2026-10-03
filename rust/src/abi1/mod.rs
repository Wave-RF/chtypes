//! ABI v1's Rust FFI layer, behind the `abi-v1` feature.
//!
//! `decls` and `invoke_gen` are generated from `spec/abi-v1/abi.json` by
//! `scripts/abi-v1/emit/rust.py` — see that module's own docstring for what
//! each half owns and why they are two files. `loader` is hand-written: the
//! plan's "only three things stay hand-written" rule (the loader, the
//! conformance runner, and — outside this module's v1 FFI scope — the public
//! API).
//!
//! `calls_gen` (the typed, copy-then-free call wrappers and the handle
//! objects), `vocab_gen` (every vocabulary) and `errmap_gen` (the error-class
//! tables) are generated too. The crate's public API (`rust/src/lib.rs` and
//! its modules) calls only those prefix-less wrappers, never a raw symbol.
//!
//! The conformance runner (`rust/tests/abi1_conformance.rs`) does not reach
//! these modules through the crate's public items: an integration test is a
//! separate crate. Instead it re-declares the module tree it needs with
//! `#[path = "../src/abi1/<file>.rs"]`, so those files are compiled twice, from
//! the identical source.

#![allow(dead_code)]

pub(crate) mod calls_gen;
pub(crate) mod decls;
pub(crate) mod errmap_gen;
mod invoke_gen;
pub(crate) mod loader;
pub(crate) mod vocab_gen;
