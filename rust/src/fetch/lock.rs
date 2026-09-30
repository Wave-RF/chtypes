//! `docs/guides/fetch.md` §5: the lock file — per `<os>-<arch>/<minor>`, the asset
//! file and sha256 that were installed. Trust on first fetch, byte-identical
//! thereafter.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::release::IndexRow;
use crate::error::{Error, FETCH_COMMAND, Result};

/// The lock file's default name, for `--frozen` without `--lock`.
pub const DEFAULT_LOCK_FILE: &str = "chtypes.lock";

/// The lock file schema every SDK reads and writes.
pub const LOCK_SCHEMA: u64 = 1;

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

/// The lock file, schema 1.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockFile {
    /// Always 1.
    pub schema: u64,
    /// `<os>-<arch>/<minor>` → the asset installed for it.
    #[serde(default)]
    pub artifacts: BTreeMap<String, LockEntry>,
    #[serde(skip)]
    path: PathBuf,
}

/// The lock key for a platform and minor line.
pub fn lock_key(platform: &str, minor: &str) -> String {
    format!("{platform}/{minor}")
}

impl LockFile {
    /// Read `path`, or start an empty schema-1 lock when it does not exist
    /// (`must_exist` refuses that — `--frozen` needs a real file).
    pub fn load(path: &Path, must_exist: bool) -> Result<LockFile> {
        match std::fs::read(path) {
            Ok(bytes) => {
                let mut lock: LockFile =
                    serde_json::from_slice(&bytes).map_err(|e| Error::Fetch {
                        message: format!("lock file {}: {e}", path.display()),
                    })?;
                if lock.schema != LOCK_SCHEMA {
                    return Err(Error::Fetch {
                        message: format!(
                            "lock file {} is schema {}, and this crate reads schema {LOCK_SCHEMA}",
                            path.display(),
                            lock.schema
                        ),
                    });
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

    /// `--frozen`: the release must offer exactly what `key` pins. The
    /// revision is checked FIRST, before the file/sha256 comparison below
    /// it: a lock that names a revision is refused the moment that revision
    /// is not this crate's own, never compared byte-for-byte against an
    /// artifact it could never have pinned (`docs/guides/fetch.md` §5).
    ///
    /// # Errors
    ///
    /// [`Error::ArtifactPinned`] — no pin for `key`; the pin names an ABI
    /// revision other than this crate's own; or the offered asset differs
    /// from the pinned one by name or sha256 (an entry with no recorded
    /// revision — written by an SDK before this field existed — carries one
    /// extra sentence saying so).
    pub fn enforce(&self, key: &str, offered: &IndexRow) -> Result<()> {
        let pinned = self
            .artifacts
            .get(key)
            .ok_or_else(|| Error::ArtifactPinned {
                key: key.to_string(),
                message: format!(
                    "not pinned in {} and --frozen refuses anything unpinned (the release offers \
                 {} {})",
                    self.path.display(),
                    offered.file,
                    offered.sha256
                ),
            })?;
        if let Some(pinned_rev) = pinned.abi_revision {
            let rev = super::fetch_abi_revision();
            if pinned_rev != rev {
                return Err(Error::ArtifactPinned {
                    key: key.to_string(),
                    message: format!(
                        "{} pins {key} at ABI revision {pinned_rev}; this SDK speaks ABI revision \
                         {rev} — re-lock with: {FETCH_COMMAND} {} --lock {}",
                        self.path.display(),
                        offered.clickhouse_minor,
                        self.path.display()
                    ),
                });
            }
        }
        if pinned.file != offered.file || pinned.sha256 != offered.sha256 {
            let note = if pinned.abi_revision.is_none() {
                format!(
                    " {} records no ABI revision (written by an older SDK); this SDK speaks ABI \
                     revision {} — re-lock with: {FETCH_COMMAND} {} --lock {}",
                    self.path.display(),
                    super::fetch_abi_revision(),
                    offered.clickhouse_minor,
                    self.path.display()
                )
            } else {
                String::new()
            };
            return Err(Error::ArtifactPinned {
                key: key.to_string(),
                message: format!(
                    "{} pins {} {} but the release offers {} {}{note}",
                    self.path.display(),
                    pinned.file,
                    pinned.sha256,
                    offered.file,
                    offered.sha256
                ),
            });
        }
        Ok(())
    }

    /// Record what was installed for `key`, including the ABI revision the
    /// selected row carries — this crate's own, since selection never
    /// picks any other (`docs/guides/fetch.md` §2). Returns whether
    /// anything changed.
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

    /// Write the lock back, atomically (a temp sibling, then rename).
    pub fn save(&self) -> Result<()> {
        let text = serde_json::to_string_pretty(self).map_err(|e| Error::Fetch {
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
            abi_revision: Some(crate::ABI_REVISION),
        }
    }

    #[test]
    fn the_lock_round_trips_and_enforces() {
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
        let key = lock_key("linux-arm64", "25.8");
        assert!(lock.enforce(&key, &row("a.tar.gz", "aa")).is_err());
        assert!(lock.record(&key, &row("a.tar.gz", "aa")));
        assert!(!lock.record(&key, &row("a.tar.gz", "aa")));
        lock.save().unwrap();

        let text = std::fs::read_to_string(&path).unwrap();
        let doc: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(doc["schema"], 1);
        assert_eq!(doc["artifacts"]["linux-arm64/25.8"]["file"], "a.tar.gz");
        assert_eq!(doc["artifacts"]["linux-arm64/25.8"]["sha256"], "aa");
        assert_eq!(
            doc["artifacts"]["linux-arm64/25.8"]["abi_revision"],
            crate::ABI_REVISION
        );

        let again = LockFile::load(&path, true).unwrap();
        again.enforce(&key, &row("a.tar.gz", "aa")).unwrap();
        let drift = again.enforce(&key, &row("a.tar.gz", "bb")).unwrap_err();
        assert!(matches!(drift, Error::ArtifactPinned { .. }), "{drift:?}");
        assert_eq!(drift.artifact_code(), Some("CHTYPES_ARTIFACT_PINNED"));
        let renamed = again.enforce(&key, &row("b.tar.gz", "aa")).unwrap_err();
        assert!(matches!(renamed, Error::ArtifactPinned { .. }));
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Issue #253: `enforce` checks the ABI revision FIRST. A pin at another
    /// revision is refused naming both numbers, regardless of whether the
    /// file/sha256 would otherwise have matched; a pin with no recorded
    /// revision (an older SDK's lock) still enforces file/sha256 as before,
    /// with one appended sentence on a mismatch.
    #[test]
    fn enforce_checks_the_abi_revision_first() {
        let key = lock_key("linux-arm64", "25.8");
        let want = row("a.tar.gz", "aa");

        // A pin at another revision is PINNED, naming both numbers, even
        // though file and sha256 match exactly.
        let mut mismatched = LockFile {
            schema: LOCK_SCHEMA,
            artifacts: BTreeMap::new(),
            path: PathBuf::from("chtypes.lock"),
        };
        mismatched.artifacts.insert(
            key.clone(),
            LockEntry {
                file: "a.tar.gz".into(),
                sha256: "aa".into(),
                abi_revision: Some(crate::ABI_REVISION + 1),
            },
        );
        let err = mismatched.enforce(&key, &want).unwrap_err();
        assert_eq!(err.artifact_code(), Some("CHTYPES_ARTIFACT_PINNED"));
        let msg = err.to_string();
        assert!(msg.contains(&format!("ABI revision {}", crate::ABI_REVISION + 1)));
        assert!(msg.contains(&format!("ABI revision {}", crate::ABI_REVISION)));
        assert!(msg.contains("re-lock with:"));

        // A pin at the SAME revision still enforces normally.
        let mut matching = mismatched.clone();
        matching.artifacts.insert(
            key.clone(),
            LockEntry {
                file: "a.tar.gz".into(),
                sha256: "aa".into(),
                abi_revision: Some(crate::ABI_REVISION),
            },
        );
        matching.enforce(&key, &want).unwrap();

        // A pin with NO recorded revision (an older SDK's lock): the old
        // path — a drift is still refused — plus one appended sentence.
        let mut no_rev = mismatched.clone();
        no_rev.artifacts.insert(
            key.clone(),
            LockEntry {
                file: "a.tar.gz".into(),
                sha256: "00".repeat(32),
                abi_revision: None,
            },
        );
        let drift = no_rev.enforce(&key, &want).unwrap_err();
        let drift_msg = drift.to_string();
        assert!(drift_msg.contains("records no ABI revision"));
        assert!(drift_msg.contains("older SDK"));

        // A pin with no recorded revision that DOES match installs cleanly
        // — no sentence needed for a success.
        let mut no_rev_match = mismatched.clone();
        no_rev_match.artifacts.insert(
            key.clone(),
            LockEntry {
                file: "a.tar.gz".into(),
                sha256: "aa".into(),
                abi_revision: None,
            },
        );
        no_rev_match.enforce(&key, &want).unwrap();
    }

    #[test]
    fn the_spec_s_example_parses() {
        let text = r#"{"schema": 1, "artifacts": {"linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "abc"}}}"#;
        let lock: LockFile = serde_json::from_str(text).unwrap();
        assert_eq!(lock.schema, 1);
        assert_eq!(
            lock.artifacts["linux-arm64/25.8"].file,
            "chtypes-25.8.28.1-lts-linux-arm64.tar.gz"
        );
    }
}
