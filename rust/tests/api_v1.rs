//! The public API (the v1 shape, at ABI v2), end to end over the ABI v2 stub libraries
//! (`scripts/abi-v1/build-stubs.sh`): the objects, their lifetimes, the error
//! classes, the loader refusals, and the zero-live-handles proof.
//!
//! The stub is an echo and status-injection library, so what is proved here is
//! the PLUMBING of every public call (one ABI call, the copy-then-free
//! read-out, the error mapping, the handle lifetimes), never ClickHouse
//! behavior. Document decoding is proved by the decoder's own unit tests over
//! documents of the described shape.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes,
//! the same discipline every v1 conformance runner follows.
//!
//! One test function, deliberately: `setup` is process-wide, and the order of
//! its steps is part of what is proved (the first open commits the empty
//! setup).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chtypes::{
    CompileOptions, Error, EvalOptions, FilterOptions, Format, Library, RowOptions, RowsOptions,
    SetupOptions, status,
};
use serde_json::Value;

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const ENV_UNVERIFIED: &str = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY";

fn stubs() -> Option<(PathBuf, serde_json::Map<String, Value>)> {
    let dir = PathBuf::from(std::env::var_os(ENV_STUBS)?);
    let text = std::fs::read_to_string(dir.join("stubs.json")).expect("read stubs.json");
    let manifest: Value = serde_json::from_str(&text).expect("parse stubs.json");
    let variants = manifest
        .get("variants")
        .and_then(Value::as_object)
        .expect("stubs.json has variants")
        .clone();
    Some((dir, variants))
}

fn variant_path(dir: &Path, variants: &serde_json::Map<String, Value>, name: &str) -> PathBuf {
    let path = variants[name]["path"].as_str().expect("variant path");
    dir.join(Path::new(path).file_name().expect("file name"))
}

/// The stub's status injection: a first `bytes_in` that begins
/// `!S:<status>:<ch_code>:<ch_name>:<message>`.
fn inject(status: &str, code: i32, name: &str, message: &str) -> Vec<u8> {
    format!("!S:{status}:{code}:{name}:{message}").into_bytes()
}

fn assert_all_zero(live: &BTreeMap<String, u64>, when: &str) {
    for (kind, n) in live {
        assert_eq!(
            *n, 0,
            "{when}: {kind} still has {n} live handle(s): {live:?}"
        );
    }
}

