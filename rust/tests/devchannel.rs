//! The ABI v2 dev channel (`src/ocifetch/channel.rs`; spec/abi-v2/docs.md,
//! rules r5 and r6), tested as a user's build speaks it: no test here selects a
//! seam except the one control that reads a 1.x cache under the v1 contract.
//! Nothing here reaches the network: every refusal is proven to come before the
//! layout is made, which every request follows, and every cache read is
//! offline. Nothing here sets an environment variable, so these tests run in
//! parallel with the fetch layer's own unit tests this file recompiles; the
//! environment overrides are proven against the real binary
//! (tests/cli_v1.rs).
//!
//! WHY THIS FILE RE-DECLARES `src/ocifetch` WITH `#[path]`. The fetch layer is
//! not public API (see `src/ocifetch/mod.rs`), so this suite compiles it under
//! its own crate root, the same way tests/ocifetch_conformance.rs does.

#[path = "../src/ocifetch/mod.rs"]
mod ocifetch;

use std::path::{Path, PathBuf};

use ocifetch::channel::{self, DEV_CACHE_DIR, DEV_CHANNEL_BASE, DEV_KEY_HEX, DEV_KEY_ID};
use ocifetch::dsse;
use ocifetch::ensure::{self, Options};
use ocifetch::error::Error;
use ocifetch::layout::{self, VerifiedRecord};
use ocifetch::oci::VersionRequest;

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "devchannel-{name}-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

/// One pinning request's options, made for a lock path.
type MakeOptions = fn(&Path) -> Options;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// r6: the base, the key and its id are the ones the rule pins, and the
/// release key is not in the dev trust list.
#[test]
fn the_dev_channel_pins_the_staging_base_and_key() {
    assert_eq!(
        DEV_CHANNEL_BASE,
        "https://registry-staging.wavehouse.dev/chtypes/v2-dev"
    );
    assert_eq!(
        DEV_KEY_HEX,
        "5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c"
    );
    // The id is KEYID_ALGORITHM (sha256-first16hex) over the raw key.
    let computed = dsse::TrustedKey::from_hex(DEV_KEY_HEX).expect("the staging key parses");
    assert_eq!(computed.keyid, DEV_KEY_ID);
    assert_eq!(DEV_KEY_ID, "824345f9bcf8e5bf");

    let a = channel::active();
    assert_eq!((a.name, a.abi, a.record_schema), ("v2-dev", 2, 2));
    assert!(!a.overridable && !a.pinnable);
    assert_eq!(
        ensure::configured_bases(&Options::default()),
        vec![DEV_CHANNEL_BASE.to_string()]
    );
    let trust = dsse::trusted_keys(None).expect("the default trust");
    assert_eq!(
        trust.len(),
        1,
        "the dev trust list holds the staging key alone"
    );
    assert_eq!(
        (trust[0].keyid.as_str(), hex(&trust[0].key)),
        (DEV_KEY_ID, DEV_KEY_HEX.to_string())
    );
    for rk in ocifetch::constants::RELEASE_KEYS {
        assert!(
            trust.iter().all(|k| hex(&k.key) != rk.ed25519_hex),
            "the release key {} is in the dev trust list",
            rk.keyid
        );
    }
}

