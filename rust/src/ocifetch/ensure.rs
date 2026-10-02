//! The seam (plan §1.3): `ensure`, `resolve_installed`, `list_installed`,
//! `verify_installed`, `fetch_signed`, and the `Resolved` type they return.
//! This is the only module other crates reach through — `mod.rs` is the
//! whole public surface, everything else here is `pub(crate)`.

use std::path::{Path, PathBuf};

use super::constants;
use super::dsse::{self, TrustedKey};
use super::error::{Error, Result};
use super::http::{AuthConfig, Client, Clock};
use super::layout::{self, VerifiedRecord};
use super::lock::{self, Lock, LockEntry};
use super::oci::{self, Source, VersionRequest};
use super::referrers;
use super::unpack;

/// Every digest this module checked, for diagnostics and the lock file.
#[derive(Clone, Debug)]
pub struct Digests {
    pub index: Option<String>,
    pub manifest: String,
    pub layer: String,
    pub bundle: Option<String>,
}

/// The result of a successful [`ensure`], [`resolve_installed`] or one entry
/// of [`list_installed`] (plan §1.3).
#[derive(Clone, Debug)]
pub struct Resolved {
    pub abi_generation: u32,
    pub platform: String,
    pub request: String,
    pub version: String,
    pub channel: Option<String>,
    pub build: String,
    pub library_path: PathBuf,
    pub dir: PathBuf,
    pub digests: Digests,
    pub predicate: serde_json::Value,
    pub signed_by: String,
    pub source: String,
    pub already_installed: bool,
    pub warnings: Vec<String>,
}

/// One entry of [`verify_installed`]'s report.
pub struct VerifyResult {
    pub dir: PathBuf,
    pub ok: bool,
    pub detail: String,
}

/// Options shared by every entry point. A library caller builds one per
/// call; nothing here is read from `std::env` except where documented.
pub struct Options {
    /// The platform key (`"linux-arm64"`, …). Defaults to the host platform.
    pub platform: Option<String>,
    /// Base URLs, most-preferred first. Defaults to `$CHTYPES_ARTIFACTS_URL`
    /// (comma-separated) or `constants::DEFAULT_BASES`.
    pub bases: Option<Vec<String>>,
    /// The OCI-layout cache root. Defaults to `$CHTYPES_CACHE` or
    /// `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`.
    pub cache_dir: Option<String>,
    /// Read-only system directories, searched after the cache.
    pub system_dirs: Vec<PathBuf>,
    /// Never touch the network; read the cache only.
    pub offline: bool,
    /// Perform no discovery: fetch exactly the lock's pinned digests.
    pub frozen: bool,
    /// Where the lock lives. `frozen`/`lock_write` without one is a
    /// configuration error (`SourceIncompatible`).
    pub lock_path: Option<PathBuf>,
    /// After a successful online resolve, record it in the lock.
    pub lock_write: bool,
    /// Re-resolve even if the lock already pins this request.
    pub update: bool,
    /// Proceed, with a warning, when no bundle verifies.
    pub allow_unsigned: bool,
    /// Also trust `constants::TEST_KEYS` (fixtures only; never the default).
    pub trust_test_keys: bool,
    /// `$CHTYPES_DOWNLOAD_TOKEN` override.
    pub token: Option<String>,
    /// An injected clock, for the conformance suite's exact-sleep assertions.
    pub clock: Option<Box<dyn Clock>>,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            platform: None,
            bases: None,
            cache_dir: None,
            system_dirs: constants::SYSTEM_CACHE_DIRS
                .iter()
                .map(PathBuf::from)
                .collect(),
            offline: false,
            frozen: false,
            lock_path: None,
            lock_write: false,
            update: false,
            allow_unsigned: false,
            trust_test_keys: false,
            token: None,
            clock: None,
        }
    }
}

struct Resources {
    root: PathBuf,
    platform: String,
    bases: Vec<String>,
    system_dirs: Vec<PathBuf>,
    client: Client,
    auth: AuthConfig,
    trust: Vec<TrustedKey>,
}

