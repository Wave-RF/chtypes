//! Errors, and the one sentinel that must never be confused with a rejection.

use std::path::PathBuf;

/// `CHS_CODE_UNSUPPORTED` from `include/chtypes.h`: "this build refuses to
/// answer", and never a real ClickHouse error code.
///
/// Mapping it onto a rejection manufactures an over-reject the product never
/// made; mapping it onto an acceptance manufactures an over-accept, which is
/// the cardinal sin. It is exposed here so a caller can branch on it.
pub const CODE_UNSUPPORTED: i32 = -2;

/// The `chs_*` ABI revision this crate was written against — `CHS_ABI_REVISION`
/// in `include/chtypes.h`.
///
/// This crate `dlopen`s artifacts rather than compiling against the header, so
/// this is a hand-kept mirror and MUST be bumped in the same cycle the header
/// is. [`crate::Library`] refuses to load an artifact reporting a different
/// nonzero revision; 0 means the artifact predates the probe, which is
/// ignorance rather than incompatibility (`spec/artifact.md` §Loading).
///
/// Revision 3 (2026-08-31): `chs_rows` gained `export_format` / `doc_flags` /
/// `out_bytes`, and the `chs_filter_compile` / `chs_filter_free` /
/// `chs_filter_rows` trio joined the surface.
///
/// Revision 4 (2026-08-31, the filter phase-2 cycle): `chs_filter_compile`
/// gained `params_json` (`{name:Type}` query parameters), and the block twin
/// joined — `chs_block_parse` / `chs_block_free` / `chs_filter_eval`. This
/// crate therefore speaks 4 and refuses revision-3 artifacts: calling the
/// 5-argument `chs_filter_compile` against the 4-argument revision-3 artifact
/// is undefined behaviour, which is exactly what this gate exists to refuse.
pub const ABI_REVISION: i32 = 4;

/// `CHTYPES_ARTIFACT_MISSING` — no installed artifact answers for the line
/// (`docs/fetch.md` §7). The code every SDK shares for [`Error::ArtifactMissing`].
pub const CODE_ARTIFACT_MISSING: &str = "CHTYPES_ARTIFACT_MISSING";
/// `CHTYPES_ARTIFACT_UNTRUSTED` — the release's `SHA256SUMS` is unsigned or
/// mis-signed (§3 step 0); nothing was downloaded around it.
pub const CODE_ARTIFACT_UNTRUSTED: &str = "CHTYPES_ARTIFACT_UNTRUSTED";
/// `CHTYPES_ARTIFACT_CORRUPT` — any hash mismatch anywhere in the chain (§3).
pub const CODE_ARTIFACT_CORRUPT: &str = "CHTYPES_ARTIFACT_CORRUPT";
/// `CHTYPES_ARTIFACT_PINNED` — the release offers something other than what
/// the lock file pins (§5).
pub const CODE_ARTIFACT_PINNED: &str = "CHTYPES_ARTIFACT_PINNED";
/// `CHTYPES_ARTIFACT_UNPUBLISHED` — the release publishes nothing for the
/// requested line or exact patch on this platform (§2).
pub const CODE_ARTIFACT_UNPUBLISHED: &str = "CHTYPES_ARTIFACT_UNPUBLISHED";
/// `CHTYPES_SOURCE_UNREACHABLE` — the source could not be reached, or was not
/// consulted because the fetch was offline.
pub const CODE_SOURCE_UNREACHABLE: &str = "CHTYPES_SOURCE_UNREACHABLE";

/// This SDK's fetch command, as the "Install it:" line of
/// [`Error::ArtifactMissing`] spells it (`docs/fetch.md` §6: the crate's
/// `[[bin]]`, reached through `cargo install chtypes`).
pub const FETCH_COMMAND: &str = "cargo install chtypes && chtypes fetch";

/// `Result` with this crate's [`Error`].
pub type Result<T> = std::result::Result<T, Error>;

