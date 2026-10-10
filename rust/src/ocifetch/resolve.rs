//! `chtypes resolve` (docs/guides/fetch-v1.md §3, "What a request resolves
//! to"; public issue #493): what a line, a patch or an exact version resolves
//! to on every platform the registry offers, each verified exactly as a fetch
//! verifies it (§4), and never a layer downloaded. Under offline it is the
//! cache's answer instead, the lookup an open makes.

use super::channel;
use super::constants;
use super::ensure::{
    Options, cached_own_build, check_artifact_statement, find_installed, notes_for, probe,
    resources, unsigned_config, validate_predicate, with_notes,
};
use super::error::{Error, Result};
use super::oci::{self, Source, VersionRequest};
use super::referrers;

/// What a request resolves to on one platform.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolution {
    /// The platform key (`linux-arm64`, …).
    pub platform: String,
    /// The signed `clickhouse_version`.
    pub version: String,
    /// The signed build id.
    pub build: String,
    /// The platform manifest's digest, `sha256:<hex>`.
    pub manifest: String,
}

/// What `spelling` resolves to, one [`Resolution`] per platform, in the
/// constants' platform order, with allow-unsigned's warnings.
///
/// Online it resolves the index (through the dev channel's alias first, §3, as
/// [`super::ensure::ensure`] does), and for each platform the index offers it
/// fetches the manifest by digest and verifies its signed statement against
/// the request (§4); no layer is requested and nothing is written. A platform
/// the index does not offer is left out, and an index that offers none is
/// `CHTYPES_ARTIFACT_UNPUBLISHED`. Offline (the option or
/// `CHTYPES_OFFLINE=1`) it answers from the cache: each platform's installed
/// lookup, and `CHTYPES_ARTIFACT_MISSING` when no platform has an installed
/// build.
pub fn resolve(spelling: &str, mut options: Options) -> Result<(Vec<Resolution>, Vec<String>)> {
    // The spelling is refused first, as a fetch refuses it.
    let version_request = VersionRequest::parse(spelling)?;
    // Every platform is answered, so the host's own is never needed; the
    // shared setup still wants one known key.
    if options.platform.is_none() {
        options.platform = constants::PLATFORMS.first().map(|p| p.key.to_string());
    }
    let res = resources(&mut options)?;

    if channel::offline_mode(options.offline) {
        probe(&res)?;
        let mut out = Vec::new();
        for platform in constants::PLATFORMS {
            if let Some((_, record, _)) = find_installed(
                &res.root,
                &res.system_dirs,
                &version_request,
                platform.key,
                &res.trust,
            )? {
                out.push(Resolution {
                    platform: platform.key.to_string(),
                    version: record.version,
                    build: record.build,
                    manifest: record.manifest_digest,
                });
            }
        }
        if out.is_empty() {
            return Err(Error::ArtifactMissing(with_notes(
                &format!(
                    "no installed artifact for {spelling} on any platform, and --offline forbids a network fetch"
                ),
                &notes_for(&res),
            )));
        }
        return Ok((out, Vec::new()));
    }

    // The option or `CHTYPES_ALLOW_UNSIGNED=1`, where the contract honors
    // them; never under the dev channel.
    let allow_unsigned = channel::allow_unsigned(options.allow_unsigned);
    let source = Source {
        bases: &res.bases,
        client: &res.client,
        auth: &res.auth,
    };
    let (fetched_index, alias_absent) = oci::fetch_by_request(&source, &version_request)?;
    let index = oci::parse_index(&fetched_index.bytes, &format!("index for {spelling:?}"))?;
    let mut out = Vec::new();
    let mut warnings = Vec::new();
    for platform in constants::PLATFORMS {
        let descriptor = match oci::select_platform(&index, platform.key) {
            Ok(descriptor) => descriptor,
            // The index does not offer this platform.
            Err(Error::ArtifactUnpublished(_)) => continue,
            Err(e) => return Err(e),
        };
        let fetched_manifest =
            oci::fetch_by_digest(&source, &descriptor.digest, constants::MANIFEST_MAX_BYTES)?;
        let manifest_digest = format!("sha256:{}", oci::sha256_hex(&fetched_manifest.bytes));
        let manifest: oci::Manifest = serde_json::from_slice(&fetched_manifest.bytes)?;
        oci::check_platform_manifest(&manifest, &format!("manifest {manifest_digest}"))?;
        let layer = &manifest.layers[0];
        let predicate = match referrers::find_trusted_statement(
            &source,
            &manifest_digest,
            &res.trust,
        ) {
            Ok(stmt) => {
                check_artifact_statement(&stmt, &layer.digest)?;
                validate_predicate(&stmt.predicate, platform.key, Some(&version_request))?;
                // An SDK ahead of the registry (public issue #578), from the
                // signed statement, exactly as a fetch refuses it.
                channel::ahead_of_registry(alias_absent, &stmt.predicate, || {
                    cached_own_build(&res.root, &res.system_dirs, &version_request, platform.key)
                })?;
                stmt.predicate
            }
            // Allow-unsigned: no statement verified, so the version and
            // build are the config blob's, as an unsigned install records
            // them.
            Err(Error::ArtifactUntrusted(detail)) if allow_unsigned => {
                warnings.push(format!(
                    "chtypes: no verified signature found; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned: {detail}"
                ));
                unsigned_config(&source, &manifest)?
            }
            Err(e) => return Err(e),
        };
        out.push(Resolution {
            platform: platform.key.to_string(),
            version: predicate["clickhouse_version"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            build: predicate["build"].as_str().unwrap_or("").to_string(),
            manifest: manifest_digest,
        });
    }
    if out.is_empty() {
        return Err(Error::ArtifactUnpublished(format!(
            "the index for {spelling} offers no platform this SDK knows"
        )));
    }
    Ok((out, warnings))
}
