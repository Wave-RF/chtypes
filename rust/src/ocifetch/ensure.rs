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
    /// The bundle's own layer digest — the signed bytes a trust check
    /// verified (`dsse::verify_bundle`'s input).
    pub bundle: Option<String>,
    /// The referrer manifest that carried the bundle layer above.
    pub bundle_manifest: Option<String>,
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
    /// The trust list: raw 32-byte ed25519 public keys as hex. Non-empty
    /// REPLACES the default (else `$CHTYPES_TRUSTED_KEYS`, else the release
    /// key); it never appends to it.
    pub trusted_keys: Option<Vec<String>>,
    /// `$CHTYPES_DOWNLOAD_TOKEN` override.
    pub token: Option<String>,
    /// An injected clock, for the conformance suite's exact-sleep assertions.
    pub clock: Option<Box<dyn Clock>>,
    /// Test seam: runs between the read and the rename of this call's
    /// `index.json` update, to play a competing writer
    /// (`before_index_rename_hook`, docs/guides/fetch-v1.md §10).
    pub before_index_rename: Option<Box<dyn Fn()>>,
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
            trusted_keys: None,
            token: None,
            clock: None,
            before_index_rename: None,
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

/// The ordered base list one call uses: the options' own, else
/// `$CHTYPES_ARTIFACTS_URL` (comma-separated), else the built-in default.
pub fn configured_bases(options: &Options) -> Vec<String> {
    options.bases.clone().unwrap_or_else(|| {
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
    })
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
    let bases = configured_bases(options);
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
    let trust = dsse::trusted_keys(options.trusted_keys.as_deref())?;
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
    if std::env::var(constants::ENV_ALLOW_UNSIGNED_NAME).is_ok_and(|v| v == "1") {
        options.allow_unsigned = true;
    }
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

    // `update` always rewrites the lock (it exists to refresh a stale
    // entry), even when `lock_write` was not separately requested — plan
    // §6: "update re-resolves every locked request... and rewrites the
    // lock; it never merges a stale entry with a fresh one."
    if options.lock_write || options.update {
        if let Some(path) = &options.lock_path {
            let mut lock = lock.unwrap_or_default();
            if options.lock_write {
                // Plan §6: "Writing a lock for every platform the index
                // offers... downloads and verifies every platform's bundle
                // but fetches the layer only for the host's own platform."
                for (platform, entry) in
                    lock_entries_for_all_platforms(&res, &version_request, &resolved)?
                {
                    lock.set_entry(request, &platform, entry);
                }
            } else {
                lock.set_entry(
                    request,
                    &res.platform,
                    LockEntry {
                        version: resolved.version.clone(),
                        build: resolved.build.clone(),
                        manifest: resolved.digests.manifest.clone(),
                        layer: resolved.digests.layer.clone(),
                        bundle: resolved.digests.bundle.clone().unwrap_or_default(),
                        // The lock never records the index (§6: informational only).
                        index: None,
                    },
                );
            }
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
    let index = oci::parse_index(&fetched_index.bytes, &format!("index for {request:?}"))?;
    let descriptor = oci::select_platform(&index, &res.platform)?;

    let fetched_manifest =
        oci::fetch_by_digest(&source, &descriptor.digest, constants::MANIFEST_MAX_BYTES)?;
    let manifest: oci::Manifest = serde_json::from_slice(&fetched_manifest.bytes)?;
    oci::check_platform_manifest(&manifest, &format!("manifest {}", descriptor.digest))?;
    let layer = &manifest.layers[0];

    let trust_result = referrers::find_trusted_statement(&source, &descriptor.digest, &res.trust);
    let (predicate, signed_by, bundle_layer_digest, bundle_manifest_digest, mut warnings) =
        match trust_result {
            Ok(stmt) => {
                check_artifact_statement(&stmt, &layer.digest)?;
                (
                    stmt.predicate,
                    stmt.signed_by.clone(),
                    Some(stmt.bundle_layer_digest),
                    Some(stmt.bundle_manifest_digest),
                    Vec::new(),
                )
            }
            Err(Error::ArtifactUntrusted(detail)) if options.allow_unsigned => (
                unsigned_config(&source, &manifest)?,
                String::new(),
                None,
                None,
                vec![format!(
                    "chtypes: no verified signature found; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned: {detail}"
                )],
            ),
            Err(e) => return Err(e),
        };
    // An empty `signed_by` means nothing verified: `predicate` is then the
    // manifest's own (untrusted) config blob, so none of the signed-only
    // checks below apply to it.
    let signed = !signed_by.is_empty();

    if signed {
        validate_predicate(&predicate, &res.platform, Some(version_request))?;
    }

    let already = is_fully_installed(&layout::unpacked_dir(&res.root, &descriptor.digest)?)?;

    // Monotonicity (docs/guides/fetch-v1.md §9, `monotonic-warning`): a
    // HIGHER build within the request, already installed, is kept in place
    // of what the source now offers, with a warning, whether or not the
    // offered build is installed too. Decided as soon as the candidate's own
    // signed version/build are known and before its layer is fetched — there
    // is no point downloading bytes this call is about to discard.
    if signed {
        if let Some((dir, record)) =
            newer_installed(res, version_request, &descriptor.digest, &predicate)?
        {
            warnings.push(format!(
                "chtypes: a newer build is already installed locally ({} build {}, monotonic check) than the source just resolved ({} build {}) — keeping the existing install",
                record.version,
                record.build,
                predicate["clickhouse_version"].as_str().unwrap_or("?"),
                predicate["build"].as_str().unwrap_or("?")
            ));
            let mut resolved =
                record_to_resolved(request, &res.platform, &dir, record, "cache", true);
            resolved.warnings = warnings;
            return Ok(resolved);
        }
    }

    let (library_sha256, library_bytes, library_name) = library_fields(&predicate)?;

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
            index_digest: Some(format!("sha256:{}", oci::sha256_hex(&fetched_index.bytes))),
            manifest_digest: descriptor.digest.clone(),
            layer_digest: layer.digest.clone(),
            bundle_digest: bundle_layer_digest.clone(),
            bundle_manifest_digest: bundle_manifest_digest.clone(),
            signed_by: signed_by.clone(),
            library: library_name.clone(),
            library_sha256: library_sha256.clone(),
            library_bytes,
            predicate: predicate.clone(),
        };
        layout::install_unpacked(&res.root, &descriptor.digest, |tmp| {
            unpack::unpack_tar(&decompressed, tmp)?;
            verify_unpacked_library(tmp, &library_name, &library_sha256, library_bytes)?;
            layout::write_atomic(
                &tmp.join(constants::CACHE_VERIFIED_RECORD),
                &record.to_json_bytes()?,
            )
        })?
    };

    if let Some(record) = layout::read_verified(&dir)? {
        // Keep the OCI layout's own view in step (oras interop): the platform
        // manifest as a blob and an `index.json` entry, merged by a
        // read-check-rename loop so a concurrent writer is never clobbered.
        layout::put_blob(&res.root, &descriptor.digest, &fetched_manifest.bytes)?;
        let platform = constants::PLATFORMS.iter().find(|p| p.key == res.platform);
        layout::record_in_index(
            &res.root,
            &descriptor.digest,
            fetched_manifest.bytes.len() as u64,
            constants::MEDIA_TYPE_MANIFEST,
            platform.map(|p| (p.os, p.architecture)),
            options.before_index_rename.as_deref(),
        )?;
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
                index: record.index_digest,
                manifest: descriptor.digest.clone(),
                layer: layer.digest.clone(),
                bundle: record.bundle_digest,
                bundle_manifest: record.bundle_manifest_digest,
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

/// `lock_write`'s "every platform the index offers" rule (plan §6): the
/// host's own platform reuses `host_resolved` (already fetched and
/// verified, no extra request); every other platform has its manifest
/// fetched by digest and its signature verified — but never its layer, so
/// `lock-write-all-platforms`'s "zero layer GETs for non-host platforms"
/// holds.
fn lock_entries_for_all_platforms(
    res: &Resources,
    version_request: &VersionRequest,
    host_resolved: &Resolved,
) -> Result<Vec<(String, LockEntry)>> {
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
    };
    let fetched_index = oci::fetch_by_tag(&source, &version_request.tag())?;
    let index = oci::parse_index(&fetched_index.bytes, "index")?;

    let mut out = Vec::new();
    for d in &index.manifests {
        let Some(p) = &d.platform else { continue };
        let Some(platform) = constants::PLATFORMS
            .iter()
            .find(|pl| pl.os == p.os && pl.architecture == p.architecture)
        else {
            continue;
        };
        if platform.key == res.platform {
            out.push((
                platform.key.to_string(),
                LockEntry {
                    version: host_resolved.version.clone(),
                    build: host_resolved.build.clone(),
                    manifest: host_resolved.digests.manifest.clone(),
                    layer: host_resolved.digests.layer.clone(),
                    bundle: host_resolved.digests.bundle.clone().unwrap_or_default(),
                    index: None,
                },
            ));
            continue;
        }
        let manifest_fetched =
            oci::fetch_by_digest(&source, &d.digest, constants::MANIFEST_MAX_BYTES)?;
        let manifest: oci::Manifest = serde_json::from_slice(&manifest_fetched.bytes)?;
        oci::check_platform_manifest(&manifest, &format!("manifest {}", d.digest))?;
        let layer = &manifest.layers[0];
        let stmt = referrers::find_trusted_statement(&source, &d.digest, &res.trust)?;
        check_artifact_statement(&stmt, &layer.digest)?;
        out.push((
            platform.key.to_string(),
            LockEntry {
                version: stmt.predicate["clickhouse_version"]
                    .as_str()
                    .unwrap_or("")
                    .to_string(),
                build: stmt.predicate["build"].as_str().unwrap_or("").to_string(),
                manifest: d.digest.clone(),
                layer: layer.digest.clone(),
                bundle: stmt.bundle_layer_digest.clone(),
                index: None,
            },
        ));
    }
    Ok(out)
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
    let already = is_fully_installed(&layout::unpacked_dir(&res.root, &entry.manifest)?)?;
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
                bundle_manifest: record.bundle_manifest_digest.clone(),
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
    oci::check_platform_manifest(&manifest, &format!("manifest {}", entry.manifest))?;
    let layer = &manifest.layers[0];
    if layer.digest != entry.layer {
        return Err(Error::ArtifactCorrupt(format!(
            "frozen manifest's layer {} does not match the lock's {}",
            layer.digest, entry.layer
        )));
    }

    // `--frozen` performs NO discovery (docs/guides/fetch-v1.md §6): the
    // signature bundle is fetched by the digest the lock pins, never found
    // through the referrers API or the fallback tag.
    let bundle = oci::fetch_blob_by_digest(&source, &entry.bundle, constants::BUNDLE_MAX_BYTES)?;
    let verified = dsse::verify_bundle(&bundle.bytes, &res.trust, constants::STATEMENT_TYPE);
    let mut warnings: Vec<String> = Vec::new();
    let (predicate, signed_by, bundle_manifest_digest) = match verified {
        Ok(stmt) => {
            check_artifact_statement(&stmt, &layer.digest)?;
            validate_predicate(
                &stmt.predicate,
                platform,
                Some(&VersionRequest::parse(request)?),
            )?;
            (stmt.predicate, stmt.signed_by.clone(), None)
        }
        Err(Error::ArtifactUntrusted(detail)) if options.allow_unsigned => {
            warnings.push(format!(
                "chtypes: the locked bundle does not verify; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned: {detail}"
            ));
            (unsigned_config(&source, &manifest)?, String::new(), None)
        }
        Err(e) => return Err(e),
    };

    let fetched_layer =
        oci::fetch_blob_by_digest(&source, &layer.digest, constants::MAX_UNPACKED_BYTES)?;
    let decompressed = unpack::decompress_zstd(&fetched_layer.bytes)?;
    let (library_sha256, library_bytes, library_name) = library_fields(&predicate)?;
    let record = VerifiedRecord {
        platform: platform.to_string(),
        version: entry.version.clone(),
        build: entry.build.clone(),
        channel: predicate["channel"].as_str().map(str::to_string),
        index_digest: entry.index.clone(),
        manifest_digest: entry.manifest.clone(),
        layer_digest: entry.layer.clone(),
        bundle_digest: Some(entry.bundle.clone()),
        bundle_manifest_digest: bundle_manifest_digest.clone(),
        signed_by: signed_by.clone(),
        library: library_name.clone(),
        library_sha256: library_sha256.clone(),
        library_bytes,
        predicate: predicate.clone(),
    };
    let dir = layout::install_unpacked(&res.root, &entry.manifest, |tmp| {
        unpack::unpack_tar(&decompressed, tmp)?;
        verify_unpacked_library(tmp, &library_name, &library_sha256, library_bytes)?;
        layout::write_atomic(
            &tmp.join(constants::CACHE_VERIFIED_RECORD),
            &record.to_json_bytes()?,
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
            bundle_manifest: bundle_manifest_digest,
        },
        predicate,
        signed_by,
        source: fetched_manifest.base,
        already_installed: false,
        warnings,
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
            // Not yet unpacked: the lock still names an exact digest, so
            // install it from local blobs only (`offline-frozen`) — no
            // need for find_index_candidate's annotation search, the lock
            // already says exactly which manifest.
            let resolved = install_from_local_blobs(
                &res.root,
                &res.root,
                &entry.manifest,
                &res.platform,
                &res.trust,
            )?;
            return Ok(Resolved {
                request: request.to_string(),
                ..resolved
            });
        }
    }
    match find_installed(
        &res.root,
        &res.system_dirs,
        version_request,
        &res.platform,
        &res.trust,
    )? {
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
    match find_installed(
        &res.root,
        &res.system_dirs,
        &version_request,
        &res.platform,
        &res.trust,
    )? {
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
/// For `expected_predicate_type` = `PREDICATE_TYPE_GOLDENS`, `reference` is a
/// PLATFORM manifest digest and the result is its highest-`revision` verified
/// goldens referrer ([`super::goldens::fetch_goldens`]).
pub fn fetch_signed(
    repository_bases: &[String],
    reference: &str,
    expected_predicate_type: &str,
    options: &mut Options,
) -> Result<(Vec<u8>, serde_json::Value, Digests)> {
    let clock = options
        .clock
        .take()
        .unwrap_or_else(|| Box::new(super::http::RealClock));
    let client = Client::with_clock(clock);
    let token = options
        .token
        .clone()
        .or_else(|| std::env::var(constants::ENV_TOKEN_NAME).ok());
    let auth = AuthConfig {
        static_token: token,
    };
    let trust = dsse::trusted_keys(options.trusted_keys.as_deref())?;
    let source = Source {
        bases: repository_bases,
        client: &client,
        auth: &auth,
    };

    if expected_predicate_type == constants::PREDICATE_TYPE_GOLDENS {
        // Goldens are a referrer of a platform manifest (D7) and the registry
        // is append-only, so `reference` names the PLATFORM manifest and the
        // highest-revision verified goldens referrer is returned
        // (docs/guides/fetch-v1.md §9; goldens.rs).
        return super::goldens::fetch_goldens(&source, reference, &trust);
    }

    let fetched = if reference.starts_with("sha256:") {
        oci::fetch_by_digest(&source, reference, constants::MANIFEST_MAX_BYTES)?
    } else {
        oci::fetch_by_tag(&source, reference)?
    };
    let manifest: oci::Manifest = serde_json::from_slice(&fetched.bytes)?;
    if manifest.media_type.as_deref() != Some(constants::MEDIA_TYPE_MANIFEST) {
        return Err(Error::SourceIncompatible(format!(
            "{reference} has unrecognized mediaType {:?}",
            manifest.media_type
        )));
    }
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
            bundle: Some(stmt.bundle_layer_digest),
            bundle_manifest: Some(stmt.bundle_manifest_digest),
        },
    ))
}

fn strip_sha256(digest: &str) -> Result<String> {
    digest
        .strip_prefix("sha256:")
        .map(str::to_string)
        .ok_or_else(|| Error::ArtifactCorrupt(format!("digest {digest:?} is not sha256:<hex>")))
}

/// Whether `dir` holds a complete install: `verified.json` **and** the
/// library file it names, both present. A record surviving alone (the
/// library removed or corrupted out of band) must not short-circuit a
/// re-fetch, or `ensure()`'s "the library file exists on disk" guarantee
/// (plan §1.3) would not hold on the cache-hit path.
fn is_fully_installed(dir: &Path) -> Result<bool> {
    let Some(record) = layout::read_verified(dir)? else {
        return Ok(false);
    };
    Ok(dir.join(&record.library).is_file())
}

/// Pull `library`/`library_sha256`/`library_bytes` out of a verified,
/// non-null predicate. Each is required: a signed predicate missing one is
/// malformed, and silently skipping the hash/size check it exists for would
/// accept unverified library bytes — this fails loudly instead.
fn library_fields(predicate: &serde_json::Value) -> Result<(String, u64, String)> {
    let library_sha256 = predicate
        .get("library_sha256")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            Error::ArtifactCorrupt("signed predicate has no library_sha256".to_string())
        })?
        .to_string();
    let library_bytes = predicate
        .get("library_bytes")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| {
            Error::ArtifactCorrupt("signed predicate has no library_bytes".to_string())
        })?;
    let library_name = predicate
        .get("library")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| Error::ArtifactCorrupt("signed predicate has no library".to_string()))?
        .to_string();
    Ok((library_sha256, library_bytes, library_name))
}

