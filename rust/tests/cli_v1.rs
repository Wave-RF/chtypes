//! The `chtypes` command line, run as the real binary (public issue #486): a
//! `verify` that verified nothing says so, a 0.x registry used as the cache is
//! named, and a read-only command writes nothing.

use std::path::{Path, PathBuf};
use std::process::Command;

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("cli-v1-test-{name}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn run(args: &[&str]) -> (i32, String, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_chtypes"))
        .args(args)
        .env_remove("CHTYPES_CACHE")
        .env_remove("CHTYPES_ARTIFACTS_URL")
        .output()
        .expect("run the chtypes binary");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

fn zero_x_registry(root: &Path) {
    std::fs::create_dir_all(root.join("26.1")).unwrap();
    std::fs::write(root.join("26.1").join("manifest.json"), b"{}").unwrap();
}

#[test]
fn verify_of_nothing_says_so_and_names_a_zero_x_registry() {
    let base = scratch("verify");
    let empty = base.join("empty");
    std::fs::create_dir_all(&empty).unwrap();
    let (code, out, err) = run(&["verify", "--cache", empty.to_str().unwrap()]);
    assert_eq!((code, out.as_str()), (0, ""), "{err}");
    assert!(
        err.contains(&format!(
            "chtypes: verified 0 builds under {}\n",
            empty.display()
        )),
        "{err}"
    );

    let zero_x = base.join("zero-x");
    zero_x_registry(&zero_x);
    let hint = format!(
        "{} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at",
        zero_x.display()
    );
    let (code, _, err) = run(&["verify", "--cache", zero_x.to_str().unwrap()]);
    assert!(code == 0 && err.contains(&hint), "{code} {err}");
    let (code, _, err) = run(&[
        "fetch",
        "26.1",
        "--offline",
        "--platform",
        "linux-arm64",
        "--cache",
        zero_x.to_str().unwrap(),
    ]);
    assert!(
        code == 1 && err.contains("CHTYPES_ARTIFACT_MISSING") && err.contains(&hint),
        "{code} {err}"
    );
    assert!(
        !zero_x.join("oci-layout").exists() && !zero_x.join("index.json").exists(),
        "a read-only command wrote into the 0.x registry"
    );
    let _ = std::fs::remove_dir_all(&base);
}
