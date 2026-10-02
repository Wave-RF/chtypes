//! DSSE + in-toto verification (layout-v2 spec §4.1-4.3, plan §1.1.3 "Trust").
//!
//! A key-based Sigstore bundle v0.3: no certificate, no transparency log —
//! `verificationMaterial` carries only a public-key `hint`, which this module
//! never trusts on its own (a hint is an optimization, not an identity). The
//! cryptographic decision is always "does this signature verify against one
//! of OUR pinned key bytes", tried over every signature in the envelope.

use std::collections::HashMap;

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Deserialize;

use super::constants;
use super::error::{Error, Result};

/// A pinned ed25519 public key this module may trust a signature against.
#[derive(Clone)]
pub struct TrustedKey {
    pub keyid: &'static str,
    pub key: [u8; 32],
}

/// The default trust list (`constants::RELEASE_KEYS`) plus, when the caller
/// opts in, the fixture-only test keys (never on by default, plan "Eric's
/// decisions" D8).
pub fn trusted_keys(include_test_keys: bool) -> Result<Vec<TrustedKey>> {
    let mut out = Vec::new();
    for rk in constants::RELEASE_KEYS {
        out.push(TrustedKey {
            keyid: rk.keyid,
            key: hex32(rk.ed25519_hex)?,
        });
    }
    if include_test_keys {
        for tk in constants::TEST_KEYS {
            out.push(TrustedKey {
                keyid: tk.keyid,
                key: hex32(tk.ed25519_hex)?,
            });
        }
    }
    Ok(out)
}

fn hex32(s: &str) -> Result<[u8; 32]> {
    if s.len() != 64 {
        return Err(Error::InvalidInput(format!(
            "key hex {s:?} is not 32 bytes"
        )));
    }
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16)
            .map_err(|e| Error::InvalidInput(format!("key hex {s:?}: {e}")))?;
    }
    Ok(out)
}

/// A Sigstore bundle v0.3, reduced to the key-based shape layout-v2 §4.2
/// produces: `{"verificationMaterial":{"publicKey":{"hint":...}},
/// "dsseEnvelope":{"payload":...,"payloadType":...,"signatures":[...]}}`.
#[derive(Deserialize)]
struct Bundle {
    #[serde(rename = "dsseEnvelope")]
    dsse_envelope: DsseEnvelope,
}

#[derive(Deserialize)]
struct DsseEnvelope {
    /// Base64-encoded statement bytes.
    payload: String,
    #[serde(rename = "payloadType")]
    payload_type: String,
    signatures: Vec<DsseSignature>,
}

#[derive(Deserialize)]
struct DsseSignature {
    /// Base64-encoded raw ed25519 signature.
    sig: String,
}

/// The in-toto Statement v1, after the subject/predicateType/abi checks.
/// `predicate` is the verified object verbatim (seam `Resolved.predicate`).
pub struct VerifiedStatement {
    pub subject_sha256: String,
    pub predicate_type: String,
    pub predicate: serde_json::Value,
    /// The keyid of the trusted key that verified this bundle.
    pub signed_by: &'static str,
}

/// DSSE's Pre-Authentication Encoding (the DSSE spec):
/// `"DSSEv1" SP LEN(type) SP type SP LEN(body) SP body`.
fn pae(payload_type: &str, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + payload_type.len() + 32);
    out.extend_from_slice(b"DSSEv1");
    out.push(b' ');
    out.extend_from_slice(payload_type.len().to_string().as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload_type.as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload.len().to_string().as_bytes());
    out.push(b' ');
    out.extend_from_slice(payload);
    out
}