/// r6: no override. Every base, trust and unsigned option is ignored, and
/// each is named exactly once per process, however many calls see it.
#[test]
fn the_dev_channel_ignores_every_override_option_loudly_once() {
    channel::capture_warnings_for_tests();
    let test_key = ocifetch::constants::TEST_KEYS[0].ed25519_hex.to_string();
    let cache = scratch("overrides");
    let options = || Options {
        bases: Some(vec!["http://127.0.0.1:9/x".to_string()]),
        trusted_keys: Some(vec![test_key.clone()]),
        allow_unsigned: true,
        cache_dir: Some(cache.to_string_lossy().into_owned()),
        system_dirs: Vec::new(),
        ..Options::default()
    };
    for _ in 0..3 {
        let found = ensure::resolve_installed("26.8", "linux-amd64", options())
            .expect("an offline lookup in an empty dev cache");
        assert!(found.is_none());
        assert_eq!(
            ensure::configured_bases(&options()),
            vec![DEV_CHANNEL_BASE.to_string()],
            "a base override was honored"
        );
        let trust = dsse::trusted_keys(Some(std::slice::from_ref(&test_key))).unwrap();
        assert_eq!(
            trust.iter().map(|k| hex(&k.key)).collect::<Vec<_>>(),
            vec![DEV_KEY_HEX.to_string()],
            "a trust override was honored"
        );
        assert!(
            !channel::allow_unsigned(true),
            "an unsigned override was honored"
        );
    }
    let want = [
        "the allow_unsigned option",
        "the bases option",
        "the trusted_keys option",
    ];
    // The environment's own overrides, if a job sets any, are named too
    // (tests/cli_v1.rs proves each against the real binary); these are the
    // options'.
    let warned = channel::warned_for_tests();
    for s in want {
        assert!(
            warned.iter().any(|w| w == s),
            "{s} was not warned about: {warned:?}"
        );
    }
    let text = channel::warning_text_for_tests();
    for s in want {
        let n = text
            .matches(&format!("WARNING: {s} is set and IGNORED"))
            .count();
        assert_eq!(
            n, 1,
            "{s} was warned about {n} times, want exactly once:\n{text}"
        );
    }
    assert!(
        text.contains(DEV_CHANNEL_BASE) && text.contains(DEV_KEY_ID),
        "the warning does not name the base and the key it uses instead:\n{text}"
    );
    assert!(!cache.exists(), "a lookup made the cache");
}

/// r6: `lock`, `frozen` and `update`, as options, are refused before anything
/// else, with the dev-channel reason: a lock that is not even a lock is never
/// read, and the cache layout every request follows is never made.
#[test]
fn the_dev_channel_refuses_pinning_before_any_request() {
    let dir = scratch("pinning");
    std::fs::create_dir_all(&dir).unwrap();
    let lock = dir.join("chtypes.lock");
    std::fs::write(&lock, b"not a lock").unwrap();
    let cache = dir.join("cache");
    let cases: [(&str, MakeOptions); 5] = [
        ("frozen", |_| Options {
            frozen: true,
            ..Options::default()
        }),
        ("lock write", |l| Options {
            lock_write: true,
            lock_path: Some(l.to_path_buf()),
            ..Options::default()
        }),
        ("update", |l| Options {
            update: true,
            lock_path: Some(l.to_path_buf()),
            ..Options::default()
        }),
        ("a lock path", |l| Options {
            lock_path: Some(l.to_path_buf()),
            ..Options::default()
        }),
        ("frozen with a lock", |l| Options {
            frozen: true,
            lock_path: Some(l.to_path_buf()),
            ..Options::default()
        }),
    ];
    for (name, make) in cases {
        let options = || Options {
            cache_dir: Some(cache.to_string_lossy().into_owned()),
            platform: Some("linux-amd64".to_string()),
            ..make(&lock)
        };
        assert!(channel::refuse_pinning(&options()).is_some(), "{name}");
        match ensure::ensure("26.8", options()) {
            Err(Error::InvalidInput(m)) => assert!(
                m.contains("refused by a 2.0.0-dev SDK") && m.contains("14 days"),
                "{name}: the refusal does not say why: {m}"
            ),
            Err(other) => panic!("{name}: want the pinning refusal, got {other:?}"),
            Ok(r) => panic!("{name}: a pinning request was honored: {}", r.dir.display()),
        }
        assert!(
            !cache.exists(),
            "{name}: the layout was made before the refusal"
        );
    }
    assert_eq!(std::fs::read(&lock).unwrap(), b"not a lock");
    let _ = std::fs::remove_dir_all(&dir);
}

