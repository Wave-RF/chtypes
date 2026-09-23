//! Issue #119, items 3 and 4 (the revision-5 CSV/TSV reader) and item 1 (the
//! two revision-5 `src` provenances), executed END TO END against a real
//! revision-5 artifact rather than against a hand-built document.
//!
//! A test that hand-sets the value the code computes tests the belief, not the
//! computation, so every expectation below comes off a loaded library.
//!
//! ⚠️ Nothing here asserts on an error MESSAGE. With revision-5 artifacts a
//! rejected CSV or TSV row carries ClickHouse's own wording, which changes
//! whenever ClickHouse rewords an error; the CODE is the stable part and is the
//! only thing pinned. 15,245 TSV records in the artifact producer's corpus
//! differ in the message alone — that is the fragility this change exists to
//! warn consumers about.
//!
//! Like `integration.rs`, this file SKIPS loudly when no registry is found and
//! announces on the real stderr, because a suite that silently tests nothing
//! looks exactly like a suite that passes.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{
    DocFlags, Format, Library, Outcome, Registry, RowOptions, RowResult, reason, source,
};

/// The two rejection codes, measured on 24.8, 26.7 and 26.8 (linux-arm64) and
/// on 26.3 through 26.8 (darwin-arm64), identical on every line: ClickHouse's
/// own INCORRECT_DATA for the CSV reader's trailing-garbage refusal and
/// CANNOT_PARSE_INPUT_ASSERTION_FAILED for the TSV reader's.
const CSV_REJECTION_CODE: i32 = 117;
const TSV_REJECTION_CODE: i32 = 27;

const DEFAULTS_OFF: &[(&str, &str)] = &[("input_format_defaults_for_omitted_fields", "0")];
const DEFAULTS_ON: &[(&str, &str)] = &[("input_format_defaults_for_omitted_fields", "1")];

static LIBRARIES: OnceLock<Vec<Arc<Library>>> = OnceLock::new();

fn registry_dir() -> PathBuf {
    match std::env::var_os(chtypes::REGISTRY_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => chtypes::default_registry_dir(),
    }
}

/// `eprintln!` is captured by libtest for a passing test, so a skip printed
/// with it is invisible. This writes to the real stderr.
fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().flush();
}

/// Every line the registry holds that loads at ABI revision 5.
///
/// Every block runs against ALL of them — CI fetches three (the newest -lts,
/// the newest -stable and 24.8) — rather than one resolved through a helper
/// such as `integration.rs`'s `primary()`, which now answers the NEWEST loaded
/// line. None of these behaviors is line-sensitive and a silent retarget onto
/// a different ClickHouse would hide that. A line this platform has not
/// relinked to revision 5 refuses to load through the loader's own ABI guard:
/// that is the guard working, so it is announced and passed over.
fn rev5_libraries() -> &'static [Arc<Library>] {
    LIBRARIES.get_or_init(|| {
        let dir = registry_dir();
        if !dir.is_dir() {
            announce(&format!(
                "\nSKIP: no chtypes artifact registry at {} — fetch one with scripts/fetch.sh \
                 (docs/guides/fetch.md), or point ${} at a registry. Every test in this file is \
                 skipped, each by name below.\n",
                dir.display(),
                chtypes::REGISTRY_ENV
            ));
            return Vec::new();
        }
        let reg = match Registry::new(&dir) {
            Ok(r) => r,
            Err(e) => {
                announce(&format!(
                    "\nSKIP: registry at {} did not open: {e}. Every test in this file is \
                     skipped.\n",
                    dir.display()
                ));
                return Vec::new();
            }
        };
        let mut libraries = Vec::new();
        for line in reg.versions() {
            match reg.for_version(&line) {
                Ok(lib) if lib.abi_revision() >= 5 => libraries.push(lib),
                Ok(lib) => announce(&format!(
                    "\nline {line} not exercised: ABI revision {}, these cases need 5\n",
                    lib.abi_revision()
                )),
                Err(e) => announce(&format!("\nline {line} not exercised: {e}\n")),
            }
        }
        if libraries.is_empty() {
            announce(&format!(
                "\nSKIP: registry {} holds no ABI revision-5 artifact — every case in this file \
                 needs one. Each test below is skipped by name.\n",
                dir.display()
            ));
        }
        libraries
    })
}

macro_rules! rev5 {
    ($name:literal) => {{
        let libs = rev5_libraries();
        if libs.is_empty() {
            // Leading newline: libtest writes "test name ... ok" in pieces, and
            // a line landing between them would not START a line.
            announce(concat!(
                "\nSKIP ",
                $name,
                ": needs a revision-5 artifact registry\n"
            ));
            return;
        }
        libs
    }};
}

