//! Integration tests against a real artifact registry.
//!
//! They need artifacts (166–302 MB each, hours of C++ compute), so they SKIP
//! with a clear message when none is found: `$CHTYPES_REGISTRY` if set, otherwise
//! the per-user artifact cache (`chtypes::default_registry_dir()`). Every assertion below is on an
//! *output* — never on an exit code, never on a self-report.
//!
//! All tests share one process-wide [`Registry`], because `chs_init` must run
//! exactly once per loaded library and `cargo test` runs tests as threads of one
//! process.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{
    CODE_UNSUPPORTED, Format, NO_SETTINGS, Outcome, Registry, SETTING_CLOCK_OFFSET_NANOS,
    SETTING_MAX_CLOCK_SKEW_NANOS, SETTING_NOW_EPOCH_NANOS, Schema, reason,
};

static REGISTRY: OnceLock<Option<Arc<Registry>>> = OnceLock::new();

fn registry_dir() -> PathBuf {
    match std::env::var_os(chtypes::REGISTRY_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => chtypes::default_registry_dir(),
    }
}

/// Announce a skip on the *real* stderr. `eprintln!` is captured by libtest for
/// a passing test, so a skip printed with it is invisible — which is how a suite
/// that silently tests nothing looks exactly like a suite that passes.
fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().flush();
}

fn registry() -> Option<&'static Arc<Registry>> {
    REGISTRY
        .get_or_init(|| {
            let dir = registry_dir();
            if !dir.is_dir() {
                announce(&format!(
                    "\nSKIP: no chtypes artifact registry at {} — set ${} or fetch artifacts \
                     (scripts/fetch.sh, see docs/artifacts.md). Every test in this file is \
                     skipped.\n",
                    dir.display(),
                    chtypes::REGISTRY_ENV
                ));
                return None;
            }
            match Registry::new(&dir) {
                Ok(r) => Some(Arc::new(r)),
                Err(e) => {
                    announce(&format!(
                        "\nSKIP: registry at {} did not load: {e}. Every test in this file is \
                         skipped.\n",
                        dir.display()
                    ));
                    None
                }
            }
        })
        .as_ref()
}

/// Bind the shared registry, or skip the test with a message.
macro_rules! registry {
    () => {
        match registry() {
            Some(r) => r,
            None => return,
        }
    };
}

/// A library to run single-version assertions against: `25.8` when present (the
/// version every observation in `spec/` was captured on), else the first loaded.
fn primary(reg: &Registry) -> Arc<chtypes::Library> {
    reg.for_version("25.8")
        .unwrap_or_else(|_| Arc::clone(&reg.libraries()[0]))
}

fn stored(schema: &Schema, format: Format, body: &[u8]) -> chtypes::BatchResult {
    schema.rows(format, body, NO_SETTINGS).expect("rows")
}

// ---------------------------------------------------------------- the loader

#[test]
fn the_registry_loads_and_libraries_name_themselves() {
    let reg = registry!();
    assert!(!reg.versions().is_empty());
    println!("registry {}: {:?}", reg.dir().display(), reg.versions());

    // Release order, not directory order (spec/bindings.md §Version selection,
    // rule 2 — every ordered surface): the lexical scan put 25.10 before 25.8
    // until 2026-08-26. versions() and libraries() must agree.
    let libraries = reg.libraries();
    let minors: Vec<&str> = libraries.iter().map(|l| l.minor()).collect();
    let numeric = |m: &str| -> (u64, u64) {
        let mut it = m.split('.');
        (
            it.next().and_then(|s| s.parse().ok()).unwrap_or(0),
            it.next().and_then(|s| s.parse().ok()).unwrap_or(0),
        )
    };
    for w in minors.windows(2) {
        assert!(
            numeric(w[0]) < numeric(w[1]),
            "libraries() out of release order: {minors:?}"
        );
    }
    assert_eq!(
        reg.versions(),
        minors.iter().map(|m| m.to_string()).collect::<Vec<_>>(),
        "versions() and libraries() disagree on order"
    );

    for lib in reg.libraries() {
        // The library names itself, and its minor line comes from that string.
        assert!(!lib.version().is_empty(), "library reported no version");
        let want_minor: String = lib
            .version()
            .splitn(3, '.')
            .take(2)
            .collect::<Vec<_>>()
            .join(".");
        assert_eq!(lib.minor(), want_minor, "minor line of {}", lib.version());

        // The file name came from manifest.library — not from a hard-coded
        // libchtypes.so, which would find nothing on the shipping platform.
        let dir = lib.path().parent().unwrap();
        let manifest: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(dir.join("manifest.json")).unwrap())
                .unwrap();
        assert_eq!(
            lib.path().file_name().unwrap().to_str().unwrap(),
            manifest["library"].as_str().unwrap(),
            "library file name must come from the manifest"
        );
        // And the library's own answer agrees with the manifest: the right bytes
        // in the wrong directory is the one corruption a hash cannot catch.
        assert_eq!(
            lib.version(),
            manifest["clickhouse_version"].as_str().unwrap()
        );

        // chs_init ran for THIS library: the family registry is this build's own,
        // and a DEFAULT needs the global Context chs_init builds.
        let families = lib.registered_families().expect("chs_registered_families");
        assert!(
            families.len() > 100,
            "{} families on {}",
            families.len(),
            lib.version()
        );
        assert!(families.iter().any(|f| f == "UInt8"));
        let schema = lib
            .compile("a UInt8, b UInt8 DEFAULT a + 1")
            .compile()
            .unwrap();
        let batch = stored(&schema, Format::JsonEachRow, br#"{"a":41}"#);
        assert_eq!(batch.outcome, Outcome::Accepted, "{}", lib.version());
        assert_eq!(batch.rows[0].values[1].text, "42", "on {}", lib.version());
    }

    // chs_function_flags is what the statelessness gate is computed from, and it
    // requires chs_init. It is exercised on ONE library on purpose: calling it on
    // two loaded artifacts in one process was measured to abort the process at
    // teardown unless chs_shutdown runs first, which `Library::drop` now does.
    let flags = primary(reg).function_flags().expect("chs_function_flags");
    assert!(flags.lines().count() > 1_000, "function flags look empty");
    assert!(
        flags.lines().any(|l| l.starts_with("now\t")),
        "now() must be audited"
    );
}

