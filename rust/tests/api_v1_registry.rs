//! The registry over the REAL fetch derivation: the one conformance case that
//! pins the `Resolved -> loader input` adapter, which a hand-built loader input
//! cannot test (a test that supplies a derived value only tests the belief
//! about the derivation).
//!
//! It packages the `ok` stub library as an OCI layout signed with the fetch
//! fixtures' test key (`tests/fixtures/fetch-v1/test-key/`), seeds it as the
//! cache's `index.json`, and asks a [`chtypes::Registry`] for the version with
//! that key trusted. The fetch layer's own `resolve_installed` then verifies the
//! signed statement, unpacks the library and hands back `Resolved`; the registry
//! adapts it and runs loader steps 1 to 7 against the stub. A second layout
//! whose signed statement disagrees with the library must be refused at step 5.
//!
//! `CHTYPES_ABI1_STUBS` unset: this suite skips LOUDLY by name and passes.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use chtypes::{Error, FetchOptions, Registry, RegistryOptions};

const ENV_STUBS: &str = "CHTYPES_ABI1_STUBS";

const MEDIA_INDEX: &str = "application/vnd.oci.image.index.v1+json";
const MEDIA_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
const MEDIA_EMPTY: &str = "application/vnd.oci.empty.v1+json";
const MEDIA_CONFIG: &str = "application/vnd.wavehouse.chtypes.config.v1+json";
const MEDIA_LAYER: &str = "application/vnd.oci.image.layer.v1.tar+zstd";
const MEDIA_BUNDLE: &str = "application/vnd.dev.sigstore.bundle.v0.3+json";
const ARTIFACT_TYPE: &str = "application/vnd.wavehouse.chtypes.artifact.v1";
const PAYLOAD_TYPE: &str = "application/vnd.in-toto+json";
const STATEMENT_TYPE: &str = "https://in-toto.io/Statement/v1";
const PREDICATE_TYPE: &str = "https://artifacts.wavehouse.dev/spec/artifact/v1";
const LIBRARY_NAME: &str = "libchtypes.so";
const TEST_KEYID: &str = "6c3468e4ec653ac0";
/// The same key's public half (`constants.json`'s `test_keys`): the trust list
/// a test names, since the default trust is the release key alone.
const TEST_KEY_HEX: &str = "b9b314491f92f6c4b93fc69f932164739a619965ed79dbbcea7a4ae2da611ce2";

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("rust/ has a parent")
        .to_path_buf()
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The fixtures' test key: the seed is the last 32 bytes of the PKCS#8 DER the
/// PEM carries (an ed25519 PrivateKeyInfo is a 16-byte prefix and the seed).
fn test_signing_key() -> (SigningKey, String, String) {
    let dir = repo_root().join("tests/fixtures/fetch-v1/test-key");
    let pem = std::fs::read_to_string(dir.join("private.pem")).expect("read private.pem");
    let body: String = pem.lines().filter(|l| !l.starts_with("-----")).collect();
    let der = base64::engine::general_purpose::STANDARD
        .decode(body.trim())
        .expect("the PEM is base64");
    let seed: [u8; 32] = der[der.len() - 32..].try_into().expect("a 32-byte seed");
    let key = SigningKey::from_bytes(&seed);
    let public_hex: String = key
        .verifying_key()
        .to_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let want = std::fs::read_to_string(dir.join("public.hex")).expect("read public.hex");
    assert_eq!(
        public_hex,
        want.trim(),
        "the PEM and public.hex are one key"
    );
    // The id the fetch constants give the test key (`constants.json`'s `test_keys`),
    // which is what a verified statement reports as `signed_by`.
    (key, public_hex, TEST_KEYID.to_string())
}

/// DSSE's pre-authentication encoding.
fn pae(payload_type: &str, payload: &[u8]) -> Vec<u8> {
    let mut out = format!(
        "DSSEv1 {} {payload_type} {} ",
        payload_type.len(),
        payload.len()
    )
    .into_bytes();
    out.extend_from_slice(payload);
    out
}

struct Layout {
    cache: PathBuf,
    manifest_digest: String,
}

fn put_blob(root: &Path, bytes: &[u8]) -> (String, u64) {
    let hex = sha256_hex(bytes);
    let dir = root.join("blobs").join("sha256");
    std::fs::create_dir_all(&dir).expect("create blobs");
    std::fs::write(dir.join(&hex), bytes).expect("write blob");
    (format!("sha256:{hex}"), bytes.len() as u64)
}

