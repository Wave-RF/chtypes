//! Issue #304's own end-to-end regression, against a real revision-5
//! artifact: `RowOptions::columns` and a compiled [`Filter`](chtypes::Filter)
//! attached to the SAME
//! [`Schema::rows_export_with_options_and_filter`](chtypes::Schema::rows_export_with_options_and_filter)
//! call compose exactly as the C ABI's `chs_rows` already allows —
//! `columns_json` and the attached filter are two independent trailing
//! parameters on the one call — and exactly as Python and TypeScript already
//! let a caller combine the two. Before this fix, neither Rust entry point
//! let a caller reach both at once: `rows_export_with` hardcoded
//! `columns: None`, so a listed EPHEMERAL column's value never reached the
//! DEFAULT a filter then reads — the issue's own measurement ("tenant is
//! empty and the filter answers f") — and `rows_export_with_options` took no
//! filter at all. The lead's ruling on this issue (option b) keeps
//! `rows_export_with`'s released 0.5.0 signature — the settings-slice form
//! below, `rows_export_with_settings_slice_still_compiles_and_behaves`, is
//! that form's own regression — and adds
//! `rows_export_with_options_and_filter` as the non-breaking variant that
//! carries both.
//!
//! Like `csv_reader.rs`, this file SKIPS loudly when no registry is found and
//! announces on the real stderr, because a suite that silently tests nothing
//! looks exactly like a suite that passes.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{
    DocFlags, Format, Library, NO_PARAMS, NO_SETTINGS, Outcome, Registry, RowOptions, Verdict,
};

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

/// Every line the registry holds that loads at ABI revision 5 — the same
/// shape as `csv_reader.rs`'s own `rev5_libraries`.
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

/// The chtypes#298 relink reached only the SUPPORTED lines' artifacts
/// (`docs/support.md`): 26.3, 26.7, 26.8 and 26.9, at `chtypes_build`
/// 1790845279 or later. A served, unsupported (retired) line gets no new
/// build or ABI revision, ever, so its existing artifact keeps the
/// pre-relink behavior permanently — measured: 26.6's newest served build,
/// 1790767905, predates this relink and was never republished.
/// `rev5_libraries` also opens such a line (anything ABI revision 5+), so
/// the three regression tests below gate on the artifact's OWN
/// `chtypes_build` (`relinked_libraries`) rather than a hand-typed line
/// list — a hand-typed list goes stale the moment a new supported line
/// ships, and silently stops exercising it.
///
/// `RELINK_BUILD_298` is the one constant `relinked_libraries` is built
/// from: the relink is a property of the BUILD, not of which lines happened
/// to be supported the day this file was written.
const RELINK_BUILD_298: i64 = 1790845279;

/// `relinked_libraries`' predicate, factored out so it can be pinned against
/// fabricated build numbers without a loaded artifact (see
/// `is_relinked_build_threshold` below). A missing or zero `chtypes_build` —
/// `0` is what a manifest that predates the field reads as — must never be
/// treated as relinked.
fn is_relinked_build(build: i64) -> bool {
    build >= RELINK_BUILD_298
}

/// Reads lib's own manifest.json for its `chtypes_build` field, the same
/// file and the same place `scripts/lib/provenance.py` reads it from: next
/// to the loaded library. There is no public accessor for this field —
/// `chtypes::Manifest` does not carry it — so this reads the manifest
/// directly rather than guessing. A manifest that cannot be read or parsed
/// panics: this gate must never default a line it could not actually
/// measure to "relinked".
fn chtypes_build_of(lib: &Library) -> i64 {
    let manifest_path = lib
        .path()
        .parent()
        .expect("a loaded library's path has a parent directory")
        .join("manifest.json");
    let text = std::fs::read_to_string(&manifest_path)
        .unwrap_or_else(|e| panic!("{}: {e}", manifest_path.display()));
    let doc: serde_json::Value =
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", manifest_path.display()));
    doc.get("chtypes_build")
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(0)
}