#[test]
fn version_resolution_accepts_a_minor_line_a_patch_and_a_drifted_patch() {
    let reg = registry!();
    let lib = primary(reg);

    // Exact patch, and the minor line, both resolve to the same library.
    assert_eq!(
        reg.for_version(lib.version()).unwrap().version(),
        lib.version()
    );
    assert_eq!(
        reg.for_version(lib.minor()).unwrap().version(),
        lib.version()
    );

    // A drifted patch falls back to its minor line: docker tags move, and an
    // exact-match-only lookup silently costs a whole version column.
    let drifted = format!("{}.99.7-lts", lib.minor());
    assert_eq!(reg.for_version(&drifted).unwrap().version(), lib.version());

    // A version with no artifact is an error naming what IS loaded — never the
    // nearest neighbour.
    let err = reg.for_version("19.1").unwrap_err();
    let msg = err.to_string();
    for v in reg.versions() {
        assert!(msg.contains(&v), "error must name loaded versions: {msg}");
    }
    assert!(err.code().is_none());
}

// ------------------------------------------------- multi-version coexistence

#[test]
fn several_versions_answer_in_one_process_with_their_own_semantics() {
    let reg = registry!();
    if reg.libraries().len() < 2 {
        announce("\nSKIP: only one artifact loaded, nothing to isolate\n");
        return;
    }

    // Distinct builds, all live at once, each loaded RTLD_LOCAL so their
    // ClickHouse symbols stay private.
    let libraries = reg.libraries();
    let mut versions: Vec<&str> = libraries.iter().map(|l| l.version()).collect();
    versions.sort_unstable();
    let count = versions.len();
    versions.dedup();
    assert_eq!(versions.len(), count, "two libraries reported one version");

    // Probe 1 — `JSON`: rejected on 24.8 with code 44, accepted from 25.3.
    // Probe 2 — a mixed-type DEFAULT: rejected on 25.10 with code 386 and
    // accepted on 24.8 through 25.8 and 26.6 onward. Newer is NOT always more
    // permissive, which is the entire reason the registry exists.
    let mut json_answers = Vec::new();
    let mut default_answers = Vec::new();
    for lib in reg.libraries() {
        // The experimental-type gate fires at column creation inside chs_rows, so
        // the probe has to feed a row rather than only compile.
        let json = match lib.compile("j JSON").compile() {
            Ok(schema) => {
                let batch = schema
                    .rows(Format::JsonEachRow, br#"{"j":{"k":1}}"#, NO_SETTINGS)
                    .expect("rows");
                match batch.outcome {
                    Outcome::Accepted => "accepted".to_string(),
                    _ => format!("code {}", batch.err_code),
                }
            }
            Err(e) => format!("code {:?}", e.code()),
        };
        let mixed = match lib
            .compile("a UInt8, x Int64 DEFAULT if(1,2,'a')")
            .compile()
        {
            Ok(schema) => {
                let batch = stored(&schema, Format::JsonEachRow, br#"{"a":1}"#);
                assert_eq!(batch.outcome, Outcome::Accepted, "{}", lib.version());
                batch.rows[0]
                    .values
                    .iter()
                    .find(|v| v.column == "x")
                    .map(|v| v.text.to_string())
                    .unwrap()
            }
            Err(e) => format!("code {:?}", e.code()),
        };
        println!("{:>16}  JSON={json:<12} if(1,2,'a')={mixed}", lib.version());
        json_answers.push((lib.minor().to_string(), json));
        default_answers.push((lib.minor().to_string(), mixed));
    }

    // The known per-version answers, asserted for whichever versions are loaded.
    for (minor, answer) in &json_answers {
        match minor.as_str() {
            "24.8" => assert_eq!(answer, "code 44", "24.8 must gate JSON"),
            _ => assert_eq!(answer, "accepted", "{minor} must accept JSON"),
        }
    }
    for (minor, answer) in &default_answers {
        match minor.as_str() {
            "25.10" => assert_eq!(
                answer, "code Some(386)",
                "25.10 must reject the mixed DEFAULT"
            ),
            _ => assert_eq!(answer, "2", "{minor} must accept the mixed DEFAULT"),
        }
    }

    // The point of the exercise: at least one probe answered two different ways
    // in this one process. With RTLD_GLOBAL one build's DataTypeFactory would
    // serve them all and this would collapse to a single answer.
    let json_distinct = distinct(&json_answers);
    let default_distinct = distinct(&default_answers);
    assert!(
        json_distinct > 1 || default_distinct > 1,
        "no divergence observed across {} versions — symbol isolation unproven",
        reg.libraries().len()
    );
}

fn distinct(answers: &[(String, String)]) -> usize {
    let mut v: Vec<&str> = answers.iter().map(|(_, a)| a.as_str()).collect();
    v.sort_unstable();
    v.dedup();
    v.len()
}

// ------------------------------------------------------- types and schemas

#[test]
fn canonical_types_round_trip_in_clickhouses_own_spelling() {
    let reg = registry!();
    let lib = primary(reg);

    for (input, canonical) in [
        ("DECIMAL(18,4)", "Decimal(18, 4)"),
        ("Decimal64(4)", "Decimal(18, 4)"),
        ("Nullable(Decimal(18,4))", "Nullable(Decimal(18, 4))"),
        ("Enum8('a'=1,'b'=2)", "Enum8('a' = 1, 'b' = 2)"),
        ("Map(String,Array(UInt8))", "Map(String, Array(UInt8))"),
        ("LowCardinality( String )", "LowCardinality(String)"),
        ("BIGINT", "Int64"),
        ("INT", "Int32"),
        // Members are sorted.
        ("Variant(UInt8, String)", "Variant(String, UInt8)"),
        // Surplus parameters are dropped, not rejected.
        ("Int8(3)", "Int8"),
    ] {
        assert_eq!(
            lib.validate_type(input).unwrap(),
            canonical,
            "canonical spelling of {input} on {}",
            lib.version()
        );
    }

    // An unknown family is ClickHouse's own error, code 50 — not unsupported.
    let err = lib.validate_type("NotAType").unwrap_err();
    assert_eq!(err.code(), Some(50), "{err}");
    assert!(!err.is_unsupported());
    assert!(err.to_string().contains("NotAType"), "{err}");

    // The reference type is exposed for explainability, and is absent for types
    // with nothing wider to compare against.
    assert_eq!(
        lib.reference_type("UInt8").unwrap().as_deref(),
        Some("Int256")
    );
    assert_eq!(
        lib.reference_type("DateTime").unwrap().as_deref(),
        Some("DateTime64(0, 'UTC')")
    );
    assert_eq!(lib.reference_type("String").unwrap(), None);
}

#[test]
fn compiling_ddl_is_schema_aware_in_a_way_validate_type_cannot_be() {
    let reg = registry!();
    let lib = primary(reg);

    let schema = lib
        .compile("a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()")
        .compile()
        .unwrap();
    let cols = schema.columns();
    assert_eq!(cols.len(), 3, "column introspection is missing");
    assert_eq!(cols[0].ty, "UInt8");
    assert_eq!(cols[0].default_kind, chtypes::DefaultKind::None);
    assert_eq!(cols[1].ty, "Nullable(String)");
    assert_eq!(cols[1].default_kind, chtypes::DefaultKind::Default);
    assert_eq!(cols[1].default_expr, "'x'");
    assert!(cols[1].default_is_literal);
    assert_eq!(cols[2].default_kind, chtypes::DefaultKind::Materialized);
    assert_eq!(cols[2].default_expr, "now()");
    assert!(!cols[2].default_is_literal);

    // The DEFAULT rewrote the declared type — which is exactly why validate_type
    // alone is insufficient.
    let schema = lib.compile("x Int64 DEFAULT NULL").compile().unwrap();
    assert_eq!(schema.columns()[0].ty, "Nullable(Int64)");
    assert_eq!(schema.columns()[0].default_expr, "NULL");
    assert!(schema.columns()[0].default_is_literal);

    // An ALIAS column's type is inferred.
    let schema = lib.compile("a UInt8, al ALIAS a + 1").compile().unwrap();
    assert_eq!(schema.columns()[1].ty, "UInt16");
    assert_eq!(
        schema.columns()[1].default_kind,
        chtypes::DefaultKind::Alias
    );
}

// ------------------------------------------------------------------- rows

#[test]
fn an_overflowing_uint8_is_stored_wrapped_and_reported() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("x UInt8").compile().unwrap();

    let batch = stored(&schema, Format::JsonEachRow, br#"{"x":256}"#);

    // ClickHouse returns success. The insert would be accepted; the value would
    // be wrong; nothing in ClickHouse says so.
    assert_eq!(batch.outcome, Outcome::Accepted);
    assert_eq!(batch.rows_read, 1);
    assert_eq!(batch.rows.len(), 1);
    assert_eq!(batch.rows[0].values.len(), 1);
    assert_eq!(batch.rows[0].values[0].column, "x");
    assert_eq!(batch.rows[0].values[0].text, "0");
    assert!(!batch.rows[0].values[0].null);
    assert_eq!(batch.rows[0].values[0].source, "input");

    assert_eq!(batch.transformed.len(), 1, "{:?}", batch.transformed);
    let t = &batch.transformed[0];
    assert_eq!(t.column, "x");
    assert_eq!(t.input, "256");
    assert_eq!(t.stored, "0");
    assert_eq!(t.reason, reason::OVERFLOW_WRAP);
    assert_eq!(t.row, 0);
    assert!(t.lossy());

    // The row index is filled in for every row of a batch.
    let batch = stored(
        &schema,
        Format::JsonEachRow,
        b"{\"x\":1}\n{\"x\":300}\n{\"x\":2}\n",
    );
    assert_eq!(batch.rows_read, 3);
    assert_eq!(batch.transformed.len(), 1);
    assert_eq!(batch.transformed[0].row, 1);
    assert_eq!(batch.transformed[0].stored, "44");
}

#[test]
fn a_rejected_row_surfaces_clickhouses_own_error_code() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("x UInt8").compile().unwrap();

    let batch = stored(&schema, Format::JsonEachRow, br#"{"x":"abc"}"#);
    assert_eq!(batch.outcome, Outcome::Rejected);
    assert_eq!(batch.err_code, 27, "{}", batch.err_msg);
    assert!(batch.err_msg.contains("Cannot parse"), "{}", batch.err_msg);
    // A rejection is not the unsupported sentinel, and it is not an acceptance.
    assert_ne!(batch.err_code, CODE_UNSUPPORTED);

    // The single-row entry point agrees.
    let row = schema
        .row(Format::JsonEachRow, br#"{"x":"abc"}"#)
        .expect("row");
    assert_eq!(row.outcome, Outcome::Rejected);
    assert_eq!(row.err_code, 27);
    assert!(row.values.is_empty());
}

#[test]
fn a_pinned_volatile_default_stores_an_exact_timestamp() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib
        .compile("a UInt8, ts DateTime DEFAULT now()")
        .compile()
        .unwrap();

    // The setting value crosses as a STRING. As a JSON number through a double
    // this 19-digit epoch becomes 1.7e+18 and the setting is silently ignored.
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"a":1}"#,
            &[(SETTING_NOW_EPOCH_NANOS, "1700000000000000000")],
        )
        .expect("rows");

    assert_eq!(batch.outcome, Outcome::Accepted);
    let ts = batch.rows[0]
        .values
        .iter()
        .find(|v| v.column == "ts")
        .expect("ts column");
    assert_eq!(
        ts.text, "\"2023-11-14 22:13:20\"",
        "the instant must be pinned"
    );
    assert_eq!(ts.source, "default_substituted");

    // The caller MUST send this column explicitly, so it is reported as a
    // substitution and as a transform.
    assert_eq!(batch.rows[0].substituted.len(), 1);
    assert_eq!(batch.rows[0].substituted[0].column, "ts");
    assert_eq!(batch.rows[0].substituted[0].expr, "now()");
    assert_eq!(batch.rows[0].substituted[0].text, "\"2023-11-14 22:13:20\"");

    let t = batch
        .transformed
        .iter()
        .find(|t| t.column == "ts")
        .expect("ts transform");
    assert_eq!(t.reason, reason::DEFAULT_MATERIALISED);
    assert!(!t.lossy(), "materialising a DEFAULT loses nothing");

    // Past the caller's skew budget the honest answer is a decline, not a
    // timestamp the server would not have written.
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"a":1}"#,
            &[
                (SETTING_CLOCK_OFFSET_NANOS, "5000000000"),
                (SETTING_MAX_CLOCK_SKEW_NANOS, "1"),
            ],
        )
        .expect("rows");
    assert_eq!(batch.rows[0].outcome, Outcome::Unsupported);
    assert_eq!(batch.rows[0].err_code, CODE_UNSUPPORTED);
    let ts = batch.rows[0]
        .values
        .iter()
        .find(|v| v.column == "ts")
        .unwrap();
    assert_eq!(ts.source, "default_volatile_unresolved");
    // Nothing is reported as transformed for a value that was never resolved.
    assert!(batch.transformed.iter().all(|t| t.column != "ts"));
}

