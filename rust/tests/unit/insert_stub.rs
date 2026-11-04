//! `Schema::insert` over the generated ABI v2 stub: the arguments reach
//! `chs_preview_insert` (the stub echoes them into `out`, as the
//! conformance cases assert), and the result decodes. This is a unit test of
//! the crate because the echo is only visible below the public decoder
//! (`Schema::insert_raw`).
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes.

use std::path::{Path, PathBuf};

use serde_json::Value as Json;
use sha2::{Digest, Sha256};

use crate::library::open_image;
use crate::{CompileOptions, Error, FilterOptions, Format, InsertOptions, Outcome};

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";

fn sha(b: &[u8]) -> String {
    Sha256::digest(b)
        .iter()
        .map(|x| format!("{x:02x}"))
        .collect()
}

fn args(doc: &[u8]) -> Vec<Json> {
    let v: Json = serde_json::from_slice(doc).expect("the echo is JSON");
    assert_eq!(v["fn"], "chs_preview_insert", "{v}");
    v["args"].as_array().expect("echo args").clone()
}

#[test]
fn insert_reaches_the_abi_with_its_arguments_and_decodes_over_the_stub() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): insert_stub needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return;
    };
    let manifest: Json =
        serde_json::from_str(&std::fs::read_to_string(dir.join("stubs.json")).unwrap()).unwrap();
    let file = manifest["variants"]["ok"]["path"]
        .as_str()
        .expect("ok variant");
    let lib = open_image(&dir.join(Path::new(file).file_name().unwrap()), None, None)
        .expect("the ok stub opens");
    let schema = lib
        .compile_table(
            "CREATE TABLE t (x Int32) ENGINE = Memory",
            &CompileOptions::default(),
        )
        .unwrap();
    let body: &[u8] = br#"{"x":1}"#;

    // No filter (NULL), an output format, memory_limit_bytes in the options.
    let options = InsertOptions {
        memory_limit_bytes: Some(1234),
        columns: vec![b"x".to_vec()],
        ..Default::default()
    };
    let (doc, rows) = schema
        .insert_raw(Format::JsonEachRow, body, Some(Format::Csv), &options)
        .unwrap();
    let a = args(&doc);
    assert_eq!(a[1], Format::JsonEachRow.code());
    assert_eq!(a[2]["sha256"], sha(body));
    assert!(a[5].is_null(), "no filter is NULL: {}", a[5]);
    assert_eq!(a[6], Format::Csv.code());
    assert_eq!(a[7]["sha256"], sha(br#"{"memory_limit_bytes":"1234"}"#));
    assert_eq!(a[4]["sha256"], sha(br#"[{"name":"x"}]"#));
    let rows = rows.expect("out_rows is returned for an output format");
    assert_eq!(args(&rows)[6], Format::Csv.code(), "out_rows echoes too");

    // No output format: validate only.
    let (doc, _) = schema
        .insert_raw(Format::JsonEachRow, body, None, &InsertOptions::default())
        .unwrap();
    assert_eq!(args(&doc)[6], crate::abi2::vocab_gen::EXPORT_NONE);
    assert!(
        args(&doc)[7]["len"] == 0,
        "an empty options document is not sent"
    );

    // A filter: set, not NULL; the public call decodes the echo (not an
    // insert_result) as an unknown outcome, never an error.
    let filter = schema
        .compile_filter("x = 1", &FilterOptions::default())
        .unwrap();
    let options = InsertOptions {
        filter: Some(filter),
        ..Default::default()
    };
    let (doc, _) = schema
        .insert_raw(Format::JsonEachRow, body, None, &options)
        .unwrap();
    assert!(
        args(&doc)[5].get("kind").is_some(),
        "a filter is a handle: {}",
        args(&doc)[5]
    );
    let result = schema
        .insert(
            Format::JsonEachRow,
            body,
            Some(Format::JsonEachRow),
            &options,
        )
        .expect("the echo decodes");
    assert_eq!(result.outcome, Outcome::Unknown(String::new()));
    assert!(result.output.is_some());

    // Misuse is raised before any call.
    let bad = InsertOptions {
        insert_options_extra: vec![("k".into(), "not json".into())],
        ..Default::default()
    };
    let err = schema
        .insert(Format::JsonEachRow, body, None, &bad)
        .unwrap_err();
    assert!(matches!(err, Error::Usage(_)), "{err:?}");
}
