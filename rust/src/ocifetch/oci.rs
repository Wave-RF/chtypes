//! OCI distribution: wire URLs, the image index/manifest shapes, platform
//! selection and the version-spelling rules (layout-v2 spec §5.1, §7.1;
//! plan §1.1.2 "Resolve").

use std::collections::HashMap;

use serde::Deserialize;

use super::constants;
use super::error::{Error, Result};
use super::http::{AuthConfig, Client};
use super::url::Url;

/// One descriptor, as both the index's `manifests[]` and the referrers API's
/// response use it.
#[derive(Deserialize, Clone, Debug)]
pub struct Descriptor {
    #[serde(rename = "mediaType")]
    pub media_type: String,
    pub digest: String,
    pub size: u64,
    #[serde(rename = "artifactType")]
    pub artifact_type: Option<String>,
    pub platform: Option<PlatformDescriptor>,
    pub annotations: Option<HashMap<String, String>>,
}

#[derive(Deserialize, Clone, Debug)]
pub struct PlatformDescriptor {
    pub architecture: String,
    pub os: String,
}

/// An OCI image index (`application/vnd.oci.image.index.v1+json`): the
/// result of a manifest-by-tag lookup, one entry per platform.
#[derive(Deserialize, Clone, Debug)]
pub struct Index {
    #[serde(rename = "schemaVersion")]
    pub schema_version: u32,
    #[serde(rename = "mediaType")]
    pub media_type: Option<String>,
    pub manifests: Vec<Descriptor>,
}

/// An OCI image manifest (`application/vnd.oci.image.manifest.v1+json`):
/// one platform's config, layer and (when present) its `subject`.
#[derive(Deserialize, Clone, Debug)]
pub struct Manifest {
    #[serde(rename = "schemaVersion")]
    pub schema_version: u32,
    #[serde(rename = "mediaType")]
    pub media_type: Option<String>,
    #[serde(rename = "artifactType")]
    pub artifact_type: Option<String>,
    pub config: Descriptor,
    pub layers: Vec<Descriptor>,
    pub subject: Option<Descriptor>,
    pub annotations: Option<HashMap<String, String>>,
}

/// The source this fetch runs against: an ordered base list plus the client
/// and auth used for `http`/`https` bases. `file://` bases are read directly
/// off disk and never touch `client`.
pub struct Source<'a> {
    pub bases: &'a [String],
    pub client: &'a Client,
    pub auth: &'a AuthConfig,
}

/// Build the real wire URL for `suffix` (e.g. `"manifests/26.8"`,
/// `"blobs/sha256:abcd…"`) against one `base`.
///
/// For `http`/`https`, a base names the host and repository
/// (`https://registry.wavehouse.dev/chtypes/v1`); the OCI distribution
/// spec's literal `/v2/` segment is inserted right after the authority, so
/// the real request is `https://registry.wavehouse.dev/v2/chtypes/v1/<suffix>`.
/// For `file://`, the base already names the full route-tree directory
/// (`.../trees/basic/v2/chtypes/v1`, fixtures §3.2), so `suffix` is appended
/// directly with no insertion.
pub fn wire_url(base: &str, suffix: &str) -> Result<String> {
    let base = base.trim_end_matches('/');
    let suffix = suffix.trim_start_matches('/');
    if let Some(path) = base.strip_prefix("file://") {
        return Ok(format!("file://{path}/{suffix}"));
    }
    let u =
        Url::parse(base).map_err(|e| Error::SourceIncompatible(format!("base {base:?}: {e}")))?;
    if !constants::ALLOWED_SCHEMES.contains(&u.scheme.as_str()) {
        return Err(Error::SourceIncompatible(format!(
            "base {base:?} uses scheme {:?}, not one of {:?}",
            u.scheme,
            constants::ALLOWED_SCHEMES
        )));
    }
    let mut full = u.clone();
    full.path = format!("/v2{}/{}", u.path, suffix);
    Ok(full.to_string())
}