#[test]
fn a_ttl_expired_row_is_accepted_per_row_and_not_stored_per_batch() {
    let reg = registry!();
    let lib = primary(reg);
    let mut schema = lib.compile("ts DateTime, v UInt8").compile().unwrap();
    schema.set_ttl("ts + INTERVAL 1 DAY").expect("set_ttl");

    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"ts":"2020-01-01 00:00:00","v":9}"#,
            &[(SETTING_NOW_EPOCH_NANOS, "1700000000000000000")],
        )
        .expect("rows");

    // The row's own document says accepted; the batch says the part holds
    // nothing. A binding that read only `rows` would preview a row the table
    // silently deletes at merge time.
    assert_eq!(batch.rows[0].outcome, Outcome::Accepted);
    assert_eq!(batch.engine_rows.as_deref(), Some(&[][..]));
    let t = batch
        .transformed
        .iter()
        .find(|t| t.reason == reason::TTL_EXPIRED)
        .expect("ttl_expired must be folded into the batch");
    assert_eq!(t.row, 0);
    assert!(t.lossy());

    // A clock-reading TTL is declined, never guessed.
    let mut schema = lib.compile("ts DateTime, v UInt8").compile().unwrap();
    let err = schema.set_ttl("now() + INTERVAL 1 DAY").unwrap_err();
    assert!(err.is_unsupported(), "{err}");
    assert_eq!(err.code(), Some(CODE_UNSUPPORTED));
}

