//! The v1 fetch layer's error type (docs/guides/fetch-v1.md §8, plan §1.3).
//!
//! `CHTYPES_ARTIFACT_INCOMPATIBLE` is reserved for the FFI loader (the
//! `abi_fingerprint`/`chs_build_info()` cross-check after `dlopen`), which
//! this module never performs, so it has no variant here.

use std::fmt;
use std::path::{Path, PathBuf};

use super::constants;

/// What a `CHTYPES_CACHE_UNUSABLE` names (docs/guides/fetch-v1.md §1, the
/// cache faults; public issue #486): the exact path that failed, the reason,
/// and the errno name when there is one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheFault {
    pub path: PathBuf,
    /// `unreadable_root`, `not_a_directory`, `unreadable_entry`,
    /// `unacceptable_record`, `layout_0x` or `unwritable`.
    pub reason: String,
    /// The errno name (`EACCES`), or `None` when there is none.
    pub os_error: Option<String>,
    /// The sentence every binding prints: `<path> is unusable as a cache:
    /// <reason> (<errno>)`, and for `layout_0x` the 0.x hint after it.
    pub message: String,
}

/// One failure from the v1 fetch layer.
///
/// Every variant names the v0-carried error code (docs/guides/fetch-v1.md
/// §8, `constants::ERROR_EXIT_CODES`) through [`Error::code`].
#[derive(Debug)]
pub enum Error {
    /// No published line or build satisfies the request (`CHTYPES_ARTIFACT_MISSING`).
    ArtifactMissing(String),
    /// The predicate did not verify under a trusted key, or allow-unsigned was
    /// off and no bundle was found at all (`CHTYPES_ARTIFACT_UNTRUSTED`).
    ArtifactUntrusted(String),
    /// A digest, size, structural or decode check failed
    /// (`CHTYPES_ARTIFACT_CORRUPT`).
    ArtifactCorrupt(String),
    /// `--frozen` and the lock does not already pin the request
    /// (`CHTYPES_ARTIFACT_PINNED`).
    ArtifactPinned(String),
    /// A by-tag lookup 404s on every base (`CHTYPES_ARTIFACT_UNPUBLISHED`).
    ArtifactUnpublished(String),
    /// Every base was exhausted (connection failure, timeout, a by-digest
    /// 404, or `Retry-After` past budget) (`CHTYPES_SOURCE_UNREACHABLE`).
    SourceUnreachable(String),
    /// A source refused credentials (401 after the anonymous-token flow, or a
    /// `CHTYPES_DOWNLOAD_TOKEN` the source rejects) (`CHTYPES_SOURCE_UNAUTHORIZED`).
    SourceUnauthorized(String),
    /// A source refused the request outright (403) (`CHTYPES_SOURCE_FORBIDDEN`).
    SourceForbidden(String),
    /// The source is unusable as configured: an unsupported scheme, a
    /// malformed base, or `--offline`/`--frozen` combined with a request that
    /// needs discovery (`CHTYPES_SOURCE_INCOMPATIBLE`).
    SourceIncompatible(String),
    /// An option or environment value is malformed (a refused version
    /// spelling, an unreadable cache or lock file). Carries no v0 exit code
    /// of its own; callers map it like `SourceIncompatible`.
    InvalidInput(String),
    /// A cache directory or entry this layer could not read or write, or one
    /// strict mode refuses (`CHTYPES_CACHE_UNUSABLE`): never "not installed"
    /// and never the network's `SourceUnreachable`.
    CacheUnusable(CacheFault),
    /// A source answered 410 Gone: a retired repository, which is permanent,
    /// so the request was never retried and never sent to the next base
    /// (`CHTYPES_SOURCE_RETIRED`; docs/guides/fetch-v1.md §2, public issue
    /// #571). The message names the URL that answered and carries the
    /// registry's own message, made safe to print (`retired.rs`).
    SourceRetired(String),
}