fn resources(options: &mut Options) -> Result<Resources> {
    let root = layout::cache_root(options.cache_dir.as_deref())?;
    layout::ensure_layout(&root)?;
    let platform = options
        .platform
        .clone()
        .unwrap_or_else(default_host_platform);
    if !constants::PLATFORMS.iter().any(|p| p.key == platform) {
        return Err(Error::InvalidInput(format!(
            "platform {platform:?} is not one of {:?}",
            constants::PLATFORMS
                .iter()
                .map(|p| p.key)
                .collect::<Vec<_>>()
        )));
    }
    let bases = options.bases.clone().unwrap_or_else(|| {
        std::env::var(constants::ENV_BASES_NAME)
            .ok()
            .filter(|s| !s.is_empty())
            .map(|s| {
                s.split(constants::BASE_SEPARATOR)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_else(|| {
                constants::DEFAULT_BASES
                    .iter()
                    .map(|s| s.to_string())
                    .collect()
            })
    });
    let token = options
        .token
        .clone()
        .or_else(|| std::env::var(constants::ENV_TOKEN_NAME).ok());
    let clock = options
        .clock
        .take()
        .unwrap_or_else(|| Box::new(super::http::RealClock));
    let client = Client::with_clock(clock);
    let auth = AuthConfig {
        static_token: token,
    };
    let trust = dsse::trusted_keys(options.trust_test_keys)?;
    Ok(Resources {
        root,
        platform,
        bases,
        system_dirs: options.system_dirs.clone(),
        client,
        auth,
        trust,
    })
}

fn default_host_platform() -> String {
    let os = if cfg!(target_os = "macos") {
        "darwin"
    } else {
        "linux"
    };
    let arch = if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "amd64"
    };
    format!("{os}-{arch}")
}

/// `ensure(request, options)`: the full online-or-cached resolve (plan
/// §1.3). On success, the library file exists on disk and every guarantee
/// in §1.3 holds.
pub fn ensure(request: &str, mut options: Options) -> Result<Resolved> {
    let version_request = VersionRequest::parse(request)?;
    let res = resources(&mut options)?;

    let lock = match &options.lock_path {
        Some(p) => lock::read(p)?,
        None => None,
    };

    if options.offline {
        return resolve_offline(&res, &version_request, request, &options, lock.as_ref());
    }

    if options.frozen {
        let lock = lock.ok_or_else(|| {
            Error::ArtifactPinned(format!(
                "--frozen was requested but {} does not exist",
                options
                    .lock_path
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|| "<no lock path given>".to_string())
            ))
        })?;
        let entry = lock.entry(request, &res.platform).ok_or_else(|| {
            Error::ArtifactPinned(format!(
                "--frozen: the lock does not pin {request} for {}",
                res.platform
            ))
        })?;
        return ensure_frozen(&res, request, &res.platform, entry, &options);
    }

    if !options.update {
        if let Some(lock) = &lock {
            if let Some(entry) = lock.entry(request, &res.platform) {
                return ensure_frozen(&res, request, &res.platform, entry, &options);
            }
        }
    }

    let resolved = ensure_online(&res, &version_request, request, &options)?;

    if options.lock_write {
        if let Some(path) = &options.lock_path {
            let mut lock = lock.unwrap_or_default();
            lock.set_entry(
                request,
                &res.platform,
                LockEntry {
                    version: resolved.version.clone(),
                    build: resolved.build.clone(),
                    manifest: resolved.digests.manifest.clone(),
                    layer: resolved.digests.layer.clone(),
                    bundle: resolved.digests.bundle.clone().unwrap_or_default(),
                    index: resolved.digests.index.clone(),
                },
            );
            lock::write(path, &lock)?;
        }
    }

    Ok(resolved)
}