#[test]
fn an_engines_insert_time_merge_is_the_stored_truth() {
    let reg = registry!();
    let lib = primary(reg);
    let mut schema = lib
        .compile("day Date, key UInt8, v UInt64")
        .compile()
        .unwrap();
    schema
        .set_engine("SummingMergeTree", "(day, key)", NO_SETTINGS)
        .expect("set_engine");

    let batch = stored(
        &schema,
        Format::JsonEachRow,
        b"{\"day\":\"2026-01-01\",\"key\":1,\"v\":5}\n{\"day\":\"2026-01-01\",\"key\":1,\"v\":7}\n",
    );
    assert_eq!(batch.outcome, Outcome::Accepted);
    assert_eq!(batch.rows.len(), 2, "the type layer still saw both inputs");
    let engine_rows = batch.engine_rows.expect("engine_rows");
    assert_eq!(engine_rows.len(), 1, "{engine_rows:?}");
    let merged: serde_json::Value = serde_json::from_str(&engine_rows[0]).unwrap();
    assert_eq!(merged["v"], 12, "{}", engine_rows[0]);
    assert_eq!(merged["day"], "2026-01-01");

    // An invalid Sign is refused before anything is stored, with ClickHouse's
    // own code 117.
    let mut schema = lib.compile("id UInt8, sign Int8").compile().unwrap();
    schema
        .set_engine("CollapsingMergeTree(sign)", "id", NO_SETTINGS)
        .expect("set_engine");
    let batch = stored(&schema, Format::JsonEachRow, br#"{"id":1,"sign":3}"#);
    assert_eq!(batch.outcome, Outcome::Rejected);
    assert_eq!(batch.err_code, 117, "{}", batch.err_msg);
}

// --------------------------------------------------------- the sentinel

#[test]
fn a_server_property_default_is_unsupported_not_rejected() {
    let reg = registry!();
    let lib = primary(reg);

    // Measured on the 25.8 artifact: the DDL compiles, and the decline arrives
    // when a row would have to evaluate the expression.
    let schema = lib
        .compile("h String DEFAULT hostName()")
        .compile()
        .unwrap();
    let batch = stored(&schema, Format::JsonEachRow, b"{}");

    assert_eq!(batch.outcome, Outcome::Unsupported);
    assert_eq!(batch.rows[0].outcome, Outcome::Unsupported);
    assert!(
        batch.rows[0]
            .err_msg
            .contains("property of the ClickHouse server"),
        "{}",
        batch.rows[0].err_msg
    );
    // The column has no value, and nothing is claimed about one: resolving a
    // server property here would store the gateway's answer. The wrapper spells
    // that as `"stored": null`, and a present null is a stored null — measured
    // against the reference SDK on this artifact, which reports
    // `Text = "null"` (bytes 6e 75 6c 6c) and `Null = true` for this column.
    // The row is `unsupported`, which is the field that says "no answer here";
    // the value is reported exactly as the document gave it.
    assert_eq!(batch.rows[0].values[0].source, "default_expr_unsupported");
    assert_eq!(batch.rows[0].values[0].text, "null");
    assert!(batch.rows[0].values[0].null);
    assert!(batch.transformed.is_empty());

    // Whatever the code carried, the outcome is neither accepted nor rejected:
    // mapping it onto a rejection would manufacture an over-reject the product
    // never made, and onto an acceptance an over-accept, which is the cardinal
    // sin. (Observed: this class carries code 0 with outcome `unsupported`, while
    // a clock-skew decline carries -2 — see the volatile-DEFAULT test.)
    assert_ne!(batch.outcome, Outcome::Accepted);
    assert_ne!(batch.outcome, Outcome::Rejected);
    assert_eq!(CODE_UNSUPPORTED, -2);

    // A schema whose DEFAULT exceeds the admission memory budget is refused at
    // compile time instead, and that one does carry the sentinel.
    let err = lib
        .compile("b UInt8 DEFAULT range(400000000)[1]")
        .compile()
        .unwrap_err();
    assert!(err.is_unsupported(), "{err}");
    assert_eq!(err.code(), Some(CODE_UNSUPPORTED));
    assert!(err.to_string().contains("budget"), "{err}");
}

#[test]
fn an_accepted_insert_that_destroys_the_value_is_reported_as_accepted() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("e Enum8('a'=1,'b'=2)").compile().unwrap();

    // The insert returns rc=0 and every later SELECT fails with code 691. It is
    // an ACCEPTED insert, and reporting it as a rejection would be wrong: the
    // ground truth comes from a real CREATE/INSERT/SELECT cycle.
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"e":null}"#,
            &[("input_format_defaults_for_omitted_fields", "0")],
        )
        .expect("rows");
    assert_eq!(batch.outcome, Outcome::AcceptedPoisoned);
    assert_eq!(batch.err_code, 691);
    // No renderable value, and not a stored null either.
    assert_eq!(batch.rows[0].values[0].text, "");
    assert!(!batch.rows[0].values[0].null);
    let t = &batch.transformed[0];
    assert_eq!(t.reason, reason::POISONED);
    assert_eq!(t.stored, "<unreadable>");
    assert!(t.lossy());
}