/// Package `library` as a one-platform OCI layout under `cache`, signed with the
/// test key over `predicate`, and seed the cache's `index.json` with it.
fn package(
    cache: &Path,
    library: &[u8],
    predicate: &Value,
    key: &SigningKey,
    keyid: &str,
) -> Layout {
    std::fs::create_dir_all(cache).expect("create cache");
    // The layer: a tar holding the one library, zstd-framed (uncompressed).
    let mut tar_bytes = Vec::new();
    {
        let mut builder = tar::Builder::new(&mut tar_bytes);
        let mut header = tar::Header::new_gnu();
        header.set_size(library.len() as u64);
        header.set_mode(0o755);
        header.set_mtime(0);
        header.set_entry_type(tar::EntryType::Regular);
        builder
            .append_data(&mut header, LIBRARY_NAME, library)
            .expect("append the library");
        builder.finish().expect("finish the tar");
    }
    let layer = ruzstd::encoding::compress_to_vec(
        &tar_bytes[..],
        ruzstd::encoding::CompressionLevel::Uncompressed,
    );
    let (layer_digest, layer_size) = put_blob(cache, &layer);
    let (config_digest, config_size) = put_blob(cache, b"{}");
    let manifest = json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": MEDIA_CONFIG, "digest": config_digest, "size": config_size},
        "layers": [{"mediaType": MEDIA_LAYER, "digest": layer_digest, "size": layer_size}],
    });
    let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
    let (manifest_digest, manifest_size) = put_blob(cache, &manifest_bytes);

    // The signed statement: subject is the layer, predicate is the artifact's.
    let statement = json!({
        "_type": STATEMENT_TYPE,
        "subject": [{"name": LIBRARY_NAME, "digest": {"sha256": layer_digest.trim_start_matches("sha256:")}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate,
    });
    let payload = serde_json::to_vec(&statement).unwrap();
    let signature = key.sign(&pae(PAYLOAD_TYPE, &payload));
    let b64 = base64::engine::general_purpose::STANDARD;
    let bundle = json!({
        "mediaType": MEDIA_BUNDLE,
        "verificationMaterial": {"publicKey": {"hint": keyid}},
        "dsseEnvelope": {
            "payload": b64.encode(&payload),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [{"sig": b64.encode(signature.to_bytes()), "keyid": keyid}],
        },
    });
    let (bundle_digest, bundle_size) = put_blob(cache, &serde_json::to_vec(&bundle).unwrap());
    let (empty_digest, empty_size) = put_blob(cache, b"{}");
    // The signature referrer: a manifest whose `subject` is the platform manifest.
    let referrer = json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "artifactType": MEDIA_BUNDLE,
        "config": {"mediaType": MEDIA_EMPTY, "digest": empty_digest, "size": empty_size},
        "layers": [{"mediaType": MEDIA_BUNDLE, "digest": bundle_digest, "size": bundle_size}],
        "subject": {"mediaType": MEDIA_MANIFEST, "digest": manifest_digest, "size": manifest_size},
    });
    put_blob(cache, &serde_json::to_vec(&referrer).unwrap());

    let os = predicate["os"].as_str().expect("predicate os");
    let arch = predicate["arch"].as_str().expect("predicate arch");
    std::fs::write(
        cache.join("oci-layout"),
        br#"{"imageLayoutVersion":"1.0.0"}"#,
    )
    .unwrap();
    let index = json!({
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": [{
            "mediaType": MEDIA_MANIFEST,
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": {"architecture": arch, "os": os},
        }],
    });
    std::fs::write(
        cache.join("index.json"),
        serde_json::to_vec(&index).unwrap(),
    )
    .unwrap();
    Layout {
        cache: cache.to_path_buf(),
        manifest_digest,
    }
}

