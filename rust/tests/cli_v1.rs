//! The `chtypes` command line, run as the real binary (public issue #486): a
//! `verify` that verified nothing says so, a 0.x registry used as the cache is
//! named, and a read-only command writes nothing.
//!
//! The real binary is a build no test seam reaches, so every case here sees the
//! ABI v2 dev channel exactly as a user's 2.0.0-dev CLI speaks it
//! (spec/abi-v2/docs.md, rules r5 and r6): an explicit cache is read through its
//! `v2-dev` subroot, the default root is `${XDG_CACHE_HOME}/chtypes/v2-dev`,
//! `--lock`, `--frozen` and `--update` are refused before any network call, and
//! each ignored override is named exactly once. Nothing here reaches the
//! network.

// The fingerprint this binary speaks, from the generated constant itself, so
// the hand-written records below are the binary's own fingerprint's.
#[path = "../src/ocifetch/abi_fingerprint.rs"]
mod abi_fingerprint;

use std::path::{Path, PathBuf};
use std::process::Command;

/// The dev channel's cache directory: the default root's last element and an
/// explicit cache's subroot (rule r5).
const DEV: &str = "v2-dev";

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("cli-v1-test-{name}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn run(args: &[&str]) -> (i32, String, String) {
    run_env(args, &[])
}

