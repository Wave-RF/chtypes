//! The v1 fetch layer's error type (docs/guides/fetch-v1.md §8, plan §1.3).
//!
//! `CHTYPES_ARTIFACT_INCOMPATIBLE` is reserved for the FFI loader (the
//! `abi_fingerprint`/`chs_build_info()` cross-check after `dlopen`), which
//! this module never performs, so it has no variant here.

use std::fmt;

use super::constants;

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
            | Error::InvalidInput(m) => m,
        }
    }
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
