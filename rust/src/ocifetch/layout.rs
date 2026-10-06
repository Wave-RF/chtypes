//! The cache: a standard OCI image layout plus `unpacked/sha256/<hex>/`
//! beside `blobs/` (plan §1.1.5 "Cache", §3.4 §1 "The cache").
//!
//! **The source of truth for `--offline` and `resolve_installed` is the
//! immutable `unpacked/sha256/*/verified.json` record**, never `index.json`:
//! losing an `index.json` write race costs only `oras` pre-seed interop,
//! never correctness (plan §3.4, "The resolution rule").

use std::path::{Path, PathBuf};

use super::constants;
use super::error::{Error, Result, unwritable};

/// One verified, unpacked install — the durable record this module reads
/// back for `resolve_installed`/`list_installed`/`--offline`, and what
/// `ensure.rs` builds [`crate::ocifetch::Resolved`] from.
///
/// Its on-disk form is the one canonical record every binding reads and
/// writes (`spec/fetch-v1/schema/verified.schema.json`,
/// `docs/guides/fetch-v1.md` §1): see [`VerifiedRecord::to_json_bytes`] and
/// [`VerifiedRecord::from_json_bytes`]. `channel`, the optional digests and
/// an empty `signed_by` are `null` on disk.
#[derive(Clone, Debug)]
pub struct VerifiedRecord {
    pub platform: String,
    pub version: String,
    pub build: String,
    pub channel: Option<String>,
    pub index_digest: Option<String>,
    pub manifest_digest: String,
    pub layer_digest: String,
    pub bundle_digest: Option<String>,
    pub bundle_manifest_digest: Option<String>,
    /// Empty means nothing verified (allow-unsigned); written as `null`.
    pub signed_by: String,
    /// The library's file name, relative to the unpacked directory.
    pub library: String,
    pub library_sha256: String,
    pub library_bytes: u64,
    pub predicate: serde_json::Value,
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn is_digest(s: &str) -> bool {
    s.strip_prefix("sha256:").is_some_and(is_hex64)
}

impl VerifiedRecord {
    /// The canonical `verified.json`: every member present, `null` where the
    /// schema allows it.
    pub fn to_json_bytes(&self) -> Result<Vec<u8>> {
        let doc = serde_json::json!({
            "schema": 1,
            "platform": self.platform,
            "version": self.version,
            "channel": self.channel,
            "build": self.build,
            "library": self.library,
            "library_sha256": self.library_sha256,
            "library_bytes": self.library_bytes,
            "digests": {
                "index": self.index_digest,
                "manifest": self.manifest_digest,
                "layer": self.layer_digest,
                "bundle": self.bundle_digest,
                "bundle_manifest": self.bundle_manifest_digest,
            },
            "signed_by": if self.signed_by.is_empty() { None } else { Some(&self.signed_by) },
            "predicate": self.predicate,
        });
        Ok(serde_json::to_vec(&doc)?)
    }