/// Everything that can go wrong loading an artifact or asking it a question.
///
/// Three different answers travel through this one type, and a caller must
/// keep them apart (`spec/c-abi.md` §Error model):
///
/// * **A rejection** — [`Error::Schema`]: ClickHouse itself refused, with its
///   own code and message. The DDL or profile can never exist on that server
///   and the tenant has to be told.
/// * **A decline** — [`Error::Unsupported`] / [`Error::PredatesFeature`]:
///   this build refuses to answer ([`CODE_UNSUPPORTED`]). A real server might
///   well have accepted the input, so the caller must fall back to the server
///   (validate cautiously, forward unpreviewed) rather than report a tenant
///   error. Mapping a decline onto a rejection manufactures an over-reject;
///   both over-accepts and over-rejects are budgeted at zero.
/// * **Everything else** is the machinery: loading, parsing, argument
///   marshalling. No ClickHouse verdict was reached at all
///   ([`Error::code`] answers `None`).
///
/// Note what is *not* an error: a row the server would reject comes back as
/// `Ok` with [`crate::Outcome::Rejected`] in the result — the `Result` is
/// about whether the question could be asked, and the verdict lives in the
/// answer.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum Error {
    /// The registry directory could not be read.
    #[error("chtypes: registry {dir}: {source}")]
    Registry {
        /// The directory that could not be read.
        dir: PathBuf,
        /// The underlying I/O failure.
        #[source]
        source: std::io::Error,
    },

    /// A directory carried a `manifest.json` and the library still would not
    /// `dlopen`. That is broken, not absent, so it aborts the scan.
    #[error("chtypes: dlopen {path}: {message}")]
    Load {
        /// The shared library that would not load.
        path: PathBuf,
        /// `dlerror()`'s text.
        message: String,
    },

    /// The library loaded but does not export the four mandatory `chs_*`
    /// symbols, so it is not a chtypes artifact.
    #[error("chtypes: {path} does not export the chtypes C API (missing {symbol})")]
    NotAnArtifact {
        /// The library that loaded but is not a chtypes artifact.
        path: PathBuf,
        /// The first mandatory symbol found missing.
        symbol: &'static str,
    },

    /// `manifest.library_bytes` disagrees with the file on disk. A move that
    /// reported success and truncated a 232 MB library looks identical to one
    /// that worked, which is exactly why this is checked.
    #[error("chtypes: {path}: manifest says {expected} bytes, file is {actual}")]
    CorruptArtifact {
        /// The library whose size disagrees with its manifest.
        path: PathBuf,
        /// `manifest.library_bytes`.
        expected: u64,
        /// The size on disk.
        actual: u64,
    },

    /// `chs_clickhouse_version()` disagrees with `manifest.clickhouse_version`:
    /// the right bytes in the wrong directory, the one corruption a checksum
    /// cannot catch.
    #[error("chtypes: {path}: library reports ClickHouse {reported}, manifest says {manifest}")]
    VersionMismatch {
        /// The library that disagrees with its manifest.
        path: PathBuf,
        /// What `chs_clickhouse_version()` said — the authority.
        reported: String,
        /// What `manifest.clickhouse_version` claimed.
        manifest: String,
    },

    /// `chs_init` returned nonzero. The one reachable failure is an unknown
    /// `timezone`, and `message` is ClickHouse's own text saying so — a bare
    /// `rc` cannot say which name was rejected.
    #[error("chtypes: chs_init failed for {path}: rc={rc}: {message}")]
    Init {
        /// The library whose initialisation failed.
        path: PathBuf,
        /// `chs_init`'s nonzero return.
        rc: i32,
        /// ClickHouse's own message, from `chs_init`'s `out_err`.
        message: String,
    },

    /// The same artifact image is already initialized with a different
    /// configuration. `dlopen` refcounts one image per path, so `chs_init`
    /// runs at most once per artifact — a second load asking for a different
    /// timezone cannot be honoured and must not silently re-timezone the
    /// first load's live libraries.
    #[error(
        "chtypes: {path} is already initialized with timezone {have:?}; \
         cannot re-initialize with {want:?} (one image per path — \
         chs_init runs at most once)"
    )]
    InitConflict {
        /// The artifact whose image is already initialized.
        path: PathBuf,
        /// The timezone the image was initialized with.
        have: String,
        /// The conflicting timezone this load requested.
        want: String,
    },

    /// The directory exists and holds no loadable artifact. An empty registry is
    /// a configuration mistake, not an empty result.
    #[error("chtypes: no version artifacts under {dir}")]
    EmptyRegistry {
        /// The directory that held no loadable artifact.
        dir: PathBuf,
    },

    /// No artifact answers for the requested version. Naming what *is* loaded is
    /// part of the contract: answering 26.7 semantics from a 25.8 artifact would
    /// be a lie, so there is deliberately no nearest-match fallback.
    #[error("chtypes: no vendored build for ClickHouse {requested} (have {loaded})")]
    NoSuchVersion {
        /// The version that was asked for.
        requested: String,
        /// The minor lines that *are* loaded.
        loaded: String,
    },

    /// The environment variable naming a registry is unset.
    #[error("chtypes: ${var} is not set")]
    NoRegistryEnv {
        /// The variable that is unset.
        var: &'static str,
    },

    /// ClickHouse itself rejected the schema, with its own error code.
    ///
    /// `code` is ALWAYS a real ClickHouse error code: a decline is a
    /// DIFFERENT variant ([`Error::Unsupported`] / [`Error::PredatesFeature`]),
    /// never this one carrying a negative sentinel (spec/bindings.md rule 12).
    #[error("{}", schema_display(*code, message, column.as_deref()))]
    Schema {
        /// ClickHouse's own error code.
        code: i32,
        /// ClickHouse's own message.
        message: String,
        /// The offending column, when the failure is attributable to one.
        /// Never guessed: populated only when the C layer's own structured
        /// answer names one — which no schema-path entry point does today, so
        /// the compile/engine/TTL/validate paths always carry `None` and a
        /// message that names a column rides through verbatim in `message`.
        /// The field stays for callers that KNOW a column (a gateway's
        /// EPHEMERAL decline names columns it detected itself).
        column: Option<String>,
    },

    /// This build refuses to answer: [`CODE_UNSUPPORTED`]. A real server might
    /// well have accepted the input — this is not a rejection and MUST NOT be
    /// reported as one.
    ///
    /// The rendered message keeps the frozen `[-2]` shape [`Error::Schema`]
    /// renders its code with (spec/bindings.md rule 12): the conformance
    /// drivers put this exact string on the protocol wire as an `unsupported`
    /// scope, so the rendering is part of the contract even though the
    /// sentinel is not a field. Whatever negative integer the binding saw
    /// internally (`-1` a guarded exception, `-2`), the rendered code is
    /// always the header's `CHS_CODE_UNSUPPORTED`.
    #[error("chtypes: [-2] {message}")]
    Unsupported {
        /// Why this build declines, in its own words.
        message: String,
    },

    /// The artifact does not export a symbol this call needs, i.e. it predates
    /// the feature. Reported as unsupported at call time, never as a load
    /// failure. Renders with the same frozen `[-2]` shape as
    /// [`Error::Unsupported`] — a binding-internal missing-symbol sentinel
    /// must never leak into the rendering (spec/bindings.md rule 12).
    #[error("chtypes: [-2] this artifact predates {feature} (rebuild it)")]
    PredatesFeature {
        /// The symbol or capability the artifact does not export.
        feature: &'static str,
    },

    /// The result document could not be parsed even after the bare-denormal
    /// repair.
    ///
    /// This is a hard error on purpose: the previous behaviour — retrying the
    /// parse through `String::from_utf8_lossy` — silently replaced a `String`
    /// column's bytes with U+FFFD, which then read downstream as a coercion that
    /// never happened. A document this crate cannot read exactly is reported,
    /// never approximated.
    #[error("chtypes: bad result document at byte {offset}: {message}")]
    BadDocument {
        /// What the reader expected, in its own words.
        message: String,
        /// The byte offset in the (denormal-repaired) document.
        offset: usize,
    },

    /// A string argument contained an interior NUL, so it cannot cross the C
    /// boundary.
    #[error("chtypes: interior NUL byte in {what}")]
    Nul {
        /// Which argument carried the NUL.
        what: &'static str,
    },

    /// A discovery-query result could not be parsed, or a discovered table
    /// description could not be reconstructed into DDL (`crate::discover`).
    /// Client-side and carries no ClickHouse code: the server never saw a
    /// question it could reject.
    #[error("chtypes: {message}")]
    Discovery {
        /// What went wrong, in the parser's own words.
        message: String,
    },

    /// A [`crate::Filter`] and a [`crate::Block`] from two DIFFERENT loaded
    /// libraries were paired in an eval — refused here, because no handle
    /// ever crosses a `dlopen`'d image boundary. A pair from two schemas of
    /// the SAME library is NOT this error: the C layer itself answers that
    /// with a rejected result document, code 1002 (`spec/c-abi.md` §Blocks).
    #[error(
        "chtypes: filter (ClickHouse {filter_version}) and block (ClickHouse {block_version}) \
         come from different libraries"
    )]
    CrossLibrary {
        /// The filter's library, by its own reported version.
        filter_version: String,
        /// The block's library, by its own reported version.
        block_version: String,
    },

    /// No installed artifact answers for the requested ClickHouse line on this
    /// platform: the §1 search path was walked and none of its directories
    /// holds `<line>/manifest.json` (`docs/fetch.md` §7). The message is the
    /// one every SDK renders, verbatim apart from the bracketed parts, and
    /// [`Error::artifact_code`] answers [`CODE_ARTIFACT_MISSING`].
    ///
    /// Raised by the search-path registry ([`crate::Registry::from_search_path`])
    /// with autofetch off; a registry over one explicit directory keeps
    /// answering [`Error::NoSuchVersion`], which names what IS loaded.
    #[error("{}", artifact_missing_display(line, platform, looked_in))]
    ArtifactMissing {
        /// The minor line that was asked for (`25.8`).
        line: String,
        /// `<os>-<arch>`, the artifact spelling (`linux-arm64`).
        platform: String,
        /// Every directory that was tried, in search order.
        looked_in: Vec<PathBuf>,
    },

    /// The release's `SHA256SUMS` did not verify (`docs/fetch.md` §3 step 0):
    /// no `SHA256SUMS.sig`, a malformed one, or a signature under no trusted
    /// key. Nothing was downloaded around it. Code [`CODE_ARTIFACT_UNTRUSTED`].
    #[error("chtypes: {origin}: SHA256SUMS is not trusted: {reason}")]
    ArtifactUntrusted {
        /// The source the release was read from.
        origin: String,
        /// Why, in the verifier's own words.
        reason: String,
    },

    /// A hash disagreed somewhere in the chain (`docs/fetch.md` §3): the index
    /// and `SHA256SUMS`, the downloaded tarball, the library inside it, or the
    /// installed library re-hashed in place. Reported, never repaired. Code
    /// [`CODE_ARTIFACT_CORRUPT`].
    #[error("chtypes: {subject}: sha256 is {actual}, expected {expected}")]
    ArtifactCorrupt {
        /// What was hashed, or which two records disagree.
        subject: String,
        /// The sha256 the chain said it should be.
        expected: String,
        /// The sha256 that was found.
        actual: String,
    },

    /// The release offers something other than what the lock file pins for
    /// this `<os>-<arch>/<minor>` (`docs/fetch.md` §5, `--frozen`). Code
    /// [`CODE_ARTIFACT_PINNED`].
    #[error("chtypes: {key}: {message}")]
    ArtifactPinned {
        /// The lock key, `<os>-<arch>/<minor>`.
        key: String,
        /// What was pinned and what was offered.
        message: String,
    },

    /// The release publishes nothing for the requested line (or exact patch —
    /// a hard requirement) on this platform (`docs/fetch.md` §2). Code
    /// [`CODE_ARTIFACT_UNPUBLISHED`].
    #[error(
        "chtypes: {origin} publishes no artifact for ClickHouse {requested} on {platform} (it has: {offered})"
    )]
    ArtifactUnpublished {
        /// The line or exact patch that was asked for.
        requested: String,
        /// `<os>-<arch>`.
        platform: String,
        /// The source that was consulted.
        origin: String,
        /// What the release does publish, for the message.
        offered: String,
    },

    /// The source could not be reached — or was not consulted at all because
    /// the fetch was offline. Code [`CODE_SOURCE_UNREACHABLE`].
    #[error("chtypes: {origin}: {message}")]
    SourceUnreachable {
        /// The source that was (or would have been) contacted.
        origin: String,
        /// The transport's own words, or `offline`.
        message: String,
    },

    /// Fetch machinery that reached no verdict: an unreadable release listing,
    /// an unwritable install directory, an unusable option. No artifact code.
    #[error("chtypes: fetch: {message}")]
    Fetch {
        /// What went wrong.
        message: String,
    },
}

