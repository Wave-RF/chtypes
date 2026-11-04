//! The `null_input` decode cells, through the public API, over the generated
//! v2 stub (variant `null-input`). The stub answers
//! `scripts/abi-v1/emit/_stubshared.py`'s `NULL_INPUT_DOCS`: for a JSON null
//! and a TSV `\N` into each of six non-Nullable types, the entry carries
//! `null_input` true; two controls carry it false and absent. Each entry's
//! expected value is read off its name (a `control.` name expects false), and
//! the cell count is the generator's `NULL_INPUT_CELL_COUNT`, pinned here as 12
//! (the Python suite fails first if the generator changes it), so a document
//! that lost its cells fails.
//!
//! `CHTYPES_ABI2_STUBS` unset: this suite skips LOUDLY by name and passes. One
//! test function, deliberately: it sets the process environment the
//! unverified open needs.
//!
//! The same function then runs the `poison` and `dup_dropped` cells over the
//! `value-flags` variant (`VALUE_FLAGS_DOCS`): an Enum DEFAULT outside its
//! domain and a Map key named twice, with controls holding each member false
//! and absent; a `poison.` entry expects `poison` only, a `dup.` entry
//! `dup_dropped` only.

use std::path::{Path, PathBuf};

use chtypes::{CompileOptions, Format, Library, RowOptions, RowsOptions, Value};

const ENV_STUBS: &str = "CHTYPES_ABI2_STUBS";
const ENV_UNVERIFIED: &str = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY";
/// `_stubshared.NULL_INPUT_CELL_COUNT`: 2 formats x 6 types.
const CELL_COUNT: usize = 12;
/// `_stubshared.VALUE_FLAGS_POISON_CELLS` and `VALUE_FLAGS_DUP_CELLS`.
const POISON_CELLS: usize = 2;
const DUP_CELLS: usize = 1;

fn open_variant(dir: &Path, variant: &str) -> std::sync::Arc<Library> {
    let text = std::fs::read_to_string(dir.join("stubs.json")).expect("read stubs.json");
    let manifest: serde_json::Value = serde_json::from_str(&text).expect("parse stubs.json");
    let path = manifest["variants"][variant]["path"]
        .as_str()
        .unwrap_or_else(|| panic!("stubs.json names no {variant} variant"));
    let file = dir.join(Path::new(path).file_name().expect("file name"));
    Library::open_unverified(file, true)
        .unwrap_or_else(|e| panic!("the {variant} stub does not open: {e}"))
}

fn check(where_: &str, cols: &[Value]) -> usize {
    let mut cells = 0;
    for c in cols {
        let want = !c.column.as_bytes().starts_with(b"control.");
        if want {
            cells += 1;
        }
        assert_eq!(
            c.null_input, want,
            "{where_}: column {:?}: null_input",
            c.column
        );
    }
    cells
}

/// A `poison.` entry expects `poison` true and `dup_dropped` false, a `dup.`
/// entry the reverse, a `control.` entry both false, so a binding that swaps
/// the members fails. Returns the poison and dup_dropped cell counts.
fn check_flags(where_: &str, cols: &[Value]) -> (usize, usize) {
    let (mut poison, mut dup) = (0, 0);
    for c in cols {
        let name = c.column.as_bytes();
        let want_p = name.starts_with(b"poison.");
        let want_d = name.starts_with(b"dup.");
        assert!(
            want_p || want_d || name.starts_with(b"control."),
            "{where_}: unexpected column {:?}",
            c.column
        );
        poison += usize::from(want_p);
        dup += usize::from(want_d);
        assert_eq!(
            (c.poison, c.dup_dropped),
            (want_p, want_d),
            "{where_}: column {:?}: (poison, dup_dropped)",
            c.column
        );
    }
    (poison, dup)
}

#[test]
fn null_input_is_exposed_over_the_v2_stub() {
    let Some(dir) = std::env::var_os(ENV_STUBS).map(PathBuf::from) else {
        eprintln!(
            "SKIPPED (loudly): null_input needs {ENV_STUBS} (the ABI v2 stub directory); nothing was exercised"
        );
        return;
    };
    // SAFETY: this is the only test in the process, so nothing reads or writes
    // the environment concurrently.
    unsafe { std::env::set_var(ENV_UNVERIFIED, "1") };

    let lib = open_variant(&dir, "null-input");
    let schema = lib
        .compile_table("CREATE TABLE t (x Int32)", &CompileOptions::default())
        .expect("compile");
    let r = schema
        .row(
            Format::JsonEachRow,
            br#"{"x":null}"#,
            &RowOptions::default(),
        )
        .expect("row");
    assert_eq!(check("row", &r.columns), CELL_COUNT, "{r:?}");

    let b = schema
        .rows(Format::Tsv, b"\\N\n", &RowsOptions::default())
        .expect("batch");
    let cells: usize = b
        .rows
        .iter()
        .enumerate()
        .map(|(i, row)| check(&format!("batch.rows[{i}]"), &row.columns))
        .sum();
    assert_eq!(cells, CELL_COUNT, "{b:?}");

    // The poison and dup_dropped cells (variant "value-flags"): an Enum
    // DEFAULT outside its domain left out of the row, and a Map key named
    // twice in a JSONEachRow object.
    let lib = open_variant(&dir, "value-flags");
    let schema = lib
        .compile_table("CREATE TABLE t (x Int32)", &CompileOptions::default())
        .expect("compile");
    let r = schema
        .row(
            Format::JsonEachRow,
            br#"{"m":{"a":1,"a":2}}"#,
            &RowOptions::default(),
        )
        .expect("row");
    assert_eq!(
        check_flags("row", &r.columns),
        (POISON_CELLS, DUP_CELLS),
        "{r:?}"
    );
    let b = schema
        .rows(Format::Tsv, b"1\n", &RowsOptions::default())
        .expect("batch");
    let (mut poison, mut dup) = (0, 0);
    for (i, row) in b.rows.iter().enumerate() {
        let (p, d) = check_flags(&format!("batch.rows[{i}]"), &row.columns);
        poison += p;
        dup += d;
    }
    assert_eq!((poison, dup), (POISON_CELLS, DUP_CELLS), "{b:?}");
}