/// The text of one column's stored value, or `None` when the row carries none.
fn stored_text(row: &RowResult, column: &str) -> Option<String> {
    row.values
        .iter()
        .find(|v| v.column == column)
        .map(|v| v.text.as_str().unwrap_or("<non-utf8>").to_string())
}

/// The transform reasons reported for one column.
fn reasons_for(row: &RowResult, column: &str) -> Vec<String> {
    row.transformed
        .iter()
        .filter(|t| t.column == column)
        .map(|t| t.reason.clone())
        .collect()
}

// ----------------------------------------- item 3: codes, not messages

/// A CSV row the library rejects returns the same code as the pre-revision-5
/// path, and so does a TSV one; only the wording moved.
///
/// A rejected CSV row also now carries an EMPTY per-column list, where the
/// previous reader could report a partial one — a consumer reading per-column
/// detail off a rejected row sees this. That detail was measured for CSV
/// rejections and is asserted for CSV only; the TSV row's own emptiness is
/// announced, not pinned.
#[test]
fn csv_and_tsv_rejections_keep_their_codes() {
    let libs = rev5!("csv_and_tsv_rejections_keep_their_codes");
    // `abc` in the second field: the first field parses, so a reader that
    // reported per-column detail for what it managed to read would report one
    // column here. That is the shape the empty-cols assertion pins.
    let cases: &[(&str, Format, &[u8], i32, bool)] = &[
        ("CSV", Format::Csv, b"1,abc\n", CSV_REJECTION_CODE, true),
        ("TSV", Format::Tsv, b"1\tabc\n", TSV_REJECTION_CODE, false),
    ];
    let mut ran = 0usize;
    for lib in libs {
        let schema = lib.compile("id UInt8, n UInt8").compile().expect("compile");
        for (name, format, body, code, empty_cols) in cases {
            let where_ = format!("{} {name}", lib.version());
            let row = schema.row(*format, body).expect("row");
            assert_eq!(
                row.outcome,
                Outcome::Rejected,
                "{where_}: the row must be refused, not coerced"
            );
            // The CODE, and only the code. `err_msg` is deliberately never
            // asserted — it is ClickHouse's own text now.
            assert_eq!(
                row.err_code, *code,
                "{where_}: message, not asserted, was {:?}",
                row.err_msg
            );
            if *empty_cols {
                assert!(
                    row.values.is_empty(),
                    "{where_}: a rejected row reported per-column values {:?} — the revision-5 \
                     reader reports no partial column list for a row it refused",
                    row.values
                );
                assert!(
                    row.transformed.is_empty(),
                    "{where_}: a rejected row reported transforms {:?} — there is no stored row \
                     to have changed",
                    row.transformed
                );
            } else {
                println!(
                    "{where_} rejected row carries {} value(s) (not pinned: the empty-cols \
                     measurement is CSV-scoped)",
                    row.values.len()
                );
            }
            ran += 1;

            // The same refusal through the batch path, where an allowed error
            // budget turns it into a skipped row rather than a rejected batch.
            let batch = schema
                .rows(*format, body, &[("input_format_allow_errors_num", "10")])
                .expect("rows");
            assert_eq!(batch.rows.len(), 1, "{where_}: want one row document");
            assert_eq!(batch.rows[0].outcome, Outcome::Skipped, "{where_}");
            assert_eq!(
                batch.rows[0].err_code, *code,
                "{where_}: batch row; message, not asserted, was {:?}",
                batch.rows[0].err_msg
            );
            if *empty_cols {
                assert!(batch.rows[0].values.is_empty(), "{where_}: batch row");
            }
            ran += 1;
        }
    }
    // The count assertion. A block that exercised a library and then ran
    // nothing must say so by name rather than pass.
    assert_eq!(
        ran,
        4 * libs.len(),
        "csv_and_tsv_rejections_keep_their_codes ran {ran} case(s) over {} line(s)",
        libs.len()
    );
    assert!(
        ran > 0,
        "csv_and_tsv_rejections_keep_their_codes ran ZERO cases — a block that asserts nothing \
         is not a pass"
    );
}

// --------------------------- item 4: the empty CSV field under `=0`

