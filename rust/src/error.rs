//! The error classes (`docs/reference/bindings-v1.md` §4).
//!
//! Four **call** classes carry the five fields of the library's own error
//! ([`CallError`]); two **artifact** classes carry a loader [`Refusal`]; the
//! fetch layer's own codes are one variant each. They are one family: a caller
//! matching [`Error::ArtifactCorrupt`] sees the fetch layer's corruption and
//! the loader's step 5 alike.
//!
//! The refusal ([`Error::Schema`]) and the decline ([`Error::Unsupported`]) are
//! PEERS, never one a subtype of the other: a handler that forgot the
//! distinction must not be able to turn every decline into a rejection. Catching
//! all four call classes is an explicit choice, [`Error::call_error`].

use std::fmt;
use std::path::PathBuf;

use crate::abi2::calls_gen::RawCallError;
use crate::abi2::errmap_gen::{ErrorClass, RefusalClass, refusal_class, status_class};
use crate::abi2::vocab_gen::{Status, status};
use crate::ocifetch::constants::code;
use crate::raw::RawText;

pub use crate::abi2::loader::Refusal;

/// What a [`Error::CacheUnusable`] names: a cache directory or entry the
/// fetch layer could not read or write, or one strict mode refuses
/// (`docs/guides/fetch-v1.md` §1, the cache faults).
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct CacheFault {
    /// The exact path that failed.
    pub path: PathBuf,
    /// `unreadable_root`, `not_a_directory`, `unreadable_entry`,
    /// `unacceptable_record`, `layout_0x` or `unwritable`.
    pub reason: String,
    /// The errno name (`EACCES`), or `None` when there is none.
    pub os_error: Option<String>,
    message: String,
}

/// `<path> is unusable as a cache: <reason> (<errno>)`, the sentence every
/// binding prints.
impl fmt::Display for CacheFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

/// The five fields of one `chs_error`, verbatim, and nothing synthesized.
///
/// A misuse the binding detects itself carries the same shape: `status`
/// `CHS_INVALID_ARGUMENT`, `ch_code` 0, an empty `ch_name` and `column`, and a
/// message naming the misuse, so a handler sees one shape whichever side
/// caught it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallError {
    /// The raw `chs_status` value; compare with [`crate::status`].
    pub status: i32,
    /// ClickHouse's own error code; nonzero only for a refusal.
    pub ch_code: i32,
    /// The name this build's vendored table gives the code. ASCII; empty if none.
    pub ch_name: String,
    /// ClickHouse's own message for a refusal, the library's otherwise. Bytes:
    /// it may quote input.
    pub message: RawText,
    /// The column concerned, empty if none. Bytes.
    pub column: RawText,
}

impl CallError {
    /// A misuse this binding detected itself: `CHS_INVALID_ARGUMENT`.
    pub(crate) fn usage(message: impl Into<String>) -> CallError {
        CallError::detected(status::INVALID_ARGUMENT, message)
    }

    /// A library bug this binding detected itself (a document that does not
    /// decode, a status outside the closed set): `CHS_INTERNAL`.
    pub(crate) fn internal(message: impl Into<String>) -> CallError {
        CallError::detected(status::INTERNAL, message)
    }

    fn detected(status: i32, message: impl Into<String>) -> CallError {
        CallError {
            status,
            ch_code: 0,
            ch_name: String::new(),
            message: RawText::from(message.into()),
            column: RawText::default(),
        }
    }
}

impl From<RawCallError> for CallError {
    fn from(e: RawCallError) -> CallError {
        CallError {
            status: e.status,
            ch_code: e.ch_code,
            ch_name: e.ch_name,
            message: RawText::from(e.message),
            column: RawText::from(e.column),
        }
    }
}

/// The one lossy display form each error has: invalid UTF-8 is replaced for
/// printing only.
impl fmt::Display for CallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.ch_name.is_empty() {
            write!(f, "status {}", self.status)?;
        } else {
            write!(f, "{} ({})", self.ch_name, self.ch_code)?;
        }
        write!(f, ": {}", self.message.to_lossy())?;
        if !self.column.is_empty() {
            write!(f, " [column {}]", self.column.to_lossy())?;
        }
        Ok(())
    }
}