static RELINKED_LIBRARIES: OnceLock<Vec<Arc<Library>>> = OnceLock::new();

fn relinked_libraries() -> &'static [Arc<Library>] {
    RELINKED_LIBRARIES.get_or_init(|| {
        let mut libraries = Vec::new();
        for lib in rev5_libraries() {
            let build = chtypes_build_of(lib);
            if is_relinked_build(build) {
                libraries.push(Arc::clone(lib));
            } else {
                announce(&format!(
                    "\nskipping {} at build {build}: predates the relink {RELINK_BUILD_298}\n",
                    lib.minor()
                ));
            }
        }
        libraries
    })
}

#[test]
fn is_relinked_build_threshold() {
    assert!(is_relinked_build(RELINK_BUILD_298));
    assert!(!is_relinked_build(RELINK_BUILD_298 - 1));
    assert!(!is_relinked_build(0));
}

macro_rules! relinked {
    ($name:literal) => {{
        let libs = relinked_libraries();
        if libs.is_empty() {
            announce(concat!(
                "\nSKIP ",
                $name,
                ": needs an artifact at chtypes_build 1790845279 or later (the chtypes#298 relink)\n"
            ));
            return;
        }
        libs
    }};
}

/// `_t` is EPHEMERAL: its value is read only when `RowOptions::columns` lists
/// it, never stored, never exported. `tenant`'s DEFAULT reads `_t`, so
/// `tenant` resolves to `"acme"`/`"other"` only when `_t` was actually
/// carried through — proof the column list reached the SAME `chs_rows` call
/// as the filter, not a second, column-list-less one.
#[test]
fn rows_export_with_columns_and_filter_compose_end_to_end() {
    let libs = rev5!("rows_export_with_columns_and_filter_compose_end_to_end");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib
            .compile("id UInt32, _t String EPHEMERAL, tenant String DEFAULT _t")
            .compile()
            .expect("compile");
        let filter = schema
            .compile_filter("tenant = 'acme'", NO_PARAMS)
            .expect("compile_filter");

        let options = RowOptions {
            settings: Vec::new(),
            columns: Some(vec!["id".to_string(), "_t".to_string()]),
        };
        let body = b"{\"id\":1,\"_t\":\"acme\"}\n{\"id\":2,\"_t\":\"other\"}\n";
        let batch = schema
            .rows_export_with_options_and_filter(
                Format::JsonEachRow,
                body,
                &options,
                Some(Format::JsonCompactEachRow),
                DocFlags::ALL,
                &filter,
            )
            .expect("rows_export_with_options_and_filter");

        assert_eq!(
            batch.outcome,
            Outcome::Accepted,
            "{where_}: code {} {:?}",
            batch.err_code,
            batch.err_msg
        );
        assert_eq!(batch.rows.len(), 2, "{where_}: want 2 rows");
        assert_eq!(
            batch.rows[0].verdict,
            Some(Verdict::True),
            "{where_}: row 0 verdict — tenant == \"acme\""
        );
        assert_eq!(
            batch.rows[1].verdict,
            Some(Verdict::False),
            "{where_}: row 1 verdict — tenant == \"other\""
        );
        assert_eq!(
            (batch.rows_passed, batch.rows_cut),
            (1, 1),
            "{where_}: rows_passed/rows_cut"
        );

        let spans = batch.spans.as_ref().expect("spans");
        assert_eq!(spans.len(), 2, "{where_}: want 2 spans");
        assert_eq!(
            (spans[1].off, spans[1].len),
            (0, 0),
            "{where_}: the cut row's span must be the zero span"
        );
        let payload = batch.payload.as_ref().expect("exported bytes");
        let passed = &payload[spans[0].off..spans[0].off + spans[0].len];
        let fields: Vec<serde_json::Value> =
            serde_json::from_slice(passed).expect("one JSON array for the admitted row");
        assert_eq!(
            fields,
            vec![serde_json::json!(1), serde_json::json!("acme")],
            "{where_}: exported row {:?} — [id, tenant], the stored columns in declared order",
            String::from_utf8_lossy(passed)
        );

        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "rows_export_with_columns_and_filter_compose_end_to_end ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "rows_export_with_columns_and_filter_compose_end_to_end ran ZERO cases — a block that \
         asserts nothing is not a pass"
    );
}