    /// Accept exactly the canonical schema-1 record. Anything else (not
    /// JSON, another schema, a missing member, a member of the wrong type,
    /// a broken rule) is an `Err`, which every caller treats as an ABSENT
    /// record, never as a failure by itself.
    pub fn from_json_bytes(bytes: &[u8]) -> std::result::Result<Self, String> {
        let doc: serde_json::Value = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        let obj = doc.as_object().ok_or("not an object")?;
        for key in [
            "schema",
            "platform",
            "version",
            "channel",
            "build",
            "library",
            "library_sha256",
            "library_bytes",
            "digests",
            "signed_by",
            "predicate",
        ] {
            if !obj.contains_key(key) {
                return Err(format!("missing member {key:?}"));
            }
        }
        let digests = obj["digests"]
            .as_object()
            .ok_or("digests is not an object")?;
        for key in ["index", "manifest", "layer", "bundle", "bundle_manifest"] {
            if !digests.contains_key(key) {
                return Err(format!("missing digests member {key:?}"));
            }
        }
        if obj["schema"].as_u64() != Some(1) {
            return Err("schema is not 1".to_string());
        }
        let text = |name: &str| -> std::result::Result<String, String> {
            obj[name]
                .as_str()
                .map(str::to_string)
                .ok_or(format!("{name} is not a string"))
        };
        let nullable =
            |v: &serde_json::Value, name: &str| -> std::result::Result<Option<String>, String> {
                match v {
                    serde_json::Value::Null => Ok(None),
                    serde_json::Value::String(s) => Ok(Some(s.clone())),
                    _ => Err(format!("{name} is neither a string nor null")),
                }
            };
        let digest = |name: &str, optional: bool| -> std::result::Result<Option<String>, String> {
            let v = nullable(&digests[name], name)?;
            match &v {
                None if optional => Ok(None),
                Some(d) if is_digest(d) => Ok(v),
                _ => Err(format!("digests.{name} is not sha256:<hex>")),
            }
        };
        let platform = text("platform")?;
        if !constants::PLATFORMS.iter().any(|p| p.key == platform) {
            return Err(format!("platform {platform:?} is not one of v1's"));
        }
        let version = text("version")?;
        let parts: Vec<&str> = version.split('.').collect();
        if parts.len() != 4
            || parts
                .iter()
                .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
        {
            return Err("version is not four-part".to_string());
        }
        let build = text("build")?;
        if build.is_empty() {
            return Err("empty build".to_string());
        }
        let library = text("library")?;
        if library.is_empty()
            || library == "."
            || library == ".."
            || library.contains('/')
            || library.contains('\\')
        {
            return Err("library is not a plain relative file name".to_string());
        }
        let library_sha256 = text("library_sha256")?;
        if !is_hex64(&library_sha256) {
            return Err("library_sha256 is not 64 lowercase hex".to_string());
        }
        let library_bytes = obj["library_bytes"]
            .as_u64()
            .ok_or("library_bytes is not a non-negative integer")?;
        if !obj["predicate"].is_object() {
            return Err("predicate is not an object".to_string());
        }
        Ok(Self {
            platform,
            version,
            build,
            channel: nullable(&obj["channel"], "channel")?,
            index_digest: digest("index", true)?,
            manifest_digest: digest("manifest", false)?.unwrap_or_default(),
            layer_digest: digest("layer", false)?.unwrap_or_default(),
            bundle_digest: digest("bundle", true)?,
            bundle_manifest_digest: digest("bundle_manifest", true)?,
            signed_by: nullable(&obj["signed_by"], "signed_by")?.unwrap_or_default(),
            library,
            library_sha256,
            library_bytes,
            predicate: obj["predicate"].clone(),
        })
    }
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
    for dir in [
        root.join("blobs").join("sha256"),
        root.join(constants::CACHE_UNPACKED_DIR),
    ] {
        std::fs::create_dir_all(&dir).map_err(unwritable(&dir))?;
    }
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

/// Read back an existing `unpacked/sha256/<hex>/verified.json`. A record
/// that is missing, unreadable, unparsable, of another schema or breaks a
/// rule is ABSENT (`Ok(None)`), never an error and never trusted, as in every
/// binding: what could not be read is the cache-fault probe's to warn about,
/// or in strict mode to refuse (public issue #486).
pub fn read_verified(dir: &Path) -> Result<Option<VerifiedRecord>> {
    let path = dir.join(constants::CACHE_VERIFIED_RECORD);
    match std::fs::read(&path) {
        Ok(bytes) => Ok(VerifiedRecord::from_json_bytes(&bytes).ok()),
        Err(_) => Ok(None),
    }
}

/// Install one unpacked artifact atomically: `build` populates a fresh temp
/// directory (unpack the tar, write `verified.json`), which [`install_dir`]
/// then moves into place. If the final directory already carries an
/// acceptable record (another process won the race, or this exact build was
/// already installed), the temp directory is discarded and the existing one
/// is left untouched. If it exists WITHOUT one (a foreign, torn or
/// older-format record), the caller has just re-verified from the cache's
/// own blobs, so the stale directory is moved aside and replaced. The temp
/// directory is consumed either way.
///
/// The temp directory is a new one, `.tmp-<pid>-<random>`, made with
/// `create_dir`, never `create_dir_all`: two installers never share one, even
/// if their names repeat (public issue #482).
pub fn install_unpacked(
    root: &Path,
    manifest_digest: &str,
    build: impl FnOnce(&Path) -> Result<()>,
) -> Result<PathBuf> {
    let final_dir = unpacked_dir(root, manifest_digest)?;
    if read_verified(&final_dir)?.is_some() {
        return Ok(final_dir);
    }
    let parent = final_dir
        .parent()
        .ok_or_else(|| Error::InvalidInput("unpacked dir has no parent".to_string()))?;
    std::fs::create_dir_all(parent).map_err(unwritable(parent))?;
    let tmp = create_unique_dir(parent, ".tmp-")?;
    let result = build(&tmp).and_then(|()| install_dir(&tmp, &final_dir));
    // Gone already after a successful rename; removed here otherwise.
    let _ = std::fs::remove_dir_all(&tmp);
    result.map(|_| final_dir)
}

/// `install_dir`'s bounded retries: each follows another process changing
/// the destination under it, so a handful is plenty.
const INSTALL_ATTEMPTS: usize = 8;

/// Move `src`, a complete directory carrying its `verified.json`, to `dest`
/// by rename, and never remove an entry another process may be using (public
/// issue #482). Several processes of any binding may install the same build
/// into one cache at once, and each must end up with a usable `dest`; Go,
/// Python and TypeScript follow the same rule:
///
/// - The rename comes first. It fails while `dest` exists (POSIX refuses to
///   rename a directory onto a non-empty one), so the first installer wins
///   and every later one finds `dest` in place.
/// - A `dest` that holds an acceptable record is kept: the same content is
///   already installed.
/// - Only a `dest` WITHOUT an acceptable record (foreign, torn or older
///   format) is moved aside, into a fresh `.stale-*` directory beside it
///   (created with `create_dir`, so never one another process holds), and
///   replaced. If what was moved turns out to carry an acceptable record
///   (another installer's rename landed in between), it is put back.
///
/// Returns true when another process's install is the one in place.
fn install_dir(src: &Path, dest: &Path) -> Result<bool> {
    let parent = dest
        .parent()
        .ok_or_else(|| Error::InvalidInput("unpacked dir has no parent".to_string()))?;
    let mut last: Option<std::io::Error> = None;
    for _ in 0..INSTALL_ATTEMPTS {
        match std::fs::rename(src, dest) {
            Ok(()) => return Ok(false),
            Err(e) => last = Some(e),
        }
        if read_verified(dest)?.is_some() {
            return Ok(true);
        }
        if std::fs::symlink_metadata(dest).is_err() {
            continue; // what stood there went away: try the rename again
        }
        let stale = create_unique_dir(parent, ".stale-")?;
        let aside = stale.join("old");
        if std::fs::rename(dest, &aside).is_err() {
            let _ = std::fs::remove_dir_all(&stale);
            continue; // another process moved or replaced it: look again
        }
        if read_verified(&aside)?.is_some() && std::fs::rename(&aside, dest).is_ok() {
            let _ = std::fs::remove_dir_all(&stale);
            return Ok(true);
        }
        let _ = std::fs::remove_dir_all(&stale);
    }
    Err(match last {
        Some(e) => unwritable(dest)(e),
        None => Error::InvalidInput(format!("could not install {}", dest.display())),
    })
}

/// An entry under `unpacked/sha256/`: its manifest digest's 64 lowercase hex.
/// An installer's temporary directory beside the entries (`.tmp-*`,
/// `.stale-*`, another binding's `unpack-*` or `.staging-*`) may already carry
/// a record, and is renamed away a moment later, so it is never listed.
fn is_entry_name(name: &std::ffi::OsStr) -> bool {
    name.to_str().is_some_and(|n| {
        n.len() == 64
            && n.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

/// List every verified record under `root`'s `unpacked/` tree (used by
/// `list_installed`/`resolve_installed`/the monotonicity check). A read-only
/// system directory is scanned the same way, by the caller passing its root.
///
/// A root or an entry that cannot be read is ABSENT here, as in every binding
/// (public issue #486): the cache-fault probe warns about it, or in strict
/// mode refuses it, before any listing is trusted.
pub fn list_verified(root: &Path) -> Result<Vec<(PathBuf, VerifiedRecord)>> {
    let dir = root.join(constants::CACHE_UNPACKED_DIR);
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Ok(out);
    };
    let mut names: Vec<std::ffi::OsString> = entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()) && is_entry_name(&e.file_name()))
        .map(|e| e.file_name())
        .collect();
    names.sort();
    for name in names {
        let entry = dir.join(name);
        if let Some(record) = read_verified(&entry)? {
            out.push((entry, record));
        }
    }
    Ok(out)
}

/// A 0.x registry directory's per-line entry: two numeric parts, `26.8`.
fn is_zero_x_minor(name: &str) -> bool {
    let mut parts = name.split('.');
    let numeric =
        |p: Option<&str>| p.is_some_and(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
    numeric(parts.next()) && numeric(parts.next()) && parts.next().is_none()
}

/// The `<minor>/manifest.json` a 0.x registry directory holds, when `root` has
/// that shape: no `oci-layout`, no `unpacked/`, and at least one
/// `<minor>/manifest.json` (the first by name). A missing `oci-layout` alone is
/// not the shape (a Python-written 1.x cache has none), and a directory that
/// has `unpacked/` is a 1.x cache whoever wrote it. `None` for anything else,
/// including a directory that is missing or cannot be read.
pub fn zero_x_shape(root: &Path) -> Option<String> {
    for name in ["oci-layout", "unpacked"] {
        match std::fs::symlink_metadata(root.join(name)) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            _ => return None,
        }
    }
    let mut names: Vec<String> = std::fs::read_dir(root)
        .ok()?
        .filter_map(|e| e.ok()?.file_name().into_string().ok())
        .filter(|n| is_zero_x_minor(n))
        .collect();
    names.sort();
    names.into_iter().find_map(|name| {
        let dir = std::fs::symlink_metadata(root.join(&name)).ok()?;
        let manifest = std::fs::symlink_metadata(root.join(&name).join("manifest.json")).ok()?;
        (dir.is_dir() && manifest.is_file()).then(|| format!("{name}/manifest.json"))
    })
}

/// The sentence a `CHTYPES_ARTIFACT_MISSING` answer from a cache with the 0.x
/// shape carries (docs/guides/fetch-v1.md, "Upgrading from 0.x"), or `None`.
pub fn zero_x_hint(root: &Path) -> Option<String> {
    let shape = zero_x_shape(root)?;
    Some(format!(
        "{} holds a 0.x registry ({shape}); chtypes 1.x uses an OCI layout at \
         ${{XDG_CACHE_HOME:-~/.cache}}/chtypes/v1 — point CHTYPES_CACHE at an empty or 1.x directory.",
        root.display()
    ))
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
/// into place, so a reader never observes a partial write. A failure is the
/// cache's: `CHTYPES_CACHE_UNUSABLE` with reason `unwritable`, naming the path
/// that failed (public issue #486).
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        Error::InvalidInput(format!("{} has no parent directory", path.display()))
    })?;
    std::fs::create_dir_all(parent).map_err(unwritable(parent))?;
    let tmp = write_temp_sibling(path, bytes).map_err(|(tmp, e)| unwritable(&tmp)(e))?;
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        unwritable(path)(e)
    })
}