fn registry_over(layout: &Layout, platform: &str) -> Registry {
    Registry::new(RegistryOptions {
        fetch: FetchOptions {
            platform: Some(platform.to_string()),
            cache_dir: Some(layout.cache.to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            trusted_keys: Some(vec![TEST_KEY_HEX.to_string()]),
            ..Default::default()
        },
        autofetch: Some(false),
        preload: Vec::new(),
    })
    .expect("construction opens nothing")
}

#[test]
fn a_signed_layout_resolves_adapts_and_loads_and_a_mismatch_is_refused() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): api_v1_registry needs {ENV_STUBS} (the v1-abi-stubs directory); nothing was exercised"
        );
        return;
    };
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(dir.join("stubs.json")).expect("read stubs.json"))
            .expect("parse stubs.json");
    let ok = &manifest["variants"]["ok"];
    let library_path = dir.join(
        Path::new(ok["path"].as_str().expect("ok path"))
            .file_name()
            .expect("file name"),
    );
    let library = std::fs::read(&library_path).expect("read the ok stub");

    // The statement the artifact producer would sign: the stub's own predicate,
    // plus the library's name and hashes. A Linux statement carries the glibc
    // floor the loader's step 1 reads.
    let mut predicate = ok["predicate"].clone();
    let fields = predicate.as_object_mut().expect("predicate is an object");
    fields.insert("library".into(), json!(LIBRARY_NAME));
    fields.insert("library_sha256".into(), json!(sha256_hex(&library)));
    fields.insert("library_bytes".into(), json!(library.len()));
    if fields["os"] == "linux" {
        fields.entry("glibc_floor").or_insert(json!("2.17"));
    }
    let platform = format!(
        "{}-{}",
        predicate["os"].as_str().unwrap(),
        predicate["arch"].as_str().unwrap()
    );
    let version = predicate["clickhouse_version"]
        .as_str()
        .unwrap()
        .to_string();
    let minor = predicate["clickhouse_minor"].as_str().unwrap().to_string();

    let (key, public_hex, keyid) = test_signing_key();
    let work = std::env::temp_dir().join(format!("chtypes_api_v1_registry_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&work);

    // --- the good statement: resolve, adapt, load ------------------------------
    let good = package(&work.join("good"), &library, &predicate, &key, &keyid);
    let registry = registry_over(&good, &platform);
    assert!(
        registry.libraries().is_empty(),
        "construction opens nothing"
    );

    // The default trust is the release key alone: without the opt-in the test
    // key is refused, which is how the fetch layer reports an unverifiable
    // statement (nothing installed answers the request).
    let untrusting = Registry::new(RegistryOptions {
        fetch: FetchOptions {
            platform: Some(platform.clone()),
            cache_dir: Some(good.cache.to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            ..Default::default()
        },
        autofetch: Some(false),
        preload: Vec::new(),
    })
    .unwrap();
    // (Run on a copy of the layout with nothing unpacked yet: the trusted
    // registry below installs into the cache, and an install is an install.)
    let err = untrusting.for_version(&minor).unwrap_err();
    assert!(
        matches!(err, Error::ArtifactMissing(_)),
        "the test key is not trusted by default, got {err:?} (key {public_hex})"
    );

    let lib = registry
        .for_version(&minor)
        .expect("the signed layout loads");
    assert_eq!(lib.version(), version);
    let resolved = lib
        .resolved()
        .expect("a registry open carries its fetch record");
    assert_eq!(resolved.signed_by, keyid);
    assert_eq!(resolved.digests.manifest, good.manifest_digest);
    // The predicate the loader cross-checked is the one the fetch layer
    // verified, verbatim.
    assert_eq!(resolved.predicate, predicate);
    assert_eq!(lib.build_info().clickhouse_version, version);

    // The memo: a request returns the same Library for the registry's life, and
    // another spelling of the same build is the same image.
    let memo = registry.for_version(&minor).unwrap();
    assert!(Arc::ptr_eq(&lib, &memo));
    let exact = registry.for_version(&version).unwrap();
    assert!(Arc::ptr_eq(&lib, &exact), "one image per file");
    assert_eq!(registry.libraries().len(), 1);
    assert_eq!(registry.installed().unwrap().len(), 1);

    // A request nothing answers is the ordinary ArtifactMissing, naming it. This
    // runs against an empty cache on purpose: the fetch layer's pre-seeded index
    // path answers an unrelated request from an entry it already unpacked, which
    // is reported separately and is not what this case proves.
    let empty = Layout {
        cache: work.join("empty"),
        manifest_digest: String::new(),
    };
    std::fs::create_dir_all(&empty.cache).unwrap();
    let missing = registry_over(&empty, &platform)
        .for_version("1.1")
        .unwrap_err();
    let Error::ArtifactMissing(message) = &missing else {
        panic!("want ArtifactMissing, got {missing:?}")
    };
    assert!(
        message.contains("1.1") && message.contains(&platform),
        "{message}"
    );

    // --- a statement that disagrees with the library is refused at step 5 ------
    let mut lying = predicate.clone();
    lying["inputs_sha256"] = json!("d".repeat(64));
    let bad = package(&work.join("bad"), &library, &lying, &key, &keyid);
    let err = registry_over(&bad, &platform)
        .for_version(&minor)
        .unwrap_err();
    let Error::ArtifactCorrupt(refusal) = &err else {
        panic!("want ArtifactCorrupt (loader step 5), got {err:?}")
    };
    assert_eq!(refusal.reason, "build_info_mismatch:inputs_sha256");

    // preload opens at construction and never fetches.
    let preloaded = Registry::new(RegistryOptions {
        fetch: FetchOptions {
            platform: Some(platform.clone()),
            cache_dir: Some(good.cache.to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            trusted_keys: Some(vec![TEST_KEY_HEX.to_string()]),
            ..Default::default()
        },
        autofetch: Some(true),
        preload: vec![minor.clone()],
    })
    .expect("a preloaded request opens at construction");
    assert_eq!(preloaded.libraries().len(), 1);
    let missing_preload = Registry::new(RegistryOptions {
        fetch: FetchOptions {
            platform: Some(platform),
            cache_dir: Some(empty.cache.to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            trusted_keys: Some(vec![TEST_KEY_HEX.to_string()]),
            ..Default::default()
        },
        autofetch: Some(true),
        preload: vec![minor.clone()],
    })
    .unwrap_err();
    assert!(
        matches!(missing_preload, Error::ArtifactMissing(_)),
        "preload never fetches, even with autofetch on: {missing_preload:?}"
    );

    let _ = std::fs::remove_dir_all(&work);
}
