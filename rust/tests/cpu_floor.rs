//! The CPU floor, loader step 2b (spec/abi-v2/docs.md, "Loading"), through the
//! public API: a library below its floor opens, nothing of it has run, and the
//! loader refuses it as an ordinary error, carrying the library's three answers
//! verbatim. They are spelled out here, not taken from the code under test.
//!
//! The stubs plant it on every leg with no emulation
//! (`scripts/abi-v1/emit/_stubshared.py`, `CPU_FLOOR`). In `cpu-floor-refused`
//! the setup call answers `CHS_INTERNAL`, so a loader that reached it reads as
//! [`Error::Internal`], never as this refusal.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes. One
//! test function, deliberately: the unverified open needs the process
//! environment.
#![cfg(unix)]

use std::path::{Path, PathBuf};

use chtypes::{Error, Library, code};
use serde_json::Value;

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const ENV_UNVERIFIED: &str = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY";

const WANT: &str = "x86-64-v3+pclmul";
const GOT: &str = "avx2,bmi2";
const SENTENCE: &str = "this CPU cannot run this library: missing avx2,bmi2 (the 26.8 linux-amd64 build requires x86-64-v3+pclmul). Use a CPU at or above that floor.";

fn stub_path(dir: &Path, manifest: &Value, name: &str) -> PathBuf {
    let path = manifest["variants"][name]["path"]
        .as_str()
        .unwrap_or_else(|| panic!("stubs.json names no {name} variant"));
    dir.join(Path::new(path).file_name().expect("file name"))
}

#[test]
fn a_library_below_its_floor_is_an_ordinary_refusal() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): cpu_floor needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return;
    };
    let text = std::fs::read_to_string(dir.join("stubs.json")).expect("read stubs.json");
    let manifest: Value = serde_json::from_str(&text).expect("parse stubs.json");
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };

    // Below the floor: the class existing handlers catch, and the new code.
    let path = stub_path(&dir, &manifest, "cpu-floor-refused");
    let err = Library::open_unverified(&path, true)
        .err()
        .unwrap_or_else(|| panic!("the loader accepted a library below its CPU floor"));
    let Error::ArtifactIncompatible(refusal) = &err else {
        panic!("want Error::ArtifactIncompatible, got {err:?}");
    };
    assert_eq!(err.code(), Some("CHTYPES_CPU_UNSUPPORTED"));
    assert_eq!(err.code(), Some(code::CPU_UNSUPPORTED));
    assert_eq!(refusal.reason, "cpu_floor");
    assert_eq!(refusal.path, path);
    assert_eq!(refusal.want.as_deref(), Some(WANT));
    assert_eq!(refusal.got.as_deref(), Some(GOT));
    let shown = err.to_string();
    assert!(shown.contains(SENTENCE), "{shown}");
    assert!(shown.starts_with("CHTYPES_CPU_UNSUPPORTED: "), "{shown}");

    // A library from before the check is today's fingerprint refusal: not
    // cpu_floor, and not a missing symbol.
    let path = stub_path(&dir, &manifest, "cpu-floor-absent");
    let err = Library::open_unverified(&path, true)
        .err()
        .unwrap_or_else(|| panic!("the loader accepted a library with another fingerprint"));
    let Error::ArtifactIncompatible(refusal) = &err else {
        panic!("want Error::ArtifactIncompatible, got {err:?}");
    };
    assert_eq!(err.code(), Some(code::ARTIFACT_INCOMPATIBLE));
    assert_eq!(refusal.reason, "fingerprint");
    assert_eq!(
        refusal.got.as_deref(),
        Some(format!("sha256:{}", "0".repeat(64)).as_str())
    );

    // The ordinary stub answers "" and opens.
    let path = stub_path(&dir, &manifest, "ok");
    Library::open_unverified(&path, true).expect("the ok stub answers \"\" and must open");
}
