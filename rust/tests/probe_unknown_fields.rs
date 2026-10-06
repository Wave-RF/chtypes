//! PROBE (do not merge): do the RELEASED 1.0.4 decoders tolerate members no
//! 1.0 description names? The "ok-x" stub (scripts/abi-v1/emit/_stubshared.py
//! PROBE_X_DOCS) answers every document-returning call with a document carrying
//! unknown members at every object level; its build_info and live_handles carry
//! unknown members too. Each test decodes one document through the public API
//! and checks the known fields still decode.

use std::path::PathBuf;
use std::sync::{Arc, Once, OnceLock};

use chtypes::{
    CompileOptions, EvalOptions, FilterOptions, FilterOutcome, Format, Library, Outcome,
    RowOptions, RowsOptions,
};

const BODY: &[u8] = br#"{"x":1}"#;
const CREATE: &str = "CREATE TABLE t (x Int32)";

static LIB: OnceLock<Option<Arc<Library>>> = OnceLock::new();
static ENV: Once = Once::new();

/// The stub variant's path, after the one write of the environment this
/// binary makes (every open goes through here, so no thread reads the
/// environment while it is written).
fn variant(name: &str) -> Option<PathBuf> {
    let dir = std::env::var_os("CHTYPES_ABI1_STUBS")?;
    // SAFETY: Once runs this exactly once, and every other thread that opens
    // a library waits in call_once until it has returned.
    ENV.call_once(|| unsafe { std::env::set_var("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1") });
    Some(PathBuf::from(dir).join(format!("{name}.so")))
}

fn probe_lib() -> Option<Arc<Library>> {
    LIB.get_or_init(|| {
        let Some(path) = variant("ok-x") else {
            eprintln!("SKIPPED (loudly): probe_unknown_fields needs CHTYPES_ABI1_STUBS");
            return None;
        };
        Some(Library::open_unverified(&path, true).expect("PROBE-FAIL open ok-x (build_info)"))
    })
    .clone()
}

#[test]
fn probe_build_info() {
    let Some(lib) = probe_lib() else { return };
    let info = lib.build_info();
    assert_eq!(info.clickhouse_version, "26.8.15.10");
    assert_eq!(info.channel, "lts");
    assert_eq!(info.abi, 1);
    assert!(
        info.capabilities
            .features
            .iter()
            .any(|f| f == "default_generators")
    );
}

#[test]
fn probe_live_handles() {
    let Some(lib) = probe_lib() else { return };
    let live = lib.live_handles().expect("PROBE-FAIL live_handles");
    eprintln!("live handles decoded as {live:?}");
    assert!(live.contains_key("chs_schema"));
}

#[test]
fn probe_error_code_table() {
    let Some(lib) = probe_lib() else { return };
    let table = lib.error_codes().expect("PROBE-FAIL error_code_table");
    assert_eq!(table.name(53), Some("TYPE_MISMATCH"));
}