/// `GET` bytes for `suffix` against one base: `file://` reads the
/// filesystem, `http(s)://` uses `client`. Returns `(status, body)`; a
/// `file://` read that hits "not found" is reported as status 404 so
/// callers share one branch with the HTTP path.
pub fn get_from_base(
    source: &Source<'_>,
    base: &str,
    suffix: &str,
    extra_headers: &[(String, String)],
    max_bytes: u64,
) -> Result<(u16, Vec<u8>)> {
    let url = wire_url(base, suffix)?;
    if let Some(path) = url.strip_prefix("file://") {
        return Ok(match std::fs::read(path) {
            Ok(bytes) => {
                if bytes.len() as u64 > max_bytes {
                    return Err(Error::ArtifactCorrupt(format!(
                        "{path} is {} bytes, over the {max_bytes}-byte cap",
                        bytes.len()
                    )));
                }
                (200, bytes)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (404, Vec::new()),
            Err(e) => return Err(Error::SourceUnreachable(format!("{path}: {e}"))),
        });
    }
    let resp = source
        .client
        .get(&url, extra_headers, source.auth, max_bytes)?;
    Ok((resp.status, resp.body))
}

/// Where a lookup came from: which base satisfied it, used only for
/// `Resolved.source` and diagnostics.
pub struct Fetched {
    pub bytes: Vec<u8>,
    pub base: String,
}

/// Fetch a manifest or index **by tag**, across the base list: a 404 moves
/// to the next base, and `ArtifactUnpublished` once every base 404s
/// (layout-v2 §7.5, A5).
pub fn fetch_by_tag(source: &Source<'_>, tag: &str) -> Result<Fetched> {
    let suffix = format!("manifests/{tag}");
    let accept = manifest_accept_header();
    let mut last_err: Option<Error> = None;
    for base in source.bases {
        match get_from_base(
            source,
            base,
            &suffix,
            &accept,
            constants::MANIFEST_MAX_BYTES,
        ) {
            Ok((200, bytes)) => {
                return Ok(Fetched {
                    bytes,
                    base: base.clone(),
                });
            }
            Ok((404, _)) => continue,
            Ok((status, _)) => {
                last_err = Some(Error::SourceUnreachable(format!(
                    "{base}: unexpected status {status} resolving tag {tag:?}"
                )));
            }
            Err(e) => last_err = Some(e),
        }
    }
    match last_err {
        Some(e) => Err(e),
        None => Err(Error::ArtifactUnpublished(format!(
            "no base publishes tag {tag:?}"
        ))),
    }
}

/// Fetch a manifest **by digest**, across the base list: a 404 moves to the
/// next base; on the last base, it is retried within the standard budget
/// before `SourceUnreachable` ("a listed digest that 404s is a host fault",
/// layout-v2 §7.5).
pub fn fetch_by_digest(source: &Source<'_>, digest: &str, max_bytes: u64) -> Result<Fetched> {
    fetch_bytes_by_digest(source, &format!("manifests/{digest}"), digest, max_bytes)
}

/// As [`fetch_by_digest`], for a blob rather than a manifest.
pub fn fetch_blob_by_digest(source: &Source<'_>, digest: &str, max_bytes: u64) -> Result<Fetched> {
    fetch_bytes_by_digest(source, &format!("blobs/{digest}"), digest, max_bytes)
}

fn fetch_bytes_by_digest(
    source: &Source<'_>,
    suffix: &str,
    digest: &str,
    max_bytes: u64,
) -> Result<Fetched> {
    let accept = manifest_accept_header();
    for (i, base) in source.bases.iter().enumerate() {
        let is_last = i + 1 == source.bases.len();
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            match get_from_base(source, base, suffix, &accept, max_bytes) {
                Ok((200, bytes)) => {
                    verify_digest(&bytes, digest)?;
                    return Ok(Fetched {
                        bytes,
                        base: base.clone(),
                    });
                }
                Ok((404, _)) => {
                    if !is_last || attempt >= constants::RETRY_ATTEMPTS {
                        break;
                    }
                    source.client.clock().sleep(
                        constants::RETRY_FIRST_WAIT_S
                            * constants::RETRY_MULTIPLIER.powi((attempt - 1) as i32),
                    );
                    continue;
                }
                Ok((status, _)) => {
                    if is_last {
                        return Err(Error::SourceUnreachable(format!(
                            "{base}: unexpected status {status} fetching {digest}"
                        )));
                    }
                    break;
                }
                Err(e) => {
                    if is_last {
                        return Err(e);
                    }
                    break;
                }
            }
        }
    }
    Err(Error::SourceUnreachable(format!(
        "{digest} 404s on every base — a listed digest that 404s is a host fault"
    )))
}

/// Every `GET …/manifests/<ref>` — by tag or by digest, the fallback tag
/// included — sends this (delivery-side review, 2026-10-02): our own host
/// ignores it, but a mirror may need it to answer with the right shape.
pub fn manifest_accept_header() -> Vec<(String, String)> {
    vec![(
        "Accept".to_string(),
        format!(
            "{}, {}",
            constants::MEDIA_TYPE_INDEX,
            constants::MEDIA_TYPE_MANIFEST
        ),
    )]
}

