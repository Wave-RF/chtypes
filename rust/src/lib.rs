//! chtypes — ClickHouse's own type system, per ClickHouse version, from Rust.
//!
//! chtypes answers one question, exactly: **if this row were inserted into this
//! ClickHouse table on this ClickHouse version, what would happen?** It answers
//! it by running ClickHouse's own C++ machinery, vendored per release and
//! linked behind the `chs_*` C ABI. Nothing here reimplements a coercion rule,
//! which is why the answers are exact by construction: this crate is a thin
//! passthrough, and every public call makes exactly one ABI call.
//!
//! The language-neutral contract is `docs/reference/bindings-v1.md` in this
//! repository; where this crate and that page disagree, the page wins and this
//! is a bug.
//!
//! ```no_run
//! use chtypes::{Format, Registry, RegistryOptions, RowsOptions, CompileOptions};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! // Optional, and first: the image zone and the default settings are fixed
//! // once per process. See `setup`.
//! chtypes::setup(chtypes::SetupOptions::default())?;
//!
//! let registry = Registry::new(RegistryOptions::default())?;
//! let lib = registry.for_version("26.8")?;
//! let schema = lib.compile_table(
//!     "CREATE TABLE t (ts DateTime, seq UInt8) ENGINE = MergeTree ORDER BY ts",
//!     &CompileOptions::default(),
//! )?;
//!
//! let batch = schema.rows(
//!     Format::JsonEachRow,
//!     br#"{"ts":"2026-01-15 10:30:00","seq":256}"#,
//!     &RowsOptions::default(),
//! )?;
//! println!("{}", batch.outcome);
//! for t in &batch.transformed {
//!     // seq: 256 -> 0, overflow_wrap, lossy, and ClickHouse returned success.
//!     println!("row {} {}: {} -> {}", t.row, t.column, t.input, t.stored);
//! }
//! # Ok(()) }
//! ```
//!
//! # What this crate will not do
//!
//! * **Never map [`Error::Unsupported`] onto a rejection or an acceptance.** A
//!   decline means "a real server might well have accepted this; this build will
//!   not answer". Mapping it to a rejection manufactures an over-reject;
//!   mapping it to an acceptance manufactures an over-accept.
//! * **Never treat bytes as text.** A column name, a message and a rendered
//!   value are [`RawText`]: the bytes are authoritative and the UTF-8 view is
//!   fallible.
//! * **Never rewrite a setting.** Settings values are strings, and only
//!   strings; they cross the boundary as the caller wrote them.
//! * **Never insert the original body when a row carries a generated default.**
//!   A value whose [`Value::source`] is [`source::DEFAULT_GENERATED`] is stored
//!   only if the caller inserts the library's own output: ask `rows` for an
//!   export in the format you will insert ([`RowsOptions::export`]) and insert
//!   [`BatchResult::payload`].
//!
//! # Threads
//!
//! Every object is safe to share. [`Registry`] and [`Library`] (shared as
//! `Arc<Library>`) are `Send + Sync`; [`Schema`], [`Filter`] and [`Block`] are
//! `Clone + Send + Sync + 'static`, and `&self` methods run concurrently with
//! no lock. A library is never unloaded: dropping the last `Arc<Library>`
//! releases only Rust memory.
//!
//! # Platform
//!
//! Unix only: the loader is `dlopen`. Linux is the shipping target; macOS is a
//! development floor and **not** an oracle (its `long double` is 53-bit, so
//! float parses diverge from a real server).

#![deny(missing_docs)]
#![warn(clippy::undocumented_unsafe_blocks)]

#[cfg(not(unix))]
compile_error!("chtypes loads artifacts with dlopen and supports unix targets only");

mod abi1;
mod decode;
mod error;
mod library;
// The fetch layer's own documentation is its module docs; its record types are
// re-exported below, undocumented field by field.
#[allow(missing_docs)]
mod ocifetch;
mod raw;
mod registry;
mod result;
mod schema;
mod setup;

pub use abi1::vocab_gen::{
    DefaultKind, DocFlags, FilterOutcome, Format, Outcome, Verdict, reason, source, status,
};
pub use error::{CallError, Error, Refusal, Result};
pub use library::Library;
pub use ocifetch::ensure::Resolved;
pub use raw::RawText;
pub use registry::{FetchOptions, Registry, RegistryOptions};
pub use result::{
    BatchResult, BuildInfo, Capabilities, Column, Computed, DiscoveredColumn, Discovery,
    EngineCell, ErrorCodeEntry, ErrorCodeTable, FilterResult, FilterRowError, Framing, Header,
    RowResult, SchemaDescription, Span, Transform, Value,
};
pub use schema::{
    Block, CompileOptions, EvalOptions, Filter, FilterOptions, RowOptions, RowsOptions, Schema,
};
pub use setup::{SetupOptions, setup};

#[cfg(test)]
mod thread_contract {
    use super::*;

    fn clone_send_sync_static<T: Clone + Send + Sync + 'static>() {}
    fn send_sync<T: Send + Sync>() {}

    #[test]
    fn every_object_is_safe_to_share() {
        send_sync::<Registry>();
        send_sync::<Library>();
        send_sync::<std::sync::Arc<Library>>();
        clone_send_sync_static::<Schema>();
        clone_send_sync_static::<Filter>();
        clone_send_sync_static::<Block>();
        // Results, errors and BuildInfo are plain values.
        send_sync::<RowResult>();
        send_sync::<BatchResult>();
        send_sync::<FilterResult>();
        send_sync::<Error>();
        send_sync::<BuildInfo>();
    }
}
