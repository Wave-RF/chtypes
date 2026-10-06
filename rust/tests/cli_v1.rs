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
        .env_remove("CHTYPES_CACHE_STRICT")
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

/// `--strict` (or `CHTYPES_CACHE_STRICT=1`) turns every command's cache fault
/// into `CHTYPES_CACHE_UNUSABLE` (exit 9) naming the path and the reason, and a
/// verify of nothing into `CHTYPES_ARTIFACT_MISSING` (exit 1) (public issue
/// #486).
#[test]
fn strict_flag() {
    let base = scratch("strict");
    let empty = base.join("empty");
    std::fs::create_dir_all(&empty).unwrap();
    let e = empty.to_str().unwrap();
    let (code, _, err) = run(&["verify", "--strict", "--cache", e]);
    assert!(
        code == 1 && err.contains("CHTYPES_ARTIFACT_MISSING"),
        "{code} {err}"
    );
    let out = Command::new(env!("CARGO_BIN_EXE_chtypes"))
        .args(["verify", "--cache", e])
        .env("CHTYPES_CACHE_STRICT", "1")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    let (code, out, _) = run(&["where", "--strict", "--cache", e]);
    assert_eq!((code, out.trim()), (0, e));
    let zero_x = base.join("zero-x");
    zero_x_registry(&zero_x);
    let z = zero_x.to_str().unwrap();
    let want = format!("{z} is unusable as a cache: layout_0x");
    for argv in [
        vec!["where", "--strict"],
        vec!["list", "--offline", "--strict"],
        vec!["verify", "--strict"],
        vec![
            "fetch",
            "26.1",
            "--offline",
            "--platform",
            "linux-arm64",
            "--strict",
        ],
    ] {
        let mut args = argv.clone();
        args.extend(["--cache", z]);
        let (code, out, err) = run(&args);
        assert!(
            code == 9
                && out.is_empty()
                && err.contains(&want)
                && err.contains("CHTYPES_CACHE_UNUSABLE"),
            "{argv:?}: {code} {out:?} {err}"
        );
    }
    let _ = std::fs::remove_dir_all(&base);
}
