//! `docs/fetch.md` §3 steps 0–2: the release's `SHA256SUMS` (verified),
//! `index.json`, and the choice of one asset for a line and platform.

use std::collections::BTreeMap;

use serde::Deserialize;

use super::source::Source;
use super::trust::TrustPolicy;
use crate::error::{Error, Result};

/// One row of a release's `index.json` (schema 1): the asset for one
/// ClickHouse version on one platform, and what is inside it.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct IndexRow {
    /// The asset file name, `chtypes-<version>-<os>-<arch>.tar.gz`.
    #[serde(default)]
    pub file: String,
    /// sha256 of the asset; `SHA256SUMS` must say the same.
    #[serde(default)]
    pub sha256: String,
    /// Size of the asset.
    #[serde(default)]
    pub bytes: u64,
    /// The exact ClickHouse release inside, `25.8.28.1-lts`.
    #[serde(default)]
    pub clickhouse_version: String,
    /// The minor line, `25.8` — the registry subdirectory it installs to.
    #[serde(default)]
    pub clickhouse_minor: String,
    /// `linux` | `darwin`.
    #[serde(default)]
    pub os: String,
    /// `arm64` | `amd64`.
    #[serde(default)]
    pub arch: String,
    /// The shared library's file name inside the tarball.
    #[serde(default)]
    pub library: String,
    /// sha256 of that library — what the installed file must hash to.
    #[serde(default)]
    pub library_sha256: String,
    /// The wrapper build for this ClickHouse version. A rebuild of the same
    /// version is a NEW row beside the old one, never a swap, so this is what
    /// separates them. Absent on a row published before builds existed.
    #[serde(default)]
    pub build: u32,
    /// The core commit the wrapper was built from; empty on an old row.
    #[serde(default)]
    pub core_commit: String,
}

impl IndexRow {
    /// The wrapper build: this row's own `build` when it has one, else the
    /// `-b<N>` the file name ends with, else 0 — an old row, which is build 0 by
    /// definition.
    ///
    /// Public because it is the rule a consumer needs to answer "which of these
    /// two rows is the newer build", not just an internal detail.
    pub fn build_number(&self) -> u32 {
        if self.build > 0 {
            return self.build;
        }
        // No regex crate here, and none is wanted for this: strip the extension,
        // then read the digits after the last `-b`.
        let stem = match self.file.strip_suffix(".tar.gz") {
            Some(stem) => stem,
            None => return 0,
        };
        let Some((_, digits)) = stem.rsplit_once("-b") else {
            return 0;
        };
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return 0;
        }
        digits.parse().unwrap_or(0)
    }

    /// "Which row wins": newest ClickHouse version, then the highest wrapper
    /// build. Without the build half a rebuild's older sibling could win on
    /// nothing but its position in the index.
    pub(crate) fn rank(&self) -> (Vec<u64>, u32) {
        (version_key(&self.clickhouse_version), self.build_number())
    }
}

impl IndexRow {
    /// `<os>-<arch>`.
    pub fn platform(&self) -> String {
        format!("{}-{}", self.os, self.arch)
    }

    fn check_complete(&self) -> Result<()> {
        let missing: Vec<&str> = [
            ("file", self.file.is_empty()),
            ("sha256", self.sha256.is_empty()),
            ("bytes", self.bytes == 0),
            ("clickhouse_version", self.clickhouse_version.is_empty()),
            ("clickhouse_minor", self.clickhouse_minor.is_empty()),
            ("library", self.library.is_empty()),
            ("library_sha256", self.library_sha256.is_empty()),
        ]
        .into_iter()
        .filter_map(|(k, absent)| absent.then_some(k))
        .collect();
        if missing.is_empty() {
            Ok(())
        } else {
            Err(Error::Fetch {
                message: format!(
                    "index.json entry for {:?} is missing {}",
                    self.file,
                    missing.join(", ")
                ),
            })
        }
    }
}

#[derive(Debug, Deserialize)]
struct Index {
    #[serde(default)]
    schema: u64,
    #[serde(default)]
    license: String,
    #[serde(default)]
    license_url: String,
    #[serde(default)]
    artifacts: Vec<IndexRow>,
}

/// What a request asks for: a minor line, and optionally an exact patch that
/// is then a hard requirement (`docs/fetch.md` §2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Request {
    pub(crate) spelling: String,
    pub(crate) minor: String,
    pub(crate) exact: Option<String>,
}

