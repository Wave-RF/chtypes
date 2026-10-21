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
            // A retired repository (a 410, §2) is permanent: never read as
            // "nothing here", and never a reason to try the next base or the
            // fallback tag.
            Err(e) if e.is_gone() => return Err(e),
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
            // A retired repository (a 410, §2) is permanent: never read as
            // "nothing here", and never a reason to try the next base or the
            // fallback tag.
            Err(e) if e.is_gone() => return Err(e),
            Ok(_) | Err(_) => continue,
        }
    }
    Ok(None)
}

/// Fetch one signature-bundle (or revocation-bundle) referrer descriptor's
/// bundle: GET its manifest, check it has exactly one layer of
/// `constants::MEDIA_TYPE_BUNDLE`, and GET that layer. Returns the layer's
/// bytes and its digest.
pub fn fetch_bundle(source: &Source<'_>, descriptor: &Descriptor) -> Result<(Vec<u8>, String)> {
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
    Ok((blob.bytes, layer.digest.clone()))
}

/// Fetch and verify one signature-bundle referrer descriptor: its bundle
/// ([`fetch_bundle`]), then the DSSE envelope. Returns `Err(ArtifactUntrusted)`
/// when the bundle is well-formed but no signature verifies (the caller may
/// then try another candidate); any other `Err` is structural and final.
pub fn verify_one(
    source: &Source<'_>,
    descriptor: &Descriptor,
    trust: &[TrustedKey],
) -> Result<VerifiedStatement> {
    let (bytes, layer_digest) = fetch_bundle(source, descriptor)?;
    let mut stmt = dsse::verify_bundle(&bytes, trust, constants::STATEMENT_TYPE)?;
    stmt.bundle_manifest_digest = descriptor.digest.clone();
    stmt.bundle_layer_digest = layer_digest;
    Ok(stmt)
}

/// One platform manifest's referrers listing, as the signature lookup reads
/// it (docs/guides/fetch-v1.md §4, "Build-level revocation"): the first answer
/// that names a signature referrer (the referrers API on the bases in order,
/// then the fallback tag), its signature and revocation referrers, each capped
/// at `constants::MAX_REFERRERS`, and, when no answer named a signature, the
/// last failure that kept a base from answering (`None` when every base
/// answered 404, or with a listing that names none).
pub struct Listing {
    /// Whether an answer named a signature referrer.
    pub found: bool,
    /// The signature referrers (`constants::MEDIA_TYPE_BUNDLE`).
    pub signatures: Vec<Descriptor>,
    /// The revocation referrers (`constants::REVOCATION_ARTIFACT_TYPE`).
    pub revocations: Vec<Descriptor>,
    /// Why a base did not answer, when none did.
    pub failure: Option<Error>,
}

/// Read `subject_digest`'s listing exactly as the signature lookup does. Its
/// error is only ever a 410's ([`Error::is_gone`]): every other failure is
/// recorded on the listing. The source's budget is the call's: the retry
/// table, or the installed-build check's single attempt.
pub fn read_listing(source: &Source<'_>, subject_digest: &str) -> Result<Listing> {
    let mut listing = Listing {
        found: false,
        signatures: Vec::new(),
        revocations: Vec::new(),
        failure: None,
    };
    let Some(hex) = subject_digest.strip_prefix("sha256:") else {
        return Err(Error::InvalidInput(format!(
            "subject digest {subject_digest:?} is not sha256:<hex>"
        )));
    };
    // The referrers API is not a manifests/<ref> GET, so it sends no Accept.
    let steps = [
        (format!("referrers/{subject_digest}"), Vec::new()),
        (
            format!("manifests/sha256-{hex}"),
            oci::manifest_accept_header(),
        ),
    ];
    for (suffix, accept) in &steps {
        let Some(index) = listing_index(source, suffix, accept, &mut listing.failure)? else {
            continue;
        };
        let signatures = filter(&index, constants::MEDIA_TYPE_BUNDLE);
        if signatures.is_empty() {
            // Not a listing of this manifest's signature: try the fallback tag.
            continue;
        }
        listing.found = true;
        listing.signatures = signatures;
        listing.revocations = filter(&index, constants::REVOCATION_ARTIFACT_TYPE);
        return Ok(listing);
    }
    Ok(listing)
}