/// r5: an explicit cache is used through the v2-dev subroot, never as a whole
/// layout; the default root and the system directories are v2-dev ones.
#[test]
fn the_dev_channel_cache_roots() {
    let opt = scratch("roots");
    assert_eq!(
        layout::cache_root(Some(&opt.to_string_lossy())).unwrap(),
        opt.join(DEV_CACHE_DIR)
    );
    // The default root (or CHTYPES_CACHE's subroot, when the environment
    // names one) ends in v2-dev either way; the binary's own run pins each
    // spelling (tests/cli_v1.rs).
    let default_root = layout::cache_root(None).expect("a default root");
    assert!(
        default_root.ends_with(DEV_CACHE_DIR),
        "{}",
        default_root.display()
    );
    let dirs = Options::default().system_dirs;
    assert!(!dirs.is_empty());
    for d in dirs {
        assert!(
            d.ends_with(DEV_CACHE_DIR),
            "system dir {} is not a v2-dev dir: a 1.x system dir is never read",
            d.display()
        );
    }
}

fn sample_record() -> VerifiedRecord {
    VerifiedRecord {
        platform: "linux-amd64".to_string(),
        version: "26.8.15.10".to_string(),
        build: "20261001.000000".to_string(),
        channel: None,
        index_digest: None,
        manifest_digest: format!("sha256:{}", "b".repeat(64)),
        layer_digest: format!("sha256:{}", "c".repeat(64)),
        bundle_digest: None,
        bundle_manifest_digest: None,
        signed_by: DEV_KEY_ID.to_string(),
        library: "libchtypes.so".to_string(),
        library_sha256: "a".repeat(64),
        library_bytes: 1,
        predicate: serde_json::json!({"abi": 2}),
    }
}

/// r5: the dev channel writes schema-2 records and reads only those; a
/// schema-1 record (every released 1.x writer's) is absent to it; and what it
/// lists says abi 2.
#[test]
fn the_dev_channel_records_are_schema_2() {
    let bytes = sample_record().to_json_bytes().unwrap();
    let text = String::from_utf8(bytes.clone()).unwrap();
    assert!(
        text.contains("\"schema\":2"),
        "the dev channel wrote {text}"
    );
    VerifiedRecord::from_json_bytes(&bytes).expect("the dev channel reads its own record");
    let one = text.replacen("\"schema\":2", "\"schema\":1", 1);
    assert!(
        VerifiedRecord::from_json_bytes(one.as_bytes()).is_err(),
        "the dev channel accepted a schema-1 record: a 1.x record must read as absent"
    );

    let cache = scratch("records");
    let root = layout::cache_root(Some(&cache.to_string_lossy())).unwrap();
    let entry = root
        .join(ocifetch::constants::CACHE_UNPACKED_DIR)
        .join("b".repeat(64));
    layout::write_atomic(&entry.join("libchtypes.so"), b"x").unwrap();
    layout::write_atomic(
        &entry.join(ocifetch::constants::CACHE_VERIFIED_RECORD),
        &bytes,
    )
    .unwrap();
    let listed = ensure::list_installed(Options {
        cache_dir: Some(cache.to_string_lossy().into_owned()),
        system_dirs: Vec::new(),
        ..Options::default()
    })
    .unwrap();
    assert_eq!(listed.len(), 1, "the dev channel lists its own record");
    assert_eq!(listed[0].abi_generation, 2, "a Resolved names abi 2");
    let _ = std::fs::remove_dir_all(&cache);
}