/// The real binary with `env` set over an environment that names no cache, no
/// base, no trust and no unsigned fetch of its own.
fn run_env(args: &[&str], env: &[(&str, &str)]) -> (i32, String, String) {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_chtypes"));
    cmd.args(args);
    for name in [
        "CHTYPES_CACHE",
        "CHTYPES_ARTIFACTS_URL",
        "CHTYPES_CACHE_STRICT",
        "CHTYPES_TRUSTED_KEYS",
        "CHTYPES_ALLOW_UNSIGNED",
    ] {
        cmd.env_remove(name);
    }
    for (k, v) in env {
        cmd.env(k, v);
    }
    let out = cmd.output().expect("run the chtypes binary");
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
            empty.join(DEV).display()
        )),
        "{err}"
    );

    // A 0.x registry where this CLI reads the explicit cache: its v2-dev subroot.
    let zero_x = base.join("zero-x");
    zero_x_registry(&zero_x.join(DEV));
    let hint = format!(
        "{} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at",
        zero_x.join(DEV).display()
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
        !zero_x.join(DEV).join("oci-layout").exists()
            && !zero_x.join(DEV).join("index.json").exists(),
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
    assert_eq!((code, out.trim()), (0, empty.join(DEV).to_str().unwrap()));
    let zero_x = base.join("zero-x");
    zero_x_registry(&zero_x.join(DEV));
    let z = zero_x.to_str().unwrap();
    let want = format!(
        "{} is unusable as a cache: layout_0x",
        zero_x.join(DEV).display()
    );
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

/// Rule r5: the default root, and an explicit cache from the environment or
/// `--cache`, each used through the v2-dev subroot, never as a whole layout.
#[test]
fn where_is_the_v2_dev_root_and_subroot() {
    let base = scratch("where");
    let xdg = base.join("xdg");
    let cache = base.join("cache");
    let (code, out, err) = run_env(&["where"], &[("XDG_CACHE_HOME", xdg.to_str().unwrap())]);
    assert_eq!(
        (code, out.trim()),
        (0, xdg.join("chtypes").join(DEV).to_str().unwrap()),
        "the default root: {err}"
    );
    let (code, out, err) = run_env(&["where"], &[("CHTYPES_CACHE", cache.to_str().unwrap())]);
    assert_eq!(
        (code, out.trim()),
        (0, cache.join(DEV).to_str().unwrap()),
        "CHTYPES_CACHE: {err}"
    );
    let (code, out, err) = run(&["where", "--cache", cache.to_str().unwrap()]);
    assert_eq!(
        (code, out.trim()),
        (0, cache.join(DEV).to_str().unwrap()),
        "--cache: {err}"
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// Rule r6: `--lock`, `--frozen` and `--update` are refused, exit 2, before
/// anything else: no lock is written and no cache is made.
#[test]
fn lock_frozen_and_update_are_refused() {
    let base = scratch("pinning");
    let lock = base.join("chtypes.lock");
    let l = lock.to_str().unwrap();
    let cache = base.join("cache");
    for args in [
        vec!["fetch", "26.8", "--lock", l],
        vec!["fetch", "26.8", "--frozen"],
        vec!["fetch", "26.8", "--frozen", "--lock", l],
        vec!["fetch", "--update", "--lock", l],
        vec!["fetch", "--all", "--frozen"],
    ] {
        let (code, out, err) = run_env(&args, &[("CHTYPES_CACHE", cache.to_str().unwrap())]);
        assert!(
            code == 2
                && out.is_empty()
                && err.contains("refused by a 2.0.0-dev SDK")
                && err.contains("14 days"),
            "{args:?}: {code} stdout {out:?} stderr {err}"
        );
    }
    assert!(!lock.exists(), "a refused --lock wrote {}", lock.display());
    assert!(
        !cache.exists(),
        "a refused fetch made the cache {}",
        cache.display()
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// Rule r6: every override in the environment is ignored and named exactly
/// once, and an offline fetch from an empty dev cache is
/// CHTYPES_ARTIFACT_MISSING (exit 1), whatever base, key or unsigned opt-in
/// the environment names.
#[test]
fn every_override_is_ignored_and_named_once() {
    let base = scratch("overrides");
    let cache = base.join("cache");
    // The fixture key (tests/fixtures/fetch-v1/test-key/public.hex).
    let test_key = "b9b314491f92f6c4b93fc69f932164739a619965ed79dbbcea7a4ae2da611ce2";
    let (code, out, err) = run_env(
        &["fetch", "26.8", "--offline", "--platform", "linux-amd64"],
        &[
            ("CHTYPES_CACHE", cache.to_str().unwrap()),
            ("CHTYPES_ARTIFACTS_URL", "http://127.0.0.1:9/chtypes/v1"),
            ("CHTYPES_TRUSTED_KEYS", test_key),
            ("CHTYPES_ALLOW_UNSIGNED", "1"),
        ],
    );
    assert!(
        code == 1 && out.is_empty() && err.contains("CHTYPES_ARTIFACT_MISSING"),
        "an offline fetch from an empty dev cache: {code} {out:?} {err}"
    );
    for name in [
        "CHTYPES_ARTIFACTS_URL",
        "CHTYPES_TRUSTED_KEYS",
        "CHTYPES_ALLOW_UNSIGNED",
    ] {
        let n = err
            .matches(&format!("WARNING: {name} is set and IGNORED"))
            .count();
        assert_eq!(
            n, 1,
            "{name} was warned about {n} times, want exactly once:\n{err}"
        );
    }
    assert!(
        err.contains("https://registry-staging.wavehouse.dev/chtypes/v2-dev")
            && err.contains("824345f9bcf8e5bf"),
        "the warning does not name the base and the key it uses instead:\n{err}"
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// `where --all` lists every directory searched, the cache root through its
/// v2-dev subroot first, one per line; the default output stays the root alone
/// (public issues #530 and #527).
#[test]
fn where_all_lists_the_search_dirs() {
    let base = scratch("where-all");
    let c = base.to_str().unwrap();
    let root = base.join(DEV);
    let want = format!(
        "{}\n/usr/local/share/chtypes/{DEV}\n/opt/chtypes/{DEV}\n",
        root.display()
    );
    let (code, out, err) = run(&["where", "--all", "--cache", c]);
    assert_eq!((code, out.as_str(), err.as_str()), (0, want.as_str(), ""));
    let (code, out, _) = run(&["where", "--cache", c]);
    assert_eq!((code, out), (0, format!("{}\n", root.display())));
    let _ = std::fs::remove_dir_all(&base);
}

/// CHTYPES_OFFLINE=1 is `--offline` (public issue #528): `list` asks the
/// registry for nothing (no "published" line), and `fetch` with nothing
/// installed is CHTYPES_ARTIFACT_MISSING, exit 1, not a network error, and no
/// cache is made (every request follows the layout's creation). The installed
/// build and the in-process mode are tests/offline_env.rs.
#[test]
fn offline_env_is_the_offline_flag() {
    let base = scratch("offline-env");
    let cache = base.join("cache");
    let env = [
        ("CHTYPES_CACHE", cache.to_str().unwrap()),
        ("CHTYPES_OFFLINE", "1"),
    ];
    let (code, out, err) = run_env(&["list"], &env);
    assert!(
        code == 0 && !out.contains("published"),
        "list under CHTYPES_OFFLINE=1: {code} {out:?} {err}"
    );
    let (code, out, err) = run_env(&["fetch", "26.8", "--platform", "linux-amd64"], &env);
    assert!(
        code == 1 && out.is_empty() && err.contains("CHTYPES_ARTIFACT_MISSING"),
        "fetch under CHTYPES_OFFLINE=1: {code} {out:?} {err}"
    );
    assert!(!cache.exists(), "an offline fetch made {}", cache.display());
    let _ = std::fs::remove_dir_all(&base);
}

/// `resolve` and `prune` usage errors exit 2 with nothing on stdout, and their
/// flags are refused by every other command (public issues #493 and #494).
#[test]
fn resolve_and_prune_usage_errors_exit_two() {
    let base = scratch("resolve-prune-usage");
    let cache = base.join("cache");
    for args in [
        vec!["resolve"],
        vec!["resolve", "26.8", "26.9"],
        vec!["resolve", "v26.8"],
        vec!["resolve", "26.8", "--platform", "linux-arm64"],
        vec!["resolve", "26.8", "--dry-run"],
        vec!["prune", "26.8"],
        vec!["prune", "--keep", "0"],
        vec!["prune", "--keep", "x"],
        vec!["prune", "--line", "26.8.15"],
        vec!["prune", "--line", "v26.8"],
        vec!["prune", "--offline"],
        vec!["prune", "--platform", "linux-arm64"],
        vec!["prune", "--json"],
        vec!["fetch", "26.8", "--json"],
        vec!["list", "--dry-run"],
    ] {
        let (code, out, err) = run_env(&args, &[("CHTYPES_CACHE", cache.to_str().unwrap())]);
        assert!(
            code == 2 && out.is_empty() && !err.is_empty(),
            "{args:?}: {code} stdout {out:?} stderr {err}"
        );
    }
    assert!(!cache.exists(), "a usage error made {}", cache.display());
    let _ = std::fs::remove_dir_all(&base);
}

/// The 64 hex digits of test record `n`'s manifest.
fn hex(n: u64) -> String {
    format!("{n:064x}")
}

/// One record of this binary's own fingerprint, written by hand where the dev
/// CLI reads the explicit cache `cache` (its v2-dev subroot), as an install
/// leaves it; `n` names its manifest. Returns the entry directory.
fn install_record(cache: &Path, n: u64, version: &str, build: &str) -> PathBuf {
    let entry = cache.join(DEV).join("unpacked/sha256").join(hex(n));
    std::fs::create_dir_all(&entry).unwrap();
    let record = serde_json::json!({
        "schema": 2,
        "platform": "linux-arm64",
        "version": version,
        "channel": null,
        "build": build,
        "library": "libchtypes.so",
        "library_sha256": "0".repeat(64),
        "library_bytes": 0,
        "digests": {
            "index": null,
            "manifest": format!("sha256:{}", hex(n)),
            "layer": format!("sha256:{}", hex(n + 1000)),
            "bundle": null,
            "bundle_manifest": null,
        },
        "signed_by": null,
        "predicate": {
            "clickhouse_version": version,
            "build": build,
            "abi_fingerprint": abi_fingerprint::DEV_ABI_FINGERPRINT,
        },
    });
    std::fs::write(
        entry.join("verified.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    entry
}

/// `resolve --offline` answers from the cache, one line per platform it has a
/// build for, and `--json` gives the same rows as one array; with nothing
/// installed it is CHTYPES_ARTIFACT_MISSING (exit 1). `prune` removes the
/// builds of a line newer ones supersede: a dry run names them and removes
/// nothing, `--line` and `--keep` narrow it (public issues #493 and #494). The
/// real binary reaches only the staging registry, so the online answer is the
/// conformance suite's (`resolve-build-*`).
#[test]
fn resolve_offline_and_prune_over_a_dev_cache() {
    let base = scratch("resolve-prune");
    let cache = base.join("cache");
    let c = cache.to_str().unwrap();
    let (code, out, err) = run(&["resolve", "26.8", "--offline", "--cache", c]);
    assert!(
        code == 1 && out.is_empty() && err.contains("CHTYPES_ARTIFACT_MISSING"),
        "resolve --offline with nothing installed: {code} {out:?} {err}"
    );

    let older = install_record(&cache, 1, "26.7.10.3", "20260710.000000");
    let line_newest = install_record(&cache, 2, "26.7.15.5", "20260715.000000");
    let newest = install_record(&cache, 3, "26.8.15.10", "20261001.183455");
    let (code, out, err) = run(&["resolve", "26.8", "--offline", "--cache", c]);
    let want = format!(
        "resolved 26.8.15.10 linux-arm64 20261001.183455 sha256:{}\n",
        hex(3)
    );
    assert_eq!((code, out.as_str()), (0, want.as_str()), "{err}");
    let (code, out, err) = run(&["resolve", "--json", "26.8", "--offline", "--cache", c]);
    let want = format!(
        "[{{\"platform\":\"linux-arm64\",\"version\":\"26.8.15.10\",\"build\":\"20261001.183455\",\"manifest\":\"sha256:{}\"}}]\n",
        hex(3)
    );
    assert_eq!((code, out.as_str()), (0, want.as_str()), "{err}");

    let would = format!("would-prune 26.7.10.3 linux-arm64 {}\n", older.display());
    let (code, out, err) = run(&["prune", "--dry-run", "--cache", c]);
    let summary = format!(
        "chtypes: would prune 1 build(s) under {}",
        cache.join(DEV).display()
    );
    assert!(
        code == 0 && out == would && err.contains(&summary),
        "prune --dry-run: {code} {out:?} {err}"
    );
    assert!(older.exists(), "the dry run removed {}", older.display());
    for args in [
        ["prune", "--line", "26.8", "--cache", c],
        ["prune", "--keep", "2", "--cache", c],
    ] {
        let (code, out, err) = run(&args);
        assert_eq!((code, out.as_str()), (0, ""), "{args:?}: {err}");
    }
    let (code, out, err) = run(&["prune", "--line", "26.7", "--cache", c]);
    assert!(
        code == 0
            && out == would.replacen("would-prune", "pruned", 1)
            && err.contains("chtypes: pruned 1 build(s) under "),
        "prune --line 26.7: {code} {out:?} {err}"
    );
    assert!(!older.exists(), "{} is still there", older.display());
    assert!(line_newest.exists() && newest.exists());
    let (code, out, err) = run(&["list", "--offline", "--cache", c]);
    assert!(
        code == 0
            && !out.contains("26.7.10.3")
            && out.contains("installed 26.7.15.5 ")
            && out.contains("installed 26.8.15.10 "),
        "list --offline after the prune: {code} {out:?} {err}"
    );
    let (code, out, err) = run(&["prune", "--cache", c]);
    assert_eq!(
        (code, out.as_str()),
        (0, ""),
        "a second prune: nothing left to prune: {err}"
    );
    let _ = std::fs::remove_dir_all(&base);
}