/// The first base that serves a 200 with a parsable index, recording on
/// `failure` why a base did not answer.
fn listing_index(
    source: &Source<'_>,
    suffix: &str,
    accept: &[(String, String)],
    failure: &mut Option<Error>,
) -> Result<Option<Vec<Descriptor>>> {
    for base in source.bases {
        let Ok(url) = oci::wire_url(base, suffix) else {
            continue;
        };
        match oci::get_from_base(source, base, suffix, accept, constants::MANIFEST_MAX_BYTES) {
            Ok((200, bytes)) => match serde_json::from_slice::<Index>(&bytes) {
                Ok(index) => return Ok(Some(index.manifests)),
                Err(e) => {
                    *failure = Some(Error::SourceUnreachable(format!(
                        "{url} is not a referrers listing: {e}"
                    )));
                }
            },
            Ok((404, _)) => {}
            Ok((401, _)) => {
                *failure = Some(Error::SourceUnauthorized(format!("{url} returned 401")));
            }
            Ok((403, _)) => {
                *failure = Some(Error::SourceForbidden(format!("{url} returned 403")));
            }
            Ok((status, _)) => {
                *failure = Some(Error::SourceUnreachable(format!(
                    "{url} returned unexpected status {status}"
                )));
            }
            Err(e) if e.is_gone() => return Err(e),
            Err(e) => *failure = Some(e),
        }
    }
    Ok(None)
}

/// [`find_trusted_statement`] over a listing read once ([`read_listing`]),
/// which it returns beside the statement: an install, a lock and an update read
/// their revocation candidates from it at no extra listing request.
pub fn trusted_statement_with_listing(
    source: &Source<'_>,
    subject_digest: &str,
    trust: &[TrustedKey],
) -> Result<(VerifiedStatement, Listing)> {
    let listing = read_listing(source, subject_digest)?;
    let mut verified = None;
    for d in &listing.signatures {
        match verify_one(source, d, trust) {
            Ok(stmt) => {
                verified = Some(stmt);
                break;
            }
            Err(Error::ArtifactUntrusted(_)) => continue,
            Err(e) => return Err(e),
        }
    }
    match verified {
        Some(stmt) => Ok((stmt, listing)),
        None => Err(Error::ArtifactUntrusted(format!(
            "no referrer of {subject_digest} (checked the referrers API and the fallback tag) \
             carries a {} signature that verifies under a trusted key",
            constants::MEDIA_TYPE_BUNDLE
        ))),
    }
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

/// Find a trusted signature referrer of `subject_digest` **entirely from
/// local blobs** — no referrers API, no fallback tag, no network at all.
///
/// A plain OCI image layout's `index.json` lists only the artifact
/// manifest itself (docs/guides/fetch-v1.md §10, "Cache fixtures and
/// `installed.json`": "it never carries a pre-populated... record" — nor
/// does it list the referrer). The referrer manifest is still present as
/// an ordinary content-addressed blob, so it is found by scanning every
/// blob under `layout_root/blobs/sha256/`, parsing each as an OCI manifest,
/// and keeping the ones whose own `subject.digest` equals `subject_digest`
/// — exactly how a real OCI 1.1 client reconstructs referrers from a
/// layout with no referrers index.
pub fn find_local_referrer(
    layout_root: &std::path::Path,
    subject_digest: &str,
    trust: &[TrustedKey],
) -> Result<VerifiedStatement> {
    let blobs_dir = layout_root.join("blobs").join("sha256");
    let entries = std::fs::read_dir(&blobs_dir)
        .map_err(|e| Error::SourceUnreachable(format!("{}: {e}", blobs_dir.display())))?;

    let mut last_untrusted: Option<Error> = None;
    for entry in entries {
        let entry = entry?;
        let Ok(bytes) = std::fs::read(entry.path()) else {
            continue;
        };
        let Ok(manifest) = serde_json::from_slice::<oci::Manifest>(&bytes) else {
            continue;
        };
        let Some(subject) = &manifest.subject else {
            continue;
        };
        if subject.digest != subject_digest {
            continue;
        }
        if manifest.artifact_type.as_deref() != Some(constants::MEDIA_TYPE_BUNDLE) {
            continue;
        }
        let [layer] = manifest.layers.as_slice() else {
            continue;
        };
        if layer.media_type != constants::MEDIA_TYPE_BUNDLE {
            continue;
        }
        let referrer_digest = format!("sha256:{}", entry.file_name().to_string_lossy());
        let Some(blob) = super::layout::read_blob(layout_root, &layer.digest)? else {
            continue;
        };
        match dsse::verify_bundle(&blob, trust, constants::STATEMENT_TYPE) {
            Ok(mut stmt) => {
                stmt.bundle_manifest_digest = referrer_digest;
                stmt.bundle_layer_digest = layer.digest.clone();
                return Ok(stmt);
            }
            Err(Error::ArtifactUntrusted(e)) => last_untrusted = Some(Error::ArtifactUntrusted(e)),
            Err(e) => return Err(e),
        }
    }
    Err(last_untrusted.unwrap_or_else(|| {
        Error::ArtifactUntrusted(format!(
            "no local blob is a referrer of {subject_digest} with a {} signature that verifies under a trusted key",
            constants::MEDIA_TYPE_BUNDLE
        ))
    }))
}
