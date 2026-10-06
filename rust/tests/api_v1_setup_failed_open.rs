//! Before any image has completed load step 7, an open that attempted a load
//! and failed unlocks the setup record, and the caller's own misuse unlocks
//! nothing (`docs/reference/bindings-v1.md` §6, rule 4), here for the cases that
//! need no library at all: a request nothing installed answers (autofetch off),
//! and a refused version spelling. It runs in its own process, as one test,
//! because `setup` is process-wide and nothing public resets it. It needs no
//! stubs and no network.

use chtypes::{Error, FetchOptions, Registry, RegistryOptions, SetupOptions};

fn zone(name: &str) -> SetupOptions {
    SetupOptions {
        timezone: Some(name.to_string()),
        defaults: Vec::new(),
    }
}

#[test]
fn a_failed_open_before_step_7_unlocks_the_setup_and_misuse_does_not() {
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

    chtypes::setup(zone("Asia/Tokyo")).expect("the first setup records");

    // A refused version spelling is the caller's own misuse, refused before
    // anything is attempted: it unlocks nothing.
    let err = registry.for_version("v26.8").unwrap_err();
    assert!(
        matches!(err, Error::Usage(_)),
        "a refused spelling: want Usage, got {err:?}"
    );
    let err = chtypes::setup(zone("UTC")).unwrap_err();
    assert!(
        matches!(err, Error::Usage(_)),
        "a refused spelling must unlock nothing: {err:?}"
    );

    // Nothing installed answers the request: an open that attempted to resolve
    // and failed, before any load. It keeps the record and makes it replaceable.
    let err = registry.for_version("26.8").unwrap_err();
    assert!(
        matches!(err, Error::ArtifactMissing(_)),
        "an empty cache with autofetch off: want ArtifactMissing, got {err:?}"
    );
    chtypes::setup(zone("UTC"))
        .expect("the failed open unlocked the record, so a different setup replaces it");

    // The replacement is locked again: a second, different setup before the
    // next open is still misuse.
    let err = chtypes::setup(zone("Europe/Berlin")).unwrap_err();
    assert!(matches!(err, Error::Usage(_)), "{err:?}");

    let _ = std::fs::remove_dir_all(&cache);
}