/// r6: a signed predicate must say abi 2; an abi-1 one (a 1.x build) is
/// refused even when every other field matches.
#[test]
fn the_dev_channel_predicate_is_abi_2() {
    let request = VersionRequest::parse("26.8").unwrap();
    let mut predicate = serde_json::json!({
        "abi": 1,
        "os": "linux",
        "arch": "arm64",
        "clickhouse_version": "26.8.15.10",
    });
    match ensure::validate_predicate(&predicate, "linux-arm64", Some(&request)) {
        Err(Error::ArtifactCorrupt(m)) => assert!(m.contains("want the integer 2"), "{m}"),
        other => panic!("an abi-1 predicate = {other:?}; want it refused as not abi 2"),
    }
    predicate["abi"] = serde_json::json!(2);
    ensure::validate_predicate(&predicate, "linux-arm64", Some(&request))
        .expect("an abi-2 predicate is accepted");
}

fn copy_dir(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let to = dst.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_dir(&entry.path(), &to);
        } else {
            std::fs::copy(entry.path(), &to).unwrap();
        }
    }
}

/// r5, end to end: a cache a 1.x binding wrote is never what the dev channel
/// reads, whether the explicit cache names it (the dev channel reads its
/// subroot) or the 1.x layout sits in the subroot itself (its schema-1 records
/// are absent). The control is the same cache read under the v1 contract.
#[test]
fn the_dev_channel_never_reads_a_v1_cache() {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../tests/fixtures/fetch-v1/layouts/cache-record-canonical");
    assert!(fixture.is_dir(), "{} is missing", fixture.display());
    let cache = scratch("v1-cache");
    copy_dir(&fixture, &cache);
    copy_dir(&fixture, &cache.join(DEV_CACHE_DIR));
    let options = || Options {
        cache_dir: Some(cache.to_string_lossy().into_owned()),
        system_dirs: Vec::new(),
        ..Options::default()
    };

    let v1 = {
        let _v1 = channel::use_fetch_v1_for_tests();
        ensure::list_installed(options()).expect("the control lists")
    };
    assert!(
        !v1.is_empty(),
        "control: the v1 contract lists nothing in the 1.x cache; the fixture is not a 1.x cache"
    );

    assert_eq!(channel::active().name, "v2-dev");
    let dev = ensure::list_installed(options()).expect("the dev channel lists");
    assert!(
        dev.is_empty(),
        "the dev channel listed {} build(s) from a 1.x cache",
        dev.len()
    );
    for r in &v1 {
        let got = ensure::resolve_installed(&r.version, &r.platform, options())
            .expect("an offline lookup");
        assert!(
            got.is_none(),
            "the dev channel resolved {} from a 1.x cache",
            r.version
        );
    }
    let _ = std::fs::remove_dir_all(&cache);
}

/// The alias step (docs/guides/fetch-v1.md §3): the dev channel names its alias
/// with its OWN fingerprint, the generated constant, which is the header's;
/// only a version spelling gets one; the v1 contract (the control, under its
/// seam) has none.
#[test]
fn the_dev_channel_aliases_with_its_own_generated_fingerprint() {
    let fp = ocifetch::abi_fingerprint::DEV_ABI_FINGERPRINT;
    let hex = fp
        .strip_prefix("sha256:")
        .expect("the fingerprint is sha256:<hex>");
    assert!(
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "{fp} is not sha256: and 64 lowercase hex"
    );
    let header = Path::new(env!("CARGO_MANIFEST_DIR")).join("../include/v2/chtypes.h");
    match std::fs::read_to_string(&header) {
        Ok(text) => assert!(
            text.contains(&format!("#define CHS_ABI_FINGERPRINT \"{fp}\"")),
            "{} does not define CHS_ABI_FINGERPRINT as {fp}",
            header.display()
        ),
        Err(e) => eprintln!(
            "SKIPPED the header cross-check: {} is not beside this checkout ({e})",
            header.display()
        ),
    }
    let alias_of = |spelling: &str| {
        channel::alias_tag(&VersionRequest::parse(spelling).expect("the spelling parses"))
    };
    for tag in ["26.9", "26.9.8", "26.9.8.3"] {
        assert_eq!(alias_of(tag), Some(format!("{tag}--fp-{hex}")));
    }
    assert_eq!(alias_of("26.9").map(|a| a.len()), Some(73));
    assert_eq!(alias_of("latest"), None);
    let _v1 = channel::use_fetch_v1_for_tests();
    assert_eq!(alias_of("26.9"), None);
}

