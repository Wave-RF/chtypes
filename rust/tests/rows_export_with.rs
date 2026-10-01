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
