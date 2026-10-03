//! `docs/guides/fetch.md` §5: the lock file — per `<os>-<arch>/<clickhouse_version>`
//! (schema 2, since #284; a schema-1 file, keyed per `<os>-<arch>/<minor>`, is
//! still read and converted the first time this crate writes it), the asset
//! file and sha256 that were installed. Trust on first fetch, byte-identical
//! thereafter.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::release::{IndexRow, Request, version_key};
use crate::legacy::error::{Error, Result};

/// The lock file's default name, for `--frozen` without `--lock`.
pub const DEFAULT_LOCK_FILE: &str = "chtypes.lock";

/// The lock file schema this crate WRITES. A schema-1 file (`docs/guides/fetch.md`
/// §5, before #284) is still READ — see [`LockFile::load`] — and converted the
/// next time this crate writes it; [`LockFile::save`] always writes schema 2.
pub const LOCK_SCHEMA: u64 = 2;

/// The lock schema this crate last read, before converting — schema 1's own
/// number, kept only so [`LockFile::load`] can accept both without a second
/// constant named `1`.
const READABLE_SCHEMA_1: u64 = 1;

/// One pinned asset.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LockEntry {
    /// The asset file name.
    pub file: String,
    /// Its sha256.
    pub sha256: String,
    /// The ABI revision the pinned row carried when this entry was written.
    /// Optional and additive (`docs/guides/fetch.md` §5): an entry written
    /// before this SDK recorded it simply has none — `None`, never a typed
    /// number, and never serialized as `null`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub abi_revision: Option<i32>,
}

/// The lock file. Always schema 2 once loaded — [`LockFile::load`] converts a
/// schema-1 file in memory — and [`LockFile::save`] always writes schema 2.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockFile {
    /// [`LOCK_SCHEMA`], always, after [`LockFile::load`] returns.
    pub schema: u64,
    /// `<os>-<arch>/<clickhouse_version>` → the asset installed for it.
    /// Several patches of one line are several entries.
    #[serde(default)]
    pub artifacts: BTreeMap<String, LockEntry>,
    #[serde(skip)]
    path: PathBuf,
}

/// The lock key for a platform and an EXACT patch (`docs/guides/fetch.md`
/// §5, schema 2): `<os>-<arch>/<clickhouse_version>`. Before #284 this took a
/// minor line; a pin now names the exact patch it installed, so several
/// patches of one line coexist as separate entries under separate keys.
pub fn lock_key(platform: &str, version: &str) -> String {
    format!("{platform}/{version}")
}