impl std::error::Error for CallError {}

/// Every failure this crate returns.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum Error {
    /// `CHS_REJECTED`: ClickHouse's own refusal, which a server would also give,
    /// including a zone name `DateLUT` will not load.
    Schema(CallError),
    /// `CHS_DECLINED`: this build will not answer, where a server might accept.
    /// Never read it as a rejection or as an acceptance.
    Unsupported(CallError),
    /// `CHS_INVALID_ARGUMENT`, and the misuse this binding detects before any
    /// call (a refused version spelling, a conflicting setup, a zone given
    /// twice, an unverified open without both opt-ins).
    Usage(CallError),
    /// `CHS_INTERNAL`; a status outside the closed set, naming its
    /// `unknown(n)` (rule r3); a document that does not decode.
    Internal(CallError),
    /// A loader refusal at steps 1, 2, 3, 4 (the fingerprint) or 6. A dev SDK's
    /// fingerprint refusal displays exactly rule r6's message
    /// ([`Refusal::dev_message`]).
    ArtifactIncompatible(Refusal),
    /// A loader refusal at step 5 or a malformed `build_info`, and the fetch
    /// layer's own corruption.
    ArtifactCorrupt(Refusal),
    /// `CHTYPES_ARTIFACT_MISSING`: nothing installed answers the request.
    ArtifactMissing(String),
    /// `CHTYPES_ARTIFACT_UNTRUSTED`: no bundle verified under a trusted key.
    ArtifactUntrusted(String),
    /// `CHTYPES_ARTIFACT_PINNED`: frozen, and the lock does not pin the request.
    ArtifactPinned(String),
    /// `CHTYPES_ARTIFACT_UNPUBLISHED`: no source publishes the request.
    ArtifactUnpublished(String),
    /// `CHTYPES_SOURCE_UNREACHABLE`: every source was exhausted.
    SourceUnreachable(String),
    /// `CHTYPES_SOURCE_UNAUTHORIZED`: a source refused the credentials.
    SourceUnauthorized(String),
    /// `CHTYPES_SOURCE_FORBIDDEN`: a source refused the request outright.
    SourceForbidden(String),
    /// `CHTYPES_SOURCE_INCOMPATIBLE`: the source is unusable as configured.
    SourceIncompatible(String),
    /// `CHTYPES_CACHE_UNUSABLE`: a cache directory or entry the fetch layer
    /// could not read or write, or one strict mode
    /// ([`crate::FetchOptions::strict_cache`]) refuses. Never "not
    /// installed", never the network's [`Error::SourceUnreachable`].
    CacheUnusable(CacheFault),
    /// `CHTYPES_SOURCE_RETIRED`: a source answered 410 Gone, a retired
    /// repository. It is permanent, so the request was never retried and
    /// never sent to the next source; the message names the URL that
    /// answered and carries the registry's own message, made safe to print.
    SourceRetired(String),
}

