//! chtypes — ClickHouse's own type system, per ClickHouse version, from Rust.
//!
//! chtypes answers one question, exactly: **if this row were inserted into this
//! ClickHouse table on this ClickHouse version, what would happen?** It answers
//! it by running ClickHouse's own C++ machinery — `DataTypeFactory`,
//! `ISerialization`, `ReadHelpers`, `evaluateMissingDefaults`, the TTL
//! algorithms, `MergeTreeDataWriter::mergeBlock` — vendored per release and
//! linked behind the frozen `chs_*` C ABI. Nothing here reimplements a coercion
//! rule, which is why the answers are exact by construction.
//!
//! This crate is a peer SDK over that ABI, alongside Go, Python and TypeScript.
//! The language-neutral contract is `spec/` in this repository; where this crate
//! and `spec/` disagree, the spec wins and this is a bug.
//!
//! ```no_run
//! use chtypes::{Format, Registry, NO_SETTINGS};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let registry = Registry::from_env_or_default()?;   // $CHTYPES_REGISTRY, else the per-user cache
//! let lib = registry.for_version("25.8")?;             // minor line or exact patch
//! let schema = lib.compile("ts DateTime, seq UInt8").compile()?;
//!
//! let batch = schema.rows(
//!     Format::JsonEachRow,
//!     br#"{"ts":"2026-01-15 10:30:00","seq":256}"#,
//!     NO_SETTINGS,
//! )?;
//!
//! println!("{} {:?}", batch.outcome, batch.rows[0].values);
//! for t in &batch.transformed {
//!     // seq: 256 -> 0, overflow_wrap, lossy — and ClickHouse returned success.
//!     println!("row {} {}: {} -> {} ({})", t.row, t.column, t.input, t.stored, t.reason);
//! }
//! # Ok(()) }
//! ```
//!
//! # What this crate will not do
//!
//! * **Never map [`Error::Unsupported`] onto a rejection or an acceptance.**
//!   `-2` ([`CODE_UNSUPPORTED`]) means "a real server might well have accepted
//!   this; I decline to guess". Mapping it to a rejection manufactures an
//!   over-reject; mapping it to an acceptance manufactures an over-accept, which
//!   is the cardinal sin — rows stream to subscribers and then the insert fails.
//! * **Never infer one version's answer from another's.** Behavior is not
//!   monotonic: 25.10 rejects a mixed-type DEFAULT that 24.8 through 25.8 and
//!   26.6 onward all accept; `JSON` is rejected on 24.8 and accepted from 25.3.
//!   [`Registry::for_version`] fails, naming what is loaded, rather than
//!   answering from the nearest artifact.
//! * **Never treat a per-row `accepted` as "stored".** A TTL-expired row is
//!   accepted per row and not stored per batch. [`BatchResult::transformed`]
//!   folds in the batch-level `storage_transforms`, and
//!   [`BatchResult::engine_rows`] — when present — is the stored truth, not
//!   [`BatchResult::rows`].
//! * **Never route a value through a float.** Settings values cross as strings
//!   and stored values stay raw JSON text; `18446744073709551615` must not
//!   become `18446744073709552000`.
//! * **Never decode a stored value into a language type before comparing it.**
//!   A ClickHouse `String` holds arbitrary bytes, so a stored rendering is
//!   [`RawText`] — bytes, with a *fallible* UTF-8 view — and never a `String`
//!   that silently carries U+FFFD where the value had bytes. See [`Value::text`].
//!
//! # Getting artifacts
//!
//! An artifact is one ClickHouse release compiled behind the C ABI — 166–302 MB
//! each, hours of C++ compute. Fetch prebuilt, signed ones with the crate's own
//! command (`cargo install chtypes` → `chtypes fetch 25.8`) or from Rust with
//! [`ensure`] — the `docs/guides/fetch.md` contract, behind the default-on `fetch`
//! feature; `scripts/fetch.sh` is the reference implementation of the same
//! chain. A local build lands in the same per-user cache
//! (`~/.cache/chtypes/artifacts/<os>-<arch>`). [`Registry::from_search_path`]
//! looks there, in `$CHTYPES_REGISTRY` and in the system locations, and names
//! every place it looked when a line is missing ([`Error::ArtifactMissing`]);
//! [`Registry::new`] loads one explicit directory.
//!
//! # Platform
//!
//! Unix only — the loader is `dlopen`. Linux is the shipping target; macOS is a
//! development floor and **not** an oracle: its `long double` is 53-bit, so float
//! parses diverge from a real server (the float corpus matches 395/395 on Linux
//! and 0/395 on macOS). Any float expectation must come from a Linux artifact or
//! a live server.