impl LockFile {
    /// Read `path`, or start an empty (schema 2) lock when it does not exist
    /// (`must_exist` refuses that — `--frozen` needs a real file).
    ///
    /// A schema-1 file is read too, and its entries are converted to schema
    /// 2 in memory (`docs/guides/fetch.md` §5 "Reading schema 1"): a
    /// `<os>-<arch>/<minor>` entry becomes `<os>-<arch>/<v>`, `<v>` being the
    /// `clickhouse_version` its `file` names under the artifacts.md asset
    /// grammar (`chtypes-<v>-<os>-<arch>[-b<N>].tar.gz`), with `<v>`'s line
    /// checked against `<minor>`. An entry whose `file` does not parse that
    /// way makes the WHOLE lock unreadable, naming the entry — never a
    /// silent drop of just that one pin.
    ///
    /// # Errors
    ///
    /// [`Error::Fetch`] — the file does not parse, is a schema this crate
    /// does not read (only 1 and 2), or a schema-1 entry's `file` does not
    /// parse into a version. [`Error::ArtifactPinned`] — `must_exist` and the
    /// file does not exist.
    pub fn load(path: &Path, must_exist: bool) -> Result<LockFile> {
        match std::fs::read(path) {
            Ok(bytes) => {
                let mut lock: LockFile =
                    serde_json::from_slice(&bytes).map_err(|e| Error::Fetch {
                        message: format!("lock file {}: {e}", path.display()),
                    })?;
                match lock.schema {
                    LOCK_SCHEMA => {}
                    READABLE_SCHEMA_1 => {
                        lock.artifacts = convert_schema1(lock.artifacts, path)?;
                        lock.schema = LOCK_SCHEMA;
                    }
                    other => {
                        return Err(Error::Fetch {
                            message: format!(
                                "lock file {} is schema {other}, and this crate reads schema \
                                 {READABLE_SCHEMA_1} and {LOCK_SCHEMA}",
                                path.display()
                            ),
                        });
                    }
                }
                // Every schema-2 key names an EXACT patch, never a line —
                // `convert_schema1` already guarantees this for a converted
                // file, but a hand-written or another SDK's schema-2 file
                // could still carry a line-shaped key, and reading it as one
                // would silently narrow every line request pinned under it
                // to a single, wrong "patch". Refused loudly instead.
                for key in lock.artifacts.keys() {
                    let Some((_, version)) = key.split_once('/') else {
                        return Err(Error::Fetch {
                            message: format!(
                                "lock file {}: entry {key:?} is not <os>-<arch>/<clickhouse_version>",
                                path.display()
                            ),
                        });
                    };
                    let is_patch = Request::parse(version).is_ok_and(|r| r.exact.is_some());
                    if !is_patch {
                        return Err(Error::Fetch {
                            message: format!(
                                "lock file {}: entry {key:?} names a LINE ({version:?}), not an \
                                 exact ClickHouse patch — schema 2 keys must be exact versions",
                                path.display()
                            ),
                        });
                    }
                }
                lock.path = path.to_path_buf();
                Ok(lock)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && !must_exist => Ok(LockFile {
                schema: LOCK_SCHEMA,
                artifacts: BTreeMap::new(),
                path: path.to_path_buf(),
            }),
            // `--frozen` with no lock file: nothing is pinned, so nothing is
            // installed — PINNED, like every other refusal under `--frozen`
            // (docs/guides/fetch.md, Decisions).
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(Error::ArtifactPinned {
                key: path.display().to_string(),
                message: "--frozen, but there is no such lock file; nothing is pinned, so nothing is installed".into(),
            }),
            Err(e) => Err(Error::Fetch {
                message: format!("lock file {}: {e}", path.display()),
            }),
        }
    }

    /// The file this lock was read from and writes to.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// F1: the entries pinned for `platform` that satisfy `request` — the
    /// same [`Request::accepts`] matching rule selection itself uses
    /// (Decision 7). A line spelling collects every entry pinning a patch of
    /// that line; an exact patch collects at most one. Sorted oldest to
    /// newest, so [`LockFile::best_candidate`] is the last.
    pub(crate) fn candidates<'a>(
        &'a self,
        platform: &str,
        request: &Request,
    ) -> Vec<(&'a str, &'a LockEntry)> {
        let prefix = format!("{platform}/");
        let mut out: Vec<(&str, &LockEntry)> = self
            .artifacts
            .iter()
            .filter_map(|(k, e)| {
                let version = k.strip_prefix(prefix.as_str())?;
                request.accepts(version).then_some((k.as_str(), e))
            })
            .collect();
        out.sort_by_key(|(k, _)| version_key(&k[prefix.len()..]));
        out
    }

    /// F1's actual selection: the newest of [`LockFile::candidates`] — a line
    /// spelling's several pins collapse to one (the newest pinned patch of
    /// the line); an exact patch has at most one candidate anyway.
    pub(crate) fn best_candidate<'a>(
        &'a self,
        platform: &str,
        request: &Request,
    ) -> Option<(&'a str, &'a LockEntry)> {
        self.candidates(platform, request).into_iter().next_back()
    }

    /// Every entry pinned for `platform`, grouped by minor line, each with
    /// the newest patch pinned for it (F1) — what `--all --frozen` installs
    /// (F6), before the release is consulted to verify each one.
    pub(crate) fn lines_for(&self, platform: &str) -> BTreeMap<String, (String, LockEntry)> {
        let prefix = format!("{platform}/");
        let mut out: BTreeMap<String, (String, LockEntry)> = BTreeMap::new();
        for (key, entry) in &self.artifacts {
            let Some(version) = key.strip_prefix(prefix.as_str()) else {
                continue;
            };
            let minor = super::minor_of(version);
            let better = match out.get(&minor) {
                None => true,
                Some((existing_key, _)) => {
                    version_key(version) > version_key(&existing_key[prefix.len()..])
                }
            };
            if better {
                out.insert(minor, (key.clone(), entry.clone()));
            }
        }
        out
    }

    /// Record what was installed for `key` (`docs/guides/fetch.md` §5's
    /// `<os>-<arch>/<clickhouse_version>`), including the ABI revision the
    /// selected row carries — this crate's own, since selection never picks
    /// any other (§2). Removes nothing: a stale entry for another patch of
    /// the same line, or for another platform, is left exactly as it was
    /// (§6 — `--lock` never un-pins). Returns whether anything changed.
    pub fn record(&mut self, key: &str, installed: &IndexRow) -> bool {
        let entry = LockEntry {
            file: installed.file.clone(),
            sha256: installed.sha256.clone(),
            abi_revision: installed.abi_revision,
        };
        let changed = self.artifacts.get(key) != Some(&entry);
        self.artifacts.insert(key.to_string(), entry);
        changed
    }

    /// Write the lock back, atomically (a temp sibling, then rename) —
    /// always as schema 2, whatever schema it was read as.
    pub fn save(&self) -> Result<()> {
        let mut out = self.clone();
        out.schema = LOCK_SCHEMA;
        let text = serde_json::to_string_pretty(&out).map_err(|e| Error::Fetch {
            message: format!("serializing the lock file: {e}"),
        })? + "\n";
        let tmp = self
            .path
            .with_extension(format!("tmp.{}", std::process::id()));
        let io = |e: std::io::Error| Error::Fetch {
            message: format!("writing lock file {}: {e}", self.path.display()),
        };
        std::fs::write(&tmp, text).map_err(io)?;
        std::fs::rename(&tmp, &self.path).map_err(|e| {
            std::fs::remove_file(&tmp).ok();
            io(e)
        })
    }
}

