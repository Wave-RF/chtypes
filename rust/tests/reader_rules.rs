//! ABI v2's reader rules and the dev fingerprint (spec/abi-v2/docs.md), through
//! the public API, over the generated v2 stub (`scripts/abi-v1/build-stubs.sh`):
//!
//! * r2: a member the description does not name is ignored at every object
//!   level, `_b64` members included, in every result document and in
//!   `build_info` (stub variant `r2-unknown-members`);
//! * r3: a value a vocabulary does not list is kept as that vocabulary's
//!   `Unknown`, for that field alone, and never fails the document, the row or
//!   the batch; the fallback's fail-closed reading stays (stub variant
//!   `r3-unknown-values`, one planted value per `!E:<id>` body, and
//!   `r3-unknown-capabilities`); a call status outside the closed set is an
//!   internal error naming `unknown(n)`;
//! * r6: a library with another fingerprint is refused as
//!   `CHTYPES_ARTIFACT_INCOMPATIBLE`, with exactly the rule's message (stub
//!   variant `fingerprint-other`).
//!
//! The documents and the planted values are `scripts/abi-v1/emit/_stubshared.py`'s
//! `R2_DOCS` and `R3_MUTATIONS`, the shapes public pull request #509 measured on
//! the released 1.0.4 bindings. The enums no document carries (`chs_format`,
//! `discover_query_param`) are covered, with every other enum the description
//! defines, by the crate's own unit test over the generated
//! `DESCRIBED_VOCABULARIES`.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes. One
//! test function, deliberately: it sets the process environment the unverified
//! open needs, and the process setup is process-wide.

use std::path::{Path, PathBuf};

use chtypes::{
    CompileOptions, DefaultKind, Error, EvalOptions, FilterOptions, FilterOutcome, Format, Library,
    Outcome, Reason, RowOptions, RowsOptions, Source, Status, Verdict,
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
    let path = variants[name]["path"]
        .as_str()
        .unwrap_or_else(|| panic!("stubs.json names no {name} variant"));
    dir.join(Path::new(path).file_name().expect("file name"))
}

fn open(
    dir: &Path,
    variants: &serde_json::Map<String, Value>,
    name: &str,
) -> std::sync::Arc<Library> {
    Library::open_unverified(variant_path(dir, variants, name), true)
        .unwrap_or_else(|e| panic!("the {name} stub does not open: {e}"))
}

#[test]
fn the_reader_rules_and_the_dev_fingerprint_over_the_v2_stub() {
    let Some((dir, variants)) = stubs() else {
        eprintln!(
            "SKIPPED (loudly): reader_rules needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return;
    };
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };

    r2_unknown_members_are_ignored(&dir, &variants);
    r3_unknown_values_are_kept(&dir, &variants);
    r3_unknown_status_is_internal_naming_it(&dir, &variants);
    r3_unknown_capabilities_are_kept(&dir, &variants);
    r6_another_fingerprint_is_refused_with_the_rules_message(&dir, &variants);
}

