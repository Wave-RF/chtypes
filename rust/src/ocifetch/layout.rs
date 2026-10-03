//! The cache: a standard OCI image layout plus `unpacked/sha256/<hex>/`
//! beside `blobs/` (plan §1.1.5 "Cache", §3.4 §1 "The cache").
//!
//! **The source of truth for `--offline` and `resolve_installed` is the
//! immutable `unpacked/sha256/*/verified.json` record**, never `index.json`:
//! losing an `index.json` write race costs only `oras` pre-seed interop,
//! never correctness (plan §3.4, "The resolution rule").

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::constants;
use super::error::{Error, Result};

/// One verified, unpacked install — the durable record this module reads
/// back for `resolve_installed`/`list_installed`/`--offline`, and what
/// `ensure.rs` builds [`crate::ocifetch::Resolved`] from.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct VerifiedRecord {
    pub platform: String,
    pub version: String,
    pub build: String,
    pub channel: Option<String>,
    pub manifest_digest: String,
    pub layer_digest: String,
    pub bundle_digest: Option<String>,
    pub bundle_manifest_digest: Option<String>,
    pub signed_by: String,
    /// The library's file name, relative to the unpacked directory.
    pub library: String,
    pub library_sha256: String,
    pub library_bytes: u64,
    pub predicate: serde_json::Value,
}

/// Resolve the layout root: `CHTYPES_CACHE` names the layout directory
/// itself; otherwise `${XDG_CACHE_HOME:-$HOME/.cache}/chtypes/v1`
/// (`constants::CACHE_ROOT_TEMPLATE`).
pub fn cache_root(cache_env_override: Option<&str>) -> Result<PathBuf> {
    if let Some(dir) = cache_env_override {
        return Ok(PathBuf::from(dir));
    }
    if let Ok(dir) = std::env::var(constants::ENV_CACHE_NAME) {
        if !dir.is_empty() {
            return Ok(PathBuf::from(dir));
        }
    }
    let base = std::env::var("XDG_CACHE_HOME")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var("HOME")
                .ok()
                .map(|h| PathBuf::from(h).join(".cache"))
        })
        .ok_or_else(|| {
            Error::SourceIncompatible(
                "neither CHTYPES_CACHE, XDG_CACHE_HOME nor HOME is set".to_string(),
            )
        })?;
    Ok(base.join("chtypes").join("v1"))
}