impl Error {
    /// The ClickHouse error code, or [`CODE_UNSUPPORTED`] for the two
    /// unsupported shapes. `None` for loader-level failures, which have no code.
    pub fn code(&self) -> Option<i32> {
        match self {
            Error::Schema { code, .. } => Some(*code),
            Error::Unsupported { .. } | Error::PredatesFeature { .. } => Some(CODE_UNSUPPORTED),
            _ => None,
        }
    }

    /// Whether this is the "I decline to guess" sentinel rather than a
    /// ClickHouse rejection.
    pub fn is_unsupported(&self) -> bool {
        self.code() == Some(CODE_UNSUPPORTED)
    }

    /// The shared artifact code (`docs/fetch.md` §7) — `CHTYPES_ARTIFACT_MISSING`,
    /// `…_UNTRUSTED`, `…_CORRUPT`, `…_PINNED`, `…_UNPUBLISHED` or
    /// `CHTYPES_SOURCE_UNREACHABLE` — for the fetch and lookup failures, `None`
    /// for everything else. Distinct from [`Error::code`], which is the
    /// ClickHouse error code of a rejection.
    pub fn artifact_code(&self) -> Option<&'static str> {
        match self {
            Error::ArtifactMissing { .. } => Some(CODE_ARTIFACT_MISSING),
            Error::ArtifactUntrusted { .. } => Some(CODE_ARTIFACT_UNTRUSTED),
            Error::ArtifactCorrupt { .. } => Some(CODE_ARTIFACT_CORRUPT),
            Error::ArtifactPinned { .. } => Some(CODE_ARTIFACT_PINNED),
            Error::ArtifactUnpublished { .. } => Some(CODE_ARTIFACT_UNPUBLISHED),
            Error::SourceUnreachable { .. } => Some(CODE_SOURCE_UNREACHABLE),
            _ => None,
        }
    }

    /// Build the right variant from a C code. The SIGN decides
    /// (spec/bindings.md rule 12, spec/c-abi.md §Error model): a positive
    /// code is the server's own refusal and rides through verbatim; ANY
    /// negative code is this library declining — `-2` "I will not guess",
    /// `-1` a guarded exception, and any sentinel a later era adds — and
    /// becomes [`Error::Unsupported`]. Keying on the sign rather than on
    /// `== CODE_UNSUPPORTED` means a negative sentinel can never become
    /// "an `Error::Schema` with a negative code", which that variant's own
    /// contract forbids.
    pub(crate) fn from_code(code: i32, message: String) -> Error {
        if code < 0 {
            Error::Unsupported { message }
        } else {
            Error::Schema {
                code,
                message,
                column: None,
            }
        }
    }
}