/// Re-hash the just-unpacked library against the signed predicate's
/// `library_sha256`/`library_bytes`, inside the install's temp directory
/// (before the atomic rename that makes it visible as installed).
fn verify_unpacked_library(
    tmp: &Path,
    library_name: &str,
    library_sha256: &str,
    library_bytes: u64,
) -> Result<()> {
    let lib_path = tmp.join(library_name);
    let lib_bytes = std::fs::read(&lib_path)
        .map_err(|e| Error::ArtifactCorrupt(format!("library {}: {e}", lib_path.display())))?;
    oci::verify_digest(&lib_bytes, &format!("sha256:{library_sha256}"))?;
    if lib_bytes.len() as u64 != library_bytes {
        return Err(Error::ArtifactCorrupt(format!(
            "library is {} bytes, the signed predicate says {library_bytes}",
            lib_bytes.len()
        )));
    }
    Ok(())
}

/// `predicate.abi` must be the JSON integer `1` (never the v0 field
/// `abi_revision`), and `os`/`arch` must equal `platform`, and the version
/// must lie within `version_request` (plan §1.3's guarantees).
fn validate_predicate(
    predicate: &serde_json::Value,
    platform: &str,
    version_request: Option<&VersionRequest>,
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
    // A literal (non-numeric) spelling names a tag, not a version range:
    // nothing to compare the resolved version against.
    if let Some(req) = version_request {
        if !req.is_literal() && !req.matches(version) {
            return Err(Error::ArtifactCorrupt(format!(
                "predicate.clickhouse_version {version:?} does not lie within the request"
            )));
        }
    }
    Ok(())
}