/// Verify one bundle's DSSE envelope against the trust list, returning the
/// parsed, verified statement's subject digest, predicate type and raw
/// predicate (as [`VerifiedStatement`]). Does **not** check the predicate
/// against the request; `ensure.rs` does that (plan §1.3's guarantees).
///
/// `bundle_bytes` is the raw layer blob (already size/digest verified by the
/// caller, the same byte-order rule as the artifact layer itself).
pub fn verify_bundle(
    bundle_bytes: &[u8],
    trust: &[TrustedKey],
    expected_statement_type: &str,
) -> Result<VerifiedStatement> {
    if bundle_bytes.len() as u64 > constants::BUNDLE_MAX_BYTES {
        return Err(Error::ArtifactCorrupt(format!(
            "signature bundle is {} bytes, over the {}-byte cap",
            bundle_bytes.len(),
            constants::BUNDLE_MAX_BYTES
        )));
    }
    check_no_duplicate_keys(bundle_bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("signature bundle: {e}")))?;
    let bundle: Bundle = serde_json::from_slice(bundle_bytes)
        .map_err(|e| Error::ArtifactCorrupt(format!("signature bundle is not valid JSON: {e}")))?;
    let env = bundle.dsse_envelope;
    if env.payload_type != constants::DSSE_PAYLOAD_TYPE {
        return Err(Error::ArtifactCorrupt(format!(
            "DSSE payloadType {:?}, want {:?}",
            env.payload_type,
            constants::DSSE_PAYLOAD_TYPE
        )));
    }
    if env.signatures.len() as u32 > constants::DSSE_MAX_SIGNATURES {
        return Err(Error::ArtifactCorrupt(format!(
            "DSSE envelope carries {} signatures, over the {} cap",
            env.signatures.len(),
            constants::DSSE_MAX_SIGNATURES
        )));
    }
    let payload = b64_decode(&env.payload)
        .map_err(|e| Error::ArtifactCorrupt(format!("DSSE payload is not base64: {e}")))?;
    let pae_bytes = pae(&env.payload_type, &payload);

    let mut signed_by: Option<&'static str> = None;
    'keys: for tk in trust {
        let Ok(vk) = VerifyingKey::from_bytes(&tk.key) else {
            continue;
        };
        for sig in &env.signatures {
            let Ok(raw_sig) = b64_decode(&sig.sig) else {
                continue;
            };
            let Ok(raw_sig): std::result::Result<[u8; 64], _> = raw_sig.try_into() else {
                continue;
            };
            let signature = Signature::from_bytes(&raw_sig);
            if vk.verify(&pae_bytes, &signature).is_ok() {
                signed_by = Some(tk.keyid);
                break 'keys;
            }
        }
    }
    let Some(signed_by) = signed_by else {
        return Err(Error::ArtifactUntrusted(
            "no signature in the bundle verifies under a trusted key".to_string(),
        ));
    };

    check_no_duplicate_keys(&payload)
        .map_err(|e| Error::ArtifactCorrupt(format!("in-toto statement: {e}")))?;
    let statement: Statement = serde_json::from_slice(&payload)
        .map_err(|e| Error::ArtifactCorrupt(format!("in-toto statement is not valid JSON: {e}")))?;
    if statement.statement_type != expected_statement_type {
        return Err(Error::ArtifactCorrupt(format!(
            "statement _type {:?}, want {:?}",
            statement.statement_type, expected_statement_type
        )));
    }
    // Whether `predicate_type` is the one the caller actually wanted (an
    // artifact vs. a goldens vs. a fixtures statement) is the caller's
    // check, not this function's: `fetch_signed` and the platform-manifest
    // path each know which predicate type they asked for.
    let Some(subject) = statement.subject.first() else {
        return Err(Error::ArtifactCorrupt(
            "statement has no subject".to_string(),
        ));
    };
    let Some(subject_sha256) = subject.digest.get("sha256") else {
        return Err(Error::ArtifactCorrupt(
            "statement subject has no sha256 digest".to_string(),
        ));
    };

    Ok(VerifiedStatement {
        subject_sha256: subject_sha256.clone(),
        predicate_type: statement.predicate_type,
        predicate: statement.predicate,
        signed_by,
    })
}

