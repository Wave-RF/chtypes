//! A failed open before any image has completed load step 7 clears the setup
//! record, whatever failed (`docs/reference/bindings-v1.md` §6, rule 4), here
//! for the failures that need no library at all: a request nothing installed
//! answers (autofetch off), and a refused version spelling. It runs in its own
//! process, as one test, because `setup` is process-wide and nothing public
//! resets it. It needs no stubs and no network.

use chtypes::{Error, FetchOptions, Registry, RegistryOptions, SetupOptions};

fn zone(name: &str) -> SetupOptions {
    SetupOptions {
        timezone: Some(name.to_string()),
        defaults: Vec::new(),
    }
}

#[test]
fn a_failed_open_before_step_7_clears_the_setup() {
    let cache =
        std::env::temp_dir().join(format!("setup_failed_open_cache_{}", std::process::id()));
    std::fs::create_dir_all(&cache).expect("create an empty cache");
    let registry = Registry::new(RegistryOptions {
        fetch: FetchOptions {
            cache_dir: Some(cache.to_string_lossy().into_owned()),
            system_dirs: Some(Vec::new()),
            offline: true,
            ..Default::default()
        },
        autofetch: Some(false),
        ..Default::default()
    })
    .expect("constructing a registry opens nothing");

    // Nothing installed answers the request: a failed open, before any load.
    chtypes::setup(zone("Asia/Tokyo")).expect("the first setup records");
    let err = registry.for_version("26.8").unwrap_err();
    assert!(
        matches!(err, Error::ArtifactMissing(_)),
        "an empty cache with autofetch off: want ArtifactMissing, got {err:?}"
    );
    chtypes::setup(zone("UTC"))
        .expect("the failed open cleared the record, so a different setup is accepted");

    // A refused version spelling is a failed open too.
    let err = registry.for_version("v26.8").unwrap_err();
    assert!(
        matches!(err, Error::Usage(_)),
        "a refused spelling: want Usage, got {err:?}"
    );
    chtypes::setup(zone("Europe/Berlin")).expect("the refused spelling cleared the record too");

    // Before any open is attempted, a second, different setup is still misuse.
    let err = chtypes::setup(zone("Asia/Tokyo")).unwrap_err();
    assert!(matches!(err, Error::Usage(_)), "{err:?}");

    let _ = std::fs::remove_dir_all(&cache);
}