const CHANNELS: [&str; 4] = ["lts", "stable", "prestable", "testing"];

/// `25.8.28.1-lts` → `25.8.28.1`; `25.8.28.1` unchanged.
fn strip_channel(v: &str) -> &str {
    match v.rsplit_once('-') {
        Some((bare, ch)) if CHANNELS.contains(&ch) => bare,
        _ => v,
    }
}

/// Numeric components for ordering: `25.10.7.6` > `25.8.28.1`.
fn version_key(v: &str) -> Vec<u64> {
    strip_channel(v)
        .split('.')
        .map(|p| p.parse().unwrap_or(0))
        .collect()
}

impl Request {
    /// `v25.8.28.1-lts` → line 25.8, exact `25.8.28.1-lts`; `25.8` → line 25.8.
    pub(crate) fn parse(spelling: &str) -> Result<Request> {
        let s = spelling.trim();
        let s = s.strip_prefix('v').unwrap_or(s);
        let bare = strip_channel(s);
        let parts: Vec<&str> = bare.split('.').collect();
        let numeric = parts.len() >= 2
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_digit()));
        if !numeric {
            return Err(Error::Fetch {
                message: format!(
                    "cannot make a ClickHouse version out of {spelling:?} (a line like 25.8, or \
                     an exact patch like 25.8.28.1-lts)"
                ),
            });
        }
        Ok(Request {
            spelling: spelling.to_string(),
            minor: format!("{}.{}", parts[0], parts[1]),
            exact: (parts.len() >= 4).then(|| s.to_string()),
        })
    }

    /// Does an installed or published version satisfy this request?
    pub(crate) fn accepts(&self, version: &str) -> bool {
        match &self.exact {
            None => super::minor_of(version) == self.minor,
            // `25.8.28.1` spelled without a channel matches `25.8.28.1-lts`;
            // spelled with one, only that.
            Some(exact) if exact.contains('-') => version == exact,
            Some(exact) => strip_channel(version) == exact,
        }
    }
}

/// A release, read and checked through `docs/fetch.md` §3 steps 0–1: the
/// listing is only ever used after `SHA256SUMS` has verified (or the policy
/// said, loudly, to skip that).
pub(crate) struct Release {
    index: Index,
    sums: BTreeMap<String, String>,
    /// The id of the key that verified `SHA256SUMS`, `None` under
    /// `CHTYPES_ALLOW_UNSIGNED=1`.
    pub(crate) signed_by: Option<String>,
    pub(crate) origin: String,
}

/// How many times [`Release::load`] reads an HTTP release before giving up.
const RELEASE_LOAD_ATTEMPTS: u32 = 3;
/// The wait between those reads: three attempts span about ten seconds, which
/// comfortably outlasts a one-object publish window.
const RELEASE_RETRY_DELAY: std::time::Duration = std::time::Duration::from_secs(4);

/// Is this error a symptom of reading a release mid-publish, and therefore
/// worth reading again? Exactly two are.
fn is_publish_window(err: &Error) -> bool {
    matches!(
        err,
        Error::ArtifactUntrusted { .. } | Error::ArtifactCorrupt { .. }
    )
}

