//! Loading is lazy, and `RegistryOptions::preload` is the one eager path.
//!
//! The sentence this file exists to hold is the same sentence in all four
//! bindings: constructing a registry reads `manifest.json` files and `dlopen`s
//! nothing, and nothing in the crate opens an artifact except a request for a
//! specific version or an explicit `preload`.
//!
//! The proof needs no real artifact and inspects no code. Every "library" here
//! is a text file, so any open at all fails at `dlopen` — a registry that
//! opened one would return the error from whichever call opened it. What is
//! counted is `libraries()`, which is populated by the real load path and by
//! nothing else.
//!
//! Every case goes through `Registry::open`, whose search path is the one
//! directory it was given: a `from_search_path` registry would also see this
//! machine's own cache, and `versions()` has to answer about the directory
//! under test.
//!
//! **Every case here runs in BOTH feature configurations**, including the
//! `--no-default-features` build `docs/install.md` tells a consumer to use for
//! the loader without the downloader. Nothing in this file is behind a feature
//! gate, and `the_whole_file_runs_in_both_feature_configurations` at the
//! bottom is the check rather than the claim — a case that silently stopped
//! existing in one configuration would look exactly like one that passed.
//! `preload_never_fetches` is about autofetch and still needs no `fetch`
//! feature, which is not an accident: preload never reaches the fetch path at
//! all, so the same assertion holds either way.

use std::path::{Path, PathBuf};

use chtypes::{Error, Registry, RegistryOptions};

const BODY: &[u8] = b"not a shared library";

/// `sha256(BODY)`, pinned rather than computed.
///
/// The crate's only PUBLIC hasher is `chtypes::fetch::sha256_hex`, and
/// `pub mod fetch` is behind the `fetch` feature — calling it here made this
/// whole test binary fail to compile under `--no-default-features`, which is
/// the same defect #45 fixed for `tests/parity.rs`.
///
/// A wrong constant cannot pass quietly: the test that uses it asserts BOTH
/// directions on the same directory — the line whose manifest carries this
/// digest must clear verification and fail at `dlopen`, and its neighbor
/// carrying zeroes must fail with `ChecksumMismatch`. A digest that did not
/// match the bytes would turn the first of those into a `ChecksumMismatch` and
/// fail the assertion.
const BODY_SHA256: &str = "c80ebb9dc312f3c343c4e4af53b3ed437cab683a234d64b4c21f23ad097c2710";

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "chtypes-rs-lazy-{}-{tag}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::remove_dir_all(&dir).ok();
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// `<root>/<line>/` with a manifest and a "library" that is plain text.
fn stand_in(root: &Path, line: &str, sha256: Option<&str>) {
    let sub = root.join(line);
    std::fs::create_dir_all(&sub).unwrap();
    std::fs::write(sub.join("libchtypes.so"), BODY).unwrap();
    let hash = match sha256 {
        Some(s) => format!(r#","library_sha256":"{s}""#),
        None => String::new(),
    };
    std::fs::write(
        sub.join("manifest.json"),
        format!(
            r#"{{"library":"libchtypes.so","library_bytes":{}{hash},
                "clickhouse_version":"{line}.1.1","clickhouse_minor":"{line}"}}"#,
            BODY.len()
        ),
    )
    .unwrap();
}

fn stand_in_registry(tag: &str, lines: &[&str]) -> PathBuf {
    let root = scratch(tag);
    for line in lines {
        stand_in(&root, line, None);
    }
    root
}

fn plain(dir: &Path) -> chtypes::Result<Registry> {
    Registry::open(dir, RegistryOptions::default())
}