/// Under `allow_unsigned`, with no signature to read the library's name and
/// hashes from: the manifest's own config blob, which carries the same
/// fields value-for-value (layout-v2 §4.1) but was never verified — which is
/// exactly why the caller reports it as unsigned and never as signed.
fn unsigned_config(source: &Source<'_>, manifest: &oci::Manifest) -> Result<serde_json::Value> {
    let config = manifest
        .config
        .as_ref()
        .ok_or_else(|| Error::ArtifactCorrupt("manifest has no config descriptor".to_string()))?;
    let fetched = oci::fetch_blob_by_digest(source, &config.digest, constants::MANIFEST_MAX_BYTES)?;
    let value: serde_json::Value = serde_json::from_slice(&fetched.bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("config blob is not valid JSON: {e}")))?;
    if !value.is_object() {
        return Err(Error::ArtifactCorrupt(
            "config blob is not a JSON object".to_string(),
        ));
    }
    Ok(value)
}

/// What a signed artifact statement must satisfy before anything in it is
/// believed: its `predicateType` is the artifact one, and its subject is the
/// manifest's own layer (the signature covers THESE bytes, not some other
/// artifact's).
fn check_artifact_statement(stmt: &dsse::VerifiedStatement, layer_digest: &str) -> Result<()> {
    if stmt.predicate_type != constants::PREDICATE_TYPE_ARTIFACT {
        return Err(Error::ArtifactCorrupt(format!(
            "signed statement predicateType is {:?}, want {:?}",
            stmt.predicate_type,
            constants::PREDICATE_TYPE_ARTIFACT
        )));
    }
    if stmt.subject_sha256 != strip_sha256(layer_digest)? {
        return Err(Error::ArtifactCorrupt(format!(
            "signed subject {} does not match the manifest's layer {layer_digest}",
            stmt.subject_sha256
        )));
    }
    Ok(())
}