/// The lead's ruling on issue #304 (option b): `Schema::rows_export_with`
/// keeps its released 0.5.0 signature — `settings: &[(K, V)]`, no
/// `RowOptions` — exactly, so a 0.5.0 caller recompiles unchanged. This is
/// that signature's own regression: the call below is only well-typed
/// against the generic settings-slice form (a `RowOptions` argument would
/// not coerce from `NO_SETTINGS`), so the file compiling at all is the
/// compile-time half of the check; the assertions are the behavior half —
/// a filter attached to the export channel with no column list still
/// answers exactly as every revision-5 release before this fix did:
/// per-row verdicts, and the exported tuple is the stored columns in
/// declared order.
#[test]
fn rows_export_with_settings_slice_still_compiles_and_behaves() {
    let libs = rev5!("rows_export_with_settings_slice_still_compiles_and_behaves");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib
            .compile("id UInt32, tenant String")
            .compile()
            .expect("compile");
        let filter = schema
            .compile_filter("tenant = 'acme'", NO_PARAMS)
            .expect("compile_filter");

        let body = b"{\"id\":1,\"tenant\":\"acme\"}\n{\"id\":2,\"tenant\":\"other\"}\n";
        // The settings-slice form, exactly as released: no RowOptions, no
        // column list.
        let batch = schema
            .rows_export_with(
                Format::JsonEachRow,
                body,
                NO_SETTINGS,
                Some(Format::JsonCompactEachRow),
                DocFlags::ALL,
                &filter,
            )
            .expect("rows_export_with");

        assert_eq!(
            batch.outcome,
            Outcome::Accepted,
            "{where_}: code {} {:?}",
            batch.err_code,
            batch.err_msg
        );
        assert_eq!(batch.rows.len(), 2, "{where_}: want 2 rows");
        assert_eq!(
            batch.rows[0].verdict,
            Some(Verdict::True),
            "{where_}: row 0 verdict — tenant == \"acme\""
        );
        assert_eq!(
            batch.rows[1].verdict,
            Some(Verdict::False),
            "{where_}: row 1 verdict — tenant == \"other\""
        );
        assert_eq!(
            (batch.rows_passed, batch.rows_cut),
            (1, 1),
            "{where_}: rows_passed/rows_cut"
        );

        let spans = batch.spans.as_ref().expect("spans");
        assert_eq!(spans.len(), 2, "{where_}: want 2 spans");
        assert_eq!(
            (spans[1].off, spans[1].len),
            (0, 0),
            "{where_}: the cut row's span must be the zero span"
        );
        let payload = batch.payload.as_ref().expect("exported bytes");
        let passed = &payload[spans[0].off..spans[0].off + spans[0].len];
        let fields: Vec<serde_json::Value> =
            serde_json::from_slice(passed).expect("one JSON array for the admitted row");
        assert_eq!(
            fields,
            vec![serde_json::json!(1), serde_json::json!("acme")],
            "{where_}: exported row {:?} — [id, tenant], the stored columns in declared order, \
             with no column list involved",
            String::from_utf8_lossy(passed)
        );

        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "rows_export_with_settings_slice_still_compiles_and_behaves ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "rows_export_with_settings_slice_still_compiles_and_behaves ran ZERO cases — a block \
         that asserts nothing is not a pass"
    );
}