/// The frozen rendering the refusal variant shares with its peers in every
/// SDK: `chtypes: [<code>] <msg>`, with `chtypes: column "<c>": …` when a
/// column is attributed (spec/bindings.md rule 12 — the shape the conformance
/// drivers put on the wire).
fn schema_display(code: i32, message: &str, column: Option<&str>) -> String {
    match column {
        Some(c) => format!("chtypes: column {c:?}: [{code}] {message}"),
        None => format!("chtypes: [{code}] {message}"),
    }
}

/// The §7 message, verbatim apart from the bracketed parts: the line, the
/// platform, the directories that were looked in, and this SDK's own fetch
/// command ([`FETCH_COMMAND`]).
fn artifact_missing_display(line: &str, platform: &str, looked_in: &[PathBuf]) -> String {
    let dirs: Vec<String> = looked_in.iter().map(|d| d.display().to_string()).collect();
    format!(
        "chtypes: no artifact for ClickHouse {line} ({platform}). Looked in: {}.\n\
         Install it:  {FETCH_COMMAND} {line}\n\
         or set CHTYPES_AUTOFETCH=1 to fetch on first use.",
        dirs.join(", ")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_code_keys_on_the_sign() {
        // A positive code is the server's own refusal, verbatim; ANY negative
        // code is a decline — -2 "I will not guess", -1 a guarded exception,
        // and any sentinel a later era adds. A negative Error::Schema must be
        // unmakeable through the funnel (spec/bindings.md rule 12).
        assert!(matches!(
            Error::from_code(115, "bad name".into()),
            Error::Schema { code: 115, .. }
        ));
        assert!(matches!(
            Error::from_code(50, "unknown family".into()),
            Error::Schema { code: 50, .. }
        ));
        assert!(matches!(
            Error::from_code(-2, "declined".into()),
            Error::Unsupported { .. }
        ));
        assert!(matches!(
            Error::from_code(-1, "guarded exception".into()),
            Error::Unsupported { .. }
        ));
        assert!(matches!(
            Error::from_code(-3, "future sentinel".into()),
            Error::Unsupported { .. }
        ));
    }

    #[test]
    fn the_rendered_sentinel_shape_is_frozen() {
        // The decline renders the header's -2 in the same shape the refusal
        // renders its code — the conformance drivers put this exact string on
        // the protocol wire as an `unsupported` scope (spec/bindings.md rule
        // 12). Internal sentinels never leak into the rendering.
        let decline = Error::Unsupported {
            message: "engine not modelled".into(),
        };
        assert_eq!(decline.to_string(), "chtypes: [-2] engine not modelled");
        let predates = Error::PredatesFeature {
            feature: "chs_schema_engine",
        };
        assert_eq!(
            predates.to_string(),
            "chtypes: [-2] this artifact predates chs_schema_engine (rebuild it)"
        );
        let refusal = Error::Schema {
            code: 115,
            message: "bad name".into(),
            column: None,
        };
        assert_eq!(refusal.to_string(), "chtypes: [115] bad name");
        // Column-attributed shape — for callers that KNOW one, never guessed.
        let attributed = Error::Schema {
            code: 469,
            message: "constraint".into(),
            column: Some("e".into()),
        };
        assert_eq!(
            attributed.to_string(),
            "chtypes: column \"e\": [469] constraint"
        );
    }

    #[test]
    fn the_artifact_missing_message_is_the_spec_s_verbatim() {
        // docs/fetch.md §7: one message in every SDK, verbatim apart from the
        // bracketed parts; the "Install it:" line names THIS SDK's command.
        let err = Error::ArtifactMissing {
            line: "25.8".into(),
            platform: "linux-arm64".into(),
            looked_in: vec![
                PathBuf::from("/home/u/.cache/chtypes/artifacts/linux-arm64"),
                PathBuf::from("/usr/local/share/chtypes/artifacts/linux-arm64"),
                PathBuf::from("/opt/chtypes/artifacts/linux-arm64"),
            ],
        };
        assert_eq!(
            err.to_string(),
            "chtypes: no artifact for ClickHouse 25.8 (linux-arm64). Looked in: \
             /home/u/.cache/chtypes/artifacts/linux-arm64, \
             /usr/local/share/chtypes/artifacts/linux-arm64, \
             /opt/chtypes/artifacts/linux-arm64.\n\
             Install it:  cargo install chtypes && chtypes fetch 25.8\n\
             or set CHTYPES_AUTOFETCH=1 to fetch on first use."
        );
        assert_eq!(err.artifact_code(), Some("CHTYPES_ARTIFACT_MISSING"));
        // The ClickHouse-code accessor stays what it was: no verdict here.
        assert_eq!(err.code(), None);
    }

    #[test]
    fn every_artifact_code_is_the_shared_spelling() {
        let cases: Vec<(Error, &str)> = vec![
            (
                Error::ArtifactUntrusted {
                    origin: "s".into(),
                    reason: "r".into(),
                },
                "CHTYPES_ARTIFACT_UNTRUSTED",
            ),
            (
                Error::ArtifactCorrupt {
                    subject: "x".into(),
                    expected: "aa".into(),
                    actual: "bb".into(),
                },
                "CHTYPES_ARTIFACT_CORRUPT",
            ),
            (
                Error::ArtifactPinned {
                    key: "linux-arm64/25.8".into(),
                    message: "m".into(),
                },
                "CHTYPES_ARTIFACT_PINNED",
            ),
            (
                Error::ArtifactUnpublished {
                    requested: "25.8".into(),
                    platform: "linux-arm64".into(),
                    origin: "s".into(),
                    offered: "nothing".into(),
                },
                "CHTYPES_ARTIFACT_UNPUBLISHED",
            ),
            (
                Error::SourceUnreachable {
                    origin: "s".into(),
                    message: "offline".into(),
                },
                "CHTYPES_SOURCE_UNREACHABLE",
            ),
        ];
        for (err, code) in cases {
            assert_eq!(err.artifact_code(), Some(code), "{err}");
            assert_eq!(err.code(), None, "{err}");
        }
        let plain = Error::Fetch {
            message: "m".into(),
        };
        assert_eq!(plain.artifact_code(), None);
        assert_eq!(
            Error::Unsupported {
                message: "m".into()
            }
            .artifact_code(),
            None
        );
    }
}
