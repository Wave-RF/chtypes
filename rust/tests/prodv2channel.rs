//! The production generation-2 channel (`src/ocifetch/channel.rs`,
//! `PROD_V2_CHANNEL`; docs/guides/fetch-v1.md, "Generation 2 after the lock"),
//! built and tested but not the default. The fetch conformance cases run under
//! it in tests/ocifetch_conformance.rs; these are the pins those cases do not
//! reach. Nothing here sets an environment variable or reaches the network.
//!
//! The fetch layer is not public API (see `src/ocifetch/mod.rs`), so this suite
//! compiles it under its own crate root, as tests/devchannel.rs does.

#[path = "../src/ocifetch/mod.rs"]
mod ocifetch;

use ocifetch::channel::{self, DEV_KEY_HEX};
use ocifetch::constants;
use ocifetch::dsse;
use ocifetch::ensure::{self, Options};

#[test]
fn the_production_v2_channel_carries_the_v2_values() {
    let _prod = channel::use_prod_v2_for_tests();
    let a = channel::active();
    assert_eq!((a.name, a.abi, a.record_schema), ("v2", 2, 2));
    assert_eq!((a.root_leaf, a.subroot), ("v2", Some("v2")));
    assert!(a.overridable && a.pinnable);
    assert!(a.own_fingerprint.is_none());
    assert_eq!(
        a.system_dirs,
        &["/usr/local/share/chtypes/v2", "/opt/chtypes/v2"]
    );
    assert_eq!(
        ensure::configured_bases(&Options::default()),
        vec!["https://registry.wavehouse.dev/chtypes/v2".to_string()]
    );
}

#[test]
fn the_production_v2_channel_trusts_the_release_key_and_not_the_staging_key() {
    let _prod = channel::use_prod_v2_for_tests();
    let trust = dsse::trusted_keys(None).expect("the default trust");
    assert_eq!(trust.len(), constants::RELEASE_KEYS.len());
    assert_eq!(trust[0].keyid, "deb275922dbff76e");
    for k in &trust {
        let hex: String = k.key.iter().map(|b| format!("{b:02x}")).collect();
        assert_ne!(
            hex, DEV_KEY_HEX,
            "the staging key is in the production-v2 trust list"
        );
    }
}

/// A build with no test seam speaks the dev channel: the production-v2 channel
/// is reachable only through `use_prod_v2_for_tests`.
#[test]
fn the_production_v2_channel_is_not_the_default() {
    assert_eq!(channel::active().name, "v2-dev");
    {
        let _prod = channel::use_prod_v2_for_tests();
        assert_eq!(channel::active().name, "v2");
    }
    assert_eq!(channel::active().name, "v2-dev");
}