/// The lowercase-hex sha256 of `bytes` (no `sha256:` prefix).
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest as _, Sha256};
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(64);
    for b in digest.as_slice() {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// Verify `bytes` hashes to `digest` (`"sha256:<hex>"`). Size is implicitly
/// covered: a size mismatch cannot produce the same hash.
pub fn verify_digest(bytes: &[u8], digest: &str) -> Result<()> {
    let Some(hex) = digest.strip_prefix("sha256:") else {
        return Err(Error::ArtifactCorrupt(format!(
            "digest {digest:?} is not sha256:<hex>"
        )));
    };
    let got = sha256_hex(bytes);
    if !got.eq_ignore_ascii_case(hex) {
        return Err(Error::ArtifactCorrupt(format!(
            "digest mismatch: got sha256:{got}, want {digest}"
        )));
    }
    Ok(())
}

/// Pick the descriptor matching `platform` out of an index's `manifests[]`.
/// `ArtifactCorrupt` on more than one match (a duplicate platform),
/// `ArtifactUnpublished` naming what the index does offer when there is none.
pub fn select_platform<'a>(index: &'a Index, platform_key: &str) -> Result<&'a Descriptor> {
    let platform = constants::PLATFORMS
        .iter()
        .find(|p| p.key == platform_key)
        .ok_or_else(|| Error::InvalidInput(format!("unknown platform {platform_key:?}")))?;
    let mut matches = index.manifests.iter().filter(|d| {
        d.platform
            .as_ref()
            .is_some_and(|p| p.os == platform.os && p.architecture == platform.architecture)
    });
    let first = matches.next();
    if matches.next().is_some() {
        return Err(Error::ArtifactCorrupt(format!(
            "index lists more than one manifest for platform {platform_key}"
        )));
    }
    first.ok_or_else(|| {
        let offered: Vec<String> = index
            .manifests
            .iter()
            .filter_map(|d| {
                d.platform
                    .as_ref()
                    .map(|p| format!("{}-{}", p.os, p.architecture))
            })
            .collect();
        Error::ArtifactUnpublished(format!(
            "no manifest for platform {platform_key} (index offers: {})",
            offered.join(", ")
        ))
    })
}

/// A parsed version request: 2, 3 or 4 numeric components, refusing anything
/// the spelling regex excludes (a `v` prefix, an `-lts`/`-stable` suffix).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VersionRequest {
    pub components: Vec<u64>,
}

impl VersionRequest {
    pub fn parse(spelling: &str) -> Result<VersionRequest> {
        let re_hint = ["v", "-lts", "-stable"];
        for hint in re_hint {
            if spelling.starts_with('v') && hint == "v" {
                return Err(Error::InvalidInput(format!(
                    "version {spelling:?} is refused: this SDK spells versions without a leading 'v' (try {:?})",
                    spelling.trim_start_matches('v')
                )));
            }
            if (hint == "-lts" || hint == "-stable") && spelling.ends_with(hint) {
                return Err(Error::InvalidInput(format!(
                    "version {spelling:?} is refused: the channel is not part of the version spelling (try {:?})",
                    spelling.trim_end_matches(hint)
                )));
            }
        }
        let components: std::result::Result<Vec<u64>, _> =
            spelling.split('.').map(str::parse::<u64>).collect();
        let components = components
            .map_err(|_| Error::InvalidInput(format!("version {spelling:?} is not numeric")))?;
        if !(2..=4).contains(&components.len()) {
            return Err(Error::InvalidInput(format!(
                "version {spelling:?} must have 2-4 numeric components"
            )));
        }
        Ok(VersionRequest { components })
    }

    /// Exactly four components: the request names one build, not a line.
    pub fn is_exact(&self) -> bool {
        self.components.len() == 4
    }

    /// Whether `version` (e.g. `"26.8.15.10"`) lies within this request: a
    /// component-wise prefix for a floating request, equality for an exact
    /// one (layout-v2 §7.2).
    pub fn matches(&self, version: &str) -> bool {
        let parts: Vec<&str> = version.split('.').collect();
        if parts.len() != 4 {
            return false;
        }
        for (i, want) in self.components.iter().enumerate() {
            let Some(got) = parts.get(i).and_then(|p| p.parse::<u64>().ok()) else {
                return false;
            };
            if got != *want {
                return false;
            }
        }
        true
    }

    pub fn tag(&self) -> String {
        self.components
            .iter()
            .map(u64::to_string)
            .collect::<Vec<_>>()
            .join(".")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_url_inserts_v2_for_http() {
        let u = wire_url(
            "https://registry.wavehouse.dev/chtypes/v1",
            "manifests/26.8",
        )
        .unwrap();
        assert_eq!(
            u,
            "https://registry.wavehouse.dev/v2/chtypes/v1/manifests/26.8"
        );
    }

    #[test]
    fn wire_url_appends_directly_for_file() {
        let u = wire_url("file:///tmp/trees/basic/v2/chtypes/v1", "manifests/26.8").unwrap();
        assert_eq!(u, "file:///tmp/trees/basic/v2/chtypes/v1/manifests/26.8");
    }

    #[test]
    fn version_request_floating_matches_prefix() {
        let r = VersionRequest::parse("26.8").unwrap();
        assert!(r.matches("26.8.15.10"));
        assert!(!r.matches("26.7.15.10"));
        assert!(!r.is_exact());
    }

    #[test]
    fn version_request_exact_requires_equality() {
        let r = VersionRequest::parse("26.8.15.10").unwrap();
        assert!(r.matches("26.8.15.10"));
        assert!(!r.matches("26.8.15.11"));
        assert!(r.is_exact());
    }

    #[test]
    fn version_request_refuses_v_prefix_and_channel_suffix() {
        assert!(VersionRequest::parse("v26.8").is_err());
        assert!(VersionRequest::parse("26.8.15.10-lts").is_err());
    }
}