/// [`write_atomic`] for a file outside the cache (the lock), whose I/O
/// failure is the caller's to class.
pub fn write_atomic_io(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = write_temp_sibling(path, bytes).map_err(|(_, e)| e)?;
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}

/// How many fresh names [`create_unique_dir`] and [`write_temp_sibling`] try
/// before giving up. A repeat needs another process with the same process id
/// to draw the same 64 random bits, so a second try is already a remote
/// chance; this only bounds a loop that should never run twice.
const TEMP_ATTEMPTS: usize = 16;

/// A new, empty directory `<prefix><pid>-<random>` in `parent`, created with
/// `create_dir`: a name that already exists is never reused, another is drawn
/// (public issue #482). A failure is the cache's, naming the path.
fn create_unique_dir(parent: &Path, prefix: &str) -> Result<PathBuf> {
    let mut last = None;
    for _ in 0..TEMP_ATTEMPTS {
        let dir = parent.join(format!("{prefix}{}", unique_suffix()));
        match std::fs::create_dir(&dir) {
            Ok(()) => return Ok(dir),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => last = Some((dir, e)),
            Err(e) => return Err(unwritable(&dir)(e)),
        }
    }
    let (dir, e) = last.expect("TEMP_ATTEMPTS is not zero");
    Err(unwritable(&dir)(e))
}

