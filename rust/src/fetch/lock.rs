//! `docs/fetch.md` §5: the lock file — per `<os>-<arch>/<minor>`, the asset
//! file and sha256 that were installed. Trust on first fetch, byte-identical
//! thereafter.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::release::IndexRow;
use crate::error::{Error, Result};

/// The lock file's default name, for `--frozen` without `--lock`.
pub const DEFAULT_LOCK_FILE: &str = "chtypes.lock";

/// One pinned asset.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LockEntry {
    /// The asset file name.
    pub file: String,
    /// Its sha256.
    pub sha256: String,
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
                if lock.schema != 1 {
                    return Err(Error::Fetch {
                        message: format!(
                            "lock file {} is schema {}, and this crate reads schema 1",
                            path.display(),
                            lock.schema
                        ),
                    });
                }
                lock.path = path.to_path_buf();
                Ok(lock)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && !must_exist => Ok(LockFile {
                schema: 1,
                artifacts: BTreeMap::new(),
                path: path.to_path_buf(),
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

    /// `--frozen`: the release must offer exactly what `key` pins.
    ///
    /// # Errors
    ///
    /// [`Error::ArtifactPinned`] — no pin for `key`, or the offered asset
    /// differs from the pinned one by name or sha256.
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
        if pinned.file != offered.file || pinned.sha256 != offered.sha256 {
            return Err(Error::ArtifactPinned {
                key: key.to_string(),
                message: format!(
                    "{} pins {} {} but the release offers {} {}",
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

    /// Record what was installed for `key`. Returns whether anything changed.
    pub fn record(&mut self, key: &str, installed: &IndexRow) -> bool {
        let entry = LockEntry {
            file: installed.file.clone(),
            sha256: installed.sha256.clone(),
        };
        let changed = self.artifacts.get(key) != Some(&entry);
        self.artifacts.insert(key.to_string(), entry);
        changed
    }

    /// Write the lock back, atomically (a temp sibling, then rename).
    pub fn save(&self) -> Result<()> {
        let text = serde_json::to_string_pretty(self).map_err(|e| Error::Fetch {
            message: format!("serialising the lock file: {e}"),
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
        }
    }

    #[test]
    fn the_lock_round_trips_and_enforces() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-lock-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");

        assert!(
            LockFile::load(&path, true).is_err(),
            "--frozen needs a file"
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

        let again = LockFile::load(&path, true).unwrap();
        again.enforce(&key, &row("a.tar.gz", "aa")).unwrap();
        let drift = again.enforce(&key, &row("a.tar.gz", "bb")).unwrap_err();
        assert!(matches!(drift, Error::ArtifactPinned { .. }), "{drift:?}");
        assert_eq!(drift.artifact_code(), Some("CHTYPES_ARTIFACT_PINNED"));
        let renamed = again.enforce(&key, &row("b.tar.gz", "aa")).unwrap_err();
        assert!(matches!(renamed, Error::ArtifactPinned { .. }));
        std::fs::remove_dir_all(&dir).ok();
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
