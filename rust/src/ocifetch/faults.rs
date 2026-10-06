//! What can be wrong with a cache root, and what each mode does about it
//! (docs/guides/fetch-v1.md §1, the cache faults; public issue #486). The
//! default mode keeps "unreadable is absent" and says so in a warning; strict
//! mode (`strict_cache`, `CHTYPES_CACHE_STRICT=1`, `--strict`) makes every
//! fault a `CHTYPES_CACHE_UNUSABLE` naming the path, and never falls through
//! to a system dir. A write this layer needed that failed is
//! `CHTYPES_CACHE_UNUSABLE` in every mode (`error::unwritable`). Go, Python
//! and TypeScript probe the same way, in the same order, and say the same
//! sentences.

use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use super::constants;
use super::error::{Error, Result, errno_name};
use super::layout::{VerifiedRecord, zero_x_shape};

pub const REASON_UNREADABLE_ROOT: &str = "unreadable_root";
pub const REASON_NOT_A_DIRECTORY: &str = "not_a_directory";
pub const REASON_UNREADABLE_ENTRY: &str = "unreadable_entry";
pub const REASON_UNACCEPTABLE_RECORD: &str = "unacceptable_record";
pub const REASON_LAYOUT_0X: &str = "layout_0x";

/// Whether strict mode is on: the option, else `CHTYPES_CACHE_STRICT=1`.
pub fn strict_mode(option: Option<bool>) -> bool {
    option
        .unwrap_or_else(|| std::env::var(constants::ENV_CACHE_STRICT_NAME).is_ok_and(|v| v == "1"))
}

struct Fault {
    path: PathBuf,
    reason: &'static str,
    os_error: Option<String>,
}

impl Fault {
    fn new(path: &Path, reason: &'static str, os_error: Option<String>) -> Fault {
        Fault {
            path: path.to_path_buf(),
            reason,
            os_error,
        }
    }

    fn warns(&self) -> bool {
        matches!(
            self.reason,
            REASON_UNREADABLE_ROOT | REASON_NOT_A_DIRECTORY | REASON_UNREADABLE_ENTRY
        )
    }

    fn warning(&self) -> String {
        format!(
            "{} could not be read ({}); treated as not installed. Set {}=1 to make this an error.",
            self.path.display(),
            self.os_error.as_deref().unwrap_or(""),
            constants::ENV_CACHE_STRICT_NAME
        )
    }

    fn into_error(self) -> Error {
        Error::cache_unusable(&self.path, self.reason, self.os_error)
    }
}

fn denied(path: &Path) -> bool {
    matches!(std::fs::metadata(path), Err(e) if e.kind() == ErrorKind::PermissionDenied)
}

/// What is wrong with one root, in a fixed order: the root itself, then the
/// 0.x shape (the cache only), then each entry by name. A root that does not
/// exist is the empty cache, not a fault.
fn probe_root(root: &Path, is_cache: bool) -> Vec<Fault> {
    match std::fs::metadata(root) {
        Err(e) if e.kind() == ErrorKind::NotFound => return Vec::new(),
        Err(e) => {
            let reason = if e.kind() == ErrorKind::NotADirectory {
                REASON_NOT_A_DIRECTORY
            } else {
                REASON_UNREADABLE_ROOT
            };
            return vec![Fault::new(root, reason, errno_name(&e))];
        }
        Ok(md) if !md.is_dir() => {
            return vec![Fault::new(
                root,
                REASON_NOT_A_DIRECTORY,
                Some("ENOTDIR".to_string()),
            )];
        }
        Ok(_) => {}
    }
    if is_cache && zero_x_shape(root).is_some() {
        return vec![Fault::new(root, REASON_LAYOUT_0X, None)];
    }
    let unpacked = root.join("unpacked");
    let sha = root.join(constants::CACHE_UNPACKED_DIR);
    let entries = match std::fs::read_dir(&sha) {
        Ok(entries) => entries,
        Err(e) if e.kind() == ErrorKind::NotFound => return Vec::new(),
        Err(e) if e.kind() == ErrorKind::NotADirectory => {
            let path = match std::fs::metadata(&unpacked) {
                Ok(md) if !md.is_dir() => unpacked,
                _ => sha,
            };
            return vec![Fault::new(
                &path,
                REASON_NOT_A_DIRECTORY,
                Some("ENOTDIR".to_string()),
            )];
        }
        Err(e) => {
            // The path that blocks the listing: the first one that cannot be
            // passed through, else unpacked/sha256 itself.
            let path = if denied(&unpacked) {
                root.to_path_buf()
            } else if denied(&sha) {
                unpacked
            } else {
                sha
            };
            return vec![Fault::new(&path, REASON_UNREADABLE_ROOT, errno_name(&e))];
        }
    };
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| {
            n.len() == 64
                && n.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .collect();
    names.sort();
    let mut faults = Vec::new();
    for name in names {
        let entry = sha.join(&name);
        let record = entry.join(constants::CACHE_VERIFIED_RECORD);
        match std::fs::read(&record) {
            Ok(bytes) => {
                if is_cache
                    && VerifiedRecord::from_json_bytes(&bytes).is_err()
                    && std::fs::symlink_metadata(root.join("blobs").join("sha256").join(&name))
                        .is_err()
                {
                    faults.push(Fault::new(&record, REASON_UNACCEPTABLE_RECORD, None));
                }
            }
            // No record yet: a pre-seed or an unfinished install.
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(e) => {
                let path = if denied(&record) { entry } else { record };
                faults.push(Fault::new(&path, REASON_UNREADABLE_ENTRY, errno_name(&e)));
            }
        }
    }
    faults
}

/// Check the cache (`roots[0]`), then every system dir. In strict mode the
/// first fault is returned as its `CHTYPES_CACHE_UNUSABLE`, the cache's
/// before any system dir's, so nothing falls through. In the default mode one
/// warning per unusable root or unreadable entry is returned. A system dir
/// that does not exist is skipped in both, as a default list.
pub fn probe_roots(roots: &[&Path], strict: bool) -> Result<Vec<String>> {
    let mut warnings = Vec::new();
    for (i, root) in roots.iter().enumerate() {
        for fault in probe_root(root, i == 0) {
            if strict {
                if i > 0 && !fault.warns() {
                    continue;
                }
                return Err(fault.into_error());
            }
            if fault.warns() {
                warnings.push(fault.warning());
            }
        }
    }
    Ok(warnings)
}