impl Release {
    /// Step 0 (signature), then step 1 (the index).
    ///
    /// # Errors
    ///
    /// * [`Error::SourceUnreachable`] — nothing at the source, offline, or a
    ///   transport failure.
    /// * [`Error::ArtifactUntrusted`] — no `SHA256SUMS`, no `SHA256SUMS.sig`
    ///   (unless allowed), or a signature under no trusted key.
    /// * [`Error::Fetch`] — no `index.json`, or one this reader cannot use.
    fn load_once(source: &Source, policy: &TrustPolicy, progress: bool) -> Result<Release> {
        let origin = source.describe().to_string();
        let untrusted = |reason: String| Error::ArtifactUntrusted {
            origin: origin.clone(),
            reason,
        };

        let sums = match source.read("SHA256SUMS")? {
            Some(bytes) => bytes,
            None => {
                // Distinguish "no release here at all" from "a release without
                // its checksums": the first is a wrong source, the second an
                // untrustworthy one.
                if source.read("index.json")?.is_none() {
                    return Err(Error::SourceUnreachable {
                        origin: origin.clone(),
                        message: "no release here (no index.json, no SHA256SUMS)".into(),
                    });
                }
                return Err(untrusted("the release has no SHA256SUMS".into()));
            }
        };

        let signed_by = if policy.allow_unsigned() {
            eprintln!(
                "chtypes: WARNING: {} — SHA256SUMS from {origin} was NOT verified \
                 (CHTYPES_ALLOW_UNSIGNED=1)",
                super::trust::ALLOW_UNSIGNED_ENV
            );
            None
        } else {
            let sig = source.read("SHA256SUMS.sig")?.ok_or_else(|| {
                untrusted(
                    "the release is unsigned (no SHA256SUMS.sig); set CHTYPES_ALLOW_UNSIGNED=1 \
                     to install it anyway, loudly"
                        .into(),
                )
            })?;
            let key = policy.verify(&sums, &sig).map_err(untrusted)?;
            if progress {
                eprintln!("chtypes: SHA256SUMS signature verified (ed25519 key {key})");
            }
            Some(key)
        };

        let index_bytes = source.read("index.json")?.ok_or_else(|| Error::Fetch {
            message: format!("{origin} has SHA256SUMS but no index.json"),
        })?;
        let index: Index = serde_json::from_slice(&index_bytes).map_err(|e| Error::Fetch {
            message: format!("{origin}/index.json does not parse: {e}"),
        })?;
        if index.schema != 1 {
            return Err(Error::Fetch {
                message: format!(
                    "{origin}/index.json is schema {}, and this crate reads schema 1",
                    index.schema
                ),
            });
        }
        if progress && !index.license.is_empty() {
            eprintln!(
                "chtypes: artifacts are licensed under {} {} — LICENSE and NOTICE ship beside them",
                index.license, index.license_url
            );
        }

        let release = Release {
            index,
            sums: parse_sums(&sums),
            signed_by,
            origin,
        };
        release.cross_check_all()?;
        Ok(release)
    }

    /// [`Release::cross_check`] over every row the sums also name, run once for
    /// the whole release rather than only for the asset being installed.
    ///
    /// The per-asset check still stands on its own; this one exists so that a
    /// disagreement is seen while [`Release::load`] can still fix it by reading
    /// all three objects again. A row `SHA256SUMS` does not mention is skipped,
    /// not a disagreement: only the asset actually being installed has to be
    /// listed, and that is the per-asset check's business.
    fn cross_check_all(&self) -> Result<()> {
        for row in &self.index.artifacts {
            if self.sums.contains_key(&row.file) {
                self.cross_check(row)?;
            }
        }
        Ok(())
    }

    /// [`Release::load_once`], retried through a publish window.
    ///
    /// A publish into the rolling release is three objects — `SHA256SUMS`,
    /// `SHA256SUMS.sig`, `index.json` — and object storage cannot swap them
    /// atomically. They go up in that order, so an old index read against new
    /// sums still cross-checks; the unsafe window is between the sums and the
    /// signature that covers them, one small object wide and seconds long.
    ///
    /// The two symptoms of reading inside it — a signature that does not verify,
    /// and an index that disagrees with the sums — are retried. Nothing else is,
    /// and neither are these once the attempts run out: the same error surfaces,
    /// with the same exit code, as it did before. A tarball whose hash is wrong
    /// is never retried; that is the release lying about a byte.
    ///
    /// Only an HTTP source can be mid-publish, so a `file://` or directory
    /// source is read exactly once.
    pub(crate) fn load(source: &Source, policy: &TrustPolicy, progress: bool) -> Result<Release> {
        let attempts = if matches!(source, Source::Http { .. }) {
            RELEASE_LOAD_ATTEMPTS
        } else {
            1
        };
        let mut attempt = 1;
        loop {
            match Release::load_once(source, policy, progress) {
                Err(err) if attempt < attempts && is_publish_window(&err) => {
                    eprintln!(
                        "chtypes: {err} (attempt {attempt}/{attempts}) — this is what a release \
                         being published looks like from outside; retrying in {}s",
                        RELEASE_RETRY_DELAY.as_secs()
                    );
                    std::thread::sleep(RELEASE_RETRY_DELAY);
                    attempt += 1;
                }
                other => return other,
            }
        }
    }