/// Create the OCI image-layout skeleton (`oci-layout`, an empty `index.json`,
/// `blobs/sha256/`) if it does not already exist. Idempotent.
pub fn ensure_layout(root: &Path) -> Result<()> {
    std::fs::create_dir_all(root.join("blobs").join("sha256"))?;
    std::fs::create_dir_all(root.join(constants::CACHE_UNPACKED_DIR))?;
    let oci_layout = root.join("oci-layout");
    if !oci_layout.exists() {
        write_atomic(&oci_layout, br#"{"imageLayoutVersion":"1.0.0"}"#)?;
    }
    let index = root.join("index.json");
    if !index.exists() {
        write_atomic(&index, br#"{"schemaVersion":2,"manifests":[]}"#)?;
    }
    Ok(())
}

pub fn blob_path(root: &Path, digest: &str) -> Result<PathBuf> {
    let hex = digest
        .strip_prefix("sha256:")
        .ok_or_else(|| Error::InvalidInput(format!("digest {digest:?} is not sha256:<hex>")))?;
    Ok(root.join("blobs").join("sha256").join(hex))
}

/// Store `bytes` under its digest if not already present. Blobs are
/// content-addressed and immutable, so a pre-existing blob is left alone
/// rather than rewritten.
pub fn put_blob(root: &Path, digest: &str, bytes: &[u8]) -> Result<PathBuf> {
    let path = blob_path(root, digest)?;
    if !path.exists() {
        write_atomic(&path, bytes)?;
    }
    Ok(path)
}

pub fn read_blob(root: &Path, digest: &str) -> Result<Option<Vec<u8>>> {
    let path = blob_path(root, digest)?;
    match std::fs::read(&path) {
        Ok(b) => Ok(Some(b)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

pub fn unpacked_dir(root: &Path, manifest_digest: &str) -> Result<PathBuf> {
    let hex = manifest_digest.strip_prefix("sha256:").ok_or_else(|| {
        Error::InvalidInput(format!("digest {manifest_digest:?} is not sha256:<hex>"))
    })?;
    Ok(root.join(constants::CACHE_UNPACKED_DIR).join(hex))
}

/// Read back an existing `unpacked/sha256/<hex>/verified.json`, if present.
pub fn read_verified(dir: &Path) -> Result<Option<VerifiedRecord>> {
    let path = dir.join(constants::CACHE_VERIFIED_RECORD);
    match std::fs::read(&path) {
        Ok(bytes) => {
            let record: VerifiedRecord = serde_json::from_slice(&bytes)
                .map_err(|e| Error::ArtifactCorrupt(format!("{}: {e}", path.display())))?;
            Ok(Some(record))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// Install one unpacked artifact atomically: `build` populates a fresh temp
/// directory (unpack the tar, write `verified.json`), which is then renamed
/// into place. If the final directory already exists (another process won
/// the race, or this exact build was already installed), the temp directory
/// is discarded and the existing one is left untouched — never overwritten,
/// since unpacked content is immutable once verified.
pub fn install_unpacked(
    root: &Path,
    manifest_digest: &str,
    build: impl FnOnce(&Path) -> Result<()>,
) -> Result<PathBuf> {
    let final_dir = unpacked_dir(root, manifest_digest)?;
    if final_dir.join(constants::CACHE_VERIFIED_RECORD).exists() {
        return Ok(final_dir);
    }
    let parent = final_dir
        .parent()
        .ok_or_else(|| Error::InvalidInput("unpacked dir has no parent".to_string()))?;
    std::fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".tmp-{}", unique_suffix()));
    std::fs::create_dir_all(&tmp)?;
    let result = build(&tmp);
    if let Err(e) = result {
        let _ = std::fs::remove_dir_all(&tmp);
        return Err(e);
    }
    match std::fs::rename(&tmp, &final_dir) {
        Ok(()) => Ok(final_dir),
        Err(_) if final_dir.join(constants::CACHE_VERIFIED_RECORD).exists() => {
            // Another process installed the same content first.
            let _ = std::fs::remove_dir_all(&tmp);
            Ok(final_dir)
        }
        Err(e) => {
            let _ = std::fs::remove_dir_all(&tmp);
            Err(e.into())
        }
    }
}

/// List every verified record under `root`'s `unpacked/` tree (used by
/// `list_installed`/`resolve_installed`/the monotonicity check). A read-only
/// system directory is scanned the same way, by the caller passing its root.
pub fn list_verified(root: &Path) -> Result<Vec<(PathBuf, VerifiedRecord)>> {
    let dir = root.join(constants::CACHE_UNPACKED_DIR);
    let mut out = Vec::new();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(e.into()),
    };
    for entry in entries {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        if let Some(record) = read_verified(&entry.path())? {
            out.push((entry.path(), record));
        }
    }
    Ok(out)
}

/// Add a manifest descriptor to `index.json` by a read-check-rename loop:
/// read the live file, merge the entry, and — immediately before the atomic
/// rename — re-read it once more; if a concurrent writer changed it in the
/// window, throw the merge away and redo it on top of their file, so two
/// independent writers both land in the final file rather than one
/// clobbering the other (`index-race-reapply`). `before_rename` runs in
/// exactly that window (once per attempt); the conformance runner uses it to
/// play the competing writer.
pub fn record_in_index(
    root: &Path,
    digest: &str,
    size: u64,
    media_type: &str,
    platform: Option<(&str, &str)>,
    before_rename: Option<&dyn Fn()>,
) -> Result<()> {
    let path = root.join("index.json");
    for _ in 0..16 {
        let raw_before = std::fs::read(&path).ok();
        let mut value = match &raw_before {
            Some(bytes) => serde_json::from_slice(bytes)
                .unwrap_or_else(|_| serde_json::json!({"schemaVersion": 2, "manifests": []})),
            None => serde_json::json!({"schemaVersion": 2, "manifests": []}),
        };
        if has_digest(&value, digest) {
            return Ok(());
        }
        add_entry(&mut value, digest, size, media_type, platform);
        if let Some(hook) = before_rename {
            hook();
        }
        let raw_now = std::fs::read(&path).ok();
        if raw_now != raw_before {
            // A competing writer renamed a new index.json into place after
            // our read: re-merge onto theirs.
            continue;
        }
        return write_atomic(&path, serde_json::to_vec(&value)?.as_slice());
    }
    Err(Error::SourceUnreachable(format!(
        "{} kept changing under this writer; gave up after 16 attempts",
        path.display()
    )))
}

fn has_digest(index: &serde_json::Value, digest: &str) -> bool {
    index["manifests"]
        .as_array()
        .is_some_and(|m| m.iter().any(|d| d["digest"] == digest))
}

fn add_entry(
    index: &mut serde_json::Value,
    digest: &str,
    size: u64,
    media_type: &str,
    platform: Option<(&str, &str)>,
) {
    if !index["manifests"].is_array() {
        index["manifests"] = serde_json::json!([]);
    }
    let mut entry = serde_json::json!({"mediaType": media_type, "digest": digest, "size": size});
    if let Some((os, architecture)) = platform {
        entry["platform"] = serde_json::json!({"os": os, "architecture": architecture});
    }
    index["manifests"].as_array_mut().unwrap().push(entry);
}

/// Write `bytes` to `path` by writing a sibling temp file and renaming it
/// into place, so a reader never observes a partial write.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        Error::InvalidInput(format!("{} has no parent directory", path.display()))
    })?;
    std::fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(
        ".tmp-{}-{}",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("x"),
        unique_suffix()
    ));
    std::fs::write(&tmp, bytes)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

fn unique_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{nanos:x}-{:?}", std::thread::current().id())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_atomic_then_read_back() {
        let dir = std::env::temp_dir().join(format!("ocifetch-layout-test-{}", unique_suffix()));
        let path = dir.join("a").join("b.json");
        write_atomic(&path, b"{}").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"{}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn install_unpacked_is_idempotent() {
        let root = std::env::temp_dir().join(format!("ocifetch-install-test-{}", unique_suffix()));
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let mut calls = 0;
        let dir1 = install_unpacked(&root, digest, |tmp| {
            calls += 1;
            write_atomic(&tmp.join("verified.json"), b"{}")
        })
        .unwrap();
        let dir2 = install_unpacked(&root, digest, |tmp| {
            calls += 1;
            write_atomic(&tmp.join("verified.json"), b"{}")
        })
        .unwrap();
        assert_eq!(dir1, dir2);
        assert_eq!(calls, 1, "the second install must not re-run build()");
        let _ = std::fs::remove_dir_all(&root);
    }
}