/// r2: every document and `build_info`, each carrying members no description
/// names at every object level, decode as if those members were absent.
fn r2_unknown_members_are_ignored(dir: &Path, variants: &serde_json::Map<String, Value>) {
    let lib = open(dir, variants, "r2-unknown-members");
    let predicate = &variants["r2-unknown-members"]["predicate"];

    let bi = lib.build_info();
    assert_eq!(
        Some(bi.clickhouse_version.as_str()),
        predicate["clickhouse_version"].as_str(),
        "build_info with unknown members: {bi:?}"
    );
    assert_eq!(
        Some(bi.channel.as_str()),
        predicate["channel"].as_str(),
        "{bi:?}"
    );
    assert!(
        bi.capabilities
            .features
            .iter()
            .any(|f| f == "default_generators"),
        "build_info with unknown members: {bi:?}"
    );
    let handles = lib
        .live_handles()
        .expect("live_handles with an unknown key");
    assert!(handles.contains_key("chs_schema"), "{handles:?}");
    let table = lib
        .error_codes()
        .expect("error_code_table with unknown members");
    assert_eq!(table.name(53), Some("TYPE_MISMATCH"));

    let schema = lib
        .compile_table("CREATE TABLE t (x Int32)", &CompileOptions::default())
        .expect("compile");
    let d = schema
        .describe()
        .expect("schema_description with unknown members");
    assert_eq!(d.columns.len(), 1, "{d:?}");
    assert_eq!(d.columns[0].name.as_bytes(), b"x");
    assert_eq!(d.columns[0].r#type.as_bytes(), b"Int32");

    let r = schema
        .row(Format::JsonEachRow, br#"{"x":1}"#, &RowOptions::default())
        .expect("row with unknown members");
    assert_eq!(r.outcome, Outcome::Accepted, "{r:?}");
    assert_eq!(r.columns.len(), 1, "{r:?}");
    assert_eq!(r.columns[0].text.as_bytes(), b"abc");
    assert_eq!(r.input_span.map(|s| s.len), Some(3), "{r:?}");
    assert_eq!(
        (
            r.computed.len(),
            r.transformed.len(),
            r.unknown_fields.len(),
            r.unsupported_settings.len()
        ),
        (1, 1, 1, 1),
        "{r:?}"
    );

    let b = schema
        .rows(
            Format::JsonEachRow,
            br#"{"x":1}"#,
            &RowsOptions {
                export: Some(Format::JsonEachRow),
                ..Default::default()
            },
        )
        .expect("batch with unknown members");
    assert_eq!(b.outcome, Outcome::Accepted, "{b:?}");
    assert_eq!(
        (b.rows_read, b.rows.len(), b.rows[0].columns.len()),
        (1, 1, 1),
        "{b:?}"
    );
    assert_eq!(b.engine_rows.as_ref().map(Vec::len), Some(1), "{b:?}");
    assert_eq!(b.spans.as_ref().map(Vec::len), Some(1), "{b:?}");
    assert_eq!(b.unconsumed.len(), 1, "{b:?}");
    let header = b
        .framing
        .as_ref()
        .and_then(|f| f.header.as_ref())
        .expect("framing with a header");
    assert_eq!(header.names.len(), 1, "{b:?}");
    assert_eq!(b.payload.as_deref(), Some(&b"{\"s\":\"abc\"}\n"[..]));

    let filter = schema
        .compile_filter("x > 1", &FilterOptions::default())
        .expect("compile the filter");
    let f = filter
        .rows(Format::JsonEachRow, br#"{"x":1}"#, &EvalOptions::default())
        .expect("filter_result with unknown members");
    assert_eq!(f.outcome, FilterOutcome::Ok, "{f:?}");
    assert_eq!(
        (
            f.rows_read,
            f.verdicts.len(),
            f.errors.len(),
            f.unsupported_settings.len()
        ),
        (2, 2, 1, 1),
        "{f:?}"
    );

    let d = lib
        .discover_columns(b"{}")
        .expect("discovery with unknown members");
    assert_eq!(d.columns.len(), 1, "{d:?}");
    assert_eq!(d.columns[0].declaration.as_bytes(), b"c String");
    assert_eq!(d.columns_sql.as_bytes(), b"c String");
}

/// r3: one planted value a vocabulary does not list, per `!E:<id>` body: kept
/// as that field's `Unknown`, the rest of the document decoded.
fn r3_unknown_values_are_kept(dir: &Path, variants: &serde_json::Map<String, Value>) {
    let lib = open(dir, variants, "r3-unknown-values");
    let clean = lib
        .compile_table("CREATE TABLE t (x Int32)", &CompileOptions::default())
        .expect("compile");
    let row = |id: &str| {
        clean
            .row(
                Format::JsonEachRow,
                format!("!E:{id}").as_bytes(),
                &RowOptions::default(),
            )
            .unwrap_or_else(|e| {
                panic!("{id}: the row failed: {e} (rule r3: an unlisted value never fails it)")
            })
    };
    let batch = |id: &str| {
        clean
            .rows(
                Format::JsonEachRow,
                format!("!E:{id}").as_bytes(),
                &RowsOptions {
                    export: Some(Format::JsonEachRow),
                    ..Default::default()
                },
            )
            .unwrap_or_else(|e| panic!("{id}: the batch failed: {e} (rule r3)"))
    };

    // The control: the clean document decodes with every value listed.
    let control = clean
        .row(Format::JsonEachRow, b"{}", &RowOptions::default())
        .expect("control row");
    assert_eq!(control.outcome, Outcome::Accepted, "{control:?}");
    assert!(control.columns[0].source.is_known(), "{control:?}");

    let r = row("row.outcome");
    assert_eq!(
        r.outcome,
        Outcome::Unknown("x_future_outcome".into()),
        "{r:?}"
    );
    assert!(
        !r.outcome.is_known() && r.outcome != Outcome::Accepted && r.columns.len() == 1,
        "{r:?}"
    );

    let r = row("row.cols.src");
    assert_eq!(
        r.columns[0].source,
        Source::Unknown("x_future_src".into()),
        "{r:?}"
    );
    assert!(
        !r.columns[0].source.is_known() && !r.columns[0].is_stored,
        "{r:?}"
    );
    assert_eq!(r.columns[0].text.as_bytes(), b"abc");
    assert_eq!(r.outcome, Outcome::Accepted, "{r:?}");

    let r = row("row.transformed.reason");
    assert_eq!(
        r.transformed[0].reason,
        Reason::Unknown("x_future_reason".into()),
        "{r:?}"
    );
    assert!(!r.transformed[0].reason.is_known());
    assert_eq!(
        r.transformed[0].lossy,
        Reason::ValueChanged.lossy(),
        "the fallback's lossy fact"
    );

    let r = row("row.verdict");
    assert_eq!(r.verdict, Some(Verdict::Unknown("x".into())), "{r:?}");
    let v = r.verdict.as_ref().expect("a verdict");
    assert!(
        !v.is_known() && !v.answered(),
        "an unknown verdict is never an answer"
    );

    let b = batch("batch.outcome");
    assert_eq!(
        b.outcome,
        Outcome::Unknown("x_future_outcome".into()),
        "{b:?}"
    );
    assert!(
        !b.outcome.is_known() && b.outcome != Outcome::Accepted && b.rows.len() == 1,
        "{b:?}"
    );

    let b = batch("batch.rows.outcome");
    assert_eq!(
        b.rows[0].outcome,
        Outcome::Unknown("x_future_outcome".into()),
        "{b:?}"
    );
    assert_eq!(
        b.outcome,
        Outcome::Accepted,
        "the batch's own value is untouched: {b:?}"
    );

    let b = batch("batch.rows.cols.src");
    assert_eq!(
        b.rows[0].columns[0].source,
        Source::Unknown("x_future_src".into()),
        "{b:?}"
    );
    assert_eq!(b.rows.len(), 1, "{b:?}");

    let b = batch("batch.transformed.reason");
    assert_eq!(
        b.transformed[0].reason,
        Reason::Unknown("x_future_reason".into()),
        "{b:?}"
    );

    let b = batch("batch.storage_transforms.reason");
    assert_eq!(
        (b.outcome.clone(), b.rows.len()),
        (Outcome::Accepted, 1),
        "an unlisted reason in a member this binding does not read must not fail the batch: {b:?}"
    );

    let b = batch("batch.framing.container");
    assert_eq!(
        b.framing.as_ref().and_then(|f| f.container.as_deref()),
        Some("x_future_container"),
        "the schema's enum constrains the writer, never the reader (r3): {b:?}"
    );

    let filter = clean
        .compile_filter("x > 1", &FilterOptions::default())
        .expect("compile the filter");
    let f = filter
        .rows(
            Format::JsonEachRow,
            b"!E:filter.outcome",
            &EvalOptions::default(),
        )
        .expect("filter.outcome");
    assert_eq!(
        f.outcome,
        FilterOutcome::Unknown("x_future_outcome".into()),
        "{f:?}"
    );
    assert!(
        !f.outcome.is_known() && f.outcome != FilterOutcome::Ok,
        "never ok: {f:?}"
    );
    let f = filter
        .rows(
            Format::JsonEachRow,
            b"!E:filter.verdicts",
            &EvalOptions::default(),
        )
        .expect("filter.verdicts");
    assert_eq!(
        f.verdicts,
        vec![Verdict::True, Verdict::Unknown("x".into())],
        "{f:?}"
    );
    assert!(
        !f.verdicts[1].is_known() && !f.verdicts[1].answered(),
        "{f:?}"
    );

    // chs_schema_describe has no body: the stub answers the mutation the
    // schema's own statement named.
    let mutated = lib
        .compile_table("!E:describe.default_kind", &CompileOptions::default())
        .expect("compile the mutating statement");
    let d = mutated.describe().expect("describe.default_kind");
    assert_eq!(
        d.columns[0].default_kind,
        DefaultKind::Unknown("X_FUTURE".into()),
        "{d:?}"
    );
    assert!(!d.columns[0].default_kind.is_known());
    assert_eq!(d.columns[0].name.as_bytes(), b"x");
    let d = lib
        .discover_columns(b"!E:discovery.default_kind")
        .expect("discovery.default_kind");
    assert_eq!(d.columns.len(), 1, "{d:?}");
    assert_eq!(d.columns[0].declaration.as_bytes(), b"c String");
}

/// r3: a call status outside the closed set is `unknown(n)`, and the call still
/// fails, as an internal error naming it.
fn r3_unknown_status_is_internal_naming_it(dir: &Path, variants: &serde_json::Map<String, Value>) {
    let lib = open(dir, variants, "r3-unknown-values");
    let err = lib
        .validate_type("!U:")
        .expect_err("status 99 must fail the call");
    let Error::Internal(c) = &err else {
        panic!("status 99 = {err:?}; want an internal error")
    };
    assert_eq!(c.status, 99, "{err:?}");
    let read = Status::from_code(c.status);
    assert_eq!(read, Status::Unknown(99));
    assert!(!read.is_known());
    assert_eq!(read.to_string(), "unknown(99)");
    assert!(err.to_string().contains("unknown(99)"), "{err}");
}

/// r3: `build_info`'s capabilities lists keep a value no vocabulary lists.
fn r3_unknown_capabilities_are_kept(dir: &Path, variants: &serde_json::Map<String, Value>) {
    let lib = open(dir, variants, "r3-unknown-capabilities");
    fn has(list: &[String], v: &str) -> bool {
        list.iter().any(|x| x == v)
    }
    let c = &lib.build_info().capabilities;
    assert!(has(&c.input_formats, "XFutureFormat"), "{c:?}");
    assert!(has(&c.export_formats, "XFutureFormat"), "{c:?}");
    assert!(has(&c.doc_flags, "x_future_flag"), "{c:?}");
    assert!(
        has(&c.features, "x_future_feature") && has(&c.features, "default_generators"),
        "{c:?}"
    );
}

/// r6: a library built to report another fingerprint is refused as
/// `CHTYPES_ARTIFACT_INCOMPATIBLE` with exactly the rule's message. The message
/// is spelled out here, not taken from the code under test, and this SDK's own
/// fingerprint is read from the stubs' manifest (rendered from the description
/// this binding is generated from), not from the binding.
fn r6_another_fingerprint_is_refused_with_the_rules_message(
    dir: &Path,
    variants: &serde_json::Map<String, Value>,
) {
    let ours = variants["ok"]["predicate"]["abi_fingerprint"]
        .as_str()
        .expect("the ok stub's predicate names its abi_fingerprint");
    let theirs = format!("sha256:{}", "0".repeat(64));
    let want = format!(
        "this SDK speaks dev fingerprint {ours}; the library has {theirs} — update your dev SDK"
    );
    let err = Library::open_unverified(variant_path(dir, variants, "fingerprint-other"), true)
        .expect_err("a library with another fingerprint must be refused");
    let Error::ArtifactIncompatible(refusal) = &err else {
        panic!("another fingerprint = {err:?}; want ArtifactIncompatible")
    };
    assert_eq!(refusal.reason, "fingerprint", "{err:?}");
    assert_eq!(err.code(), Some("CHTYPES_ARTIFACT_INCOMPATIBLE"));
    assert_eq!(err.to_string(), want);
    assert_eq!(refusal.dev_message().as_deref(), Some(want.as_str()));
}