#[test]
fn probe_schema_description() {
    let Some(lib) = probe_lib() else { return };
    let schema = lib
        .compile_table(CREATE, &CompileOptions::default())
        .expect("compile");
    let d = schema.describe().expect("PROBE-FAIL schema_description");
    assert_eq!(d.columns.len(), 1);
    assert_eq!(d.columns[0].name.as_bytes(), b"x");
    assert_eq!(d.columns[0].r#type.as_bytes(), b"Int32");
}

#[test]
fn probe_row() {
    let Some(lib) = probe_lib() else { return };
    let schema = lib
        .compile_table(CREATE, &CompileOptions::default())
        .expect("compile");
    let r = schema
        .row(Format::JsonEachRow, BODY, &RowOptions::default())
        .expect("PROBE-FAIL row");
    assert_eq!(r.outcome, Outcome::Accepted);
    assert_eq!(r.columns.len(), 1);
    assert_eq!(r.columns[0].text.as_bytes(), b"abc");
    assert_eq!(r.input_span.as_ref().map(|s| s.len), Some(3));
    assert_eq!(r.computed.len(), 1);
    assert_eq!(r.transformed.len(), 1);
    assert_eq!(r.unknown_fields.len(), 1);
    assert_eq!(r.unsupported_settings.len(), 1);
}

#[test]
fn probe_batch() {
    let Some(lib) = probe_lib() else { return };
    let schema = lib
        .compile_table(CREATE, &CompileOptions::default())
        .expect("compile");
    let options = RowsOptions {
        export: Some(Format::JsonEachRow),
        ..RowsOptions::default()
    };
    let b = schema
        .rows(Format::JsonEachRow, BODY, &options)
        .expect("PROBE-FAIL batch");
    assert_eq!(b.outcome, Outcome::Accepted);
    assert_eq!(b.rows_read, 1);
    assert_eq!(b.rows.len(), 1);
    assert_eq!(b.rows[0].columns.len(), 1);
    assert_eq!(b.engine_rows.as_ref().map(Vec::len), Some(1));
    assert_eq!(b.spans.as_ref().map(Vec::len), Some(1));
    assert_eq!(b.unconsumed.len(), 1);
    let names = b
        .framing
        .as_ref()
        .and_then(|f| f.header.as_ref())
        .map(|h| h.names.len());
    assert_eq!(names, Some(1));
    assert_eq!(b.payload.as_deref(), Some(&b"{\"s\":\"abc\"}\n"[..]));
}

#[test]
fn probe_filter_result() {
    let Some(lib) = probe_lib() else { return };
    let schema = lib
        .compile_table(CREATE, &CompileOptions::default())
        .expect("compile");
    let filter = schema
        .compile_filter("x > 1", &FilterOptions::default())
        .expect("compile filter");
    let f = filter
        .rows(Format::JsonEachRow, BODY, &EvalOptions::default())
        .expect("PROBE-FAIL filter_result");
    assert_eq!(f.outcome, FilterOutcome::Ok);
    assert_eq!(f.rows_read, 2);
    assert_eq!(f.verdicts.len(), 2);
    assert_eq!(f.errors.len(), 1);
    assert_eq!(f.unsupported_settings.len(), 1);
}

#[test]
fn probe_discovery() {
    let Some(lib) = probe_lib() else { return };
    let d = lib.discover_columns(b"{}").expect("PROBE-FAIL discovery");
    assert_eq!(d.columns.len(), 1);
    assert_eq!(d.columns[0].declaration.as_bytes(), b"c String");
    assert_eq!(d.columns_sql.as_bytes(), b"c String");
}

// --- the second question: an UNKNOWN ENUM VALUE (stub variants ok-e, ok-e-bi).
// Each probe RECORDS what the released decoder did, as a PROBE-ENUM line; it
// asserts nothing beyond "no crash", because every outcome is a finding.

fn report<T: std::fmt::Debug>(id: &str, got: chtypes::Result<T>) {
    match got {
        Ok(v) => eprintln!("PROBE-ENUM rust {id} => {v:?}"),
        Err(e) => eprintln!("PROBE-ENUM rust {id} => ERROR {e:?}"),
    }
}

#[test]
fn probe_unknown_enum_values() {
    let Some(bi_path) = variant("ok-e-bi") else {
        eprintln!("SKIPPED (loudly): probe_unknown_enum_values needs CHTYPES_ABI1_STUBS");
        return;
    };
    report(
        "build_info.capabilities",
        Library::open_unverified(&bi_path, true).map(|l| l.build_info().capabilities.clone()),
    );
    let Some(path) = variant("ok-e") else { return };
    let lib = Library::open_unverified(&path, true).expect("open ok-e");
    report("status.unknown", lib.validate_type(b"!U:"));
    report(
        "describe.default_kind",
        lib.compile_table("!E:describe.default_kind", &CompileOptions::default())
            .and_then(|s| s.describe())
            .map(|d| d.columns[0].default_kind),
    );
    let clean = lib
        .compile_table(CREATE, &CompileOptions::default())
        .expect("compile");
    report(
        "describe.control",
        clean.describe().map(|d| d.columns[0].default_kind),
    );
    let row = |id: &str| {
        let body = format!("!E:{id}");
        clean.row(Format::JsonEachRow, body.as_bytes(), &RowOptions::default())
    };
    report("row.outcome", row("row.outcome").map(|r| r.outcome));
    report(
        "row.cols.src",
        row("row.cols.src").map(|r| (r.columns[0].source.clone(), r.columns[0].is_stored)),
    );
    report(
        "row.transformed.reason",
        row("row.transformed.reason").map(|r| (r.transformed[0].reason.clone(), r.transformed[0].lossy)),
    );
    report("row.verdict", row("row.verdict").map(|r| r.verdict));
    report(
        "row.control",
        clean
            .row(Format::JsonEachRow, b"{}", &RowOptions::default())
            .map(|r| r.outcome),
    );
    let options = RowsOptions {
        export: Some(Format::JsonEachRow),
        ..RowsOptions::default()
    };
    let batch = |id: &str| {
        let body = format!("!E:{id}");
        clean.rows(Format::JsonEachRow, body.as_bytes(), &options)
    };
    report("batch.outcome", batch("batch.outcome").map(|b| b.outcome));
    report(
        "batch.rows.outcome",
        batch("batch.rows.outcome").map(|b| b.rows[0].outcome),
    );
    report(
        "batch.rows.cols.src",
        batch("batch.rows.cols.src").map(|b| {
            let c = &b.rows[0].columns[0];
            (c.source.clone(), c.is_stored)
        }),
    );
    report(
        "batch.transformed.reason",
        batch("batch.transformed.reason").map(|b| (b.transformed[0].reason.clone(), b.transformed[0].lossy)),
    );
    report(
        "batch.framing.container",
        batch("batch.framing.container").map(|b| b.framing.and_then(|f| f.container)),
    );
    report("batch.control", batch("batch.control").map(|b| b.outcome));
    let filter = clean
        .compile_filter("x > 1", &FilterOptions::default())
        .expect("compile filter");
    for id in ["filter.outcome", "filter.verdicts", "filter.control"] {
        let body = if id == "filter.control" {
            "{}".to_string()
        } else {
            format!("!E:{id}")
        };
        report(
            id,
            filter
                .rows(Format::JsonEachRow, body.as_bytes(), &EvalOptions::default())
                .map(|f| (f.outcome, f.verdicts)),
        );
    }
    report(
        "discovery.default_kind",
        lib.discover_columns(b"!E:discovery.default_kind")
            .map(|d| d.columns),
    );
}