/// The three column types and the transform reason MEASURED for each when a CSV
/// empty field arrives under `input_format_defaults_for_omitted_fields=0`.
///
/// ⚠️ These three are WHAT WAS MEASURED, not a closed set. The detector's
/// switch would assign `date_clamp`, `ip_mangle` and others for other column
/// types, and nothing has measured whether an empty field reaches them. Nothing
/// below asserts the set is exactly three, and a fourth column type arriving
/// must not make this test red.
///
/// The Enum carries a member at value 0: an empty field is not a member
/// spelling, the reader's zero is, and the coercion between the two is what
/// `enum_coerce` names.
const EMPTY_FIELD_CASES: &[(&str, &str, &str)] = &[
    (
        "Enum8",
        "id UInt8, c Enum8('a' = 0, 'b' = 1)",
        reason::ENUM_COERCE,
    ),
    (
        "FixedString",
        "id UInt8, c FixedString(4)",
        reason::FIXEDSTRING_PAD,
    ),
    ("UUID", "id UInt8, c UUID", reason::UUID_MANGLE),
];

/// An empty CSV field under `input_format_defaults_for_omitted_fields=0` is
/// reported as an INPUT and its coercion as a TRANSFORM, where the previous
/// reader reported no transform: the field's reference value is the empty
/// string rather than absent. The STORED VALUE is unchanged — this changes what
/// is reported, not what is stored.
///
/// ⚠️ The setting is set explicitly on every call. Under the default (`1`) a
/// bare empty field still takes the column's DEFAULT, so a case that forgot the
/// setting would pass without ever exercising this.
///
/// ⚠️ TSV is NOT affected by the setting: its reader takes an empty field
/// through the typed parse whether the setting is on or off, exactly as the
/// previous splitter did. What is pinned for TSV is that the two settings give
/// the IDENTICAL answer — that is the asymmetry, and a later change making TSV
/// setting-sensitive like CSV turns it red. Note, measured: TSV DOES report
/// `fixedstring_pad` for a FixedString empty field under BOTH settings and has
/// always done so; "TSV is not affected" means the reader swap did not change
/// TSV's answer, never that TSV reports no transform at all. Do not "fix" that
/// by asserting TSV reports nothing.
#[test]
fn empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform() {
    let libs = rev5!("empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform");
    let mut ran = 0usize;
    for lib in libs {
        for (name, ddl, want_reason) in EMPTY_FIELD_CASES {
            let where_ = format!("{} {name}", lib.version());
            let schema = lib.compile(ddl).compile().expect("compile");
            let off = schema
                .row_with_settings(Format::Csv, b"1,\n", DEFAULTS_OFF)
                .expect("csv =0");
            let on = schema
                .row_with_settings(Format::Csv, b"1,\n", DEFAULTS_ON)
                .expect("csv =1");

            // The empty field is reported as an INPUT: it is in the stored row,
            // with src `input` — not absent, not a default.
            let off_value = off
                .values
                .iter()
                .find(|v| v.column == "c")
                .unwrap_or_else(|| {
                    panic!("{where_}: CSV =0 reported no value at all for the empty field")
                });
            assert_eq!(
                off_value.source, "input",
                "{where_}: the empty field is an input, not an omitted column"
            );

            // ... and its coercion as a TRANSFORM, with the measured reason.
            let off_reasons = reasons_for(&off, "c");
            let on_reasons = reasons_for(&on, "c");
            assert!(
                off_reasons.iter().any(|r| r == want_reason),
                "{where_}: CSV =0 reported {off_reasons:?}, want {want_reason:?} among them"
            );
            // Under the default the same field reports no such transform —
            // which is what makes the setting load-bearing rather than
            // decorative. (Not "no transforms at all": that would be a claim
            // about column types nobody measured.)
            assert!(
                !on_reasons.iter().any(|r| r == want_reason),
                "{where_}: CSV =1 reported {want_reason:?} too — under the default an empty \
                 field still takes the column's DEFAULT and this reason must not appear, or the \
                 =0 case proves nothing"
            );

            // THE STORED VALUE IS UNCHANGED. This changes what is reported,
            // never what is stored.
            assert_eq!(
                stored_text(&off, "c"),
                stored_text(&on, "c"),
                "{where_}: stored value differs between =0 and =1 — this change is about what \
                 is REPORTED, never about what is stored"
            );

            // TSV: the setting reaches nothing. The two answers must be the
            // same answer, whatever that answer is.
            let tsv_off = schema
                .row_with_settings(Format::Tsv, b"1\t\n", DEFAULTS_OFF)
                .expect("tsv =0");
            let tsv_on = schema
                .row_with_settings(Format::Tsv, b"1\t\n", DEFAULTS_ON)
                .expect("tsv =1");
            let note = format!(
                "{where_}: TSV's reader takes an empty field through the typed parse whether the \
                 setting is on or off; a difference here means TSV has started behaving like CSV"
            );
            assert_eq!(
                (tsv_off.outcome, tsv_off.err_code),
                (tsv_on.outcome, tsv_on.err_code),
                "{note}"
            );
            assert_eq!(
                reasons_for(&tsv_off, "c"),
                reasons_for(&tsv_on, "c"),
                "{note}"
            );
            assert_eq!(
                stored_text(&tsv_off, "c"),
                stored_text(&tsv_on, "c"),
                "{note}"
            );
            println!(
                "{where_}: CSV =0 {off_reasons:?} / =1 {on_reasons:?}   TSV =0 {:?} / =1 {:?}",
                reasons_for(&tsv_off, "c"),
                reasons_for(&tsv_on, "c")
            );
            ran += 1;
        }
    }
    assert_eq!(
        ran,
        EMPTY_FIELD_CASES.len() * libs.len(),
        "empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform ran {ran} column \
         type(s) over {} line(s)",
        libs.len()
    );
    assert!(
        ran > 0,
        "empty_csv_field_under_defaults_zero_reports_an_input_and_a_transform ran ZERO cases — \
         a block that asserts nothing is not a pass"
    );
}