#[derive(Deserialize)]
struct Statement {
    #[serde(rename = "_type")]
    statement_type: String,
    subject: Vec<Subject>,
    #[serde(rename = "predicateType")]
    predicate_type: String,
    predicate: serde_json::Value,
}

#[derive(Deserialize)]
struct Subject {
    digest: HashMap<String, String>,
}

fn b64_decode(s: &str) -> std::result::Result<Vec<u8>, base64::DecodeError> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.decode(s.trim())
}

/// Refuse a JSON document that repeats an object key anywhere, at any depth
/// (plan §1.1.3's DSSE "duplicate-key check"; the Go lane walks the
/// `json.Decoder` token stream, Python uses `object_pairs_hook` — this is
/// the same rule, generically, over `serde`).
pub fn check_no_duplicate_keys(bytes: &[u8]) -> std::result::Result<(), String> {
    use serde::de::DeserializeSeed as _;
    let mut de = serde_json::Deserializer::from_slice(bytes);
    DupCheckVisitor
        .deserialize(&mut de)
        .map_err(|e: serde_json::Error| e.to_string())?;
    Ok(())
}

/// A `Visitor` (used directly as a `DeserializeSeed`-free one-shot
/// deserializer via `Deserialize::deserialize`) that walks the whole
/// document and errors on the first duplicate object key.
struct DupCheckVisitor;

impl<'de> serde::de::DeserializeSeed<'de> for DupCheckVisitor {
    type Value = ();

    fn deserialize<D>(self, deserializer: D) -> std::result::Result<Self::Value, D::Error>
    where
        D: serde::de::Deserializer<'de>,
    {
        deserializer.deserialize_any(self)
    }
}

impl<'de> serde::de::Visitor<'de> for DupCheckVisitor {
    type Value = ();

    fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "any JSON value")
    }

    fn visit_bool<E>(self, _v: bool) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_i64<E>(self, _v: i64) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_u64<E>(self, _v: u64) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_f64<E>(self, _v: f64) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_str<E>(self, _v: &str) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_string<E>(self, _v: String) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_unit<E>(self) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_none<E>(self) -> std::result::Result<Self::Value, E> {
        Ok(())
    }
    fn visit_some<D>(self, d: D) -> std::result::Result<Self::Value, D::Error>
    where
        D: serde::de::Deserializer<'de>,
    {
        d.deserialize_any(self)
    }

    fn visit_seq<A>(self, mut seq: A) -> std::result::Result<Self::Value, A::Error>
    where
        A: serde::de::SeqAccess<'de>,
    {
        while seq.next_element_seed(DupCheckVisitor)?.is_some() {}
        Ok(())
    }

    fn visit_map<A>(self, mut map: A) -> std::result::Result<Self::Value, A::Error>
    where
        A: serde::de::MapAccess<'de>,
    {
        let mut seen = std::collections::HashSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !seen.insert(key.clone()) {
                return Err(serde::de::Error::custom(format!(
                    "duplicate object key {key:?}"
                )));
            }
            map.next_value_seed(DupCheckVisitor)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pae_matches_dsse_spec_example() {
        // https://github.com/secure-systems-lab/dsse/blob/master/protocol.md
        let got = pae("http://example.com/HelloWorld", b"hello world");
        assert_eq!(
            got,
            b"DSSEv1 30 http://example.com/HelloWorld 11 hello world".to_vec()
        );
    }

    #[test]
    fn duplicate_top_level_key_is_refused() {
        let err = check_no_duplicate_keys(br#"{"a":1,"b":2,"a":3}"#).unwrap_err();
        assert!(err.contains("duplicate"), "{err}");
    }

    #[test]
    fn duplicate_nested_key_is_refused() {
        let err = check_no_duplicate_keys(br#"{"a":{"x":1,"x":2}}"#).unwrap_err();
        assert!(err.contains("duplicate"), "{err}");
    }

    #[test]
    fn no_duplicates_is_fine() {
        check_no_duplicate_keys(br#"{"a":1,"b":{"c":2},"d":[1,2,3]}"#).unwrap();
    }
}