/// A clock whose sleeps return at once, so the retry table runs instantly.
struct NoSleep;

impl ocifetch::http::Clock for NoSleep {
    fn now(&self) -> std::time::SystemTime {
        std::time::SystemTime::now()
    }

    fn sleep(&self, _seconds: f64) {}
}

/// A loopback registry (127.0.0.1 only) that answers `alias_path` with
/// `alias_status` and every other GET with 404, each with an empty body, and
/// records every path it was asked for, in order.
fn loopback_registry(
    alias_path: String,
    alias_status: u16,
) -> (u16, std::sync::Arc<std::sync::Mutex<Vec<String>>>) {
    use std::io::{BufRead, BufReader, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind a loopback port");
    let port = listener.local_addr().expect("the bound address").port();
    let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let Ok(clone) = stream.try_clone() else {
                continue;
            };
            let mut reader = BufReader::new(clone);
            let mut request_line = String::new();
            if reader.read_line(&mut request_line).is_err() {
                continue;
            }
            loop {
                let mut header = String::new();
                match reader.read_line(&mut header) {
                    Ok(0) | Err(_) => break,
                    Ok(_) if header == "\r\n" => break,
                    Ok(_) => {}
                }
            }
            let path = request_line
                .split_whitespace()
                .nth(1)
                .unwrap_or_default()
                .to_string();
            let status = if path == alias_path {
                alias_status
            } else {
                404
            };
            log.lock().unwrap().push(path);
            let _ = write!(
                stream,
                "HTTP/1.1 {status} Scripted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            );
        }
    });
    (port, seen)
}

/// The dev channel itself (its base overridden to a loopback registry with no
/// alias): the FIRST manifest request is its own alias, and the tag follows
/// only on a 404; a 5xx on the alias is that failure, after the retry table,
/// with the tag never requested.
#[test]
fn the_dev_channel_requests_its_own_alias_first() {
    let _overrides = channel::allow_overrides_for_tests();
    let hex = ocifetch::abi_fingerprint::DEV_ABI_FINGERPRINT
        .strip_prefix("sha256:")
        .expect("the fingerprint is sha256:<hex>");
    let alias = format!("/v2/chtypes/v2-dev/manifests/26.9--fp-{hex}");
    let tag = "/v2/chtypes/v2-dev/manifests/26.9".to_string();
    for (alias_status, attempts, code) in [
        (404, 1, "CHTYPES_ARTIFACT_UNPUBLISHED"),
        (503, 5, "CHTYPES_SOURCE_UNREACHABLE"),
    ] {
        let (port, seen) = loopback_registry(alias.clone(), alias_status);
        let cache = scratch(&format!("alias-first-{alias_status}"));
        let result = ensure::ensure(
            "26.9",
            Options {
                platform: Some("linux-amd64".to_string()),
                bases: Some(vec![format!("http://127.0.0.1:{port}/chtypes/v2-dev")]),
                cache_dir: Some(cache.to_string_lossy().into_owned()),
                system_dirs: Vec::new(),
                clock: Some(Box::new(NoSleep)),
                ..Options::default()
            },
        );
        let err = match result {
            Ok(_) => panic!("alias {alias_status}: resolved, though nothing is published"),
            Err(e) => e,
        };
        assert_eq!(err.code(), code, "alias {alias_status}: {err}");
        let mut want = vec![alias.clone(); attempts];
        if alias_status == 404 {
            want.push(tag.clone());
        }
        assert_eq!(*seen.lock().unwrap(), want, "alias {alias_status}");
        let _ = std::fs::remove_dir_all(&cache);
    }
}