    /// The artifacts' licence, as the listing names it (`Elastic-2.0`).
    pub(crate) fn license(&self) -> (&str, &str) {
        (&self.index.license, &self.index.license_url)
    }

    /// Every row for a platform, newest patch per minor line, in release order.
    pub(crate) fn all(&self, platform: &str) -> Vec<&IndexRow> {
        let mut best: BTreeMap<(u64, u64), &IndexRow> = BTreeMap::new();
        for row in self
            .index
            .artifacts
            .iter()
            .filter(|r| r.platform() == platform)
        {
            let key = minor_key(&row.clickhouse_minor);
            match best.get(&key) {
                Some(cur) if cur.rank() >= row.rank() => {}
                _ => {
                    best.insert(key, row);
                }
            }
        }
        best.into_values().collect()
    }

    /// Every row of the listing, as published.
    pub(crate) fn rows(&self) -> &[IndexRow] {
        &self.index.artifacts
    }

    /// The one row for a request on a platform (`docs/fetch.md` §2), complete
    /// and agreeing with `SHA256SUMS` (§3 step 2).
    ///
    /// # Errors
    ///
    /// * [`Error::ArtifactUnpublished`] — nothing for the platform, the line,
    ///   or the exact patch.
    /// * [`Error::Fetch`] — the row is missing a field.
    /// * [`Error::ArtifactCorrupt`] — `index.json` and `SHA256SUMS` disagree
    ///   about the asset, or `SHA256SUMS` has no line for it.
    pub(crate) fn select(&self, request: &Request, platform: &str) -> Result<&IndexRow> {
        let unpublished = |offered: String| Error::ArtifactUnpublished {
            requested: request.spelling.clone(),
            platform: platform.to_string(),
            origin: self.origin.clone(),
            offered,
        };
        let on_platform: Vec<&IndexRow> = self
            .index
            .artifacts
            .iter()
            .filter(|r| r.platform() == platform)
            .collect();
        if on_platform.is_empty() {
            let mut platforms: Vec<String> =
                self.index.artifacts.iter().map(|r| r.platform()).collect();
            platforms.sort();
            platforms.dedup();
            return Err(unpublished(if platforms.is_empty() {
                "nothing".into()
            } else {
                format!("platforms {}", platforms.join(", "))
            }));
        }
        let versions = || {
            on_platform
                .iter()
                .map(|r| r.clickhouse_version.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        };
        let hits: Vec<&IndexRow> = on_platform
            .iter()
            .copied()
            .filter(|r| request.accepts(&r.clickhouse_version))
            .collect();
        let row = hits
            .into_iter()
            .max_by_key(|r| r.rank())
            .ok_or_else(|| unpublished(versions()))?;
        row.check_complete()?;
        self.cross_check(row)?;
        Ok(row)
    }

    /// The signed `SHA256SUMS` entry for a release-level file, if it has one.
    /// Used for `sdk-goldens.json`, which is a row like any tarball.
    pub(crate) fn sum_for(&self, name: &str) -> Option<&str> {
        self.sums.get(name).map(String::as_str)
    }

    /// Step 2: the signed `SHA256SUMS` must list the asset with the index's
    /// sha256. A disagreement is a broken release, reported, not repaired.
    pub(crate) fn cross_check(&self, row: &IndexRow) -> Result<()> {
        match self.sums.get(&row.file) {
            None => Err(Error::ArtifactCorrupt {
                subject: format!("SHA256SUMS has no line for {}", row.file),
                expected: row.sha256.clone(),
                actual: "absent".into(),
            }),
            Some(sum) if sum != &row.sha256 => Err(Error::ArtifactCorrupt {
                subject: format!(
                    "index.json and SHA256SUMS disagree about {} — the release disagrees with \
                     itself; not installing it",
                    row.file
                ),
                expected: sum.clone(),
                actual: row.sha256.clone(),
            }),
            Some(_) => Ok(()),
        }
    }
}

fn minor_key(minor: &str) -> (u64, u64) {
    let mut it = minor.split('.');
    let a = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let b = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    (a, b)
}

/// `<sha256>  <file>` per line, `sha256sum` style; a leading `*` on the file
/// (binary mode) is tolerated.
fn parse_sums(bytes: &[u8]) -> BTreeMap<String, String> {
    let text = String::from_utf8_lossy(bytes);
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let mut it = line.split_whitespace();
        let (Some(sum), Some(file)) = (it.next(), it.next()) else {
            continue;
        };
        let file = file.strip_prefix('*').unwrap_or(file);
        out.insert(file.to_string(), sum.to_ascii_lowercase());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_request_is_a_line_or_an_exact_patch() {
        let line = Request::parse("25.8").unwrap();
        assert_eq!(line.minor, "25.8");
        assert_eq!(line.exact, None);
        assert!(line.accepts("25.8.28.1-lts"));
        assert!(line.accepts("25.8.30.16-lts"));
        assert!(!line.accepts("25.10.7.6-stable"));

        let exact = Request::parse("v25.8.28.1-lts").unwrap();
        assert_eq!(exact.minor, "25.8");
        assert_eq!(exact.exact.as_deref(), Some("25.8.28.1-lts"));
        assert!(exact.accepts("25.8.28.1-lts"));
        assert!(!exact.accepts("25.8.28.1-stable"));
        assert!(!exact.accepts("25.8.30.16-lts"));

        let no_channel = Request::parse("25.8.28.1").unwrap();
        assert!(no_channel.accepts("25.8.28.1-lts"));
        assert!(!no_channel.accepts("25.8.30.16-lts"));

        // Three components are not a patch: the line is what is asked for.
        assert_eq!(Request::parse("25.8.28").unwrap().exact, None);

        for bad in ["", "latest", "25", "25.x", "v", "25..8"] {
            assert!(Request::parse(bad).is_err(), "{bad:?} must not parse");
        }
    }

    #[test]
    fn sums_parse_both_spellings() {
        let sums = parse_sums(b"AB  a.tar.gz\ncd *b.tar.gz\n\nbad-line\n");
        assert_eq!(sums.get("a.tar.gz").unwrap(), "ab");
        assert_eq!(sums.get("b.tar.gz").unwrap(), "cd");
        assert_eq!(sums.len(), 2);
    }

    #[test]
    fn version_ordering_is_numeric() {
        assert!(version_key("25.10.7.6-stable") > version_key("25.8.28.1-lts"));
        assert!(version_key("25.8.30.16-lts") > version_key("25.8.28.1-lts"));
        assert_eq!(strip_channel("25.8.28.1-lts"), "25.8.28.1");
        assert_eq!(strip_channel("25.8.28.1"), "25.8.28.1");
    }
}

#[cfg(test)]
mod build_tests {
    use super::*;

