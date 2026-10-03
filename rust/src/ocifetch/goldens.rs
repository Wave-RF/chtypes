//! Goldens revision selection (docs/guides/fetch-v1.md §9).
//!
//! The registry is append-only, so a corrected goldens set for the same build
//! is published as a SECOND goldens referrer of the same platform manifest;
//! the first can never be deleted. The artifact producer therefore puts an
//! integer `revision` in the goldens predicate and never reuses one for a
//! different document. [`fetch_goldens`] (reached through
//! [`crate::ocifetch::ensure::fetch_signed`] with the goldens predicate type
//! and a platform manifest digest) verifies every goldens referrer and
//! returns the one with the highest revision; [`predicate_revision`] and
//! [`select`] are the pure halves, unit-tested with no registry.

use super::constants;
use super::dsse::{TrustedKey, VerifiedStatement};
use super::ensure::Digests;
use super::error::{Error, Result};
use super::oci::{self, Source};
use super::referrers;

/// The largest revision every binding accepts: the largest integer a
/// JavaScript number holds exactly (2^53 - 1).
pub const MAX_GOLDENS_REVISION: u64 = (1 << 53) - 1;

/// One goldens referrer whose own signature verified.
#[derive(Debug, Clone)]
pub struct Candidate {
    /// The goldens referrer manifest's digest.
    pub manifest: String,
    /// Its layer digest: the signed goldens document.
    pub blob: String,
    pub revision: u64,
    pub bytes: Vec<u8>,
    pub predicate: serde_json::Value,
    pub bundle_layer: String,
    pub bundle_manifest: String,
}

/// The integer `revision` of a verified predicate. It must be a JSON integer
/// written without a fraction, an exponent, a sign or a leading zero: a
/// missing key, a boolean, a string, null, a float such as 1.0 and anything
/// above [`MAX_GOLDENS_REVISION`] are `ArtifactCorrupt`.
pub fn predicate_revision(predicate: &serde_json::Value) -> Result<u64> {
    let Some(value) = predicate.get("revision") else {
        return Err(Error::ArtifactCorrupt(
            "a goldens predicate carries no \"revision\"".to_string(),
        ));
    };
    let serde_json::Value::Number(n) = value else {
        return Err(Error::ArtifactCorrupt(format!(
            "a goldens predicate \"revision\" is {value}, not a non-negative JSON integer"
        )));
    };
    let text = n.to_string();
    let canonical = !text.is_empty()
        && text.bytes().all(|b| b.is_ascii_digit())
        && (text == "0" || !text.starts_with('0'));
    if !canonical {
        return Err(Error::ArtifactCorrupt(format!(
            "a goldens predicate \"revision\" is {text}, not a non-negative JSON integer"
        )));
    }
    match text.parse::<u64>() {
        Ok(v) if v <= MAX_GOLDENS_REVISION => Ok(v),
        _ => Err(Error::ArtifactCorrupt(format!(
            "a goldens predicate \"revision\" {text} is larger than {MAX_GOLDENS_REVISION}"
        ))),
    }
}

/// The verified candidate with the highest revision. Two or more at that
/// revision with DIFFERENT blob digests are a tie, refused as
/// `ArtifactCorrupt` naming both digests and the revision; the same blob
/// digest under the same revision (two bundles, say) is one document, not a
/// tie. The earliest-listed candidate of the winning document is returned.
pub fn select(candidates: Vec<Candidate>) -> Result<Candidate> {
    let mut best_index = 0;
    for (i, c) in candidates.iter().enumerate() {
        if c.revision > candidates[best_index].revision {
            best_index = i;
        }
    }
    let best = &candidates[best_index];
    for c in &candidates {
        if c.revision == best.revision && c.blob != best.blob {
            return Err(Error::ArtifactCorrupt(format!(
                "goldens revision {} is carried by two different documents, {} and {}",
                best.revision, best.blob, c.blob
            )));
        }
    }
    Ok(candidates
        .into_iter()
        .nth(best_index)
        .expect("index in range"))
}