/// The newest already-installed build (same platform, a different
/// manifest) that is strictly newer than the candidate — the same version
/// with a higher `build`, or a higher version still inside the request —
/// or `None`. The monotonic check: such an install is kept rather than
/// swapped for what the source now offers.
fn newer_installed(
    res: &Resources,
    version_request: &VersionRequest,
    candidate_manifest: &str,
    predicate: &serde_json::Value,
) -> Result<Option<(PathBuf, VerifiedRecord)>> {
    if version_request.is_literal() {
        return Ok(None);
    }
    let cand_version = predicate["clickhouse_version"].as_str().unwrap_or("");
    let cand_build = predicate["build"].as_str().unwrap_or("");
    let mut best: Option<(PathBuf, VerifiedRecord)> = None;
    for (dir, rec) in layout::list_verified(&res.root)? {
        if rec.platform != res.platform
            || rec.manifest_digest == candidate_manifest
            || !version_request.matches(&rec.version)
        {
            continue;
        }
        let newer = if rec.version == cand_version {
            build_key(&rec.build) > build_key(cand_build)
        } else {
            version_key(&rec.version) > version_key(cand_version)
        };
        if !newer {
            continue;
        }
        let better = match &best {
            None => true,
            Some((_, b)) => {
                (version_key(&rec.version), build_key(&rec.build))
                    > (version_key(&b.version), build_key(&b.build))
            }
        };
        if better {
            best = Some((dir, rec));
        }
    }
    Ok(best)
}