#[test]
fn construction_and_every_listing_open_nothing() {
    let dir = stand_in_registry("listing", &["25.8", "25.10", "26.7"]);
    let reg = plain(&dir).expect("construction must open nothing, so a text file cannot fail it");
    assert!(
        reg.libraries().is_empty(),
        "construction opened {} libraries; it must open none",
        reg.libraries().len()
    );

    // versions() answers from the manifest scan, not from what is open — the
    // one meaning all four bindings share, and the one that survives laziness.
    assert_eq!(reg.versions(), vec!["25.8", "25.10", "26.7"]);
    assert!(reg.libraries().is_empty(), "a listing opened a library");

    // Formatting a registry must not cost 120 MB a line: Debug goes through
    // versions(), which is manifest-derived.
    assert!(format!("{reg:?}").contains("25.8"));
    assert!(reg.libraries().is_empty(), "Debug opened a library");

    // The first request for a line is what opens it — and here that open
    // reaches dlopen and dies there, which is the proof it reached dlopen at
    // all rather than being skipped.
    let err = reg.for_version("25.8").unwrap_err();
    assert!(
        matches!(err, Error::Load { .. } | Error::NotAnArtifact { .. }),
        "for_version must reach dlopen, got {err:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn preload_opens_exactly_the_named_lines_at_construction() {
    let dir = stand_in_registry("preload", &["25.8", "25.10", "26.7"]);

    // A preloaded line is opened before the constructor returns, so a text
    // file's dlopen failure arrives from `open` rather than from `for_version`.
    let err = Registry::open(
        &dir,
        RegistryOptions {
            preload: vec!["25.10".into()],
            ..Default::default()
        },
    )
    .expect_err("a preloaded text file cannot dlopen");
    assert!(
        matches!(err, Error::Load { .. } | Error::NotAnArtifact { .. }),
        "got {err:?}"
    );
    assert!(
        err.to_string().contains("25.10"),
        "the preload failure must name the line it opened: {err}"
    );

    // An empty list is exactly the default, not a special case.
    let reg = Registry::open(
        &dir,
        RegistryOptions {
            preload: Vec::new(),
            ..Default::default()
        },
    )
    .expect("an empty preload list is the default");
    assert!(reg.libraries().is_empty());
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_preload_entry_no_directory_holds_is_the_missing_artifact_error() {
    let dir = stand_in_registry("preload-missing", &["25.8"]);
    let err = Registry::open(
        &dir,
        RegistryOptions {
            preload: vec!["26.7".into()],
            ..Default::default()
        },
    )
    .expect_err("no directory holds 26.7");
    match &err {
        Error::ArtifactMissing {
            line, looked_in, ..
        } => {
            assert_eq!(line, "26.7");
            assert_eq!(looked_in, &vec![dir.clone()]);
        }
        other => panic!("want Error::ArtifactMissing naming the line, got {other:?}"),
    }
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn preload_never_fetches() {
    // Autofetch is a first-use behavior in all four bindings: a constructor is
    // a worse place than a request to begin a 250 MB download. `Registry::open`
    // refuses `autofetch` outright, so this goes through the search path — with
    // an explicit `dir` first on it, and a line nothing publishes.
    //
    // It needs no `fetch` feature, and that is the point rather than a
    // convenience: `RegistryOptions::autofetch` is not gated (only
    // `RegistryOptions::fetch` is), and preload returns ArtifactMissing from
    // `Registry::preload` itself without ever reaching `autofetch_or_missing`.
    // So this asserts the same thing in both builds — where a `fetch`-feature
    // build would have answered `Error::Fetch` if preload HAD taken that path,
    // and a no-feature build would have answered the "feature disabled" error.
    // Neither appears, which is the proof.
    let dir = stand_in_registry("preload-nofetch", &["25.8"]);
    let err = Registry::from_search_path_with(RegistryOptions {
        dir: Some(dir.clone()),
        autofetch: Some(true),
        preload: vec!["99.9".into()],
        ..Default::default()
    })
    .expect_err("preload must not fetch");
    assert_eq!(
        err.artifact_code(),
        Some("CHTYPES_ARTIFACT_MISSING"),
        "preload must not fetch, even with autofetch on; got {err:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_checksum_is_computed_immediately_before_a_dlopen_and_at_no_other_time() {
    // Preloaded: the refusal arrives from the constructor. Lazily opened: from
    // the call that asks. Never asked for: never hashed. Reading the option as
    // "only the preload list is verified" would let a lazily-opened line load
    // UNHASHED under verify_checksums.
    let dir = scratch("verify-timing");
    let zeroes = "00".repeat(32);
    stand_in(&dir, "25.8", Some(&zeroes));
    stand_in(&dir, "26.7", Some(&zeroes));

    let err = Registry::open(
        &dir,
        RegistryOptions {
            verify_checksums: true,
            preload: vec!["25.8".into()],
            ..Default::default()
        },
    )
    .expect_err("a preloaded line is hashed at construction");
    assert!(matches!(err, Error::ChecksumMismatch { .. }), "got {err:?}");

    let reg = Registry::open(
        &dir,
        RegistryOptions {
            verify_checksums: true,
            ..Default::default()
        },
    )
    .expect("construction hashes nothing, so it cannot fail on these");
    assert!(reg.libraries().is_empty());
    let err = reg.for_version("26.7").unwrap_err();
    assert!(
        matches!(err, Error::ChecksumMismatch { .. }),
        "a lazily-opened line is hashed too, got {err:?}"
    );

    // With the option off the hash is not consulted at all, which is what
    // proves the OPTION produced the verdicts above rather than the file.
    let lenient = plain(&dir).unwrap();
    let err = lenient.for_version("25.8").unwrap_err();
    assert!(
        !matches!(err, Error::ChecksumMismatch { .. }),
        "without verify_checksums the digest must not be consulted; got {err:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_broken_neighbor_is_no_longer_the_whole_registrys_problem() {
    // A registry over a directory holding a corrupt 25.8 and a good 26.7 used
    // to fail at construction and serve neither.
    let dir = scratch("neighbor");
    stand_in(&dir, "25.8", Some(&"00".repeat(32)));
    stand_in(&dir, "26.7", Some(BODY_SHA256));

    let reg = Registry::open(
        &dir,
        RegistryOptions {
            verify_checksums: true,
            ..Default::default()
        },
    )
    .expect("a corrupt neighbor must not stop the registry constructing");
    assert_eq!(reg.versions(), vec!["25.8", "26.7"]);
    // The honest line passes its hash and dies at dlopen, which is the proof
    // the hash ran and passed rather than never running.
    let err = reg.for_version("26.7").unwrap_err();
    assert!(
        matches!(err, Error::Load { .. } | Error::NotAnArtifact { .. }),
        "matching bytes must clear verification and fail at dlopen, got {err:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_named_directory_that_cannot_be_read_still_fails_at_construction() {
    // The typo guard survives lazy loading, and costs no dlopen.
    let missing = scratch("typo").join("chtyeps");
    let err = plain(&missing).expect_err("a named directory that does not exist");
    assert!(matches!(err, Error::Registry { .. }), "got {err:?}");
    assert!(err.to_string().contains("chtyeps"), "{err}");

    let err = Registry::from_search_path_with(RegistryOptions {
        dir: Some(missing.clone()),
        ..Default::default()
    })
    .expect_err("the search-path constructor names it too");
    assert!(matches!(err, Error::Registry { .. }), "got {err:?}");
}

#[test]
fn a_directory_holding_no_manifest_at_all_still_fails_at_construction() {
    let empty = scratch("empty");
    let err = plain(&empty).expect_err("an empty registry is a configuration mistake");
    match &err {
        Error::EmptyRegistry { looked_in } => assert_eq!(looked_in, &vec![empty.clone()]),
        other => panic!("want Error::EmptyRegistry naming the directory, got {other:?}"),
    }
    assert!(
        err.to_string().contains(&empty.display().to_string()),
        "{err}"
    );
    std::fs::remove_dir_all(&empty).ok();
}

#[test]
fn from_search_path_is_fallible_and_scans_manifests() {
    // Rust's search-path constructor used to read NOTHING at construction, not
    // even manifests, which made §5's construction-time failures
    // unimplementable here. It scans now, and that scan is what makes both the
    // empty-path refusal and a bad preload entry decidable — and what made
    // this constructor fallible.
    let dir = stand_in_registry("scan", &["24.8", "26.6"]);
    let reg = Registry::from_search_path_with(RegistryOptions {
        dir: Some(dir.clone()),
        ..Default::default()
    })
    .expect("the directory holds manifests");
    assert!(reg.libraries().is_empty(), "the scan opened something");
    for line in ["24.8", "26.6"] {
        assert!(
            reg.versions().iter().any(|v| v == line),
            "the scan must discover {line}: {:?}",
            reg.versions()
        );
    }
    std::fs::remove_dir_all(&dir).ok();
}

/// Every case in this file runs in BOTH feature configurations — the check,
/// not the claim.
///
/// `tests/parity.rs` gates its `fetch::*` rows on the feature and then asserts
/// that the skip path itself cannot go quiet. This file takes the other route:
/// it carries no conditional-compilation attribute at all, so nothing here can
/// be present in one build and absent in the other. That is worth something
/// only while it stays true, and "a case that silently stops existing looks
/// exactly like one that passes" is the failure this suite exists to refuse —
/// so the invariant is read off the source rather than off a comment.
///
/// The needle is assembled at compile time on purpose: spelled out, it would
/// match this test's own text and fire on every run.
#[test]
fn the_whole_file_runs_in_both_feature_configurations() {
    let source = include_str!("lazy.rs");
    let attribute = concat!("#[c", "fg(");
    assert!(
        !source.contains(attribute),
        "tests/lazy.rs grew a conditional-compilation attribute. A case behind one vanishes \
         from the --no-default-features build — the loader-without-downloader configuration \
         docs/install.md tells a consumer to use — and looks exactly like a pass. Either keep \
         the case feature-free, or give it the loud-skip treatment tests/parity.rs uses: name \
         what was skipped, and fail if nothing was."
    );
    // Loud, so the two CI legs are distinguishable in a log rather than
    // identically green.
    let cases = source.matches("\n#[test]").count();
    assert!(cases >= 9, "this file scores {cases} cases; it had 10");
    eprintln!(
        "lazy: {cases} cases, all running with the `fetch` feature {}",
        if cfg!(feature = "fetch") { "ON" } else { "OFF" }
    );
}
