//! Lock schema 3 (plan §1.1.6, §3.4 §6). Requests float; the lock is fixed:
//! per declared platform, it records the request, the exact version, the
//! build, and the manifest/layer/bundle digests. `index` is informational
//! only — a computed index has no stored bytes a later `--frozen` fetch can
//! retrieve by digest (plan §9 M2).

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

use super::constants;
use super::error::{Error, Result};
use super::layout;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct LockEntry {
    pub version: String,
    pub build: String,
    pub manifest: String,
    pub layer: String,
    pub bundle: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index: Option<String>,
}

/// `requests[spelling][platform] = LockEntry`.
pub type Requests = BTreeMap<String, BTreeMap<String, LockEntry>>;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Lock {
    pub schema: u32,
    pub abi: u32,
    pub platforms: Vec<String>,
    #[serde(default)]
    pub requests: Requests,
}

impl Lock {
    pub fn new() -> Self {
        Lock {
            schema: constants::LOCK_SCHEMA,
            abi: constants::ABI_GENERATION,
            platforms: Vec::new(),
            requests: Requests::new(),
        }
    }

    pub fn entry(&self, spelling: &str, platform: &str) -> Option<&LockEntry> {
        self.requests.get(spelling)?.get(platform)
    }

    pub fn set_entry(&mut self, spelling: &str, platform: &str, entry: LockEntry) {
        self.requests
            .entry(spelling.to_string())
            .or_default()
            .insert(platform.to_string(), entry);
        if !self.platforms.iter().any(|p| p == platform) {
            self.platforms.push(platform.to_string());
        }
    }
}

impl Default for Lock {
    fn default() -> Self {
        Self::new()
    }
}

/// Read a lock file. `Ok(None)` if it does not exist. `schema != 3` and
/// `abi != constants::ABI_GENERATION` are `ArtifactPinned`, naming a re-lock
/// (`lock-schema-2-refused`, `frozen-lock-abi-2`): this module never reads
/// an older lock schema itself.
pub fn read(path: &Path) -> Result<Option<Lock>> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("{}: {e}", path.display())))?;
    let schema = value.get("schema").and_then(serde_json::Value::as_u64);
    if schema != Some(constants::LOCK_SCHEMA as u64) {
        return Err(Error::ArtifactPinned(format!(
            "{} is lock schema {:?}, want {} — re-lock with `update`",
            path.display(),
            schema,
            constants::LOCK_SCHEMA
        )));
    }
    let lock: Lock = serde_json::from_value(value)
        .map_err(|e| Error::ArtifactCorrupt(format!("{}: {e}", path.display())))?;
    if lock.abi != constants::ABI_GENERATION {
        return Err(Error::ArtifactPinned(format!(
            "{} pins ABI generation {}, this build speaks {} — re-lock with `update`",
            path.display(),
            lock.abi,
            constants::ABI_GENERATION
        )));
    }
    Ok(Some(lock))
}

/// Write a lock file by atomic rename.
pub fn write(path: &Path, lock: &Lock) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(lock)?;
    // The lock is the caller's file, not the cache: its I/O failure keeps the
    // class it always had.
    layout::write_atomic_io(path, &bytes).map_err(Error::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let mut lock = Lock::new();
        lock.set_entry(
            "26.8",
            "linux-arm64",
            LockEntry {
                version: "26.8.15.10".into(),
                build: "20261001.183455".into(),
                manifest: "sha256:aa".into(),
                layer: "sha256:bb".into(),
                bundle: "sha256:cc".into(),
                index: None,
            },
        );
        let json = serde_json::to_string(&lock).unwrap();
        let back: Lock = serde_json::from_str(&json).unwrap();
        assert_eq!(
            back.entry("26.8", "linux-arm64").unwrap().version,
            "26.8.15.10"
        );
    }

    #[test]
    fn refuses_schema_2() {
        let dir = std::env::temp_dir().join(format!(
            "ocifetch-lock-test-{:?}",
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("chtypes.lock");
        std::fs::write(
            &path,
            br#"{"schema":2,"abi":1,"platforms":[],"requests":{}}"#,
        )
        .unwrap();
        let err = read(&path).unwrap_err();
        assert!(matches!(err, Error::ArtifactPinned(_)));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