// --------------------------- item 1: the two `src` provenances

/// A listed EPHEMERAL column is READ — it is in scope for the DEFAULT
/// expressions that reference it — and is never stored and never exported.
///
/// The plant that makes this a test rather than a hope: drop
/// `|| c.src == source::EPHEMERAL_INPUT` from `result.rs`'s values loop and
/// this test goes red on its "values must not contain e" assertion, naming the
/// source the artifact really reported.
#[test]
fn ephemeral_input_is_read_never_stored_never_exported() {
    let libs = rev5!("ephemeral_input_is_read_never_stored_never_exported");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        // e is EPHEMERAL: it has no value at all outside a column list, and d
        // reads it. d == 6 is the proof the value was read.
        let schema = lib
            .compile("id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1")
            .compile()
            .expect("compile");
        let options = RowOptions {
            columns: Some(vec!["id".to_string(), "e".to_string()]),
            ..RowOptions::default()
        };
        let row = schema
            .row_with_options(Format::JsonEachRow, br#"{"id":3,"e":5}"#, &options)
            .expect("row");
        assert_eq!(
            row.outcome,
            Outcome::Accepted,
            "{where_}: code {} {:?}",
            row.err_code,
            row.err_msg
        );
        assert_eq!(
            stored_text(&row, "d").as_deref(),
            Some("6"),
            "{where_}: a listed EPHEMERAL column's value must reach the DEFAULT expressions \
             referencing it"
        );
        if let Some(v) = row.values.iter().find(|v| v.column == "e") {
            panic!(
                "{where_}: values contains e with src {:?} — a listed EPHEMERAL column is never \
                 stored, so it must never sit where a caller reads the stored row",
                v.source
            );
        }
        assert!(
            !row.values
                .iter()
                .any(|v| v.source == source::EPHEMERAL_INPUT),
            "{where_}: values carries a {:?} column — it is excluded from the stored-row view \
             exactly as {:?} is",
            source::EPHEMERAL_INPUT,
            source::SKIPPED
        );

        // ... and never EXPORTED. The exported tuple is the wire tuple —
        // declared order minus MATERIALIZED/ALIAS/EPHEMERAL — so the row is
        // [id, d] and the ephemeral 5 is nowhere in it. Parsed rather than
        // string-compared: the separator spacing is the vendored writer's, not
        // this repository's, and pinning it would test the wrong thing.
        let batch = schema
            .rows_export_with_options(
                Format::JsonEachRow,
                br#"{"id":3,"e":5}"#,
                &options,
                Some(Format::JsonCompactEachRow),
                DocFlags::ALL,
            )
            .expect("rows_export");
        assert_eq!(
            batch.outcome,
            Outcome::Accepted,
            "{where_}: export declined {:?}",
            batch.export_declined
        );
        let payload = batch.payload.as_ref().expect("exported bytes");
        let fields: Vec<serde_json::Value> =
            serde_json::from_slice(payload).expect("one JSON array per exported row");
        assert_eq!(
            fields,
            vec![serde_json::json!(3), serde_json::json!(6)],
            "{where_}: exported row {:?} — the wire tuple is declared order minus \
             MATERIALIZED/ALIAS/EPHEMERAL",
            String::from_utf8_lossy(payload)
        );
        assert!(
            !fields.contains(&serde_json::json!(5)),
            "{where_}: the EPHEMERAL value 5 rode in the exported bytes — a listed EPHEMERAL \
             column is never exported"
        );
        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "ephemeral_input_is_read_never_stored_never_exported ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "ephemeral_input_is_read_never_stored_never_exported ran ZERO cases — a block that \
         asserts nothing is not a pass"
    );
}