// ----------------------------------------------------- chtypes#298 (G1-G5)
//
// The artifact producer's relink served at chtypes_build 1790845279 changed
// four document fields by design, measured against that build and against
// the immediately preceding build (1790783214) of the SAME ClickHouse patch
// on darwin-arm64 — same ABI revision, only the library rebuilt:
//
// * G2: `verdict_code`/`verdict_err` are now populated on a non-answered
//   verdict whose row is not itself accepted (measured: 0/"" before, the
//   row's own code/message after).
// * G3: `rows_passed`/`rows_cut` are now 0 for a batch whose own `outcome`
//   is not `Accepted`, even when an individual row before the one that
//   aborted it was itself accepted with a `True` verdict (measured: 1/0
//   before, 0/0 after).
// * G5: `export_declined` is now populated for every non-accepted batch
//   that requested an export, including a batch with ZERO rows (measured
//   on a `CsvWithNames` body naming an unknown header column under
//   `input_format_skip_unknown_fields=0`: empty before, a reason after).
//
// G4 (a skipped row counts in neither `rows_passed` nor `rows_cut`) has no
// library change — it is a docs-only clarification — so it is not measured
// here; see docs/guides/filters.md.

const CSV_REJECTION_CODE: i32 = 117;

/// G2 and G3 together: the same strict (no `input_format_allow_errors_*`)
/// body exercises both at once. Row 0 parses and is individually `Accepted`
/// with verdict `True`; row 1 is the CSV reader's own trailing-garbage
/// refusal (code 117), which aborts the batch — so the batch's own outcome
/// is `Rejected` even though row 0, in isolation, was accepted and passed
/// the filter.
#[test]
fn non_accepted_batch_zeroes_counts_and_carries_verdict_code() {
    let libs = relinked!("non_accepted_batch_zeroes_counts_and_carries_verdict_code");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib.compile("id UInt8, n UInt8").compile().expect("compile");
        let filter = schema
            .compile_filter("id >= 0", NO_PARAMS)
            .expect("compile_filter");

        let body = b"1,2\n1,abc\n"; // row 1: "abc" in a UInt8 column
        let batch = schema
            .rows_export_with(Format::Csv, body, NO_SETTINGS, None, DocFlags::ALL, &filter)
            .expect("rows_export_with");

        assert_eq!(
            batch.outcome,
            Outcome::Rejected,
            "{where_}: outcome {:?} (code {}, {:?}), want Rejected — the aborting row must \
             still reject the whole batch",
            batch.outcome,
            batch.err_code,
            batch.err_msg
        );
        // G3: the batch's own outcome is not Accepted, so BOTH counts must
        // be zero, regardless of row 0's own accepted-and-True verdict.
        assert_eq!(
            (batch.rows_passed, batch.rows_cut),
            (0, 0),
            "{where_}: rows_passed/rows_cut, want 0/0 for a batch whose own outcome ({:?}) is \
             not Accepted",
            batch.outcome
        );
        assert_eq!(batch.rows.len(), 2, "{where_}: want 2 row document(s)");
        // G2: row 1's own parse outcome is not Accepted (Rejected, code
        // 117), so its verdict is Decline and must carry that same
        // code/message beside it — never the 0/"" the pre-relink library
        // left there.
        let row1 = &batch.rows[1];
        assert_eq!(
            row1.verdict,
            Some(Verdict::Decline),
            "{where_}: row 1 verdict, want Decline — its own outcome is not Accepted"
        );
        assert!(
            row1.verdict_code == CSV_REJECTION_CODE && !row1.verdict_err.is_empty(),
            "{where_}: row 1 verdict_code/verdict_err = {}/{:?}, want {}/<non-empty> — a \
             non-accepted row's own error, beside its decline verdict",
            row1.verdict_code,
            row1.verdict_err,
            CSV_REJECTION_CODE
        );
        assert_eq!(
            row1.verdict_code, row1.err_code,
            "{where_}: verdict_code {} != its own err_code {} — the decline must carry the \
             SAME error the row's own outcome already reports",
            row1.verdict_code, row1.err_code
        );

        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "non_accepted_batch_zeroes_counts_and_carries_verdict_code ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "non_accepted_batch_zeroes_counts_and_carries_verdict_code ran ZERO cases — a block \
         that asserts nothing is not a pass"
    );
}