// ------------------------------------------------- resource discipline

#[test]
fn a_schema_can_move_between_threads_and_answers_stably() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("x UInt8, s String").compile().unwrap();

    // Schema is Send: an executor may move a handle. It is not Sync, so it can
    // never be used from two threads at once.
    let handle = std::thread::spawn(move || {
        let mut last = String::new();
        for _ in 0..2_000 {
            let batch = schema
                .rows(Format::JsonEachRow, br#"{"x":256,"s":"hi"}"#, NO_SETTINGS)
                .expect("rows");
            assert_eq!(batch.outcome, Outcome::Accepted);
            let rendered = format!("{:?}", batch.rows[0].values);
            if !last.is_empty() {
                assert_eq!(rendered, last, "answers drifted across calls");
            }
            last = rendered;
        }
        last
    });
    let rendered = handle.join().expect("thread");
    assert!(rendered.contains("hi"), "{rendered}");
    // The stored value is ClickHouse's rendering, not a reason string.
    assert!(!rendered.contains("overflow"), "{rendered}");
}

#[test]
fn the_row_and_batch_entry_points_agree_on_one_row() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib
        .compile("x UInt8, m UInt8 MATERIALIZED x + 1, s String")
        .compile()
        .unwrap();

    // Positional formats address the k-th INSERTABLE column: MATERIALIZED
    // occupies no field position.
    let batch = stored(&schema, Format::Csv, b"7,\"hey\"\n");
    assert_eq!(batch.outcome, Outcome::Accepted, "{}", batch.err_msg);
    let row = &batch.rows[0];
    assert_eq!(row.values.len(), 2, "{:?}", row.values);
    assert_eq!(row.values[0].text, "7");
    assert_eq!(row.values[1].text, "\"hey\"");
    // MATERIALIZED is reported separately, never inside the stored row: SELECT *
    // does not return it, so a preview that mixed it in would disagree with what
    // a subscriber reading the table sees.
    assert_eq!(row.computed.len(), 1);
    assert_eq!(row.computed[0].column, "m");
    assert_eq!(row.computed[0].kind, "MATERIALIZED");
    // `8`, matching live servers on every format. The positional path used to
    // compute MATERIALIZED from the type zero (`1`) — a wrapper bug fixed
    // 2026-08-17; spec/c-abi.md §"Positional formats" records the ground truth.
    assert_eq!(row.computed[0].text, "8");

    let json = stored(&schema, Format::JsonEachRow, br#"{"x":7,"s":"hey"}"#);
    assert_eq!(json.rows[0].computed[0].text, "8");
    assert_eq!(json.rows[0].values.len(), 2);

    let single = schema.row(Format::Csv, b"7,\"hey\"").expect("row");
    assert_eq!(single.outcome, row.outcome);
    assert_eq!(single.values, row.values);
    assert_eq!(single.computed, row.computed);
}

#[test]
fn an_empty_body_is_accepted_with_zero_rows() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("x UInt8").compile().unwrap();
    let batch = stored(&schema, Format::JsonEachRow, b"");
    assert_eq!(batch.outcome, Outcome::Accepted);
    assert_eq!(batch.rows_read, 0);
    assert!(batch.rows.is_empty());
    assert!(batch.transformed.is_empty());
}

// ------------------------------------------------- compile-time settings

/// The 25.8 library — the reference line the settings-aware compile was
/// measured on — or a loud skip when it is absent. `chs_schema_compile` is
/// one of the four mandatory symbols, so there is no "predates it" case to
/// probe for any more; this only pins a stable version to assert against.
fn compile_settings_lib(reg: &Registry) -> Option<Arc<chtypes::Library>> {
    let Ok(lib) = reg.for_version("25.8") else {
        announce("\nSKIP: no 25.8 artifact loaded; the compile-settings tests target 25.8\n");
        return None;
    };
    Some(lib)
}

#[test]
fn every_loaded_artifact_reports_compile_settings_support() {
    let reg = registry!();
    for lib in reg.libraries() {
        assert!(
            lib.has_compile_settings(),
            "{} must report compile-settings support: chs_schema_compile is one of \
             the four mandatory symbols",
            lib.version()
        );
    }
}