/// The other half: the two provenances get OPPOSITE treatment, and folding them
/// together is the mistake this guards.
///
/// ⚠️ The supplied value must be the stored one: `m Int64 MATERIALIZED id + 10`
/// with id = 1 would compute 11, and 99 is what was sent. Reading 11 here would
/// mean the supplied value never replaced the expression.
///
/// ⚠️ The export channel DECLINES this row, loudly, and that is the C ABI
/// contract's own rule rather than a defect: the exported tuple is the wire
/// tuple (declared order minus MATERIALIZED/ALIAS/EPHEMERAL), which has no
/// position for a MATERIALIZED column, so bytes that carried the supplied value
/// could not be re-INSERTed. Fail-closed with the reason in `export_declined`
/// is what the header specifies, and it is asserted here so a later silent
/// emission would show up.
#[test]
fn materialized_input_is_stored_and_stays_in_values() {
    let libs = rev5!("materialized_input_is_stored_and_stays_in_values");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib
            .compile("id UInt32, p UInt8 DEFAULT 3, m Int64 MATERIALIZED id + 10")
            .compile()
            .expect("compile");
        let options = RowOptions {
            settings: vec![(
                "insert_allow_materialized_columns".to_string(),
                "1".to_string(),
            )],
            columns: Some(vec!["id".to_string(), "m".to_string()]),
        };
        let row = schema
            .row_with_options(Format::JsonEachRow, br#"{"id":1,"m":99}"#, &options)
            .expect("row");
        assert_eq!(
            row.outcome,
            Outcome::Accepted,
            "{where_}: code {} {:?}",
            row.err_code,
            row.err_msg
        );
        let m = row
            .values
            .iter()
            .find(|v| v.column == "m")
            .unwrap_or_else(|| {
                panic!(
                    "{where_}: values has no m — a listed MATERIALIZED column's supplied value IS \
                 stored and stays IN the stored-row view, unlike {:?}",
                    source::EPHEMERAL_INPUT
                )
            });
        assert_eq!(m.source, source::MATERIALIZED_INPUT, "{where_}");
        // ⚠️ 24.8 renders a stored Int64 as a JSON string and 25.8/26.8 as a
        // number — ClickHouse's own 64-bit quoting changing between lines,
        // which belongs to the artifact and is never normalized here. Both
        // spellings are the same stored value; neither is 11.
        let text = m.text.as_str().unwrap_or("<non-utf8>");
        assert!(
            text == "99" || text == "\"99\"",
            "{where_}: m is {text:?}, want the SUPPLIED 99 (as a number or, on a line that \
             quotes 64-bit integers, as \"99\") — not the expression's 11"
        );
        let computed = row
            .computed
            .iter()
            .find(|c| c.column == "m")
            .map(|c| c.text.as_str().unwrap_or("<non-utf8>").to_string());
        assert_eq!(
            computed.as_deref(),
            Some(text),
            "{where_}: the same stored value, reported twice"
        );

        // The export channel's documented refusal.
        let batch = schema
            .rows_export_with_options(
                Format::JsonEachRow,
                br#"{"id":1,"m":99}"#,
                &options,
                Some(Format::JsonCompactEachRow),
                DocFlags::ALL,
            )
            .expect("rows_export");
        assert!(
            batch.payload.as_ref().is_none_or(|p| p.is_empty()),
            "{where_}: export emitted {:?} — the wire tuple has no position for a MATERIALIZED \
             column, so bytes carrying the supplied value could not be re-INSERTed; the contract \
             is fail-closed",
            batch
                .payload
                .as_ref()
                .map(|p| String::from_utf8_lossy(p).to_string())
        );
        assert!(
            !batch.export_declined.is_empty(),
            "{where_}: export withheld its bytes without saying why — a decline carries its reason"
        );
        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "materialized_input_is_stored_and_stays_in_values ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "materialized_input_is_stored_and_stays_in_values ran ZERO cases — a block that asserts \
         nothing is not a pass"
    );
}