/// A `Result` whose error is this crate's [`Error`].
pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    /// Whether this is a decline ([`Error::Unsupported`]): the build will not
    /// answer, and a server might accept.
    pub fn is_unsupported(&self) -> bool {
        matches!(self, Error::Unsupported(_))
    }

    /// The five fields of the library's error, from any of the four call
    /// classes; `None` for an artifact or fetch error.
    pub fn call_error(&self) -> Option<&CallError> {
        match self {
            Error::Schema(c) | Error::Unsupported(c) | Error::Usage(c) | Error::Internal(c) => {
                Some(c)
            }
            _ => None,
        }
    }

    /// The `CHTYPES_*` code of an artifact or fetch error, one of the
    /// [`crate::code`] constants; `None` for a call error.
    pub fn code(&self) -> Option<&'static str> {
        Some(match self {
            Error::ArtifactIncompatible(_) => code::ARTIFACT_INCOMPATIBLE,
            Error::ArtifactCorrupt(_) => code::ARTIFACT_CORRUPT,
            Error::ArtifactMissing(_) => code::ARTIFACT_MISSING,
            Error::ArtifactUntrusted(_) => code::ARTIFACT_UNTRUSTED,
            Error::ArtifactPinned(_) => code::ARTIFACT_PINNED,
            Error::ArtifactUnpublished(_) => code::ARTIFACT_UNPUBLISHED,
            Error::SourceUnreachable(_) => code::SOURCE_UNREACHABLE,
            Error::SourceUnauthorized(_) => code::SOURCE_UNAUTHORIZED,
            Error::SourceForbidden(_) => code::SOURCE_FORBIDDEN,
            Error::SourceIncompatible(_) => code::SOURCE_INCOMPATIBLE,
            Error::CacheUnusable(_) => code::CACHE_UNUSABLE,
            Error::SourceRetired(_) => code::SOURCE_RETIRED,
            _ => return None,
        })
    }

    /// A misuse this binding detected itself.
    pub(crate) fn usage(message: impl Into<String>) -> Error {
        Error::Usage(CallError::usage(message))
    }

    /// A library bug this binding detected itself.
    pub(crate) fn internal(message: impl Into<String>) -> Error {
        Error::Internal(CallError::internal(message))
    }

    /// A call's own error, mapped by `sdk.json`'s status table. A status
    /// outside the closed set is its `unknown(n)` (rule r3), and the call still
    /// fails: an internal error naming it.
    pub(crate) fn from_call(e: RawCallError) -> Error {
        let read = Status::from_code(e.status);
        if !read.is_known() || read == Status::Ok {
            let mut call = CallError::from(e);
            let original = call.message.to_lossy().into_owned();
            call.message = RawText::from(format!(
                "call status {read} is outside the closed set of call statuses; {original}"
            ));
            return Error::Internal(call);
        }
        let class = status_class(e.status);
        let call = CallError::from(e);
        match class {
            Some(ErrorClass::Schema) => Error::Schema(call),
            Some(ErrorClass::Unsupported) => Error::Unsupported(call),
            Some(ErrorClass::Usage) => Error::Usage(call),
            Some(ErrorClass::Internal) | None => Error::Internal(call),
        }
    }

    /// A loader refusal, mapped by `sdk.json`'s refusal table.
    pub(crate) fn from_refusal(r: Refusal) -> Error {
        match refusal_class(&r.reason) {
            RefusalClass::Incompatible => Error::ArtifactIncompatible(r),
            RefusalClass::Corrupt => Error::ArtifactCorrupt(r),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Schema(c) => write!(f, "refused: {c}"),
            Error::Unsupported(c) => write!(f, "declined: {c}"),
            Error::Usage(c) => write!(f, "misuse: {c}"),
            Error::Internal(c) => write!(f, "internal error: {c}"),
            // A dev SDK's fingerprint refusal is rule r6's exact message, alone.
            Error::ArtifactIncompatible(r) if r.dev_message().is_some() => write!(f, "{r}"),
            Error::ArtifactIncompatible(r) | Error::ArtifactCorrupt(r) => {
                write!(f, "{}: {r}", self.code().unwrap_or_default())
            }
            Error::ArtifactMissing(m)
            | Error::ArtifactUntrusted(m)
            | Error::ArtifactPinned(m)
            | Error::ArtifactUnpublished(m)
            | Error::SourceUnreachable(m)
            | Error::SourceUnauthorized(m)
            | Error::SourceForbidden(m)
            | Error::SourceIncompatible(m)
            | Error::SourceRetired(m) => {
                write!(f, "{}: {m}", self.code().unwrap_or_default())
            }
            Error::CacheUnusable(c) => write!(f, "{}: {c}", self.code().unwrap_or_default()),
        }
    }
}

impl std::error::Error for Error {}