#[test]
fn a_declared_flatten_nested_zero_compiles_one_nested_column() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let schema = lib
        .compile("n Nested(a Int64, b String)")
        .settings([("flatten_nested", "0")])
        .compile()
        .expect("compile with a settings profile");
    assert_eq!(schema.columns().len(), 1, "{:?}", schema.columns());
    assert_eq!(schema.columns()[0].name, "n");
    assert!(
        schema.columns()[0].ty.starts_with("Nested("),
        "type = {}",
        schema.columns()[0].ty
    );

    // The row path follows the compiled shape: the group key matches the one
    // column, and the stored rendering is ClickHouse's own text, verbatim.
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"n":[{"a":1,"b":"x"}]}"#,
            &[
                ("flatten_nested", "0"),
                ("input_format_import_nested_json", "1"),
            ],
        )
        .expect("rows");
    assert_eq!(batch.outcome, Outcome::Accepted, "{}", batch.err_msg);
    assert_eq!(batch.rows.len(), 1);
    assert_eq!(batch.rows[0].values.len(), 1);
    assert_eq!(batch.rows[0].values[0].text, r#"[{"a":1,"b":"x"}]"#);

    // Dotted keys are plain unknown fields against the unflattened table.
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"n.a":[1,2],"n.b":["x","y"]}"#,
            &[
                ("flatten_nested", "0"),
                ("input_format_import_nested_json", "1"),
            ],
        )
        .expect("rows");
    assert_eq!(batch.outcome, Outcome::Accepted, "{}", batch.err_msg);
    assert_eq!(batch.rows.len(), 1);
    assert_eq!(
        batch.rows[0].unknown_fields.len(),
        2,
        "dotted keys: {:?}",
        batch.rows[0].unknown_fields
    );
}

#[test]
fn an_explicit_empty_settings_call_matches_the_bare_compile() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let ddl = "n Nested(a Int64, b String), x UInt8";
    let bare = lib.compile(ddl).compile().expect("compile");
    let explicit = lib
        .compile(ddl)
        .settings(Vec::<(&str, &str)>::new())
        .compile()
        .expect("compile with an explicit empty settings call");
    assert_eq!(
        bare.columns(),
        explicit.columns(),
        "an empty settings profile must produce the identical column list"
    );
    // Under the default flatten_nested=1 the Nested column is flattened, so
    // the shared list is the three-column one.
    assert_eq!(bare.columns().len(), 3, "{:?}", bare.columns());
}

#[test]
fn the_compile_settings_error_channels_stay_distinct() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    // Unknown name: the server's own 115 on the compile channel, code visible.
    let err = lib
        .compile("x UInt8")
        .settings([("made_up_setting_xyz", "1")])
        .compile()
        .unwrap_err();
    assert_eq!(err.code(), Some(115), "unknown setting: {err}");
    assert!(!err.is_unsupported());

    // chtypes_* keys are per-call keys, not ClickHouse settings: same 115.
    let err = lib
        .compile("x UInt8")
        .settings([(chtypes::SETTING_NOW_EPOCH_NANOS, "1")])
        .compile()
        .unwrap_err();
    assert_eq!(err.code(), Some(115), "chtypes_* at compile: {err}");

    // A mode the library does not define is refused loudly with the -2
    // sentinel (`chtypes.h`: "any other value is refused loudly (-2)").
    // `CompileMode` has exactly one variant, so this crate has no way to
    // construct an out-of-range one through `.mode(...)` — documented on
    // `CompileMode` rather than exercised here.
}

#[test]
fn set_engine_validates_merge_tree_settings_and_refuses_non_defaults() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let mut schema = lib.compile("id UInt64").compile().unwrap();
    // No merge-tree settings at all: the plain engine call, exactly.
    schema
        .set_engine("MergeTree", "tuple()", NO_SETTINGS)
        .expect("empty settings");
    // Declared at the default: inert, accepted.
    schema
        .set_engine("MergeTree", "tuple()", &[("allow_nullable_key", "0")])
        .expect("declared-at-default");
    // Non-default: refused, never silently ignored.
    let err = schema
        .set_engine("MergeTree", "tuple()", &[("allow_nullable_key", "1")])
        .unwrap_err();
    assert!(err.is_unsupported(), "non-default MergeTree setting: {err}");
    assert_eq!(err.code(), Some(CODE_UNSUPPORTED));
    // Unknown name: the server's own 115, code visible.
    let err = schema
        .set_engine("MergeTree", "tuple()", &[("totally_made_up_mt", "1")])
        .unwrap_err();
    assert_eq!(err.code(), Some(115), "unknown MergeTree name: {err}");
    assert!(!err.is_unsupported());
}

#[test]
fn a_reconstructed_table_compiles_through_the_library_itself() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    // What QueryTableColumns hands back over HTTP: quoted positions on stock
    // output, bare when a client unsets the 64-bit quoting.
    let body = br#"{"name":"id","type":"UInt64","default_kind":"","default_expression":"","position":"1"}
{"name":"ts","type":"DateTime","default_kind":"DEFAULT","default_expression":"now()","position":2}
{"name":"n.a","type":"Array(Int64)","default_kind":"","default_expression":"","position":"3"}
{"name":"e","type":"UInt8","default_kind":"EPHEMERAL","default_expression":"","position":4}
{"name":"m","type":"UInt64","default_kind":"MATERIALIZED","default_expression":"id + 1","position":"5"}
"#;
    let cols = chtypes::parse_columns_result(body).expect("parse_columns_result");
    assert_eq!(cols.len(), 5);
    assert_eq!(
        cols.iter().map(|c| c.position).collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5]
    );
    let ddl = chtypes::reconstruct_ddl(&cols).expect("reconstruct_ddl");

    // The helper is a spelling exercise; the library's compile is the judge.
    let schema = lib
        .compile(&ddl)
        .compile()
        .unwrap_or_else(|e| panic!("reconstructed DDL does not compile: {e}\n{ddl}"));
    assert_eq!(schema.columns().len(), 5, "{:?}", schema.columns());
    assert_eq!(schema.columns()[2].name, "n.a");
}

#[test]
fn a_discovered_profile_compiles_the_unflattened_shape() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    // What discovery hands back from a deployment running flatten_nested=0.
    let settings =
        chtypes::parse_changed_settings_result(b"{\"name\":\"flatten_nested\",\"value\":\"0\"}\n")
            .expect("parse_changed_settings_result");
    let schema = lib
        .compile("n Nested(a Int64, b String)")
        .settings(settings)
        .compile()
        .expect("compile with the discovered profile");
    assert_eq!(schema.columns().len(), 1, "{:?}", schema.columns());
    assert_eq!(schema.columns()[0].name, "n");
    assert!(schema.columns()[0].ty.starts_with("Nested("));
}

