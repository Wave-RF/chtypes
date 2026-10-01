//! `docs/guides/fetch.md` §3 steps 0–2: the release's `SHA256SUMS` (verified),
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
    /// The `chs_*` ABI revision the artifact was built from, or `None` when the
    /// row declares none — or declares something that is not an integer, which
    /// is read as none rather than failing the whole listing. The artifact
    /// producer writes the field from the revision that introduced it onward,
    /// so a row without it is an older revision. Fetch installs only a row at
    /// this crate's own [`crate::ABI_REVISION`] (`docs/guides/fetch.md` §2): any
    /// other would be refused at load.
    #[serde(default, deserialize_with = "lenient_revision")]
    pub abi_revision: Option<i32>,
}

/// `abi_revision` as an `i32` when it is a JSON integer, else `None`.
fn lenient_revision<'de, D>(deserializer: D) -> std::result::Result<Option<i32>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = serde_json::Value::deserialize(deserializer)?;
    Ok(value.as_i64().and_then(|n| i32::try_from(n).ok()))
}

/// Rows that carry no `abi_revision`, named with the reason: the artifact
/// producer records the field from the revision that introduced it onward.
/// Every unpublished message, `--all` warning and `list` note says this in
/// these words rather than that the release serves "none".
const NO_RECORDED_REVISION: &str =
    "rows that record no ABI revision (built before revisions were recorded)";

/// What the release DOES have for `noun`, for an unpublished message: the ABI
/// revision(s) its rows carry, or that it has none at any revision.
fn served(rows: &[&IndexRow], noun: &str) -> String {
    if rows.is_empty() {
        return format!("the release does not have {noun} at any ABI revision");
    }
    let mut revisions: Vec<i32> = rows.iter().filter_map(|r| r.abi_revision).collect();
    revisions.sort_unstable();
    revisions.dedup();
    if revisions.is_empty() {
        return format!("the release has {noun} only in {NO_RECORDED_REVISION}");
    }
    let mut said = if revisions.len() == 1 {
        format!("ABI revision {}", revisions[0])
    } else {
        let list: Vec<String> = revisions.iter().map(ToString::to_string).collect();
        format!("ABI revisions {}", list.join(", "))
    };
    if rows.iter().any(|r| r.abi_revision.is_none()) {
        said.push_str(" and in ");
        said.push_str(NO_RECORDED_REVISION);
    }
    format!("the release has {noun} only at {said}")
}

/// One message per line the release has for `platform` only at another ABI
/// revision, or only in rows that record none — the lines `--all` installs
/// nothing for — in numeric line order.
pub(crate) fn skipped_lines(rows: &[IndexRow], platform: &str, revision: i32) -> Vec<String> {
    let mut by_line: BTreeMap<(u64, u64), (String, Vec<&IndexRow>)> = BTreeMap::new();
    for row in rows.iter().filter(|r| r.platform() == platform) {
        by_line
            .entry(minor_key(&row.clickhouse_minor))
            .or_insert_with(|| (row.clickhouse_minor.clone(), Vec::new()))
            .1
            .push(row);
    }
    by_line
        .into_values()
        .filter(|(_, rows)| !rows.iter().any(|r| r.abi_revision == Some(revision)))
        .map(|(line, rows)| {
            format!(
                "ClickHouse line {line} on {platform} is not installed: {}, and this SDK speaks \
                 ABI revision {revision}",
                served(&rows, &format!("that line for {platform}"))
            )
        })
        .collect()
}

/// `list`'s one line naming the platform's rows at another ABI revision, which
/// it does not show (`docs/guides/fetch.md` §6); `None` when none were hidden.
/// Rows that carry no `abi_revision` are named for what they are — built before
/// revisions were recorded — never as a revision called "none".
pub(crate) fn not_shown(rows: &[IndexRow], platform: &str, revision: i32) -> Option<String> {
    let hidden: Vec<&IndexRow> = rows
        .iter()
        .filter(|r| r.platform() == platform && r.abi_revision != Some(revision))
        .collect();
    if hidden.is_empty() {
        return None;
    }
    let mut revisions: Vec<i32> = hidden.iter().filter_map(|r| r.abi_revision).collect();
    let declared = revisions.len();
    let undeclared = hidden.len() - declared;
    revisions.sort_unstable();
    revisions.dedup();
    let mut parts: Vec<String> = Vec::new();
    if declared > 0 {
        let list: Vec<String> = revisions.iter().map(ToString::to_string).collect();
        parts.push(format!(
            "{declared} row(s) at ABI revision(s) {}",
            list.join(", ")
        ));
    }
    if undeclared > 0 {
        parts.push(format!(
            "{undeclared} row(s) that record no ABI revision (built before revisions were recorded)"
        ));
    }
    Some(format!(
        "{} not shown; this SDK speaks {revision}",
        parts.join(" and ")
    ))
}