/// Write `bytes` to a new sibling of `path`, `.tmp-<name>-<pid>-<random>`,
/// opened with `create_new`: a temporary file another writer holds is never
/// opened, truncated or renamed away by this one (public issue #482). The
/// mode is `fs::write`'s, 0666 less the umask. Returns the temporary path, or
/// the path that failed with its error.
fn write_temp_sibling(
    path: &Path,
    bytes: &[u8],
) -> std::result::Result<PathBuf, (PathBuf, std::io::Error)> {
    use std::io::Write as _;
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("x");
    let mut last = None;
    for _ in 0..TEMP_ATTEMPTS {
        let tmp = path.with_file_name(format!(".tmp-{name}-{}", unique_suffix()));
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp)
        {
            Ok(mut file) => {
                let written = file.write_all(bytes);
                drop(file);
                return match written {
                    Ok(()) => Ok(tmp),
                    Err(e) => {
                        let _ = std::fs::remove_file(&tmp);
                        Err((tmp, e))
                    }
                };
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => last = Some((tmp, e)),
            Err(e) => return Err((tmp, e)),
        }
    }
    Err(last.expect("TEMP_ATTEMPTS is not zero"))
}

/// The variable part of every temporary and aside name: this process's id
/// and 64 random bits (public issue #482). It used to be a clock reading and
/// the thread id, and the main thread is `ThreadId(1)` in every process, so
/// two processes that read the clock in the same step chose the same names,
/// shared a temporary directory, and one renamed the other's half-written
/// files into place. Every name made from it is also created exclusively, so
/// even a repeat is refused, never shared.
fn unique_suffix() -> String {
    use std::hash::{BuildHasher, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};
    if let Some(forced) = forced_suffix() {
        return forced;
    }
    static DRAWN: AtomicU64 = AtomicU64::new(0);
    // RandomState's SipHash keys come from the operating system's random
    // source, drawn afresh in every process, and differ for every
    // RandomState made; the counter only keeps two draws' inputs apart.
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(DRAWN.fetch_add(1, Ordering::Relaxed));
    format!("{}-{:016x}", std::process::id(), hasher.finish())
}