fn ensure_online(
    res: &Resources,
    version_request: &VersionRequest,
    request: &str,
    options: &Options,
) -> Result<Resolved> {
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
    };
    let fetched_index = oci::fetch_by_tag(&source, &version_request.tag())?;
    let index: oci::Index = serde_json::from_slice(&fetched_index.bytes)?;
    let descriptor = oci::select_platform(&index, &res.platform)?;

    let fetched_manifest =
        oci::fetch_by_digest(&source, &descriptor.digest, constants::MANIFEST_MAX_BYTES)?;
    let manifest: oci::Manifest = serde_json::from_slice(&fetched_manifest.bytes)?;
    let [layer] = manifest.layers.as_slice() else {
        return Err(Error::ArtifactCorrupt(format!(
            "platform manifest {} has {} layers, want exactly 1",
            descriptor.digest,
            manifest.layers.len()
        )));
    };

    let trust_result = referrers::find_trusted_statement(&source, &descriptor.digest, &res.trust);
    let (predicate, signed_by, bundle_digest, mut warnings) = match trust_result {
        Ok(stmt) => {
            if stmt.subject_sha256 != strip_sha256(&layer.digest)? {
                return Err(Error::ArtifactCorrupt(format!(
                    "signed subject {} does not match the manifest's layer {}",
                    stmt.subject_sha256, layer.digest
                )));
            }
            (stmt.predicate, stmt.signed_by.to_string(), None, Vec::new())
        }
        Err(Error::ArtifactUntrusted(detail)) if options.allow_unsigned => (
            serde_json::Value::Null,
            String::new(),
            None,
            vec![format!(
                "proceeding WITHOUT a verified signature (CHTYPES_ALLOW_UNSIGNED): {detail}"
            )],
        ),
        Err(e) => return Err(e),
    };

    if !options.allow_unsigned {
        validate_predicate(&predicate, &res.platform, version_request)?;
    }

    let already = layout::unpacked_dir(&res.root, &descriptor.digest)?
        .join(constants::CACHE_VERIFIED_RECORD)
        .exists();

    let (library_sha256, library_bytes, library_name) = if predicate.is_null() {
        (String::new(), 0u64, "library".to_string())
    } else {
        (
            predicate["library_sha256"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            predicate["library_bytes"].as_u64().unwrap_or_default(),
            predicate["library"]
                .as_str()
                .unwrap_or("library")
                .to_string(),
        )
    };

    let dir = if already {
        layout::unpacked_dir(&res.root, &descriptor.digest)?
    } else {
        let fetched_layer =
            oci::fetch_blob_by_digest(&source, &layer.digest, constants::MAX_UNPACKED_BYTES)?;
        let decompressed = unpack::decompress_zstd(&fetched_layer.bytes)?;
        let record = VerifiedRecord {
            platform: res.platform.clone(),
            version: predicate["clickhouse_version"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            build: predicate["build"].as_str().unwrap_or("").to_string(),
            channel: predicate["channel"].as_str().map(str::to_string),
            manifest_digest: descriptor.digest.clone(),
            layer_digest: layer.digest.clone(),
            bundle_digest: bundle_digest.clone(),
            signed_by: signed_by.clone(),
            library: library_name.clone(),
            library_sha256: library_sha256.clone(),
            library_bytes,
            predicate: predicate.clone(),
        };
        layout::install_unpacked(&res.root, &descriptor.digest, |tmp| {
            unpack::unpack_tar(&decompressed, tmp)?;
            if !library_sha256.is_empty() {
                let lib_path = tmp.join(&library_name);
                let lib_bytes = std::fs::read(&lib_path).map_err(|e| {
                    Error::ArtifactCorrupt(format!("library {}: {e}", lib_path.display()))
                })?;
                oci::verify_digest(&lib_bytes, &format!("sha256:{library_sha256}"))?;
                if lib_bytes.len() as u64 != library_bytes {
                    return Err(Error::ArtifactCorrupt(format!(
                        "library is {} bytes, the signed predicate says {}",
                        lib_bytes.len(),
                        library_bytes
                    )));
                }
            }
            layout::write_atomic(
                &tmp.join(constants::CACHE_VERIFIED_RECORD),
                &serde_json::to_vec(&record)?,
            )
        })?
    };

    if let Some(record) = layout::read_verified(&dir)? {
        warnings.extend(monotonic_warning(res, &version_request.tag(), &record)?);
        return Ok(Resolved {
            abi_generation: constants::ABI_GENERATION,
            platform: res.platform.clone(),
            request: request.to_string(),
            version: record.version,
            channel: record.channel,
            build: record.build,
            library_path: dir.join(&record.library),
            dir,
            digests: Digests {
                index: None,
                manifest: descriptor.digest.clone(),
                layer: layer.digest.clone(),
                bundle: record.bundle_digest,
            },
            predicate: record.predicate,
            signed_by: record.signed_by,
            source: fetched_manifest.base,
            already_installed: already,
            warnings,
        });
    }
    Err(Error::ArtifactCorrupt(
        "install succeeded but no verified.json was written".to_string(),
    ))
}

fn ensure_frozen(
    res: &Resources,
    request: &str,
    platform: &str,
    entry: &LockEntry,
    options: &Options,
) -> Result<Resolved> {
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
    };
    let already = layout::unpacked_dir(&res.root, &entry.manifest)?
        .join(constants::CACHE_VERIFIED_RECORD)
        .exists();
    if already {
        let dir = layout::unpacked_dir(&res.root, &entry.manifest)?;
        let record = layout::read_verified(&dir)?.ok_or_else(|| {
            Error::ArtifactCorrupt(format!("{}: missing verified.json", dir.display()))
        })?;
        return Ok(Resolved {
            abi_generation: constants::ABI_GENERATION,
            platform: platform.to_string(),
            request: request.to_string(),
            version: record.version,
            channel: record.channel,
            build: record.build,
            library_path: dir.join(&record.library),
            dir,
            digests: Digests {
                index: entry.index.clone(),
                manifest: entry.manifest.clone(),
                layer: entry.layer.clone(),
                bundle: Some(entry.bundle.clone()),
            },
            predicate: record.predicate,
            signed_by: record.signed_by,
            source: "cache".to_string(),
            already_installed: true,
            warnings: Vec::new(),
        });
    }

    let fetched_manifest =
        oci::fetch_by_digest(&source, &entry.manifest, constants::MANIFEST_MAX_BYTES)?;
    let manifest: oci::Manifest = serde_json::from_slice(&fetched_manifest.bytes)?;
    let [layer] = manifest.layers.as_slice() else {
        return Err(Error::ArtifactCorrupt(
            "frozen manifest layer count".to_string(),
        ));
    };
    if layer.digest != entry.layer {
        return Err(Error::ArtifactCorrupt(format!(
            "frozen manifest's layer {} does not match the lock's {}",
            layer.digest, entry.layer
        )));
    }

    let stmt = referrers::find_trusted_statement(&source, &entry.manifest, &res.trust);
    let (predicate, signed_by) = match stmt {
        Ok(s) => (s.predicate, s.signed_by.to_string()),
        Err(e) if options.allow_unsigned => {
            (serde_json::Value::Null, format!("UNSIGNED (allowed): {e}"))
        }
        Err(e) => return Err(e),
    };

    let fetched_layer =
        oci::fetch_blob_by_digest(&source, &layer.digest, constants::MAX_UNPACKED_BYTES)?;
    let decompressed = unpack::decompress_zstd(&fetched_layer.bytes)?;
    let library_sha256 = predicate["library_sha256"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let library_bytes = predicate["library_bytes"].as_u64().unwrap_or_default();
    let library_name = predicate["library"]
        .as_str()
        .unwrap_or("library")
        .to_string();
    let record = VerifiedRecord {
        platform: platform.to_string(),
        version: entry.version.clone(),
        build: entry.build.clone(),
        channel: predicate["channel"].as_str().map(str::to_string),
        manifest_digest: entry.manifest.clone(),
        layer_digest: entry.layer.clone(),
        bundle_digest: Some(entry.bundle.clone()),
        signed_by: signed_by.clone(),
        library: library_name.clone(),
        library_sha256: library_sha256.clone(),
        library_bytes,
        predicate: predicate.clone(),
    };
    let dir = layout::install_unpacked(&res.root, &entry.manifest, |tmp| {
        unpack::unpack_tar(&decompressed, tmp)?;
        layout::write_atomic(
            &tmp.join(constants::CACHE_VERIFIED_RECORD),
            &serde_json::to_vec(&record)?,
        )
    })?;

    Ok(Resolved {
        abi_generation: constants::ABI_GENERATION,
        platform: platform.to_string(),
        request: request.to_string(),
        version: entry.version.clone(),
        channel: record.channel,
        build: entry.build.clone(),
        library_path: dir.join(&library_name),
        dir,
        digests: Digests {
            index: entry.index.clone(),
            manifest: entry.manifest.clone(),
            layer: entry.layer.clone(),
            bundle: Some(entry.bundle.clone()),
        },
        predicate,
        signed_by,
        source: fetched_manifest.base,
        already_installed: false,
        warnings: Vec::new(),
    })
}

fn resolve_offline(
    res: &Resources,
    version_request: &VersionRequest,
    request: &str,
    _options: &Options,
    lock: Option<&Lock>,
) -> Result<Resolved> {
    if let Some(lock) = lock {
        if let Some(entry) = lock.entry(request, &res.platform) {
            let dir = layout::unpacked_dir(&res.root, &entry.manifest)?;
            if let Some(record) = layout::read_verified(&dir)? {
                return Ok(record_to_resolved(
                    request,
                    &res.platform,
                    &dir,
                    record,
                    "cache",
                    true,
                ));
            }
        }
    }
    match find_installed(&res.root, &res.system_dirs, version_request, &res.platform)? {
        Some((dir, record, source)) => Ok(record_to_resolved(
            request,
            &res.platform,
            &dir,
            record,
            &source,
            true,
        )),
        None => Err(Error::ArtifactMissing(format!(
            "--offline: nothing installed satisfies {request} for {}",
            res.platform
        ))),
    }
}

/// `resolve_installed(request, platform, options)`: never touches the
/// network (plan §1.3) — what a loader calls on every open.
pub fn resolve_installed(
    request: &str,
    platform: &str,
    mut options: Options,
) -> Result<Option<Resolved>> {
    let version_request = VersionRequest::parse(request)?;
    options.platform = Some(platform.to_string());
    let res = resources(&mut options)?;
    match find_installed(&res.root, &res.system_dirs, &version_request, &res.platform)? {
        Some((dir, record, source)) => Ok(Some(record_to_resolved(
            request,
            &res.platform,
            &dir,
            record,
            &source,
            true,
        ))),
        None => Ok(None),
    }
}

/// `list_installed(options)`: every verified install in the cache, then the
/// system directories.
pub fn list_installed(mut options: Options) -> Result<Vec<Resolved>> {
    let res = resources(&mut options)?;
    let mut out = Vec::new();
    for (dir, record) in layout::list_verified(&res.root)? {
        out.push(record_to_resolved(
            "",
            &record.platform.clone(),
            &dir,
            record,
            "cache",
            true,
        ));
    }
    for sysdir in &res.system_dirs {
        for (dir, record) in layout::list_verified(sysdir).unwrap_or_default() {
            let source = format!("system:{}", sysdir.display());
            out.push(record_to_resolved(
                "",
                &record.platform.clone(),
                &dir,
                record,
                &source,
                true,
            ));
        }
    }
    Ok(out)
}

/// `verify_installed(options)`: re-hash every installed library against its
/// own recorded `library_sha256`, with no network.
pub fn verify_installed(mut options: Options) -> Result<Vec<VerifyResult>> {
    let res = resources(&mut options)?;
    let mut out = Vec::new();
    for (dir, record) in layout::list_verified(&res.root)? {
        out.push(verify_one_install(&dir, &record));
    }
    Ok(out)
}

fn verify_one_install(dir: &Path, record: &VerifiedRecord) -> VerifyResult {
    let lib_path = dir.join(&record.library);
    match std::fs::read(&lib_path) {
        Ok(bytes) => match oci::verify_digest(&bytes, &format!("sha256:{}", record.library_sha256))
        {
            Ok(()) => VerifyResult {
                dir: dir.to_path_buf(),
                ok: true,
                detail: format!("{} matches the recorded sha256", lib_path.display()),
            },
            Err(e) => VerifyResult {
                dir: dir.to_path_buf(),
                ok: false,
                detail: e.to_string(),
            },
        },
        Err(e) => VerifyResult {
            dir: dir.to_path_buf(),
            ok: false,
            detail: format!("{}: {e}", lib_path.display()),
        },
    }
}

/// The generic signed-artifact fetch (plan §1.3): for a tag or digest fetched
/// directly within `repository_bases` (the fixtures shape), find and verify
/// its own signature referrer, then return the verified content bytes.
/// For a goldens-shaped referrer-of-a-platform-manifest, first resolve the
/// content descriptor with [`referrers::find_content_referrer`] and pass its
/// digest as `reference`.
pub fn fetch_signed(
    repository_bases: &[String],
    reference: &str,
    expected_predicate_type: &str,
    options: &Options,
) -> Result<(Vec<u8>, serde_json::Value, Digests)> {
    let clock: Box<dyn Clock> = Box::new(super::http::RealClock);
    let client = Client::with_clock(clock);
    let token = options
        .token
        .clone()
        .or_else(|| std::env::var(constants::ENV_TOKEN_NAME).ok());
    let auth = AuthConfig {
        static_token: token,
    };
    let trust = dsse::trusted_keys(options.trust_test_keys)?;
    let source = Source {
        bases: repository_bases,
        client: &client,
        auth: &auth,
    };

    let fetched = if reference.starts_with("sha256:") {
        oci::fetch_by_digest(&source, reference, constants::MANIFEST_MAX_BYTES)?
    } else {
        oci::fetch_by_tag(&source, reference)?
    };
    let manifest: oci::Manifest = serde_json::from_slice(&fetched.bytes)?;
    let manifest_digest = if reference.starts_with("sha256:") {
        reference.to_string()
    } else {
        format!("sha256:{}", oci::sha256_hex(&fetched.bytes))
    };
    let [layer] = manifest.layers.as_slice() else {
        return Err(Error::ArtifactCorrupt(format!(
            "{reference} has {} layers, want exactly 1",
            manifest.layers.len()
        )));
    };

    let stmt = referrers::find_trusted_statement(&source, &manifest_digest, &trust)?;
    if stmt.predicate_type != expected_predicate_type {
        return Err(Error::ArtifactCorrupt(format!(
            "{reference}'s signature predicateType is {:?}, want {:?}",
            stmt.predicate_type, expected_predicate_type
        )));
    }
    if stmt.subject_sha256 != strip_sha256(&layer.digest)? {
        return Err(Error::ArtifactCorrupt(format!(
            "{reference}'s signed subject does not match its own layer {}",
            layer.digest
        )));
    }

    let blob = oci::fetch_blob_by_digest(&source, &layer.digest, constants::MAX_UNPACKED_BYTES)?;
    Ok((
        blob.bytes,
        stmt.predicate,
        Digests {
            index: None,
            manifest: manifest_digest,
            layer: layer.digest.clone(),
            bundle: None,
        },
    ))
}

fn strip_sha256(digest: &str) -> Result<String> {
    digest
        .strip_prefix("sha256:")
        .map(str::to_string)
        .ok_or_else(|| Error::ArtifactCorrupt(format!("digest {digest:?} is not sha256:<hex>")))
}

/// `predicate.abi` must be the JSON integer `1` (never the v0 field
/// `abi_revision`), and `os`/`arch` must equal `platform`, and the version
/// must lie within `version_request` (plan §1.3's guarantees).
fn validate_predicate(
    predicate: &serde_json::Value,
    platform: &str,
    version_request: &VersionRequest,
) -> Result<()> {
    let plat = constants::PLATFORMS
        .iter()
        .find(|p| p.key == platform)
        .expect("validated by `resources`");
    match predicate.get("abi") {
        Some(serde_json::Value::Number(n))
            if n.as_u64() == Some(constants::ABI_GENERATION as u64) => {}
        _ => {
            return Err(Error::ArtifactCorrupt(format!(
                "predicate.abi is {:?}, want the integer {}",
                predicate.get("abi"),
                constants::ABI_GENERATION
            )));
        }
    }
    if predicate.get("os").and_then(|v| v.as_str()) != Some(plat.os) {
        return Err(Error::ArtifactCorrupt(format!(
            "predicate.os is {:?}, want {:?}",
            predicate.get("os"),
            plat.os
        )));
    }
    if predicate.get("arch").and_then(|v| v.as_str()) != Some(plat.architecture) {
        return Err(Error::ArtifactCorrupt(format!(
            "predicate.arch is {:?}, want {:?}",
            predicate.get("arch"),
            plat.architecture
        )));
    }
    let version = predicate
        .get("clickhouse_version")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::ArtifactCorrupt("predicate has no clickhouse_version".to_string()))?;
    if !version_request.matches(version) {
        return Err(Error::ArtifactCorrupt(format!(
            "predicate.clickhouse_version {version:?} does not lie within the request"
        )));
    }
    Ok(())
}