#![deny(missing_docs)]
#![warn(clippy::undocumented_unsafe_blocks)]

#[cfg(not(unix))]
compile_error!("chtypes loads artifacts with dlopen and supports unix targets only");

mod compile;
mod discover;
mod doc;
mod error;
#[cfg(feature = "fetch")]
pub mod fetch;
mod ffi;
mod json;
mod library;
mod raw;
mod registry;
mod result;
mod schema;
mod transform;

pub use compile::{CompileMode, CompileRequest};
pub use discover::{
    DiscoveredColumn, QUERY_CHANGED_SETTINGS, QUERY_SERVER_VERSION, QUERY_TABLE_COLUMNS,
    ServerProfile, parse_changed_settings_result, parse_columns_result, parse_version_result,
    reconstruct_ddl,
};
pub use error::{
    ABI_REVISION, CODE_ARTIFACT_CORRUPT, CODE_ARTIFACT_MISSING, CODE_ARTIFACT_PINNED,
    CODE_ARTIFACT_UNPUBLISHED, CODE_ARTIFACT_UNTRUSTED, CODE_SOURCE_UNREACHABLE, CODE_UNSUPPORTED,
    Error, FETCH_COMMAND, Result,
};
#[cfg(feature = "fetch")]
pub use fetch::{Action, EnsureOptions, Installed, ensure};
pub use library::{Column, DEFAULT_TIMEZONE, DefaultKind, Library};
pub use raw::RawText;
pub use registry::{
    AUTOFETCH_ENV, Manifest, REGISTRY_ENV, Registry, RegistryOptions, SYSTEM_ARTIFACT_ROOTS,
    cache_dir_for, default_registry_dir, host_platform, install_dir, install_dir_for,
    installed_lines, locate, locate_in, registry_search_path, search_path_for,
};
pub use result::{
    BatchResult, Computed, DocFlags, FilterOutcome, FilterResult, FilterRowError, Format, Outcome,
    RowResult, Span, Substitution, Transform, Value, Verdict,
};
pub use schema::{
    Block, Filter, NO_PARAMS, NO_SETTINGS, SETTING_CLOCK_OFFSET_NANOS,
    SETTING_DEFAULT_EVAL_MEMORY_BYTES, SETTING_DEFAULT_EVAL_WALL_NANOS,
    SETTING_MAX_CLOCK_SKEW_NANOS, SETTING_NOW_EPOCH_NANOS, Schema,
};
pub use transform::reason;

#[cfg(test)]
mod thread_contract {
    use super::*;

    /// `chtypes.h`: a single handle must not be used from two threads at once,
    /// but the library is thread-safe for concurrent calls on distinct handles.
    /// So [`Schema`] is `Send` and `!Sync`, and these assertions fail to compile
    /// if that ever changes.
    fn assert_send<T: Send>() {}
    fn assert_send_sync<T: Send + Sync>() {}

    trait AmbiguousIfSync<A> {
        fn tag() {}
    }
    impl<T: ?Sized> AmbiguousIfSync<()> for T {}
    impl<T: ?Sized + Sync> AmbiguousIfSync<u8> for T {}

    #[test]
    fn schema_is_send_and_not_sync() {
        assert_send::<Schema>();
        // Resolves only while Schema is NOT Sync: a second impl would make the
        // call ambiguous and this would stop compiling.
        let _ = <Schema as AmbiguousIfSync<_>>::tag;
    }

    #[test]
    fn a_library_and_registry_are_shareable() {
        // Every call takes the library's mutex, so sharing these is safe.
        assert_send_sync::<Library>();
        assert_send_sync::<Registry>();
        assert_send_sync::<std::sync::Arc<Library>>();
    }
}