#[cfg(test)]
thread_local! {
    /// Test seam: suffixes [`unique_suffix`] hands out first, in order, so a
    /// test can make a name repeat exactly as two processes' names once did.
    static FORCED_SUFFIXES: std::cell::RefCell<std::collections::VecDeque<String>> =
        const { std::cell::RefCell::new(std::collections::VecDeque::new()) };
}

#[cfg(test)]
fn forced_suffix() -> Option<String> {
    FORCED_SUFFIXES.with(|q| q.borrow_mut().pop_front())
}

#[cfg(not(test))]
fn forced_suffix() -> Option<String> {
    None
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

    fn sample_record() -> VerifiedRecord {
        VerifiedRecord {
            platform: "linux-arm64".to_string(),
            version: "26.8.15.10".to_string(),
            build: "20261001.183455".to_string(),
            channel: None,
            index_digest: None,
            manifest_digest: format!("sha256:{}", "a".repeat(64)),
            layer_digest: format!("sha256:{}", "b".repeat(64)),
            bundle_digest: None,
            bundle_manifest_digest: None,
            signed_by: String::new(),
            library: "libchtypes.so".to_string(),
            library_sha256: "c".repeat(64),
            library_bytes: 3,
            predicate: serde_json::json!({"build": "20261001.183455"}),
        }
    }

    #[test]
    fn install_unpacked_is_idempotent() {
        let root = std::env::temp_dir().join(format!("ocifetch-install-test-{}", unique_suffix()));
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let record = sample_record().to_json_bytes().unwrap();
        let mut calls = 0;
        let dir1 = install_unpacked(&root, digest, |tmp| {
            calls += 1;
            write_atomic(&tmp.join("verified.json"), &record)
        })
        .unwrap();
        let dir2 = install_unpacked(&root, digest, |tmp| {
            calls += 1;
            write_atomic(&tmp.join("verified.json"), &record)
        })
        .unwrap();
        assert_eq!(dir1, dir2);
        assert_eq!(calls, 1, "the second install must not re-run build()");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn record_round_trips_and_writes_every_member() {
        let bytes = sample_record().to_json_bytes().unwrap();
        let doc: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(doc["channel"].is_null() && doc["signed_by"].is_null());
        assert!(doc["digests"]["index"].is_null() && doc["digests"]["bundle_manifest"].is_null());
        let back = VerifiedRecord::from_json_bytes(&bytes).unwrap();
        assert_eq!(back.build, "20261001.183455");
        assert_eq!(back.library_sha256, "c".repeat(64));
    }

    #[test]
    fn foreign_and_malformed_records_are_refused() {
        let good = String::from_utf8(sample_record().to_json_bytes().unwrap()).unwrap();
        let old_flat = r#"{"platform":"linux-arm64","version":"26.8.15.10","build":"1","manifest_digest":"x"}"#;
        for doc in [
            "not json".to_string(),
            String::new(),
            old_flat.to_string(),
            good.replacen("\"schema\":1", "\"schema\":2", 1),
            good.replacen(
                "\"library\":\"libchtypes.so\"",
                "\"library\":\"/etc/passwd\"",
                1,
            ),
            good.replacen("\"library\":\"libchtypes.so\"", "\"library\":\"../x\"", 1),
            good.replacen(&"c".repeat(64), "abc", 1),
            good.replacen("\"signed_by\":null,", "", 1),
        ] {
            assert!(
                VerifiedRecord::from_json_bytes(doc.as_bytes()).is_err(),
                "accepted: {doc}"
            );
        }
        assert!(VerifiedRecord::from_json_bytes(good.as_bytes()).is_ok());
    }

    #[test]
    fn install_unpacked_replaces_an_unreadable_record() {
        let root = std::env::temp_dir().join(format!("ocifetch-replace-test-{}", unique_suffix()));
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let stale = unpacked_dir(&root, digest).unwrap();
        std::fs::create_dir_all(&stale).unwrap();
        std::fs::write(stale.join("manifest.json"), b"{}").unwrap();
        std::fs::write(stale.join("verified.json"), br#"{"platform":"x"}"#).unwrap();
        let record = sample_record().to_json_bytes().unwrap();
        let dir = install_unpacked(&root, digest, |tmp| {
            write_atomic(&tmp.join("libchtypes.so"), b"abc")?;
            write_atomic(&tmp.join("verified.json"), &record)
        })
        .unwrap();
        assert!(read_verified(&dir).unwrap().is_some());
        assert!(!dir.join("manifest.json").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Many installers of one build into one cache at once (public issue
    /// #482): every one succeeds and names the same entry, and nothing but
    /// the entry is left under `unpacked/sha256/`.
    #[test]
    fn concurrent_installs_all_succeed_and_leave_one_entry() {
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let record = sample_record().to_json_bytes().unwrap();
        for _ in 0..10 {
            let root = std::env::temp_dir().join(format!("ocifetch-race-test-{}", unique_suffix()));
            let barrier = std::sync::Barrier::new(16);
            let results: Vec<Result<PathBuf>> = std::thread::scope(|scope| {
                let handles: Vec<_> = (0..16)
                    .map(|_| {
                        scope.spawn(|| {
                            barrier.wait();
                            install_unpacked(&root, digest, |tmp| {
                                write_atomic(&tmp.join("libchtypes.so"), b"abc")?;
                                write_atomic(&tmp.join("verified.json"), &record)
                            })
                        })
                    })
                    .collect();
                handles.into_iter().map(|h| h.join().unwrap()).collect()
            });
            let want = unpacked_dir(&root, digest).unwrap();
            for r in results {
                assert_eq!(r.unwrap(), want);
            }
            assert!(read_verified(&want).unwrap().is_some());
            let left: Vec<_> = std::fs::read_dir(want.parent().unwrap())
                .unwrap()
                .map(|e| e.unwrap().file_name())
                .collect();
            assert_eq!(left, vec![std::ffi::OsString::from("a".repeat(64))]);
            let _ = std::fs::remove_dir_all(&root);
        }
    }

    #[test]
    fn only_manifest_hex_names_are_listed() {
        let root = std::env::temp_dir().join(format!("ocifetch-list-test-{}", unique_suffix()));
        let record = sample_record().to_json_bytes().unwrap();
        for name in [".tmp-1".to_string(), "unpack-2".to_string(), "b".repeat(64)] {
            let dir = root.join(constants::CACHE_UNPACKED_DIR).join(name);
            write_atomic(&dir.join("verified.json"), &record).unwrap();
        }
        let listed = list_verified(&root).unwrap();
        assert_eq!(listed.len(), 1);
        assert!(listed[0].0.ends_with("b".repeat(64)));
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The next names [`unique_suffix`] hands out on this thread.
    fn force_suffixes(names: &[&str]) {
        FORCED_SUFFIXES.with(|q| q.borrow_mut().extend(names.iter().map(|n| n.to_string())));
    }

    /// Temporary and aside names carry the process id and fresh random bits
    /// (public issue #482): the main thread is `ThreadId(1)` in every
    /// process, so a clock reading and a thread id repeated across processes.
    #[test]
    fn temporary_names_carry_the_process_id_and_differ() {
        let (a, b) = (unique_suffix(), unique_suffix());
        assert!(a.starts_with(&format!("{}-", std::process::id())), "{a}");
        assert_ne!(a, b);
    }

    /// A temporary directory whose name another installer holds is never
    /// shared: the install draws a fresh name, and the other installer's
    /// directory and its files are left as they were. When two processes
    /// shared one, whichever renamed it into place first took the other's
    /// half-written files with it, and the other then failed on a path that
    /// had vanished, `No such file or directory` (public issue #482).
    #[test]
    fn a_temporary_directory_another_installer_holds_is_never_shared() {
        let root = std::env::temp_dir().join(format!("ocifetch-tmp-held-{}", unique_suffix()));
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let parent = unpacked_dir(&root, digest)
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf();
        let theirs = parent.join(".tmp-held");
        std::fs::create_dir_all(&theirs).unwrap();
        std::fs::write(theirs.join("half-written"), b"theirs").unwrap();
        force_suffixes(&["held"]);
        let record = sample_record().to_json_bytes().unwrap();
        let dir = install_unpacked(&root, digest, |tmp| {
            std::fs::write(tmp.join("libchtypes.so"), b"abc")?;
            std::fs::write(tmp.join("verified.json"), &record)?;
            Ok(())
        })
        .unwrap();
        assert!(read_verified(&dir).unwrap().is_some());
        assert!(
            !dir.join("half-written").exists(),
            "the other installer's file was installed as this one's"
        );
        assert_eq!(
            std::fs::read(theirs.join("half-written")).ok().as_deref(),
            Some(&b"theirs"[..]),
            "the other installer's temporary directory was taken"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    /// A temporary file another writer holds is never opened, truncated or
    /// renamed away: the write draws a fresh name (public issue #482).
    #[test]
    fn a_temporary_file_another_writer_holds_is_never_shared() {
        let dir = std::env::temp_dir().join(format!("ocifetch-tmpfile-held-{}", unique_suffix()));
        std::fs::create_dir_all(&dir).unwrap();
        let theirs = dir.join(".tmp-index.json-held");
        std::fs::write(&theirs, b"theirs").unwrap();
        force_suffixes(&["held"]);
        write_atomic(&dir.join("index.json"), b"ours").unwrap();
        assert_eq!(std::fs::read(dir.join("index.json")).unwrap(), b"ours");
        assert_eq!(
            std::fs::read(&theirs).ok().as_deref(),
            Some(&b"theirs"[..]),
            "the other writer's temporary file was taken"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The aside directory is a fresh one too: an existing `.stale-*` name
    /// (here an empty directory, which a rename onto it would silently
    /// replace) is never reused, and the entry it replaces is moved INTO a
    /// new directory.
    #[test]
    fn an_aside_name_another_installer_holds_is_never_reused() {
        let root = std::env::temp_dir().join(format!("ocifetch-stale-held-{}", unique_suffix()));
        let digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let entry = unpacked_dir(&root, digest).unwrap();
        std::fs::create_dir_all(&entry).unwrap();
        std::fs::write(entry.join("verified.json"), br#"{"platform":"x"}"#).unwrap();
        let theirs = entry.parent().unwrap().join(".stale-held");
        std::fs::create_dir(&theirs).unwrap();
        // The temporary directory draws the first name, the aside the second.
        force_suffixes(&["mine", "held"]);
        let record = sample_record().to_json_bytes().unwrap();
        let dir = install_unpacked(&root, digest, |tmp| {
            std::fs::write(tmp.join("libchtypes.so"), b"abc")?;
            std::fs::write(tmp.join("verified.json"), &record)?;
            Ok(())
        })
        .unwrap();
        assert!(read_verified(&dir).unwrap().is_some());
        assert!(
            theirs.is_dir(),
            "the other installer's aside directory was taken"
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