/// G5: a `CsvWithNames` body naming a header column no schema column
/// matches, read under `input_format_skip_unknown_fields=0`, is refused
/// before a single row is admitted — `Rejected`, `rows_read` 0, zero row
/// documents — and an export was requested. The pre-relink library left
/// `export_declined` empty here even though bytes were withheld; the
/// relinked one names the batch's own outcome as the reason.
#[test]
fn rejected_zero_row_batch_names_the_export_decline() {
    let libs = relinked!("rejected_zero_row_batch_names_the_export_decline");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib
            .compile("id UInt8, p String")
            .compile()
            .expect("compile");

        let body = b"id,unknown_col\n1,a\n";
        let settings = [("input_format_skip_unknown_fields", "0")];
        let batch = schema
            .rows_export(
                Format::CsvWithNames,
                body,
                &settings,
                Some(Format::JsonCompactEachRow),
                DocFlags::ALL,
            )
            .expect("rows_export");

        assert_eq!(
            batch.outcome,
            Outcome::Rejected,
            "{where_}: outcome {:?} (code {}, {:?}), want Rejected",
            batch.outcome,
            batch.err_code,
            batch.err_msg
        );
        assert!(
            batch.rows_read == 0 && batch.rows.is_empty(),
            "{where_}: rows_read={}, {} row document(s) — want a call-level refusal with none",
            batch.rows_read,
            batch.rows.len()
        );
        assert!(
            batch.payload.as_deref().unwrap_or(&[]).is_empty(),
            "{where_}: payload {:?}, want none — a rejected batch exports nothing",
            batch.payload
        );
        assert!(
            !batch.export_declined.is_empty(),
            "{where_}: export_declined is empty on a rejected zero-row batch that requested an \
             export — a decline must always carry its reason (chtypes#298, G5)"
        );

        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "rejected_zero_row_batch_names_the_export_decline ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "rejected_zero_row_batch_names_the_export_decline ran ZERO cases — a block that \
         asserts nothing is not a pass"
    );
}

/// G1: ClickHouse's `session_timezone` is a real, known setting name —
/// never the server's own code 115 — but this library resolves
/// bare-`DateTime` timezone once, process-wide, at `chs_init`, and never
/// re-reads it per call. Before the relink, a per-call `session_timezone`
/// was silently accepted and had no effect, indistinguishable from
/// agreement. The relinked library declines it the same way an unmodeled
/// MergeTree setting is declined (docs/guides/settings.md): it comes back
/// in `unsupported_settings`, which promotes the row's own outcome to
/// `Unsupported`.
#[test]
fn session_timezone_setting_is_declined_not_ignored() {
    let libs = relinked!("session_timezone_setting_is_declined_not_ignored");
    let mut ran = 0usize;
    for lib in libs {
        let where_ = lib.version().to_string();
        let schema = lib
            .compile("id UInt8, t DateTime")
            .compile()
            .expect("compile");

        let settings = [("session_timezone", "Europe/Berlin")];
        let result = schema
            .row_with_settings(
                Format::JsonEachRow,
                br#"{"id":1,"t":"2024-01-01 00:00:00"}"#,
                &settings,
            )
            .expect("row_with_settings");

        assert!(
            result
                .unsupported_settings
                .iter()
                .any(|s| s == "session_timezone"),
            "{where_}: unsupported_settings {:?}, want it to name session_timezone — sent on a \
             per-call map, this is a decline, never a silent admission",
            result.unsupported_settings
        );
        assert_eq!(
            result.outcome,
            Outcome::Unsupported,
            "{where_}: outcome {:?}, want Unsupported — a non-empty unsupported_settings must \
             promote the row (docs/guides/settings.md)",
            result.outcome
        );

        ran += 1;
    }
    assert_eq!(
        ran,
        libs.len(),
        "session_timezone_setting_is_declined_not_ignored ran {ran} line(s)"
    );
    assert!(
        ran > 0,
        "session_timezone_setting_is_declined_not_ignored ran ZERO cases — a block that \
         asserts nothing is not a pass"
    );
}
