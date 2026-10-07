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
//! **2.0.0-dev: UNSTABLE, staging only, not for production.** This is the ABI v2
//! dev binding (`spec/abi-v2/docs.md`, public issue #511): it loads only a
//! library of its own dev fingerprint, fetches only from the staging dev
//! channel `https://registry-staging.wavehouse.dev/chtypes/v2-dev` and trusts
//! only the staging key, ignores `CHTYPES_ARTIFACTS_URL`,
//! `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED` (and the matching
//! [`FetchOptions`] fields) with one warning each, refuses every lock, frozen
//! and update request, and caches under
//! `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev/` (or `<CHTYPES_CACHE>/v2-dev/`).
//! Every vocabulary has an explicit `Unknown` member (rule r3): a value the
//! description does not list is kept, for that field alone, and never fails a
//! document.
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
//!   A value whose [`Value::source`] is [`Source::DefaultGenerated`] is stored
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

mod abi2;
mod decode;
mod error;
mod library;
// The fetch layer's own documentation is its module docs; its record types are
// re-exported below, undocumented field by field.
#[allow(missing_docs)]
mod ocifetch;
mod raw;
mod registry;
// The registry over a real signed OCI layout of the stub: a UNIT test, because
// only a unit test reaches the fetch layer's test-only seam (the file says
// why). Its source sits under tests/, beside the suites it used to be one of,
// and outside the shipped source the security carve-out derives from.
#[cfg(test)]
#[path = "../tests/unit/registry_stub.rs"]
mod registry_stub_tests;
mod result;
mod schema;
mod setup;

pub use abi2::vocab_gen::{
    DefaultKind, DiscoverQueryParam, DocFlags, FilterOutcome, Format, Outcome, Reason, Source,
    Status, Verdict, reason, source, status,
};
pub use error::{CacheFault, CallError, Error, Refusal, Result};
pub use library::Library;
pub use ocifetch::ensure::Resolved;
pub use raw::RawText;
pub use registry::{FetchOptions, Registry, RegistryOptions, cache_root, search_dirs};
pub use result::{
    BatchResult, BuildInfo, Capabilities, Column, Computed, DiscoveredColumn, Discovery,
    EngineCell, ErrorCodeEntry, ErrorCodeTable, FilterResult, FilterRowError, Framing, Header,
    RowResult, SchemaDescription, Span, Transform, Value,
};
pub use schema::{
    Block, CompileOptions, EvalOptions, Filter, FilterOptions, RowOptions, RowsOptions, Schema,
};
pub use setup::{SetupOptions, setup};

/// The binding's compiled-in ABI identity, printed for the CI jobs that test
/// this crate without loading a library: each reads the line and asserts it
/// against spec/binding-majors.json and the description at its commit
/// (`scripts/abi-v1/majors.py assert-identity`), so a job never takes the map's
/// word for the major it tested.
#[cfg(test)]
mod abi_identity {
    use crate::abi2::decls::{CHS_ABI_FINGERPRINT, CHS_ABI_VERSION};

    #[test]
    fn chtypes_abi_identity() {
        // A malformed identity is refused here; whether it is the major and
        // the fingerprint the map and the description give is the CI step's
        // question (majors.py assert-identity), asked of the line below.
        assert!(
            CHS_ABI_FINGERPRINT
                .strip_prefix("sha256:")
                .is_some_and(|hex| hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit())),
            "{CHS_ABI_FINGERPRINT}"
        );
        println!(
            "chtypes_abi_identity binding=rust abi={CHS_ABI_VERSION} fingerprint={CHS_ABI_FINGERPRINT}"
        );
    }
}

/// Rule r3 (spec/abi-v2/docs.md), over every enum the description defines (the
/// generated `DESCRIBED_VOCABULARIES`, so a new enum is covered without an edit
/// here): an unlisted value builds that vocabulary's `Unknown`, which is not
/// known and reads back unchanged; every listed value is known.
#[cfg(test)]
mod every_vocabulary_has_its_unknown {
    use crate::abi2::vocab_gen::{DESCRIBED_VOCABULARIES, described_vocabulary};
    use crate::{
        DefaultKind, DiscoverQueryParam, FilterOutcome, Format, Outcome, Reason, Source, Status,
        Verdict,
    };

    #[test]
    fn an_unlisted_value_of_every_described_enum_is_its_unknown() {
        let named = DESCRIBED_VOCABULARIES.len();
        assert!(
            named >= 10,
            "the generated list names {named} enums: {DESCRIBED_VOCABULARIES:?}"
        );
        for name in DESCRIBED_VOCABULARIES {
            let unlisted = format!("x_unlisted_{name}");
            for raw in [unlisted.as_str(), "2147483000", "-7"] {
                let Some((known, back)) = described_vocabulary(name, raw) else {
                    // A string spelling is not an int32 enum's raw value.
                    assert!(
                        raw.parse::<i32>().is_err(),
                        "{name}: no vocabulary built from {raw:?}"
                    );
                    continue;
                };
                assert!(!known, "{name}({raw:?}) is known: want its unknown(n)");
                assert_eq!(back, raw, "{name}({raw:?}) reads back changed");
            }
        }
        assert!(described_vocabulary("no_such_enum", "1").is_none());
    }

    #[test]
    fn every_listed_value_is_known() {
        assert!(Status::from_code(0).is_known() && Format::JsonEachRow.is_known());
        assert!(Reason::ValueChanged.is_known() && Source::Input.is_known());
        assert!(Outcome::Accepted.is_known() && FilterOutcome::Ok.is_known());
        assert!(Verdict::True.is_known() && DiscoverQueryParam::Database.is_known());
        assert!(DefaultKind::None.is_known());
        for f in Format::ALL {
            assert!(f.is_known() && Format::from_code(f.code()) == f, "{f:?}");
        }
        // The fallback's fail-closed facts: an unknown verdict is never an
        // answer, an unknown reason is lossy, an unknown source is not stored.
        assert!(!Verdict::Unknown("x".into()).answered());
        assert!(Reason::Unknown("x".into()).lossy());
        assert!(!Source::Unknown("x".into()).is_stored());
        assert_eq!(Format::Unknown(99).ch_name(), None);
        assert_eq!(DiscoverQueryParam::Unknown("x".into()).ch_type(), None);
    }
}

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