/// [`LockFile::load`]'s schema-1 conversion: `<os>-<arch>/<minor>` entries
/// become `<os>-<arch>/<v>` entries, `<v>` parsed from each entry's own
/// `file` (never trusted from anywhere else) and checked against `<minor>`.
fn convert_schema1(
    entries: BTreeMap<String, LockEntry>,
    path: &Path,
) -> Result<BTreeMap<String, LockEntry>> {
    let mut out = BTreeMap::new();
    for (key, entry) in entries {
        let Some((platform, minor)) = key.split_once('/') else {
            return Err(Error::Fetch {
                message: format!(
                    "lock file {}: schema-1 entry {key:?} is not <os>-<arch>/<minor>",
                    path.display()
                ),
            });
        };
        let Some(version) = parse_schema1_version(&entry.file, platform, minor) else {
            return Err(Error::Fetch {
                message: format!(
                    "lock file {}: schema-1 entry {key:?} names {:?}, which this crate cannot \
                     read a ClickHouse version for {minor} out of",
                    path.display(),
                    entry.file
                ),
            });
        };
        out.insert(lock_key(platform, &version), entry);
    }
    Ok(out)
}

/// `chtypes-<v>-<os>-<arch>[-b<N>].tar.gz` -> `<v>`, when `<os>-<arch>`
/// matches `platform` and `<v>`'s own line matches `minor`. `None` when the
/// name does not have this shape at all, or when it does but names some
/// other line — either way, the caller cannot trust it.
fn parse_schema1_version(file: &str, platform: &str, minor: &str) -> Option<String> {
    let stem = file.strip_suffix(".tar.gz")?;
    let stem = stem.strip_prefix("chtypes-")?;
    let stem = match stem.rsplit_once("-b") {
        Some((base, digits))
            if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) =>
        {
            base
        }
        _ => stem,
    };
    let version = stem.strip_suffix(&format!("-{platform}"))?;
    if super::minor_of(version) != minor {
        return None;
    }
    Some(version.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(file: &str, sha: &str) -> IndexRow {
        IndexRow {
            file: file.into(),
            sha256: sha.into(),
            bytes: 1,
            clickhouse_version: "25.8.28.1-lts".into(),
            clickhouse_minor: "25.8".into(),
            os: "linux".into(),
            arch: "arm64".into(),
            library: "libchtypes.so".into(),
            library_sha256: "ff".into(),
            build: 0,
            core_commit: String::new(),
            abi_revision: Some(crate::legacy::error::ABI_REVISION),
        }
    }

    fn line_request() -> Request {
        Request::parse("25.8").unwrap()
    }

    #[test]
    fn the_lock_round_trips_schema_2() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-lock-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");

        let absent = LockFile::load(&path, true).unwrap_err();
        assert_eq!(
            absent.artifact_code(),
            Some("CHTYPES_ARTIFACT_PINNED"),
            "--frozen needs a file: {absent}"
        );
        let mut lock = LockFile::load(&path, false).unwrap();
        assert_eq!(lock.schema, LOCK_SCHEMA);
        let key = lock_key("linux-arm64", "25.8.28.1-lts");
        assert!(lock.candidates("linux-arm64", &line_request()).is_empty());
        assert!(lock.record(&key, &row("a.tar.gz", "aa")));
        assert!(!lock.record(&key, &row("a.tar.gz", "aa")));
        lock.save().unwrap();

        let text = std::fs::read_to_string(&path).unwrap();
        let doc: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(doc["schema"], 2);
        assert_eq!(
            doc["artifacts"]["linux-arm64/25.8.28.1-lts"]["file"],
            "a.tar.gz"
        );
        assert_eq!(
            doc["artifacts"]["linux-arm64/25.8.28.1-lts"]["sha256"],
            "aa"
        );
        assert_eq!(
            doc["artifacts"]["linux-arm64/25.8.28.1-lts"]["abi_revision"],
            crate::legacy::error::ABI_REVISION
        );

        let again = LockFile::load(&path, true).unwrap();
        let (got_key, entry) = again
            .best_candidate("linux-arm64", &line_request())
            .unwrap();
        assert_eq!(got_key, key);
        assert_eq!(entry.file, "a.tar.gz");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A line spelling collects every patch of the line the lock pins, and
    /// F1's newest-pinned-patch rule picks the right one regardless of
    /// insertion order.
    #[test]
    fn candidates_collapse_to_the_newest_pinned_patch_of_the_line() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-lock-cand-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        let mut lock = LockFile::load(&path, false).unwrap();
        let older = lock_key("linux-arm64", "25.8.28.1-lts");
        let newer = lock_key("linux-arm64", "25.8.33.5-lts");
        lock.record(&newer, &row("newer.tar.gz", "bb"));
        lock.record(&older, &row("older.tar.gz", "aa"));

        let cands = lock.candidates("linux-arm64", &line_request());
        assert_eq!(cands.len(), 2, "{cands:?}");
        let (best_key, best) = lock.best_candidate("linux-arm64", &line_request()).unwrap();
        assert_eq!(best_key, newer);
        assert_eq!(best.file, "newer.tar.gz");

        // An exact patch matches only itself.
        let exact = Request::parse("25.8.28.1-lts").unwrap();
        let exact_cands = lock.candidates("linux-arm64", &exact);
        assert_eq!(exact_cands.len(), 1);
        assert_eq!(exact_cands[0].0, older);

        // A bare (channel-less) exact spelling matches the channeled pin —
        // Decision 7, exercised through the lock the same as everywhere
        // else (R2).
        let bare = Request::parse("25.8.28.1").unwrap();
        let bare_cands = lock.candidates("linux-arm64", &bare);
        assert_eq!(bare_cands.len(), 1);
        assert_eq!(bare_cands[0].0, older);

        // Another platform's pin never counts.
        let foreign = lock.candidates("darwin-arm64", &line_request());
        assert!(foreign.is_empty());

        std::fs::remove_dir_all(&dir).ok();
    }

    /// F6: `lines_for` groups by line and keeps the newest pinned patch.
    #[test]
    fn lines_for_groups_by_line_and_keeps_the_newest() {
        let dir =
            std::env::temp_dir().join(format!("chtypes-rs-lock-lines-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        let mut lock = LockFile::load(&path, false).unwrap();
        lock.record(
            &lock_key("linux-arm64", "25.8.28.1-lts"),
            &row("older.tar.gz", "aa"),
        );
        lock.record(
            &lock_key("linux-arm64", "25.8.33.5-lts"),
            &row("newer.tar.gz", "bb"),
        );
        lock.record(
            &lock_key("linux-arm64", "26.7.3.19-stable"),
            &row("other-line.tar.gz", "cc"),
        );
        lock.record(
            &lock_key("darwin-arm64", "25.8.99.1-lts"),
            &row("foreign.tar.gz", "dd"),
        );

        let lines = lock.lines_for("linux-arm64");
        assert_eq!(lines.len(), 2, "{lines:?}");
        assert_eq!(lines["25.8"].0, lock_key("linux-arm64", "25.8.33.5-lts"));
        assert_eq!(lines["25.8"].1.file, "newer.tar.gz");
        assert_eq!(lines["26.7"].1.file, "other-line.tar.gz");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// `docs/guides/fetch.md` §5 "Reading schema 1": a schema-1 entry is read
    /// as its schema-2 equivalent, the version parsed from the entry's own
    /// `file`, never assumed. A `-b<N>` build suffix is stripped the same
    /// way; an entry that does not parse refuses the WHOLE file, naming the
    /// entry.
    #[test]
    fn schema_1_is_read_and_converted() {
        let text = r#"{"schema": 1, "artifacts": {
            "linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "abc"},
            "linux-arm64/26.7": {"file": "chtypes-26.7.3.19-stable-linux-arm64-b12.tar.gz", "sha256": "def", "abi_revision": 5}
        }}"#;
        let dir =
            std::env::temp_dir().join(format!("chtypes-rs-lock-schema1-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        std::fs::write(&path, text).unwrap();

        let lock = LockFile::load(&path, true).unwrap();
        assert_eq!(lock.schema, LOCK_SCHEMA);
        let key25 = lock_key("linux-arm64", "25.8.28.1-lts");
        assert_eq!(
            lock.artifacts[&key25].file,
            "chtypes-25.8.28.1-lts-linux-arm64.tar.gz"
        );
        assert_eq!(lock.artifacts[&key25].abi_revision, None);
        let key26 = lock_key("linux-arm64", "26.7.3.19-stable");
        assert_eq!(lock.artifacts[&key26].abi_revision, Some(5));

        // best_candidate reads the converted keys straight away.
        let req = Request::parse("25.8").unwrap();
        let (got, _) = lock.best_candidate("linux-arm64", &req).unwrap();
        assert_eq!(got, key25);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// An entry whose `file` cannot be parsed into a version refuses the
    /// whole lock, naming the entry — never a silent drop of just that pin.
    #[test]
    fn an_unparsable_schema_1_entry_refuses_the_whole_lock() {
        let text = r#"{"schema": 1, "artifacts": {"linux-arm64/25.8": {"file": "not-an-asset-name.zip", "sha256": "abc"}}}"#;
        let dir = std::env::temp_dir().join(format!("chtypes-rs-lock-bad1-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        std::fs::write(&path, text).unwrap();
        let err = LockFile::load(&path, true).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("linux-arm64/25.8"), "{msg}");
        assert!(msg.contains("not-an-asset-name.zip"), "{msg}");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Schema 3 (or any number besides 1 and 2) is refused, naming both
    /// numbers this crate DOES read.
    #[test]
    fn an_unknown_schema_is_refused_naming_both_readable_numbers() {
        let text = r#"{"schema": 3, "artifacts": {}}"#;
        let dir =
            std::env::temp_dir().join(format!("chtypes-rs-lock-bad-schema-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        std::fs::write(&path, text).unwrap();
        let err = LockFile::load(&path, true).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("schema 3"), "{msg}");
        assert!(msg.contains('1') && msg.contains('2'), "{msg}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_spec_s_schema_2_example_parses() {
        let text = r#"{"schema": 2, "artifacts": {"linux-arm64/25.8.28.1-lts": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "abc", "abi_revision": 6}}}"#;
        let lock: LockFile = serde_json::from_str(text).unwrap();
        assert_eq!(lock.schema, 2);
        assert_eq!(
            lock.artifacts["linux-arm64/25.8.28.1-lts"].file,
            "chtypes-25.8.28.1-lts-linux-arm64.tar.gz"
        );
    }

    /// A cross-binding placement rule for #284: a schema-2 lock entry whose
    /// key names a LINE (not an exact patch) is refused LOUDLY when the lock
    /// is read through [`LockFile::load`] — never silently narrowed to "the
    /// one patch a line-shaped key happens to look like". `serde_json`
    /// deserialization alone (bypassing `load`) does not run this check,
    /// which is why it lives in `load` and not in `Deserialize`.
    #[test]
    fn a_schema_2_entry_naming_a_line_is_refused_loudly() {
        let text = r#"{"schema": 2, "artifacts": {"linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "abc"}}}"#;
        let dir =
            std::env::temp_dir().join(format!("chtypes-rs-lock-line-key-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        std::fs::write(&path, text).unwrap();
        let err = LockFile::load(&path, true).unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("linux-arm64/25.8"), "{msg}");
        assert!(msg.contains("LINE"), "{msg}");
        std::fs::remove_dir_all(&dir).ok();
    }
}