/// The `offered` half of an [`Error::ArtifactUnpublished`] that the ABI
/// revision decided: what the release has for the request, then what it has
/// at `revision`, this crate's.
pub(crate) fn unpublished_at_revision(
    at_revision: &[&IndexRow],
    any_revision: &[&IndexRow],
    revision: i32,
    noun: &str,
) -> String {
    let have: Vec<&str> = at_revision
        .iter()
        .map(|r| r.clickhouse_version.as_str())
        .collect();
    let have = if have.is_empty() {
        "nothing".to_string()
    } else {
        have.join(", ")
    };
    format!(
        "{}; at ABI revision {revision} (this SDK's) the release has: {have}.",
        served(any_revision, noun)
    )
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
/// is then a hard requirement (`docs/guides/fetch.md` §2).
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

/// Numeric components for ordering: `25.10.7.6` > `25.8.28.1`. `pub(crate)`
/// so `super::lock` can order lock keys by the same rule (F1, F6).
pub(crate) fn version_key(v: &str) -> Vec<u64> {
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

/// A release, read and checked through `docs/guides/fetch.md` §3 steps 0–1: the
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

/// How many times [`Release::load`] (and `install_goldens`'s own retry, in
/// `super::install_goldens`) reads an HTTP release before giving up.
pub(crate) const RELEASE_LOAD_ATTEMPTS: u32 = 5;

/// The BASE delay before the first retry — each later retry doubles it, so
/// the default 5 attempts sleep 4+8+16+32 = 60s (~70s wall with network
/// time). That is chosen to outlast the artifacts host's edge cache (observed
/// `Cache-Control: max-age=60` on the mutable release objects, so a stale
/// pairing of any of them can persist up to 60s), while a genuine few-second
/// mid-publish window still clears on the second attempt.
///
/// `CHTYPES_FETCH_TEST_RETRY_DELAY_MS` overrides it, in milliseconds — an
/// internal, undocumented test hook, never part of the public contract
/// (`docs/guides/fetch.md` documents no such variable for this crate). It exists
/// because `tests/fetch.rs` is a separate crate that cannot reach this
/// `pub(crate)` constant directly to shrink it; `rerun`'s isolated child
/// environment sets it instead of mutating this process's own environment,
/// which `cargo test`'s parallelism would make racy.
pub(crate) fn release_retry_delay() -> std::time::Duration {
    if let Ok(raw) = std::env::var("CHTYPES_FETCH_TEST_RETRY_DELAY_MS") {
        if let Ok(ms) = raw.parse::<u64>() {
            return std::time::Duration::from_millis(ms);
        }
    }
    std::time::Duration::from_secs(4)
}

/// The total sleep time the default doubling schedule spends across every
/// attempt but the last — 60s for the default 5 attempts — and the cap
/// chtypes#365 puts on a source's own `Retry-After`: honored only as long as
/// honoring it still fits inside this, so a 503 asking for far longer than
/// this budget was ever sized for fails fast instead of blocking for it.
pub(crate) fn retry_budget() -> std::time::Duration {
    if RELEASE_LOAD_ATTEMPTS <= 1 {
        return std::time::Duration::ZERO;
    }
    release_retry_delay() * ((1u32 << (RELEASE_LOAD_ATTEMPTS - 1)) - 1)
}

/// Is `err` worth retrying through the §3a budget — either a publish-window
/// symptom (a signature under no trusted key, or an index/sums/release-file
/// disagreement — both raise `Error::ArtifactCorrupt`/`ArtifactUntrusted`) or,
/// since chtypes#365, a retryable source-level failure (an HTTP 5xx/408/429 or
/// a connection-level failure, as [`Source::open`](super::source::Source::open)
/// classifies it). The second half's own requested wait (`Retry-After` on a
/// 503/429) comes back `Some` only when the source sent one; a publish-window
/// symptom never carries one.
///
/// A tarball hash or size mismatch is never routed through this: it raises
/// `Error::ArtifactCorrupt` too, but `install::fetch_and_install` reports it
/// directly, never through a retryable `Error::SourceUnreachable`, and
/// nothing here is called on it — "a retry buys time; it never converts a
/// refusal into an install" still holds.
pub(crate) fn retry_info(err: &Error) -> (bool, Option<std::time::Duration>) {
    match err {
        Error::ArtifactUntrusted { .. } | Error::ArtifactCorrupt { .. } => (true, None),
        Error::SourceUnreachable {
            retryable,
            retry_after,
            ..
        } => (*retryable, *retry_after),
        _ => (false, None),
    }
}

/// "1 attempt" or "<n> attempts", for a message that names how many were made.
pub(crate) fn attempt_word(n: u32) -> String {
    if n == 1 {
        "1 attempt".to_string()
    } else {
        format!("{n} attempts")
    }
}

/// Appends a sentence naming the attempt count to an exhausted retryable
/// `Error::SourceUnreachable`'s own message, in place — `origin`/`retryable`/
/// `retry_after` and the artifact code are unchanged, only the text grows.
/// The pre-existing publish-window symptoms (`ArtifactUntrusted`/
/// `ArtifactCorrupt`) are returned unchanged: their exhaustion message is not
/// part of chtypes#365, and splicing text into their own differently-shaped
/// Display string is not worth the awkward grammar for no behavior change.
pub(crate) fn with_retry_note(err: Error, note: &str) -> Error {
    match err {
        Error::SourceUnreachable {
            origin,
            message,
            retryable,
            retry_after,
        } => Error::SourceUnreachable {
            origin,
            message: message + note,
            retryable,
            retry_after,
        },
        other => other,
    }
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
    ///
    /// `pub(crate)`, not just `load`'s private helper: `super::install_goldens`
    /// calls it directly for its OWN retry, to re-read the whole consistent set
    /// fresh on a golden-set window symptom without going through `load`'s
    /// nested retry loop.
    pub(crate) fn load_once(
        source: &Source,
        policy: &TrustPolicy,
        progress: bool,
    ) -> Result<Release> {
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
                        retryable: false,
                        retry_after: None,
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

    /// [`Release::load_once`], retried through a publish window — and, since
    /// chtypes#365, through a transient source-level blip too.
    ///
    /// A publish into the rolling release is three objects — `SHA256SUMS`,
    /// `SHA256SUMS.sig`, `index.json` — plus, in `super::install_goldens`'s own
    /// retry, a fourth release-level file. Object storage cannot swap any of
    /// these atomically, and the edge cache in front of the artifacts host
    /// widens the unsafe window from "between two uploads" to "as long as any
    /// one object can still be served stale from cache", which measures the
    /// same as its `Cache-Control` max-age.
    ///
    /// Retried, all through [`retry_info`]: a signature that does not verify;
    /// an index that disagrees with the sums; (via [`Release::read_goldens`])
    /// a release-level file whose hash disagrees with its own SHA256SUMS row,
    /// or that the sums list but the source does not yet serve; and, since
    /// chtypes#365, an HTTP 5xx/408/429 or a connection-level failure
    /// reaching the host at all. A `Retry-After` the source sends on a 503 or
    /// 429 is honored in place of the doubling schedule's own delay, capped
    /// so the total never exceeds [`retry_budget`] — one that does not fit
    /// fails at once, naming the requested delay, rather than blocking for
    /// it. Every retry re-reads the WHOLE set from scratch — never one
    /// freshly re-fetched object checked against another attempt's stale
    /// one. Nothing else is retried, and neither are these once the attempts
    /// (or the budget) run out: the same error surfaces, with the same exit
    /// code, naming how many attempts were made. A 404/410, and a tarball
    /// whose hash is wrong, are never retried — the release lying about a
    /// byte, or simply not having the thing, is not a transient blip.
    ///
    /// Only an HTTP source can be mid-publish, so a `file://` or directory
    /// source is read exactly once.
    pub(crate) fn load(source: &Source, policy: &TrustPolicy, progress: bool) -> Result<Release> {
        let attempts = if matches!(source, Source::Http { .. }) {
            RELEASE_LOAD_ATTEMPTS
        } else {
            1
        };
        let mut delay = release_retry_delay();
        let mut elapsed = std::time::Duration::ZERO;
        let mut attempt = 1;
        loop {
            match Release::load_once(source, policy, progress) {
                Err(err) => {
                    let (retryable, retry_after) = retry_info(&err);
                    if !retryable {
                        return Err(err);
                    }
                    if attempt >= attempts {
                        return Err(with_retry_note(
                            err,
                            &format!(" — giving up after {}", attempt_word(attempt)),
                        ));
                    }
                    let wait = retry_after.unwrap_or(delay);
                    if elapsed + wait > retry_budget() {
                        return Err(with_retry_note(
                            err,
                            &format!(
                                " — the source asked to wait {wait:?} before retrying, which \
                                 would exceed the {:?} retry budget; giving up after {}",
                                retry_budget(),
                                attempt_word(attempt)
                            ),
                        ));
                    }
                    eprintln!(
                        "chtypes: {err} (attempt {attempt}/{attempts}) — this is what a release \
                         being published (or briefly unreachable) looks like from outside; \
                         retrying in {wait:?}"
                    );
                    std::thread::sleep(wait);
                    elapsed += wait;
                    delay *= 2;
                    attempt += 1;
                }
                other => return other,
            }
        }
    }

    /// The artifacts' license, as the listing names it (`Elastic-2.0`).
    pub(crate) fn license(&self) -> (&str, &str) {
        (&self.index.license, &self.index.license_url)
    }

    /// Every row for a platform at this crate's ABI revision, newest patch
    /// (then highest build) per minor line, in release order. Rows of any
    /// other revision, and rows that record none, are never offered
    /// (`docs/guides/fetch.md` §2).
    pub(crate) fn all(&self, platform: &str) -> Vec<&IndexRow> {
        let revision = super::fetch_abi_revision();
        let mut best: BTreeMap<(u64, u64), &IndexRow> = BTreeMap::new();
        for row in self
            .index
            .artifacts
            .iter()
            .filter(|r| r.platform() == platform && r.abi_revision == Some(revision))
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

    /// The one row for a request on a platform (`docs/guides/fetch.md` §2), complete
    /// and agreeing with `SHA256SUMS` (§3 step 2). Only rows at this crate's
    /// ABI revision are considered, FIRST; the newest version, then the highest
    /// build, is taken among them — never another revision's row.
    ///
    /// # Errors
    ///
    /// * [`Error::ArtifactUnpublished`] — nothing for the platform, the line,
    ///   or the exact patch at this crate's ABI revision.
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
                "it has nothing.".into()
            } else {
                format!("it has platforms {}.", platforms.join(", "))
            }));
        }
        let revision = super::fetch_abi_revision();
        let at_revision: Vec<&IndexRow> = on_platform
            .iter()
            .copied()
            .filter(|r| r.abi_revision == Some(revision))
            .collect();
        let any_revision: Vec<&IndexRow> = on_platform
            .iter()
            .copied()
            .filter(|r| request.accepts(&r.clickhouse_version))
            .collect();
        let row = any_revision
            .iter()
            .copied()
            .filter(|r| r.abi_revision == Some(revision))
            .max_by_key(|r| r.rank())
            .ok_or_else(|| {
                let noun = if request.exact.is_some() {
                    format!("that patch for {platform}")
                } else {
                    format!("that line for {platform}")
                };
                unpublished(unpublished_at_revision(
                    &at_revision,
                    &any_revision,
                    revision,
                    &noun,
                ))
            })?;
        row.check_complete()?;
        self.cross_check(row)?;
        Ok(row)
    }

    /// The signed `SHA256SUMS` entry for a release-level file, if it has one.
    /// Used for `sdk-goldens.json`, which is a row like any tarball.
    pub(crate) fn sum_for(&self, name: &str) -> Option<&str> {
        self.sums.get(name).map(String::as_str)
    }

    /// One look at a release-level file (`sdk-goldens.json`) against THIS
    /// release's own, already-verified sums. `Ok(None)` when the release
    /// simply does not list `name` at all — not an error, and not a window
    /// symptom. A hash mismatch, or the sums listing it while `source` does
    /// not (yet) serve it, raises `Error::ArtifactCorrupt` — exactly what
    /// [`retry_info`] recognizes, so `super::install_goldens` can retry it
    /// the same way [`Release::load`] retries the signature and the index.
    pub(crate) fn read_goldens(&self, source: &Source, name: &str) -> Result<Option<Vec<u8>>> {
        let Some(want) = self.sum_for(name) else {
            return Ok(None);
        };
        let want = want.to_string();
        match source.read(name)? {
            None => Err(Error::ArtifactCorrupt {
                subject: format!(
                    "SHA256SUMS lists {name} but {} does not serve it — the release disagrees \
                     with itself; not installing it",
                    source.describe()
                ),
                expected: want,
                actual: "absent".into(),
            }),
            Some(blob) => {
                let got = super::trust::sha256_hex(&blob);
                if got == want {
                    Ok(Some(blob))
                } else {
                    Err(Error::ArtifactCorrupt {
                        subject: format!(
                            "{name} disagrees with the signed SHA256SUMS — not installing it"
                        ),
                        expected: want,
                        actual: got,
                    })
                }
            }
        }
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
            abi_revision: Some(crate::ABI_REVISION),
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

/// `docs/guides/fetch.md` §2: only rows at the crate's own ABI revision are ever
/// selected, FIRST. The revision under test is always [`crate::ABI_REVISION`]
/// itself (or one past it), never a typed number.
#[cfg(test)]
mod revision_tests {
    use super::*;

    const PLATFORM: &str = "linux-amd64";

    fn row(version: &str, build: u32, abi_revision: Option<i32>) -> IndexRow {
        IndexRow {
            file: format!("chtypes-{version}-{PLATFORM}-b{build}.tar.gz"),
            sha256: "a".repeat(64),
            bytes: 1,
            clickhouse_version: version.into(),
            clickhouse_minor: super::super::minor_of(version),
            os: "linux".into(),
            arch: "amd64".into(),
            library: "libchtypes.so".into(),
            library_sha256: "b".repeat(64),
            build,
            core_commit: String::new(),
            abi_revision,
        }
    }

    /// A release over exactly these rows, each one listed in its signed sums.
    fn release(rows: Vec<IndexRow>) -> Release {
        let sums = rows
            .iter()
            .map(|r| (r.file.clone(), r.sha256.clone()))
            .collect();
        Release {
            index: Index {
                schema: 1,
                license: String::new(),
                license_url: String::new(),
                artifacts: rows,
            },
            sums,
            signed_by: None,
            origin: "synthetic".into(),
        }
    }

    fn request(spelling: &str) -> Request {
        Request::parse(spelling).unwrap()
    }

    /// (a) Another revision's row with a HIGHER build, and a NEWER version in a
    /// row that declares none, both lose to the one row at the crate's own
    /// revision — in every listing order, for a line, an exact patch and --all.
    #[test]
    fn a_higher_build_at_another_revision_never_beats_the_own_revision() {
        let own = crate::ABI_REVISION;
        let mine = row("25.8.28.1-lts", 10, Some(own));
        let higher_build_elsewhere = row("25.8.28.1-lts", 20, Some(own + 1));
        let newer_undeclared = row("25.8.33.6-lts", 30, None);
        for order in [
            vec![
                mine.clone(),
                higher_build_elsewhere.clone(),
                newer_undeclared.clone(),
            ],
            vec![
                newer_undeclared.clone(),
                higher_build_elsewhere.clone(),
                mine.clone(),
            ],
            vec![
                higher_build_elsewhere.clone(),
                mine.clone(),
                newer_undeclared.clone(),
            ],
        ] {
            let rel = release(order);
            for spelling in ["25.8", "25.8.28.1-lts", "25.8.28.1"] {
                let got = rel.select(&request(spelling), PLATFORM).unwrap();
                assert_eq!(got.file, mine.file, "{spelling}");
            }
            let all = rel.all(PLATFORM);
            let files: Vec<&str> = all.iter().map(|r| r.file.as_str()).collect();
            assert_eq!(files, vec![mine.file.as_str()]);
        }
    }

    /// (b) Only another revision served: `CHTYPES_ARTIFACT_UNPUBLISHED` (exit 4),
    /// never a fallback, naming the crate's revision and the served one.
    #[test]
    fn only_another_revision_served_is_unpublished_naming_both() {
        let own = crate::ABI_REVISION;
        let other = own + 1;
        let rel = release(vec![
            row("25.8.28.1-lts", 20, Some(other)),
            row("26.7.3.19-stable", 20, Some(other)),
        ]);
        for spelling in ["25.8", "25.8.28.1-lts"] {
            let err = rel.select(&request(spelling), PLATFORM).unwrap_err();
            assert_eq!(err.artifact_code(), Some("CHTYPES_ARTIFACT_UNPUBLISHED"));
            let message = err.to_string();
            assert!(
                message.contains(&format!("ABI revision {own} (this SDK's)")),
                "{message}"
            );
            assert!(
                message.contains(&format!("only at ABI revision {other}")),
                "{message}"
            );
        }
        assert!(rel.all(PLATFORM).is_empty());
    }

    /// --all never skips a line silently: every line the release has only at
    /// another revision, or only in rows that record none, gets one message —
    /// line, platform, what the release serves, the crate's revision — oldest
    /// line first; a line with a row at the crate's revision gets none.
    #[test]
    fn every_line_served_only_at_another_revision_is_named() {
        let own = crate::ABI_REVISION;
        let rows = vec![
            row("26.7.3.19-stable", 1, Some(own + 1)),
            row("25.8.28.1-lts", 1, Some(own)),
            row("24.8.14.39-lts", 1, None),
        ];
        assert_eq!(
            skipped_lines(&rows, PLATFORM, own),
            vec![
                format!(
                    "ClickHouse line 24.8 on {PLATFORM} is not installed: the release has that \
                     line for {PLATFORM} only in rows that record no ABI revision (built before \
                     revisions were recorded), and this SDK speaks ABI revision {own}"
                ),
                format!(
                    "ClickHouse line 26.7 on {PLATFORM} is not installed: the release has that \
                     line for {PLATFORM} only at ABI revision {}, and this SDK speaks ABI \
                     revision {own}",
                    own + 1
                ),
            ]
        );
        assert!(skipped_lines(&rows[1..2], PLATFORM, own).is_empty());
    }

    /// list's one line: how many rows at which revision(s) it did not show;
    /// nothing when it hid nothing.
    #[test]
    fn list_names_what_it_hides_and_nothing_else() {
        let own = crate::ABI_REVISION;
        let mut rows = vec![row("25.8.28.1-lts", 1, Some(own))];
        assert_eq!(not_shown(&rows, PLATFORM, own), None);
        rows.push(row("26.7.3.19-stable", 1, Some(own + 1)));
        assert_eq!(
            not_shown(&rows, PLATFORM, own),
            Some(format!(
                "1 row(s) at ABI revision(s) {} not shown; this SDK speaks {own}",
                own + 1
            ))
        );
        rows.push(row("24.8.14.39-lts", 1, None));
        assert_eq!(
            not_shown(&rows, PLATFORM, own),
            Some(format!(
                "1 row(s) at ABI revision(s) {} and 1 row(s) that record no ABI revision \
                 (built before revisions were recorded) not shown; this SDK speaks {own}",
                own + 1
            ))
        );
    }

    /// (c) A row with no `abi_revision` — absent, or not an integer — is never
    /// selected, even when it is the only row there is.
    #[test]
    fn a_row_without_an_abi_revision_is_never_selected() {
        let rel = release(vec![row("25.8.28.1-lts", 0, None)]);
        let err = rel.select(&request("25.8"), PLATFORM).unwrap_err();
        assert_eq!(err.artifact_code(), Some("CHTYPES_ARTIFACT_UNPUBLISHED"));
        // The reason is named — rows built before revisions were recorded —
        // never that the release serves "none".
        let message = err.to_string();
        assert!(
            message.contains(&format!(
                "the release has that line for {PLATFORM} only in rows that record no ABI \
                 revision (built before revisions were recorded)"
            )),
            "{message}"
        );
        assert!(
            message.contains(&format!(
                "ABI revision {} (this SDK's)",
                crate::ABI_REVISION
            )),
            "{message}"
        );
        assert!(!message.contains("none"), "{message}");
        assert!(rel.all(PLATFORM).is_empty());

        // What index.json can carry: the revision as a JSON integer is one; as
        // a string, a float, null or a bool it is not — and none of them fails
        // the rest of the row.
        let own = crate::ABI_REVISION;
        for (raw, want) in [
            (serde_json::json!(own), Some(own)),
            (serde_json::json!(own.to_string()), None),
            (serde_json::json!(f64::from(own)), None),
            (serde_json::Value::Null, None),
            (serde_json::json!(true), None),
        ] {
            let parsed: IndexRow = serde_json::from_value(
                serde_json::json!({"file": "x.tar.gz", "abi_revision": raw}),
            )
            .unwrap();
            assert_eq!(parsed.abi_revision, want);
            assert_eq!(parsed.file, "x.tar.gz");
        }
        let absent: IndexRow =
            serde_json::from_value(serde_json::json!({"file": "x.tar.gz"})).unwrap();
        assert_eq!(absent.abi_revision, None);
    }
}