    fn row(version: &str, file: &str, build: u32) -> IndexRow {
        IndexRow {
            file: file.into(),
            sha256: "aa".into(),
            bytes: 1,
            clickhouse_version: version.into(),
            clickhouse_minor: String::new(),
            os: "linux".into(),
            arch: "amd64".into(),
            library: "libchtypes.so".into(),
            library_sha256: "bb".into(),
            build,
            core_commit: String::new(),
        }
    }

    #[test]
    fn build_comes_from_the_field_then_the_name_then_zero() {
        assert_eq!(row("25.8.28.1-lts", "x.tar.gz", 0).build_number(), 0);
        assert_eq!(row("25.8.28.1-lts", "x-b3.tar.gz", 0).build_number(), 3);
        assert_eq!(row("25.8.28.1-lts", "x-b3.tar.gz", 7).build_number(), 7);
        assert_eq!(row("25.8.28.1-lts", "x-b12.tar.gz", 0).build_number(), 12);
        // Not a build suffix: no digits, or not the archive shape at all.
        assert_eq!(row("25.8.28.1-lts", "x-b.tar.gz", 0).build_number(), 0);
        assert_eq!(row("25.8.28.1-lts", "x-bfoo.tar.gz", 0).build_number(), 0);
        assert_eq!(row("25.8.28.1-lts", "x-b3.zip", 0).build_number(), 0);
    }

    #[test]
    fn a_newer_version_beats_a_higher_build_and_a_higher_build_beats_its_sibling() {
        let older = row("25.8.28.1-lts", "a-b1.tar.gz", 1);
        let newer_build = row("25.8.28.1-lts", "a-b2.tar.gz", 2);
        let newer_version = row("25.8.33.6-lts", "b.tar.gz", 0);
        assert!(newer_build.rank() > older.rank());
        assert!(newer_version.rank() > newer_build.rank());
    }
}