#[test]
fn rowbinary_is_read_in_clickhouses_storage_encoding() {
    let reg = registry!();
    let lib = primary(reg);
    let schema = lib.compile("x UInt8").compile().unwrap();

    // One byte, counted — NOT NUL-terminated: binary formats contain NUL bytes.
    let batch = stored(&schema, Format::RowBinary, &[0x00]);
    assert_eq!(batch.outcome, Outcome::Accepted, "{}", batch.err_msg);
    assert_eq!(batch.rows[0].values[0].text, "0");
    // There is no input text in a binary format, so nothing is reported as
    // changed on the supplied-vs-stored axis.
    assert_eq!(batch.rows[0].transformed, vec![]);

    // A format an older tree does not know is ClickHouse's own code 73, exactly
    // as that server answers.
    let batch = stored(
        &schema,
        Format::RowBinaryWithNamesAndTypesAndDefaults,
        &[0x01],
    );
    assert_eq!(batch.outcome, Outcome::Rejected);
    assert!(
        batch.err_code == 73 || batch.err_code == 32,
        "unexpected code {} on {}: {}",
        batch.err_code,
        lib.version(),
        batch.err_msg
    );
}

// ---- the handle's profile as the row calls' defaults (added 2026-08-25) -----

/// The probe both tests below use: a `DateTime` column fed the ISO-8601
/// spelling `2020-01-02T03:04:05Z`. `date_time_input_format=best_effort` takes
/// it; `basic` stops at the seconds and rejects the trailing `Z` with
/// ClickHouse's code 27.
const ISO_Z_ROW: &[u8] = br#"{"ts":"2020-01-02T03:04:05Z"}"#;

/// `spec/c-abi.md` §"Compile-time vs per-call settings" rule 3, end to end:
///
/// ```text
/// per-call  >  handle profile  >  library defaults  >  ClickHouse defaults
/// ```
///
/// Before 2026-08-25 the middle channel did not exist — `rows` rebuilt its
/// `Settings` from the library defaults and the per-call map alone, so a
/// profile declaring an INSERT-time setting shaped the compile and changed
/// nothing about how a row parsed.
///
/// BOTH directions are asserted deliberately. The build default moved
/// mid-matrix — measured across the seven relinked artifacts,
/// 24.8/25.3/25.8/25.10 reject the ISO-Z form under no settings at all and
/// 26.5/26.6/26.7 accept it — so a one-direction test asserts nothing on half
/// the matrix.
#[test]
fn a_compile_profiles_settings_govern_row_calls() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let be = [("date_time_input_format", "best_effort")];
    let basic = [("date_time_input_format", "basic")];

    for (what, profile, per_call, want) in [
        // The declared profile reaches the row call with nothing per-call.
        (
            "profile best_effort, no per-call",
            &be,
            NO_SETTINGS,
            Outcome::Accepted,
        ),
        (
            "profile basic, no per-call",
            &basic,
            NO_SETTINGS,
            Outcome::Rejected,
        ),
        // ... and a per-call value outranks it, in both directions.
        (
            "profile basic, per-call best_effort",
            &basic,
            &be[..],
            Outcome::Accepted,
        ),
        (
            "profile best_effort, per-call basic",
            &be,
            &basic[..],
            Outcome::Rejected,
        ),
    ] {
        let schema = lib
            .compile("ts DateTime")
            .settings(profile.iter().copied())
            .compile()
            .unwrap_or_else(|e| panic!("{what}: compile: {e}"));
        let batch = schema
            .rows(Format::JsonEachRow, ISO_Z_ROW, per_call)
            .unwrap_or_else(|e| panic!("{what}: rows: {e}"));
        assert_eq!(
            batch.outcome, want,
            "{what}: code {:?} {}",
            batch.err_code, batch.err_msg
        );
    }
}

/// A handle with NO profile is untouched by the new channel: the per-call map
/// is still the only thing that can move its settings.
#[test]
fn a_profile_less_handle_is_untouched_by_the_profile_channel() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let schema = lib.compile("ts DateTime").compile().expect("compile");
    let forced = schema
        .rows(
            Format::JsonEachRow,
            ISO_Z_ROW,
            &[("date_time_input_format", "basic")],
        )
        .expect("rows");
    assert_eq!(forced.outcome, Outcome::Rejected, "{}", forced.err_msg);
    let bare = schema
        .rows(Format::JsonEachRow, ISO_Z_ROW, NO_SETTINGS)
        .expect("rows");
    assert!(matches!(
        bare.outcome,
        Outcome::Accepted | Outcome::Rejected
    ));
}

/// `spec/bindings.md` rule 12 — the SIGN of `chs_schema_engine`'s return
/// decides the KIND of error. A POSITIVE rc is the SERVER refusing a DDL that
/// can therefore never exist and must arrive as [`chtypes::Error::Schema`],
/// carrying the server's own code; a NEGATIVE rc is this library declining and
/// must stay [`chtypes::Error::Unsupported`].
#[test]
fn set_engine_tells_a_server_refusal_from_a_decline() {
    let reg = registry!();
    let Some(lib) = compile_settings_lib(reg) else {
        return;
    };
    let mut schema = lib.compile("id UInt64, sign Int8").compile().unwrap();

    // A REFUSAL: matched on the VARIANT, not only on `code()`, because the
    // variant is what a caller pattern-matches on.
    let err = schema
        .set_engine("MergeTree", "tuple()", &[("totally_made_up_mt", "1")])
        .unwrap_err();
    match &err {
        chtypes::Error::Schema { code, message, .. } => {
            assert_eq!(*code, 115, "unknown MergeTree name: {err}");
            assert!(message.contains("totally_made_up_mt"), "{message}");
        }
        other => panic!("unknown MergeTree name must be Error::Schema, got {other:?}"),
    }
    assert!(!err.is_unsupported());

    // DECLINES, every flavour the negative codes cover.
    for (what, err) in [
        (
            "unmodelled engine",
            schema
                .set_engine("NotAnEngine", "tuple()", NO_SETTINGS)
                .unwrap_err(),
        ),
        (
            "non-default MergeTree setting",
            schema
                .set_engine("MergeTree", "tuple()", &[("allow_nullable_key", "1")])
                .unwrap_err(),
        ),
        (
            "sorting key not in the schema",
            schema
                .set_engine("MergeTree", "nosuchcolumn", NO_SETTINGS)
                .unwrap_err(),
        ),
    ] {
        assert!(
            matches!(err, chtypes::Error::Unsupported { .. }),
            "{what} must be Error::Unsupported, got {err:?}"
        );
        assert!(err.is_unsupported(), "{what}: {err}");
        assert_eq!(err.code(), Some(CODE_UNSUPPORTED), "{what}");
    }
}

