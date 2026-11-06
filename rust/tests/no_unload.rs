//! A library is never unloaded: a library the loader REFUSES stays mapped, and so
//! does one it accepts and the caller then drops.
//!
//! `libloading`'s `Library` calls `dlclose` in its `Drop`, so a loader that lets
//! it drop on an early `?` unloads the image it just refused. That is not
//! harmless: an image built without `-z nodelete` can start background threads
//! at `dlopen` and register thread-key destructors, and `dlclose` unmaps the code
//! they point into, so the host can crash seconds later. This suite loads
//! each stub the loader refuses at a different step, then asks the dynamic
//! linker, with `RTLD_NOLOAD`, whether the image is still resident. `RTLD_NOLOAD`
//! maps nothing: it succeeds only for an image already in the process, so a
//! refused image that was `dlclose`d reads as absent and this suite fails.
//!
//! The probe could be a test that always says yes, so the suite also asks about
//! a stub it never opened (`ok-b`) and requires the answer to be no.
//!
//! Every stub here is opened by this test binary only (each file under
//! `rust/tests` is its own binary and its own process), and by no other test in
//! it: an earlier successful load of the same path would keep the image resident
//! and hide the bug. One test function, deliberately: the unverified open needs
//! the process environment.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes.
#![cfg(unix)]

use std::path::{Path, PathBuf};

use chtypes::{Error, Library};
use serde_json::Value;

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const ENV_UNVERIFIED: &str = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY";

/// The refused stubs, one per place the loader can leave after `dlopen`: the CPU
/// floor (`cpu_floor`), the handshake (`not_v1`, `abi_version`), `chs_build_info`
/// (`build_info_malformed`, `fingerprint`), and step 6's symbol resolution, which the generated
/// `Api::resolve_all` returns from. The step-5 cross-check needs a signed
/// statement and cannot be reached by an unverified open.
const REFUSED: &[&str] = &[
    "cpu-floor-refused",
    "no-abi-version",
    "abi-version-1",
    "build-info-bad-json",
    "fingerprint-other",
    "missing-chs_cancel_free",
];

fn stub_path(dir: &Path, manifest: &Value, name: &str) -> PathBuf {
    let path = manifest["variants"][name]["path"]
        .as_str()
        .unwrap_or_else(|| panic!("stubs.json names no {name} variant"));
    dir.join(Path::new(path).file_name().expect("file name"))
}

/// Whether the dynamic linker holds `path`'s image right now. `RTLD_NOLOAD`
/// loads nothing and runs no initializer; it returns a handle only for an image
/// that is already mapped, and that handle's own `dlclose` (on drop) undoes only
/// the reference the probe took.
fn resident(path: &Path) -> bool {
    use libloading::os::unix::Library as UnixLibrary;
    // SAFETY: RTLD_NOLOAD maps no image and runs no constructor, so there is
    // nothing for the call to execute.
    unsafe { UnixLibrary::open(Some(path), libc::RTLD_NOW | libc::RTLD_NOLOAD) }.is_ok()
}

#[test]
fn a_library_is_never_unloaded_refused_or_accepted() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): no_unload needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return;
    };
    let text = std::fs::read_to_string(dir.join("stubs.json")).expect("read stubs.json");
    let manifest: Value = serde_json::from_str(&text).expect("parse stubs.json");
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };

    // The control: an image nobody opened is not resident, so the probe can say no.
    let never = stub_path(&dir, &manifest, "ok-b");
    assert!(
        !resident(&never),
        "the probe says {} is mapped before anything opened it",
        never.display()
    );

    for name in REFUSED {
        let path = stub_path(&dir, &manifest, name);
        assert!(!resident(&path), "{name}: mapped before it was opened");
        let want = manifest["variants"][name]["reason"]
            .as_str()
            .expect("variant reason");
        let err = Library::open_unverified(&path, true)
            .err()
            .unwrap_or_else(|| panic!("{name}: the loader accepted a stub it must refuse"));
        let (Error::ArtifactIncompatible(refusal) | Error::ArtifactCorrupt(refusal)) = &err else {
            panic!("{name}: want a loader refusal, got {err:?}");
        };
        assert_eq!(refusal.reason, want, "{name}: refused for another reason");
        assert!(
            resident(&path),
            "{name}: the loader refused ({want}) and then unloaded the image; a library \
             must stay mapped for the life of the process, refused or not"
        );
    }

    // An accepted library the caller drops stays mapped too: dropping the last
    // `Arc<Library>` releases Rust memory, never the image.
    let ok = stub_path(&dir, &manifest, "ok");
    assert!(!resident(&ok), "ok: mapped before it was opened");
    let lib = Library::open_unverified(&ok, true).expect("the ok stub loads");
    assert!(resident(&ok), "ok: not mapped after a successful load");
    drop(lib);
    assert!(
        resident(&ok),
        "ok: unloaded when the last handle to it dropped"
    );

    // The control again, after everything: still no for the image nobody opened.
    assert!(
        !resident(&never),
        "the probe says {} is mapped though nothing opened it",
        never.display()
    );
}