/// Builds compare as fixed-width strings (`"20261001.183455"`), per plan
/// §3.2's `offline-newest-build`.
fn build_key(build: &str) -> &str {
    build
}

/// A 4-component version's numeric sort key. ClickHouse version components
/// are not zero-padded, so `"26.10.1.5"` must sort after `"26.9.3.38"` even
/// though `'1' < '9'` as a byte — a plain string compare gets this backwards
/// the moment any component crosses a digit-width boundary (9 -> 10, 99 ->
/// 100), which is routine here. A version that fails to parse this way
/// sorts lowest, rather than panicking or being silently preferred.
fn version_key(version: &str) -> [u64; 4] {
    let mut out = [0u64; 4];
    let parts: Vec<&str> = version.split('.').collect();
    if parts.len() != 4 {
        return out;
    }
    for (i, p) in parts.iter().enumerate() {
        out[i] = p.parse().unwrap_or(0);
    }
    out
}

/// Scan the cache, then each system directory in order, for the record with
/// the newest (version, then build) matching `version_request` for
/// `platform`. When nothing is already verified, also try a **pre-seeded**
/// `index.json` entry (plan §1, docs/guides/fetch-v1.md §1: "a pre-seeded
/// layout has entries in `index.json` with no corresponding `unpacked/`
/// directory... the first request for one verifies it against its
/// signature exactly as a freshly downloaded layer would, then unpacks
/// it") — entirely offline, from local blobs only.
fn find_installed(
    root: &Path,
    system_dirs: &[PathBuf],
    version_request: &VersionRequest,
    platform: &str,
    trust: &[TrustedKey],
) -> Result<Option<(PathBuf, VerifiedRecord, String)>> {
    let mut best: Option<(PathBuf, VerifiedRecord, String)> = None;
    let mut consider = |dir: PathBuf, record: VerifiedRecord, source: String| {
        if record.platform != platform || !version_request.matches(&record.version) {
            return;
        }
        let better = match &best {
            None => true,
            Some((_, b, _)) => {
                (version_key(&record.version), build_key(&record.build))
                    > (version_key(&b.version), build_key(&b.build))
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
    if best.is_some() {
        return Ok(best);
    }

    // Nothing already unpacked: look for a pre-seeded index.json entry, in
    // the cache first, then each system directory, and install it from
    // local blobs only (no network, regardless of caller).
    if let Some((dir, record)) = install_preseeded(root, root, version_request, platform, trust)? {
        return Ok(Some((dir, record, "cache".to_string())));
    }
    for sysdir in system_dirs {
        // Read blobs from the (read-only) system directory; write the
        // unpacked result into the writable cache root.
        if let Some((dir, record)) =
            install_preseeded(sysdir, root, version_request, platform, trust)?
        {
            return Ok(Some((dir, record, format!("system:{}", sysdir.display()))));
        }
    }
    Ok(None)
}

/// Every `index.json` entry of `layout_root` that is for `platform`, verified
/// and unpacked from local blobs into `cache_root`, keeping those whose
/// SIGNED version lies within `version_request`; the newest wins. An
/// entry's own annotations are never believed: the version a request is
/// matched against is the one inside its verified predicate (a pre-seeded
/// layout written by `oras copy` annotates with the tag it was copied
/// from, e.g. `26.1`, not the build's version). An entry that fails to
/// verify, or whose blobs are absent, is skipped, not fatal.
fn install_preseeded(
    layout_root: &Path,
    cache_root: &Path,
    version_request: &VersionRequest,
    platform: &str,
    trust: &[TrustedKey],
) -> Result<Option<(PathBuf, VerifiedRecord)>> {
    let index_path = layout_root.join("index.json");
    let bytes = match std::fs::read(&index_path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let index: oci::Index = serde_json::from_slice(&bytes)?;
    let plat = constants::PLATFORMS
        .iter()
        .find(|p| p.key == platform)
        .ok_or_else(|| Error::InvalidInput(format!("unknown platform {platform:?}")))?;
    let mut best: Option<(PathBuf, VerifiedRecord)> = None;
    for d in &index.manifests {
        let Some(p) = &d.platform else { continue };
        if p.os != plat.os || p.architecture != plat.architecture {
            continue;
        }
        let Ok(resolved) = install_from_local_blobs_for(
            layout_root,
            cache_root,
            &d.digest,
            platform,
            trust,
            Some(version_request),
        ) else {
            continue;
        };
        let Some(record) = layout::read_verified(&resolved.dir)? else {
            continue;
        };
        let better = match &best {
            None => true,
            Some((_, b)) => {
                (version_key(&record.version), build_key(&record.build))
                    > (version_key(&b.version), build_key(&b.build))
            }
        };
        if better {
            best = Some((resolved.dir, record));
        }
    }
    Ok(best)
}

/// Verify and unpack one manifest **entirely from local blobs** — no
/// network — reading `manifest_digest`, its layer and its signature
/// referrer from `layout_root/blobs/sha256/<hex>`, and writing the result
/// under `cache_root/unpacked/sha256/<hex>` (which may be the same
/// directory as `layout_root`, or different when `layout_root` is a
/// read-only system directory). This is the "pre-seeded `index.json` entry"
/// path (plan §1) and also what the conformance runner itself uses to
/// honor a fixture's `installed.json` test-setup convention
/// (docs/guides/fetch-v1.md §10, "Cache fixtures and `installed.json`").
pub fn install_from_local_blobs(
    layout_root: &Path,
    cache_root: &Path,
    manifest_digest: &str,
    platform: &str,
    trust: &[TrustedKey],
) -> Result<Resolved> {
    install_from_local_blobs_for(
        layout_root,
        cache_root,
        manifest_digest,
        platform,
        trust,
        None,
    )
}

/// [`install_from_local_blobs`], additionally refusing (before anything is
/// unpacked) a signed predicate whose version does not lie within `request`.
fn install_from_local_blobs_for(
    layout_root: &Path,
    cache_root: &Path,
    manifest_digest: &str,
    platform: &str,
    trust: &[TrustedKey],
    request: Option<&VersionRequest>,
) -> Result<Resolved> {
    let already = is_fully_installed(&layout::unpacked_dir(cache_root, manifest_digest)?)?;
    let dir = if already {
        layout::unpacked_dir(cache_root, manifest_digest)?
    } else {
        let manifest_bytes = layout::read_blob(layout_root, manifest_digest)?.ok_or_else(|| {
            Error::ArtifactMissing(format!("no local blob for manifest {manifest_digest}"))
        })?;
        oci::verify_digest(&manifest_bytes, manifest_digest)?;
        let manifest: oci::Manifest = serde_json::from_slice(&manifest_bytes)?;
        oci::check_platform_manifest(&manifest, &format!("manifest {manifest_digest}"))?;
        let layer = &manifest.layers[0];
        let stmt = referrers::find_local_referrer(layout_root, manifest_digest, trust)?;
        check_artifact_statement(&stmt, &layer.digest)?;
        validate_predicate(&stmt.predicate, platform, request)?;
        let (library_sha256, library_bytes, library_name) = library_fields(&stmt.predicate)?;
        let layer_bytes = layout::read_blob(layout_root, &layer.digest)?.ok_or_else(|| {
            Error::ArtifactMissing(format!("no local blob for layer {}", layer.digest))
        })?;
        oci::verify_digest(&layer_bytes, &layer.digest)?;
        let decompressed = unpack::decompress_zstd(&layer_bytes)?;
        let record = VerifiedRecord {
            platform: platform.to_string(),
            version: stmt.predicate["clickhouse_version"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            build: stmt.predicate["build"].as_str().unwrap_or("").to_string(),
            channel: stmt.predicate["channel"].as_str().map(str::to_string),
            index_digest: None,
            manifest_digest: manifest_digest.to_string(),
            layer_digest: layer.digest.clone(),
            bundle_digest: Some(stmt.bundle_layer_digest.clone()),
            bundle_manifest_digest: Some(stmt.bundle_manifest_digest.clone()),
            signed_by: stmt.signed_by.clone(),
            library: library_name.clone(),
            library_sha256: library_sha256.clone(),
            library_bytes,
            predicate: stmt.predicate.clone(),
        };
        layout::install_unpacked(cache_root, manifest_digest, |tmp| {
            unpack::unpack_tar(&decompressed, tmp)?;
            verify_unpacked_library(tmp, &library_name, &library_sha256, library_bytes)?;
            layout::write_atomic(
                &tmp.join(constants::CACHE_VERIFIED_RECORD),
                &record.to_json_bytes()?,
            )
        })?
    };
    let record = layout::read_verified(&dir)?.ok_or_else(|| {
        Error::ArtifactCorrupt(format!(
            "{}: missing verified.json after a local-blob install",
            dir.display()
        ))
    })?;
    // The request is checked against the SIGNED version on EVERY path, the
    // already-unpacked one included: a build that is merely present in the
    // cache must never answer a request it does not satisfy.
    if let Some(request) = request {
        if record.platform != platform || !request.matches(&record.version) {
            return Err(Error::ArtifactMissing(format!(
                "installed {} ({}) does not satisfy the request",
                record.version, record.platform
            )));
        }
    }
    Ok(record_to_resolved(
        "", platform, &dir, record, "cache", already,
    ))
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
            index: record.index_digest,
            manifest: record.manifest_digest,
            layer: record.layer_digest,
            bundle: record.bundle_digest,
            bundle_manifest: record.bundle_manifest_digest,
        },
        predicate: record.predicate,
        signed_by: record.signed_by,
        source: source.to_string(),
        already_installed,
        warnings: Vec::new(),
    }
}

/// Public issue #486, the behavior fixes: one root order, read-only lookups
/// that create nothing, the 0.x hint, and modes that follow the umask. Every
/// fault is made by a real write, never by a stubbed reader.
#[cfg(test)]
mod cache_roots_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn scratch(name: &str) -> PathBuf {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!(
            "ocifetch-486-{name}-{}-{nanos:x}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ))
    }

    /// One record installed by hand under `root`, as any binding leaves it;
    /// returns the entry directory.
    fn write_record(root: &Path, version: &str, build: &str) -> PathBuf {
        let library = format!("library {version} {build}");
        let manifest = oci::sha256_hex(format!("{}{version}{build}", root.display()).as_bytes());
        let record = VerifiedRecord {
            platform: "linux-arm64".to_string(),
            version: version.to_string(),
            build: build.to_string(),
            channel: None,
            index_digest: None,
            manifest_digest: format!("sha256:{manifest}"),
            layer_digest: format!("sha256:{}", "c".repeat(64)),
            bundle_digest: None,
            bundle_manifest_digest: None,
            signed_by: String::new(),
            library: "lib.so".to_string(),
            library_sha256: oci::sha256_hex(library.as_bytes()),
            library_bytes: library.len() as u64,
            predicate: serde_json::json!({"clickhouse_version": version, "build": build}),
        };
        let entry = root.join(constants::CACHE_UNPACKED_DIR).join(&manifest);
        layout::write_atomic(&entry.join("lib.so"), library.as_bytes()).unwrap();
        layout::write_atomic(
            &entry.join(constants::CACHE_VERIFIED_RECORD),
            &record.to_json_bytes().unwrap(),
        )
        .unwrap();
        entry
    }

    fn options(cache: &Path, system_dirs: Vec<PathBuf>) -> Options {
        Options {
            platform: Some("linux-arm64".to_string()),
            cache_dir: Some(cache.to_string_lossy().into_owned()),
            system_dirs,
            ..Options::default()
        }
    }

    /// The cache and the system dirs are one search: the newest (version,
    /// build) wins, a tie goes to the earlier root, and `list_installed` and
    /// `verify_installed` read the same roots.
    #[test]
    fn resolve_list_and_verify_read_every_root_newest_first() {
        for (cache_vb, sys_vb, system_wins) in [
            (
                ("26.8.1.1", "20260801.000001"),
                ("26.8.2.1", "20260802.000001"),
                true,
            ),
            (
                ("26.8.1.1", "20260801.000001"),
                ("26.8.1.1", "20260801.000002"),
                true,
            ),
            (
                ("26.8.2.1", "20260802.000001"),
                ("26.8.1.1", "20260801.000001"),
                false,
            ),
            (
                ("26.8.1.1", "20260801.000001"),
                ("26.8.1.1", "20260801.000001"),
                false,
            ),
        ] {
            let base = scratch("roots");
            let (cache, sys) = (base.join("cache"), base.join("system"));
            let cache_entry = write_record(&cache, cache_vb.0, cache_vb.1);
            let sys_entry = write_record(&sys, sys_vb.0, sys_vb.1);
            let got = resolve_installed("26.8", "linux-arm64", options(&cache, vec![sys.clone()]))
                .unwrap()
                .expect("a record answers 26.8");
            let (want_dir, want_source) = if system_wins {
                (sys_entry.clone(), format!("system:{}", sys.display()))
            } else {
                (cache_entry.clone(), "cache".to_string())
            };
            assert_eq!((&got.dir, &got.source), (&want_dir, &want_source));
            let mut offline = options(&cache, vec![sys.clone()]);
            offline.offline = true;
            assert_eq!(ensure("26.8", offline).unwrap().dir, want_dir);
            let listed: Vec<PathBuf> = list_installed(options(&cache, vec![sys.clone()]))
                .unwrap()
                .into_iter()
                .map(|r| r.dir)
                .collect();
            assert_eq!(listed, vec![cache_entry.clone(), sys_entry.clone()]);
            let verified: Vec<(PathBuf, bool)> =
                verify_installed(options(&cache, vec![sys.clone()]))
                    .unwrap()
                    .into_iter()
                    .map(|r| (r.dir, r.ok))
                    .collect();
            assert_eq!(verified, vec![(cache_entry, true), (sys_entry, true)]);
            let _ = std::fs::remove_dir_all(&base);
        }
    }

    /// Every path under `root` with its size, or `None` when `root` is absent.
    fn tree_of(root: &Path) -> Option<Vec<(PathBuf, u64)>> {
        fn walk(dir: &Path, out: &mut Vec<(PathBuf, u64)>) {
            let mut entries: Vec<_> = std::fs::read_dir(dir)
                .unwrap()
                .map(|e| e.unwrap().path())
                .collect();
            entries.sort();
            for p in entries {
                let md = std::fs::symlink_metadata(&p).unwrap();
                out.push((p.clone(), md.len()));
                if md.is_dir() {
                    walk(&p, out);
                }
            }
        }
        std::fs::symlink_metadata(root).ok()?;
        let mut out = Vec::new();
        walk(root, &mut out);
        Some(out)
    }

    /// What a 0.x install left behind: `<minor>/manifest.json`, no `oci-layout`.
    fn zero_x_registry(root: &Path) {
        for (rel, body) in [
            ("26.1/manifest.json", "{}"),
            ("26.1/libchtypes.so", "0.x library"),
            ("patches/26.1.3.4/manifest.json", "{}"),
        ] {
            layout::write_atomic(&root.join(rel), body.as_bytes()).unwrap();
        }
    }

    /// `resolve_installed`, `list_installed`, `verify_installed` and an
    /// offline `ensure` create nothing, whether the cache is missing or holds
    /// a 0.x registry.
    #[test]
    fn read_only_lookups_create_nothing() {
        let base = scratch("readonly");
        let zero_x = base.join("zero-x");
        zero_x_registry(&zero_x);
        let no_system = base.join("no-such-system-dir");
        for cache in [base.join("no-such-cache"), zero_x] {
            let before = tree_of(&cache);
            let opts = || options(&cache, vec![no_system.clone()]);
            assert!(
                resolve_installed("26.1", "linux-arm64", opts())
                    .unwrap()
                    .is_none()
            );
            assert!(list_installed(opts()).unwrap().is_empty());
            assert!(verify_installed(opts()).unwrap().is_empty());
            let mut offline = opts();
            offline.offline = true;
            assert!(matches!(
                ensure("26.1", offline),
                Err(Error::ArtifactMissing(_))
            ));
            assert_eq!(tree_of(&cache), before, "{} changed", cache.display());
            assert!(tree_of(&no_system).is_none());
        }
        let _ = std::fs::remove_dir_all(&base);
    }

    /// Every directory and file an install creates has the mode a plain
    /// `create_dir` and `fs::write` get in the same place: 0777 and 0666 less
    /// the process umask, whatever it is (std cannot set the umask, so the
    /// controls stand in for it). The controls must not be the temp APIs'
    /// private 0700/0600, or the comparison could not tell the two apart.
    #[test]
    fn install_modes_follow_the_umask() {
        let base = scratch("modes");
        std::fs::create_dir_all(&base).unwrap();
        let control_dir = base.join("control-dir");
        std::fs::create_dir(&control_dir).unwrap();
        std::fs::write(base.join("control-file"), b"x").unwrap();
        let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
        let (want_dir, want_file) = (mode(&control_dir), mode(&base.join("control-file")));
        assert!(
            want_dir != 0o700 && want_file != 0o600,
            "the umask is 077, so this test cannot discriminate; run it at umask 022"
        );
        let root = base.join("cache");
        layout::ensure_layout(&root).unwrap();
        let digest = format!("sha256:{}", "a".repeat(64));
        layout::put_blob(&root, &digest, b"{}").unwrap();
        layout::record_in_index(
            &root,
            &digest,
            2,
            constants::MEDIA_TYPE_MANIFEST,
            None,
            None,
        )
        .unwrap();
        let entry = layout::install_unpacked(&root, &digest, |tmp| {
            unpack::unpack_tar(&tar_of("lib.so", b"library"), tmp)?;
            layout::write_atomic(&tmp.join(constants::CACHE_VERIFIED_RECORD), b"{}")
        })
        .unwrap();
        let mut seen = 0;
        for (p, _) in tree_of(&root).unwrap() {
            let md = std::fs::symlink_metadata(&p).unwrap();
            let want = if md.is_dir() { want_dir } else { want_file };
            assert_eq!(mode(&p), want, "{}", p.display());
            seen += 1;
        }
        assert!(seen >= 8 && entry.join("lib.so").exists(), "saw {seen}");
        let _ = std::fs::remove_dir_all(&base);
    }

    /// A one-file tar, as the layer carries the library.
    fn tar_of(name: &str, body: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        {
            let mut builder = tar::Builder::new(&mut out);
            let mut header = tar::Header::new_gnu();
            header.set_size(body.len() as u64);
            header.set_mode(0o600);
            header.set_entry_type(tar::EntryType::Regular);
            builder.append_data(&mut header, name, body).unwrap();
            builder.finish().unwrap();
        }
        out
    }
}