#[test]
fn the_public_api_over_the_stub() {
    let Some((dir, variants)) = stubs() else {
        eprintln!(
            "SKIPPED (loudly): api_v1 needs {ENV_STUBS} (the v1-abi-stubs directory); nothing was exercised"
        );
        return;
    };
    let ok_path = variant_path(&dir, &variants, "ok");

    // --- the unverified open needs both opt-ins -----------------------------
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::remove_var(ENV_UNVERIFIED) };
    let refused = Library::open_unverified(&ok_path, true).unwrap_err();
    assert!(matches!(refused, Error::Usage(_)), "{refused:?}");
    // SAFETY: as above.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };
    let refused = Library::open_unverified(&ok_path, false).unwrap_err();
    assert!(matches!(refused, Error::Usage(_)), "{refused:?}");

    // --- the loader's refusals map by the generated table --------------------
    // Run before the good open so a refusal is shown not to poison later opens.
    let mut checked = 0;
    for (name, v) in &variants {
        let reason = v["reason"].as_str().expect("variant reason");
        // A reason only a signed statement can trip (glibc, a predicate, a
        // cross-check mismatch) cannot be reached without one.
        let reachable = matches!(
            reason,
            "not_v1" | "abi_version" | "build_info_malformed" | "fingerprint" | "dlopen"
        ) || reason.starts_with("missing_symbol:");
        if !reachable {
            continue;
        }
        let err =
            Library::open_unverified(variant_path(&dir, &variants, name), true).expect_err(name);
        let (refusal, want_corrupt) = match &err {
            Error::ArtifactIncompatible(r) => (r, false),
            Error::ArtifactCorrupt(r) => (r, true),
            other => panic!("{name}: want an artifact error, got {other:?}"),
        };
        assert_eq!(refusal.reason, reason, "{name}");
        assert_eq!(
            want_corrupt,
            reason == "build_info_malformed",
            "{name}: {err:?}"
        );
        checked += 1;
    }
    assert!(checked >= 8, "only {checked} refusal variants ran");

    // --- the first open commits the empty setup ------------------------------
    let lib = Library::open_unverified(&ok_path, true).expect("the ok stub loads");
    assert!(
        lib.resolved().is_none(),
        "an unverified open has no fetch record"
    );
    assert!(
        chtypes::setup(SetupOptions::default()).is_ok(),
        "the empty setup is the one in effect"
    );
    let differs = chtypes::setup(SetupOptions {
        timezone: Some("Asia/Tokyo".to_string()),
        defaults: Vec::new(),
    })
    .unwrap_err();
    assert!(matches!(differs, Error::Usage(_)), "{differs:?}");

    // One image per file: a second open of the same file is the same Library.
    let again = Library::open_unverified(&ok_path, true).unwrap();
    assert!(Arc::ptr_eq(&lib, &again));

    // --- what the library is --------------------------------------------------
    let info = lib.build_info();
    // This binding speaks ABI v2 (spec/binding-majors.json), and so do the stubs it loads.
    assert_eq!(info.abi, 2);
    assert!(info.abi_fingerprint.starts_with("sha256:"));
    assert_eq!(lib.version(), info.clickhouse_version);
    assert_eq!(lib.minor(), info.clickhouse_minor);
    assert!(!info.raw.is_empty());

    // --- bytes are bytes: every byte survives the round trip ------------------
    let echoed = lib.validate_type([0x61, 0x00, 0xff, 0x7a]).unwrap();
    let doc: Value = serde_json::from_slice(echoed.as_bytes()).expect("the echo is JSON");
    assert_eq!(doc["fn"], "chs_type_validate");
    assert_eq!(doc["args"][0]["head_hex"], "6100ff7a");
    assert_eq!(doc["args"][0]["len"], 4);
    // A str input is its UTF-8 bytes; an empty input is length 0.
    let echoed = lib.quote_literal("").unwrap();
    let doc: Value = serde_json::from_slice(echoed.as_bytes()).unwrap();
    assert_eq!(doc["args"][0]["len"], 0);

    // --- one error table: every status, all five fields, verbatim -------------
    for (name, class) in [
        ("CHS_REJECTED", 1),
        ("CHS_DECLINED", 2),
        ("CHS_INVALID_ARGUMENT", 3),
        ("CHS_INTERNAL", 4),
    ] {
        let err = lib
            .validate_type(inject(name, 77, "TEST_CODE", "forced"))
            .unwrap_err();
        let call = err
            .call_error()
            .unwrap_or_else(|| panic!("{name}: {err:?}"));
        assert_eq!(call.status, class, "{name}");
        assert_eq!(call.ch_code, 77, "{name}");
        assert_eq!(call.ch_name, "TEST_CODE", "{name}");
        assert_eq!(call.message.as_bytes(), b"forced", "{name}");
        assert!(call.column.is_empty(), "{name}");
        let class_ok = match class {
            status::REJECTED => matches!(err, Error::Schema(_)),
            status::DECLINED => matches!(err, Error::Unsupported(_)) && err.is_unsupported(),
            status::INVALID_ARGUMENT => matches!(err, Error::Usage(_)),
            _ => matches!(err, Error::Internal(_)),
        };
        assert!(class_ok, "{name} mapped to {err:?}");
    }

    // --- schema, filter, block: the plumbing of every call -------------------
    let schema = lib
        .compile_table(
            "CREATE TABLE t (x Int32) ENGINE = Memory",
            &CompileOptions::default(),
        )
        .unwrap();
    schema.describe().expect("describe decodes the document");
    let row = schema
        .row(Format::JsonEachRow, br#"{"x":1}"#, &RowOptions::default())
        .expect("row decodes the document");
    // The stub's document names no outcome: the empty spelling is not one the
    // description lists, so it is the vocabulary's `Unknown` (rule r3), never
    // an acceptance.
    assert_eq!(row.outcome, chtypes::Outcome::Unknown(String::new()));
    assert!(!row.outcome.is_known());
    let filter = schema
        .compile_filter("x = 1", &FilterOptions::default())
        .unwrap();
    let block = schema
        .parse_block(Format::JsonEachRow, br#"{"x":1}"#, &RowOptions::default())
        .unwrap();
    filter.eval(&block).expect("eval over a block");
    filter
        .rows(Format::JsonEachRow, br#"{"x":1}"#, &EvalOptions::default())
        .expect("eval over a body");
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"x":1}"#,
            &RowsOptions {
                filter: Some(filter.clone()),
                export: Some(Format::JsonEachRow),
                ..Default::default()
            },
        )
        .expect("rows with a filter and an export");
    assert_eq!(batch.outcome, chtypes::Outcome::Unknown(String::new()));

    // The per-call zone: written into the settings, and never twice.
    let both = schema
        .row(
            Format::JsonEachRow,
            b"{}",
            &RowOptions {
                settings: vec![("session_timezone".into(), "UTC".into())],
                session_timezone: Some("UTC".into()),
                ..Default::default()
            },
        )
        .unwrap_err();
    assert!(matches!(both, Error::Usage(_)), "{both:?}");

    // --- a handle from another library is the library's to refuse -------------
    let other = Library::open_unverified(variant_path(&dir, &variants, "ok-b"), true).unwrap();
    assert!(!Arc::ptr_eq(&lib, &other));
    let other_schema = other
        .compile_table(
            "CREATE TABLE t (x Int32) ENGINE = Memory",
            &CompileOptions::default(),
        )
        .unwrap();
    let other_block = other_schema
        .parse_block(Format::JsonEachRow, b"{}", &RowOptions::default())
        .unwrap();
    let cross = filter.eval(&other_block).unwrap_err();
    assert!(matches!(cross, Error::Usage(_)), "{cross:?}");

    // --- every handle is shareable, and calls run concurrently ----------------
    let schema = Arc::new(schema);
    let workers: Vec<_> = (0..8)
        .map(|_| {
            let schema = Arc::clone(&schema);
            let filter = filter.clone();
            std::thread::spawn(move || {
                for _ in 0..25 {
                    schema
                        .row(Format::JsonEachRow, b"{}", &RowOptions::default())
                        .expect("row");
                    filter
                        .rows(Format::JsonEachRow, b"{}", &EvalOptions::default())
                        .expect("filter rows");
                }
            })
        })
        .collect();
    for w in workers {
        w.join().expect("a worker panicked");
    }

    // --- zero live handles: abandon everything, then every count is zero ------
    let during = lib.live_handles().unwrap();
    assert_eq!(during.get("chs_schema"), Some(&1), "{during:?}");
    assert!(
        during.get("chs_filter").copied().unwrap_or(0) >= 1,
        "{during:?}"
    );
    assert_eq!(during.get("chs_block"), Some(&1), "{during:?}");
    // Closing order never matters: the schema goes first, its filter and block
    // keep working and are freed after.
    drop(schema);
    filter
        .eval(&block)
        .expect("a filter outlives its schema handle");
    drop(batch);
    drop(row);
    drop(filter);
    drop(block);
    // The other library's handles are in the other image; free them too.
    drop(other_block);
    drop(other_schema);
    assert_all_zero(&lib.live_handles().unwrap(), "after dropping every handle");
    assert_all_zero(
        &other.live_handles().unwrap(),
        "the other image, after the same",
    );

    // --- the error-code table is the library's own document -------------------
    // The stub answers every document call with its echo, which is not an
    // array: the decoder must call that what it is (a library bug), and keep
    // nothing on failure.
    match lib.error_codes() {
        Ok(table) => assert!(std::ptr::eq(table, lib.error_codes().unwrap())),
        Err(e) => assert!(matches!(e, Error::Internal(_)), "{e:?}"),
    }
}