impl Error {
    /// The v1 error code name (`CHTYPES_ARTIFACT_MISSING`, and so on), as
    /// `constants::ERROR_EXIT_CODES` spells it.
    pub fn code(&self) -> &'static str {
        match self {
            Error::ArtifactMissing(_) => "CHTYPES_ARTIFACT_MISSING",
            Error::ArtifactUntrusted(_) => "CHTYPES_ARTIFACT_UNTRUSTED",
            Error::ArtifactCorrupt(_) => "CHTYPES_ARTIFACT_CORRUPT",
            Error::ArtifactPinned(_) => "CHTYPES_ARTIFACT_PINNED",
            Error::ArtifactUnpublished(_) => "CHTYPES_ARTIFACT_UNPUBLISHED",
            Error::SourceUnreachable(_) => "CHTYPES_SOURCE_UNREACHABLE",
            Error::SourceUnauthorized(_) => "CHTYPES_SOURCE_UNAUTHORIZED",
            Error::SourceForbidden(_) => "CHTYPES_SOURCE_FORBIDDEN",
            Error::SourceIncompatible(_) | Error::InvalidInput(_) => "CHTYPES_SOURCE_INCOMPATIBLE",
            Error::CacheUnusable(_) => "CHTYPES_CACHE_UNUSABLE",
            Error::SourceRetired(_) => "CHTYPES_SOURCE_RETIRED",
        }
    }

    /// The process exit status `constants::ERROR_EXIT_CODES` assigns this
    /// code, or `None` if the generated table and this match ever drift.
    pub fn exit_code(&self) -> Option<u8> {
        let code = self.code();
        constants::ERROR_EXIT_CODES
            .iter()
            .find(|entry| entry.code == code)
            .map(|entry| entry.exit_code)
    }

    fn message(&self) -> &str {
        match self {
            Error::ArtifactMissing(m)
            | Error::ArtifactUntrusted(m)
            | Error::ArtifactCorrupt(m)
            | Error::ArtifactPinned(m)
            | Error::ArtifactUnpublished(m)
            | Error::SourceUnreachable(m)
            | Error::SourceUnauthorized(m)
            | Error::SourceForbidden(m)
            | Error::SourceIncompatible(m)
            | Error::InvalidInput(m)
            | Error::SourceRetired(m) => m,
            Error::CacheUnusable(f) => &f.message,
        }
    }

    /// A `CHTYPES_CACHE_UNUSABLE` for one fault, with the sentence every
    /// binding prints.
    pub fn cache_unusable(path: &Path, reason: &str, os_error: Option<String>) -> Error {
        let mut message = format!("{} is unusable as a cache: {reason}", path.display());
        if let Some(e) = &os_error {
            message.push_str(&format!(" ({e})"));
        }
        if reason == "layout_0x" {
            if let Some(hint) = super::layout::zero_x_hint(path) {
                message.push_str(&format!(". {hint}"));
            }
        }
        Error::CacheUnusable(CacheFault {
            path: path.to_path_buf(),
            reason: reason.to_string(),
            os_error,
            message,
        })
    }
}

/// The errno spelling of an I/O error (`EACCES`), the same in every binding
/// whatever the platform's numbers are; `None` when it carries no errno.
pub fn errno_name(e: &std::io::Error) -> Option<String> {
    let code = e.raw_os_error()?;
    let name = match code {
        1 => "EPERM",
        2 => "ENOENT",
        5 => "EIO",
        13 => "EACCES",
        16 => "EBUSY",
        17 => "EEXIST",
        18 => "EXDEV",
        20 => "ENOTDIR",
        21 => "EISDIR",
        22 => "EINVAL",
        23 => "ENFILE",
        24 => "EMFILE",
        28 => "ENOSPC",
        30 => "EROFS",
        #[cfg(target_os = "linux")]
        36 => "ENAMETOOLONG",
        #[cfg(target_os = "linux")]
        39 => "ENOTEMPTY",
        #[cfg(target_os = "linux")]
        40 => "ELOOP",
        #[cfg(target_os = "linux")]
        122 => "EDQUOT",
        #[cfg(target_os = "macos")]
        62 => "ELOOP",
        #[cfg(target_os = "macos")]
        63 => "ENAMETOOLONG",
        #[cfg(target_os = "macos")]
        66 => "ENOTEMPTY",
        #[cfg(target_os = "macos")]
        69 => "EDQUOT",
        other => return Some(format!("errno {other}")),
    };
    Some(name.to_string())
}

/// The mapping for a write this layer needed under the cache that failed:
/// `CHTYPES_CACHE_UNUSABLE` with reason `unwritable`, naming `path`, in every
/// mode (public issue #486).
pub fn unwritable(path: &Path) -> impl FnOnce(std::io::Error) -> Error + '_ {
    move |e| Error::cache_unusable(path, "unwritable", errno_name(&e))
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::SourceUnreachable(format!("io error: {e}"))
    }
}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::ArtifactCorrupt(format!("JSON: {e}"))
    }
}

/// A `Result` whose error is this module's [`Error`].
pub type Result<T> = std::result::Result<T, Error>;
