//! Public issue #528: `CHTYPES_OFFLINE=1` is the environment twin of the
//! `offline` option, under the ABI v2 dev channel (spec/abi-v2/docs.md, rule
//! r6: it names no source). With nothing installed a fetch is
//! CHTYPES_ARTIFACT_MISSING and makes no request; with a library installed it
//! resolves it. Every request follows the layout's creation (the refusals in
//! tests/devchannel.rs rely on the same ordering), so "the cache was never
//! created" is the proof that none was made.
//!
//! The environment is process-wide, so everything that sets it is ONE test.
//! The fetch layer is not public API, so this suite compiles it under its own
//! crate root, as tests/devchannel.rs does.

#[path = "../src/ocifetch/mod.rs"]
mod ocifetch;

use std::path::PathBuf;

use ocifetch::channel;
use ocifetch::constants;
use ocifetch::ensure::{self, Options};
use ocifetch::error::Error;
use ocifetch::layout::{self, VerifiedRecord};
use ocifetch::oci;

#[test]
fn offline_env_makes_no_request_and_loads_an_installed_build() {
    let base = std::env::temp_dir().join(format!("offline-env-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    let cache = base.join("cache");
    let options = || Options {
        platform: Some("linux-arm64".to_string()),
        cache_dir: Some(cache.to_string_lossy().into_owned()),
        system_dirs: Vec::new(),
        ..Options::default()
    };

    // SAFETY: this is the only test in this binary, so nothing else reads the environment.
    unsafe { std::env::set_var("CHTYPES_OFFLINE", "1") };
    assert!(channel::offline_mode(false));

    // Nothing installed: ARTIFACT_MISSING, and the cache was never made.
    match ensure::ensure("26.8", options()) {
        Err(Error::ArtifactMissing(_)) => {}
        other => panic!(
            "CHTYPES_OFFLINE=1, nothing installed: {:?}",
            other.map(|r| r.dir)
        ),
    }
    assert!(!cache.exists(), "an offline fetch made {}", cache.display());

    // A build installed by hand: it loads, still with no layout made.
    let cache_arg = cache.to_string_lossy().into_owned();
    let root = layout::cache_root(Some(cache_arg.as_str())).unwrap();
    let library = "library 26.8.1.1";
    let manifest = oci::sha256_hex(format!("{}26.8.1.1", root.display()).as_bytes());
    let record = VerifiedRecord {
        platform: "linux-arm64".to_string(),
        version: "26.8.1.1".to_string(),
        build: "20260801.000001".to_string(),
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
        predicate: serde_json::json!({"clickhouse_version": "26.8.1.1", "build": "20260801.000001"}),
    };
    let entry: PathBuf = root.join(constants::CACHE_UNPACKED_DIR).join(&manifest);
    layout::write_atomic(&entry.join("lib.so"), library.as_bytes()).unwrap();
    layout::write_atomic(
        &entry.join(constants::CACHE_VERIFIED_RECORD),
        &record.to_json_bytes().unwrap(),
    )
    .unwrap();
    let got = ensure::ensure("26.8", options()).expect("an installed build loads offline");
    assert_eq!(got.dir, entry);
    assert!(
        !root.join("index.json").exists(),
        "an offline fetch made the layout"
    );

    // Only "1" is on; the option alone is the same mode.
    for value in ["0", "", "true"] {
        // SAFETY: as above.
        unsafe { std::env::set_var("CHTYPES_OFFLINE", value) };
        assert!(!channel::offline_mode(false), "{value:?}");
    }
    assert!(channel::offline_mode(true));
    let _ = std::fs::remove_dir_all(&base);
}