/// `fetch_signed` for the goldens predicate type: the highest-revision
/// verified goldens referrer of the platform manifest `subject`.
///
/// Every candidate is verified on its own (its blob against its descriptor,
/// its own signature referrer, the statement's subject against the blob
/// digest, the predicateType). A candidate that fails is skipped: never
/// chosen, never a tie. If none verifies, the first failure is returned. A
/// VERIFIED candidate whose `revision` is unusable is not skipped: it could
/// be the newest document, and quietly choosing an older set is the stale
/// pick this rule exists to prevent.
pub fn fetch_goldens(
    source: &Source<'_>,
    subject: &str,
    trust: &[TrustedKey],
) -> Result<(Vec<u8>, serde_json::Value, Digests)> {
    if !subject.starts_with("sha256:") {
        return Err(Error::ArtifactCorrupt(format!(
            "a goldens fetch names a platform manifest by digest, not {subject:?}"
        )));
    }
    let listed = referrers::find_candidates(source, subject, constants::GOLDENS_ARTIFACT_TYPE)?;
    if listed.is_empty() {
        return Err(Error::ArtifactUnpublished(format!(
            "no goldens referrer for {subject}"
        )));
    }

    let mut verified: Vec<Candidate> = Vec::new();
    let mut first_failure: Option<Error> = None;
    let mut seen: Vec<String> = Vec::new();
    for referrer in &listed {
        if seen.contains(&referrer.digest) {
            continue;
        }
        seen.push(referrer.digest.clone());
        let (blob_digest, bytes) = match fetch_goldens_blob(source, &referrer.digest) {
            Ok(v) => v,
            Err(e) => {
                first_failure.get_or_insert(e);
                continue;
            }
        };
        let bundles =
            referrers::find_candidates(source, &referrer.digest, constants::MEDIA_TYPE_BUNDLE)?;
        for bundle in &bundles {
            let stmt: VerifiedStatement = match referrers::verify_one(source, bundle, trust) {
                Ok(s) => s,
                Err(e) => {
                    first_failure.get_or_insert(e);
                    continue;
                }
            };
            if stmt.predicate_type != constants::PREDICATE_TYPE_GOLDENS {
                first_failure.get_or_insert(Error::ArtifactCorrupt(format!(
                    "signed predicateType is {:?}, want {:?}",
                    stmt.predicate_type,
                    constants::PREDICATE_TYPE_GOLDENS
                )));
                continue;
            }
            if Some(stmt.subject_sha256.as_str()) != blob_digest.strip_prefix("sha256:") {
                first_failure.get_or_insert(Error::ArtifactCorrupt(format!(
                    "signed subject does not match the goldens blob {blob_digest}"
                )));
                continue;
            }
            let revision = predicate_revision(&stmt.predicate)?;
            verified.push(Candidate {
                manifest: referrer.digest.clone(),
                blob: blob_digest.clone(),
                revision,
                bytes: bytes.clone(),
                predicate: stmt.predicate,
                bundle_layer: stmt.bundle_layer_digest,
                bundle_manifest: stmt.bundle_manifest_digest,
            });
            break;
        }
    }

    if verified.is_empty() {
        return Err(first_failure.unwrap_or_else(|| {
            Error::ArtifactUntrusted(format!(
                "no goldens referrer of {subject} verifies under a trusted key"
            ))
        }));
    }
    let best = select(verified)?;
    Ok((
        best.bytes,
        best.predicate,
        Digests {
            index: None,
            manifest: best.manifest,
            layer: best.blob,
            bundle: Some(best.bundle_layer),
            bundle_manifest: Some(best.bundle_manifest),
        },
    ))
}

