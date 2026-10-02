//! Referrer discovery (layout-v2 §4, §7.2; plan §1.1.3 "Trust").
//!
//! Two paths to the same descriptor list: the referrers API
//! (`GET /v2/<repo>/referrers/<digest>`, an OCI image index) and the
//! tag-schema fallback (`GET /v2/<repo>/manifests/sha256-<hex>`, which
//! mirrors without Referrers-API support serve instead, also as an image
//! index). Callers always filter the result by `artifactType` themselves —
//! never rely on server-side `?artifactType=` filtering, which not every
//! host applies.
//!
//! **A 200 with zero matching-`artifactType` entries is not, on its own,
//! evidence the referrer does not exist on every host that could be
//! serving it.** [`find_candidates`] always also tries the fallback tag in
//! that case — not only when the referrers API errors or is unsupported —
//! before the caller is allowed to conclude a referrer is genuinely absent
//! (e.g. `ArtifactUntrusted`): a mirror may answer the referrers API with an
//! incomplete or empty result (it is not guaranteed to proxy it faithfully)
//! while its fallback tag is complete. This is in addition to the existing
//! API/no-API split (a mirror with no Referrers API at all lands on the
//! fallback tag too).

use super::constants;
use super::dsse::{self, TrustedKey, VerifiedStatement};
use super::error::{Error, Result};
use super::oci::{self, Descriptor, Index, Source};

/// Every referrer descriptor whose `artifactType` is `want`, found via the
/// referrers API, the fallback tag, or both — per the staleness rule above.
pub fn find_candidates(
    source: &Source<'_>,
    subject_digest: &str,
    want_artifact_type: &str,
) -> Result<Vec<Descriptor>> {
    let api = try_referrers_api(source, subject_digest)?;
    let mut candidates = api
        .as_ref()
        .map(|list| filter(list, want_artifact_type))
        .unwrap_or_default();

    // The API answered (even with zero matches) or did not: either way, a
    // zero-candidate result is inconclusive (edge-cache staleness, or no
    // Referrers API support at all), so also check the fallback tag.
    if candidates.is_empty() {
        if let Some(tag_list) = try_fallback_tag(source, subject_digest)? {
            candidates = filter(&tag_list, want_artifact_type);
        }
    }
    Ok(candidates)
}

fn filter(list: &[Descriptor], want: &str) -> Vec<Descriptor> {
    list.iter()
        .filter(|d| d.artifact_type.as_deref() == Some(want))
        .take(constants::MAX_REFERRERS as usize)
        .cloned()
        .collect()
}

/// `GET /v2/<repo>/referrers/<digest>`. `Ok(None)` when every base refuses
/// or lacks the endpoint (a 404 — the production host always has it, but a
/// mirror may not, layout-v2 §7.10 A4); `Ok(Some(index.manifests))` on a
/// 200, even an empty one.
fn try_referrers_api(source: &Source<'_>, subject_digest: &str) -> Result<Option<Vec<Descriptor>>> {
    let suffix = format!("referrers/{subject_digest}");
    for base in source.bases {
        match oci::get_from_base(source, base, &suffix, &[], constants::MANIFEST_MAX_BYTES) {
            Ok((200, bytes)) => {
                let index: Index = serde_json::from_slice(&bytes)
                    .map_err(|e| Error::ArtifactCorrupt(format!("{base}: referrers index: {e}")))?;
                return Ok(Some(index.manifests));
            }
            Ok((404, _)) => continue,
            Ok(_) | Err(_) => continue,
        }
    }
    Ok(None)
}

/// The tag-schema fallback, `manifests/sha256-<hex>` (no colon): also an OCI
/// image index of referrer descriptors, for a host or mirror with no
/// Referrers API. `Ok(None)` if no base serves it either.
fn try_fallback_tag(source: &Source<'_>, subject_digest: &str) -> Result<Option<Vec<Descriptor>>> {
    let Some(hex) = subject_digest.strip_prefix("sha256:") else {
        return Err(Error::InvalidInput(format!(
            "subject digest {subject_digest:?} is not sha256:<hex>"
        )));
    };
    let suffix = format!("manifests/sha256-{hex}");
    let accept = oci::manifest_accept_header();
    for base in source.bases {
        match oci::get_from_base(
            source,
            base,
            &suffix,
            &accept,
            constants::MANIFEST_MAX_BYTES,
        ) {
            Ok((200, bytes)) => {
                let index: Index = serde_json::from_slice(&bytes).map_err(|e| {
                    Error::ArtifactCorrupt(format!("{base}: fallback-tag index: {e}"))
                })?;
                return Ok(Some(index.manifests));
            }
            Ok((404, _)) => continue,
            Ok(_) | Err(_) => continue,
        }
    }
    Ok(None)
}