// ---- the ABI identity probe (chs_abi_revision, added 2026-08-25) -----------

/// `spec/c-abi.md` §ABI identity, over the whole registry.
///
/// A [`chtypes::Library`] that exists must report either this crate's
/// [`chtypes::ABI_REVISION`] or `0`. `0` means the artifact predates
/// `chs_abi_revision`, which `spec/artifact.md` §Loading defines as ignorance
/// rather than incompatibility. Any other value would have been refused at
/// load, so this asserts the invariant holds rather than trusting the loader's
/// own report.
#[test]
fn every_artifact_reports_a_compatible_abi_revision() {
    let reg = registry!();
    const { assert!(chtypes::ABI_REVISION > 0, "ABI_REVISION must be positive") };
    let mut seen = 0usize;
    let mut predates = Vec::new();
    for lib in reg.libraries() {
        let rev = lib.abi_revision();
        assert!(
            rev == 0 || rev == chtypes::ABI_REVISION,
            "{}: Library exists with ABI revision {rev}, but the loader must \
             refuse anything but {} or 0",
            lib.version(),
            chtypes::ABI_REVISION
        );
        if rev == 0 {
            predates.push(lib.minor().to_string());
        }
        seen += 1;
    }
    assert!(seen > 0, "no artifact was probed for its ABI revision");
    announce(&format!(
        "\nABI revision {}: {} artifact(s) current, {} predate the probe {predates:?}\n",
        chtypes::ABI_REVISION,
        seen - predates.len(),
        predates.len()
    ));
}

/// Two `Registry` values over one directory share ONE loaded image, so they must
/// share ONE lock.
///
/// `dlopen` refcounts a mapping per file: the second `Registry` gets a second
/// `Library` value pointing at the same C globals — including the seeded settings
/// list `set_default_settings` REPLACES while the row path reads it by reference
/// (`spec/c-abi.md` §Thread-safety: it "MUST be serialized against all other
/// calls"). Until 2026-08-26 the mutex lived in the `Library` VALUE, so the two
/// held different locks over one image and excluded nothing at all; it is now
/// interned on the canonicalized path, exactly as `chs_init` already was
/// (`spec/bindings.md` §Concurrency).
///
/// This drives the shape the fix exists for: readers on one `Registry`, the seed
/// on the other. The assertion is that every contended answer equals the
/// uncontended one, that nothing panics and that both sides really ran.
#[test]
fn one_image_means_one_lock_across_registries() {
    let Some(shared) = registry() else { return };
    let second = match Registry::new(registry_dir()) {
        Ok(r) => Arc::new(r),
        Err(err) => {
            announce(&format!("\nSKIP one_image_means_one_lock: {err}\n"));
            return;
        }
    };
    let version = shared.versions().last().expect("a version").to_string();
    let body = br#"{"a": 1}"#;

    // The uncontended answer first: the oracle the contended ones are compared to.
    let want = {
        let lib = shared.for_version(&version).expect("library");
        let schema = lib.compile("a UInt8").compile().expect("compile");
        schema
            .rows(Format::JsonEachRow, body, NO_SETTINGS)
            .expect("rows")
            .outcome
    };
    assert_eq!(want, Outcome::Accepted);

    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(1500);
    let reads = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let swaps = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let ord = std::sync::atomic::Ordering::Relaxed;

    std::thread::scope(|scope| {
        for _ in 0..4 {
            let reg = Arc::clone(shared);
            let reads = Arc::clone(&reads);
            let version = version.clone();
            scope.spawn(move || {
                let lib = reg.for_version(&version).expect("library");
                // One handle per thread: a single chs_schema * is single-threaded.
                let schema = lib.compile("a UInt8").compile().expect("compile");
                while std::time::Instant::now() < deadline {
                    let got = schema
                        .rows(Format::JsonEachRow, body, NO_SETTINGS)
                        .expect("rows");
                    assert_eq!(got.outcome, want);
                    reads.fetch_add(1, ord);
                }
            });
        }
        let writer_reg = Arc::clone(&second);
        let swaps_w = Arc::clone(&swaps);
        let version_w = version.clone();
        scope.spawn(move || {
            let lib = writer_reg.for_version(&version_w).expect("library");
            while std::time::Instant::now() < deadline {
                // Inert for an `a UInt8` row either way: what is exercised is the
                // REPLACEMENT of the list, not its content.
                let n = swaps_w.fetch_add(1, ord);
                let pairs: Vec<(&str, &str)> = if n % 2 == 0 {
                    vec![]
                } else {
                    vec![("chtypes_default_eval_wall_nanos", "2000000000")]
                };
                lib.set_default_settings(&pairs).expect("seed");
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
        });
    });

    // Leave the process as it was found.
    shared
        .for_version(&version)
        .expect("library")
        .set_default_settings(NO_SETTINGS)
        .expect("reset");

    let (r, w) = (reads.load(ord), swaps.load(ord));
    assert!(r > 100, "too few reads to be contention: {r}");
    assert!(w > 50, "the writer was starved: {w} swaps");
    announce(&format!(
        "\none image, one lock: {r} batch reads across 4 threads on registry A \
         against {w} settings swaps on registry B, 0 mismatches\n"
    ));
}