/// One goldens referrer manifest's single layer, verified against its
/// descriptor: `(layer digest, bytes)`.
fn fetch_goldens_blob(source: &Source<'_>, manifest_digest: &str) -> Result<(String, Vec<u8>)> {
    let fetched = oci::fetch_by_digest(source, manifest_digest, constants::MANIFEST_MAX_BYTES)?;
    let manifest: oci::Manifest = serde_json::from_slice(&fetched.bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("goldens manifest {manifest_digest}: {e}")))?;
    if manifest.media_type.as_deref() != Some(constants::MEDIA_TYPE_MANIFEST) {
        return Err(Error::SourceIncompatible(format!(
            "{manifest_digest} has unrecognized mediaType {:?}",
            manifest.media_type
        )));
    }
    let [layer] = manifest.layers.as_slice() else {
        return Err(Error::ArtifactCorrupt(format!(
            "{manifest_digest} has {} layers, want exactly 1",
            manifest.layers.len()
        )));
    };
    let blob = oci::fetch_blob_by_digest(source, &layer.digest, constants::MAX_UNPACKED_BYTES)?;
    Ok((layer.digest.clone(), blob.bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cand(manifest: &str, blob: &str, revision: u64, bundle: &str) -> Candidate {
        Candidate {
            manifest: manifest.into(),
            blob: blob.into(),
            revision,
            bytes: Vec::new(),
            predicate: json!({}),
            bundle_layer: bundle.into(),
            bundle_manifest: String::new(),
        }
    }

    #[test]
    fn revision_integers_are_accepted() {
        for (text, want) in [
            ("0", 0u64),
            ("1", 1),
            ("9007199254740991", 9007199254740991),
        ] {
            let p: serde_json::Value =
                serde_json::from_str(&format!("{{\"revision\":{text}}}")).unwrap();
            assert_eq!(predicate_revision(&p).unwrap(), want, "{text}");
        }
    }

    #[test]
    fn revision_non_integers_are_corrupt() {
        for text in [
            "true",
            "false",
            "\"2\"",
            "null",
            "1.0",
            "1e0",
            "-1",
            "-0",
            "{}",
            "[]",
            "9007199254740992",
        ] {
            let p: serde_json::Value =
                serde_json::from_str(&format!("{{\"revision\":{text}}}")).unwrap();
            assert!(
                matches!(predicate_revision(&p), Err(Error::ArtifactCorrupt(_))),
                "{text}"
            );
        }
        assert!(matches!(
            predicate_revision(&json!({"schema": 1})),
            Err(Error::ArtifactCorrupt(_))
        ));
    }

    #[test]
    fn highest_revision_wins_wherever_listed() {
        let best = select(vec![
            cand("m1", "a", 1, "b"),
            cand("m2", "b", 2, "b"),
            cand("m3", "a", 0, "b"),
        ])
        .unwrap();
        assert_eq!((best.revision, best.blob.as_str()), (2, "b"));
    }

    #[test]
    fn a_tie_of_different_documents_names_both_digests_and_the_revision() {
        let err = select(vec![cand("m1", "a", 3, "b"), cand("m2", "b", 3, "b")]).unwrap_err();
        let msg = err.to_string();
        assert!(matches!(err, Error::ArtifactCorrupt(_)));
        assert!(msg.contains("revision 3") && msg.contains(" a ") && msg.contains(" b"));
    }

    #[test]
    fn the_same_document_under_the_same_revision_is_not_a_tie() {
        let best = select(vec![
            cand("m1", "a", 2, "b1"),
            cand("m1", "a", 2, "b2"),
            cand("m2", "b", 1, "b3"),
        ])
        .unwrap();
        assert_eq!(best.bundle_layer, "b1");
    }

    #[test]
    fn a_tie_below_the_highest_revision_does_not_matter() {
        let best = select(vec![
            cand("m1", "a", 1, "b"),
            cand("m2", "b", 1, "b"),
            cand("m3", "a", 4, "b"),
        ])
        .unwrap();
        assert_eq!(best.revision, 4);
    }
}