impl From<crate::ocifetch::error::Error> for Error {
    fn from(e: crate::ocifetch::error::Error) -> Error {
        use crate::ocifetch::error::Error as F;
        match e {
            F::ArtifactMissing(m) => Error::ArtifactMissing(m),
            F::ArtifactUntrusted(m) => Error::ArtifactUntrusted(m),
            // The fetch layer's own corruption: no loader reason and no path,
            // as in Go, Python and TypeScript; its message is the display.
            F::ArtifactCorrupt(m) => Error::ArtifactCorrupt(Refusal::fetch_corruption(m)),
            F::ArtifactPinned(m) => Error::ArtifactPinned(m),
            F::ArtifactUnpublished(m) => Error::ArtifactUnpublished(m),
            F::SourceUnreachable(m) => Error::SourceUnreachable(m),
            F::SourceUnauthorized(m) => Error::SourceUnauthorized(m),
            F::SourceForbidden(m) => Error::SourceForbidden(m),
            F::SourceIncompatible(m) => Error::SourceIncompatible(m),
            F::SourceRetired(m) => Error::SourceRetired(m),
            // A refused version spelling, an unreadable cache or lock file:
            // misuse, which carries no fetch code of its own.
            F::InvalidInput(m) => Error::usage(m),
            F::CacheUnusable(f) => Error::CacheUnusable(CacheFault {
                path: f.path,
                reason: f.reason,
                os_error: f.os_error,
                message: f.message,
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(status: i32) -> RawCallError {
        RawCallError {
            status,
            ch_code: 27,
            ch_name: "CANNOT_PARSE_INPUT_ASSERTION_FAILED".to_string(),
            message: b"boom".to_vec(),
            column: b"c".to_vec(),
        }
    }

    #[test]
    fn each_status_maps_to_its_own_class_and_the_five_fields_survive() {
        assert!(matches!(
            Error::from_call(raw(status::REJECTED)),
            Error::Schema(_)
        ));
        assert!(matches!(
            Error::from_call(raw(status::DECLINED)),
            Error::Unsupported(_)
        ));
        assert!(matches!(
            Error::from_call(raw(status::INVALID_ARGUMENT)),
            Error::Usage(_)
        ));
        assert!(matches!(
            Error::from_call(raw(status::INTERNAL)),
            Error::Internal(_)
        ));
        let e = Error::from_call(raw(status::REJECTED));
        let c = e.call_error().expect("a call error");
        assert_eq!(c.status, status::REJECTED);
        assert_eq!(c.ch_code, 27);
        assert_eq!(c.ch_name, "CANNOT_PARSE_INPUT_ASSERTION_FAILED");
        assert_eq!(c.message.as_bytes(), b"boom");
        assert_eq!(c.column.as_bytes(), b"c");
    }

    #[test]
    fn a_refusal_and_a_decline_are_peers_never_one_another() {
        let refusal = Error::from_call(raw(status::REJECTED));
        let decline = Error::from_call(raw(status::DECLINED));
        assert!(!refusal.is_unsupported());
        assert!(decline.is_unsupported());
        assert!(!matches!(decline, Error::Schema(_)));
    }

    /// Rule r3: a status outside the closed set is its `unknown(n)`, and the
    /// call still fails, as an internal error naming it.
    #[test]
    fn a_status_outside_the_closed_set_is_internal_and_names_its_unknown() {
        let e = Error::from_call(raw(99));
        let Error::Internal(c) = e else {
            panic!("want an internal error, got {e:?}")
        };
        assert_eq!(c.status, 99);
        let read = Status::from_code(c.status);
        assert_eq!(read, Status::Unknown(99));
        assert!(!read.is_known());
        assert_eq!(read.to_string(), "unknown(99)");
        assert!(c.message.to_lossy().contains("unknown(99)"), "{c}");
        for listed in [
            status::OK,
            status::REJECTED,
            status::DECLINED,
            status::INVALID_ARGUMENT,
            status::INTERNAL,
        ] {
            assert!(Status::from_code(listed).is_known(), "{listed}");
        }
    }

    /// Rule r6: a fingerprint refusal of this dev SDK is
    /// `CHTYPES_ARTIFACT_INCOMPATIBLE` and displays exactly the rule's message.
    #[test]
    fn a_dev_fingerprint_refusal_is_incompatible_with_rule_r6s_message() {
        let fingerprint = crate::abi2::decls::CHS_ABI_FINGERPRINT;
        let other = format!("sha256:{}", "0".repeat(64));
        let e = Error::from_refusal(Refusal {
            reason: "fingerprint".to_string(),
            path: PathBuf::from("/p"),
            want: Some(fingerprint.to_string()),
            got: Some(other.clone()),
            message: None,
        });
        let want = format!(
            "this SDK speaks dev fingerprint {fingerprint}; the library has {other} — update your dev SDK"
        );
        assert!(matches!(e, Error::ArtifactIncompatible(_)), "{e:?}");
        assert_eq!(e.code(), Some("CHTYPES_ARTIFACT_INCOMPATIBLE"));
        assert_eq!(e.to_string(), want);
    }

    /// The fetch layer's own corruption has no loader reason and no path, as in
    /// the other bindings, and displays its message after the code.
    #[test]
    fn the_fetch_layers_corruption_has_no_reason_and_shows_its_message() {
        let e = Error::from(crate::ocifetch::error::Error::ArtifactCorrupt(
            "layer: digest mismatch".to_string(),
        ));
        let Error::ArtifactCorrupt(r) = &e else {
            panic!("want ArtifactCorrupt, got {e:?}")
        };
        assert_eq!(r.reason, "");
        assert_eq!(r.path, PathBuf::new());
        assert_eq!((r.want.as_deref(), r.got.as_deref()), (None, None));
        assert_eq!(e.code(), Some(code::ARTIFACT_CORRUPT));
        assert_eq!(
            e.to_string(),
            "CHTYPES_ARTIFACT_CORRUPT: layer: digest mismatch"
        );
    }

    #[test]
    fn a_detected_misuse_carries_the_invalid_argument_shape() {
        let Error::Usage(c) = Error::usage("closed") else {
            panic!("want a usage error")
        };
        assert_eq!(c.status, status::INVALID_ARGUMENT);
        assert_eq!(c.ch_code, 0);
        assert!(c.ch_name.is_empty() && c.column.is_empty());
        assert_eq!(c.message.as_bytes(), b"closed");
    }

    #[test]
    fn loader_refusals_map_by_the_generated_table() {
        let refusal = |reason: &str| Refusal {
            reason: reason.to_string(),
            path: PathBuf::from("/x"),
            want: None,
            got: None,
            message: None,
        };
        assert!(matches!(
            Error::from_refusal(refusal("fingerprint")),
            Error::ArtifactIncompatible(_)
        ));
        assert!(matches!(
            Error::from_refusal(refusal("missing_symbol:chs_x")),
            Error::ArtifactIncompatible(_)
        ));
        assert!(matches!(
            Error::from_refusal(refusal("build_info_mismatch:os")),
            Error::ArtifactCorrupt(_)
        ));
        assert!(matches!(
            Error::from_refusal(refusal("build_info_malformed")),
            Error::ArtifactCorrupt(_)
        ));
    }

    #[test]
    fn a_fetch_error_joins_the_one_family() {
        use crate::ocifetch::error::Error as F;
        assert_eq!(
            Error::from(F::ArtifactCorrupt("bad digest".into())).code(),
            Some("CHTYPES_ARTIFACT_CORRUPT")
        );
        assert!(matches!(
            Error::from(F::InvalidInput("v25.8".into())),
            Error::Usage(_)
        ));
        assert_eq!(
            Error::from(F::SourceUnreachable("down".into())).code(),
            Some("CHTYPES_SOURCE_UNREACHABLE")
        );
        // A retired repository (fetch-v1.md section 2, public issue #571)
        // keeps its code and its whole message.
        let retired = Error::from(F::SourceRetired("u answered 410 Gone".into()));
        assert_eq!(retired.code(), Some("CHTYPES_SOURCE_RETIRED"));
        assert_eq!(
            retired.to_string(),
            "CHTYPES_SOURCE_RETIRED: u answered 410 Gone"
        );
    }
}
