//! The process setup, in its own process (a test binary is one process, and
//! `setup` is process-wide): `setup` records, the first open commits it, and a
//! loader step-7 failure (`chs_initialize`) is the call's own error mapped by
//! the status table, never a loader refusal.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes.

use std::path::PathBuf;

use chtypes::{Error, Library, SetupOptions, status};
use serde_json::Value;

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const ENV_UNVERIFIED: &str = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY";

#[test]
fn setup_records_and_a_step_7_failure_is_the_calls_own_error() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): api_v1_setup needs {ENV_STUBS} (the v1-abi-stubs directory); nothing was exercised"
        );
        return;
    };
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(dir.join("stubs.json")).expect("read stubs.json"))
            .expect("parse stubs.json");
    let ok = dir.join(
        std::path::Path::new(
            manifest["variants"]["ok"]["path"]
                .as_str()
                .expect("ok path"),
        )
        .file_name()
        .expect("file name"),
    );
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };

    // `setup` records and does not load: it succeeds before any image exists,
    // and the same setup again is a no-op.
    let injected = "!S:CHS_INVALID_ARGUMENT:0::zones differ: UTC vs Asia/Tokyo";
    let options = SetupOptions {
        timezone: Some(injected.to_string()),
        defaults: vec![("max_threads".to_string(), "1".to_string())],
    };
    chtypes::setup(options.clone()).expect("the first setup records");
    chtypes::setup(options.clone()).expect("the same setup again is a no-op");
    // A different zone, or different defaults, is misuse naming both, and the
    // first setup stands.
    for different in [
        SetupOptions {
            timezone: Some("Asia/Tokyo".to_string()),
            ..options.clone()
        },
        SetupOptions {
            defaults: Vec::new(),
            ..options.clone()
        },
    ] {
        let err = chtypes::setup(different).unwrap_err();
        let Error::Usage(call) = &err else {
            panic!("want misuse, got {err:?}")
        };
        assert!(
            call.message.to_lossy().contains(injected),
            "the message names the setup in effect: {call}"
        );
    }

    // The first open commits that setup at step 7, and the library's own
    // answer to `chs_initialize` is what the caller gets: the status table's
    // class, with the library's five fields, never a loader refusal.
    let err = Library::open_unverified(&ok, true).unwrap_err();
    let Error::Usage(call) = &err else {
        panic!("a step 7 failure is the call's own error (misuse here), got {err:?}")
    };
    assert_eq!(call.status, status::INVALID_ARGUMENT);
    assert!(
        call.message.to_lossy().contains("zones differ"),
        "the library's own message survives: {call}"
    );
    assert!(
        !matches!(
            err,
            Error::ArtifactIncompatible(_) | Error::ArtifactCorrupt(_)
        ),
        "a step 7 failure is never a refusal reason"
    );
}