/// A higher installed build offered by the source than what is already
/// installed is fine, but worth a loud warning rather than a silent
/// downgrade-looking swap (`monotonic-warning`).
fn monotonic_warning(res: &Resources, tag: &str, record: &VerifiedRecord) -> Result<Vec<String>> {
    let mut warnings = Vec::new();
    for (_, other) in layout::list_verified(&res.root)? {
        if other.platform != record.platform || other.manifest_digest == record.manifest_digest {
            continue;
        }
        if !other.version.starts_with(tag) {
            continue;
        }
        if build_key(&other.build) > build_key(&record.build) {
            warnings.push(format!(
                "a newer build ({} at {}) is already installed for {tag}/{}; resolving to the source's offer ({} at {}) anyway",
                other.version, other.build, record.platform, record.version, record.build
            ));
        }
    }
    Ok(warnings)
}

/// Builds compare as fixed-width strings (`"20261001.183455"`), per plan
/// §3.2's `offline-newest-build`.
fn build_key(build: &str) -> &str {
    build
}

/// Scan the cache, then each system directory in order, for the record with
/// the newest (version, then build) matching `version_request` for
/// `platform`.
fn find_installed(
    root: &Path,
    system_dirs: &[PathBuf],
    version_request: &VersionRequest,
    platform: &str,
) -> Result<Option<(PathBuf, VerifiedRecord, String)>> {
    let mut best: Option<(PathBuf, VerifiedRecord, String)> = None;
    let mut consider = |dir: PathBuf, record: VerifiedRecord, source: String| {
        if record.platform != platform || !version_request.matches(&record.version) {
            return;
        }
        let better = match &best {
            None => true,
            Some((_, b, _)) => {
                (record.version.as_str(), build_key(&record.build))
                    > (b.version.as_str(), build_key(&b.build))
            }
        };
        if better {
            best = Some((dir, record, source));
        }
    };
    for (dir, record) in layout::list_verified(root)? {
        consider(dir, record, "cache".to_string());
    }
    for sysdir in system_dirs {
        for (dir, record) in layout::list_verified(sysdir).unwrap_or_default() {
            let source = format!("system:{}", sysdir.display());
            consider(dir, record, source);
        }
    }
    Ok(best)
}

fn record_to_resolved(
    request: &str,
    platform: &str,
    dir: &Path,
    record: VerifiedRecord,
    source: &str,
    already_installed: bool,
) -> Resolved {
    Resolved {
        abi_generation: constants::ABI_GENERATION,
        platform: platform.to_string(),
        request: request.to_string(),
        version: record.version,
        channel: record.channel,
        build: record.build,
        library_path: dir.join(&record.library),
        dir: dir.to_path_buf(),
        digests: Digests {
            index: None,
            manifest: record.manifest_digest,
            layer: record.layer_digest,
            bundle: record.bundle_digest,
        },
        predicate: record.predicate,
        signed_by: record.signed_by,
        source: source.to_string(),
        already_installed,
        warnings: Vec::new(),
    }
}