/// Fetch and verify one signature-bundle referrer descriptor: GET its
/// manifest, check it has exactly one layer of `constants::MEDIA_TYPE_BUNDLE`,
/// GET that layer, and verify the DSSE envelope. Returns `Err(ArtifactUntrusted)`
/// when the bundle is well-formed but no signature verifies (the caller may
/// then try another candidate); any other `Err` is structural and final.
fn verify_one(
    source: &Source<'_>,
    descriptor: &Descriptor,
    trust: &[TrustedKey],
) -> Result<VerifiedStatement> {
    let manifest_fetched =
        oci::fetch_by_digest(source, &descriptor.digest, constants::MANIFEST_MAX_BYTES)?;
    let manifest: oci::Manifest = serde_json::from_slice(&manifest_fetched.bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("referrer manifest: {e}")))?;
    let [layer] = manifest.layers.as_slice() else {
        return Err(Error::ArtifactCorrupt(format!(
            "signature referrer {} has {} layers, want exactly 1",
            descriptor.digest,
            manifest.layers.len()
        )));
    };
    if layer.media_type != constants::MEDIA_TYPE_BUNDLE {
        return Err(Error::ArtifactCorrupt(format!(
            "signature referrer {} layer mediaType {:?}, want {:?}",
            descriptor.digest,
            layer.media_type,
            constants::MEDIA_TYPE_BUNDLE
        )));
    }
    let blob = oci::fetch_blob_by_digest(source, &layer.digest, constants::BUNDLE_MAX_BYTES)?;
    let mut stmt = dsse::verify_bundle(&blob.bytes, trust, constants::STATEMENT_TYPE)?;
    stmt.bundle_manifest_digest = descriptor.digest.clone();
    stmt.bundle_layer_digest = layer.digest.clone();
    Ok(stmt)
}

/// Find a referrer of `subject_digest` whose bundle verifies under a
/// trusted key, trying every candidate (a key rotation adds a second
/// bundle, never a second signature in one envelope) before giving up.
/// A structurally bad candidate (not `ArtifactUntrusted`) is reported
/// immediately rather than masked by trying another one.
pub fn find_trusted_statement(
    source: &Source<'_>,
    subject_digest: &str,
    trust: &[TrustedKey],
) -> Result<VerifiedStatement> {
    let candidates = find_candidates(source, subject_digest, constants::MEDIA_TYPE_BUNDLE)?;
    for d in &candidates {
        match verify_one(source, d, trust) {
            Ok(stmt) => return Ok(stmt),
            Err(Error::ArtifactUntrusted(_)) => continue,
            Err(e) => return Err(e),
        }
    }
    Err(Error::ArtifactUntrusted(format!(
        "no referrer of {subject_digest} (checked the referrers API and the fallback tag) \
         carries a {} signature that verifies under a trusted key",
        constants::MEDIA_TYPE_BUNDLE
    )))
}

/// Find the single referrer of `subject_digest` whose `artifactType` is
/// `content_artifact_type` (e.g. the goldens referrer, layout-v2 D7) —
/// unsigned on its own; its *own* digest becomes the `ref` for
/// [`crate::ocifetch::ensure::fetch_signed`] to verify and fetch.
pub fn find_content_referrer(
    source: &Source<'_>,
    subject_digest: &str,
    content_artifact_type: &str,
) -> Result<Descriptor> {
    let candidates = find_candidates(source, subject_digest, content_artifact_type)?;
    match candidates.len() {
        0 => Err(Error::ArtifactMissing(format!(
            "no referrer of {subject_digest} has artifactType {content_artifact_type}"
        ))),
        1 => Ok(candidates.into_iter().next().unwrap()),
        _ => Err(Error::ArtifactCorrupt(format!(
            "{subject_digest} has {} referrers of artifactType {content_artifact_type}, want 1",
            candidates.len()
        ))),
    }
}
