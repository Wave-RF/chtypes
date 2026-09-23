//! Issue #119 items 1-5, for `chs_format` 10 (`CsvWithNames`) and 11
//! (`TsvWithNames`): the round trip against a real artifact, the 26.4 / 26.5
//! header-matching boundary from BOTH sides, the INSERT-column-list interplay,
//! and the loud export decline. Until revision 5 was published these formats
//! were declared and executed by nothing; this file executes them.
//!
//! Three rules shape it:
//!
//! * The PROBE, never the enum and never the ABI revision, decides whether an
//!   artifact knows these two values (`docs/reference/bindings.md` §Values a
//!   binding must accept and reject). They joined `enum chs_format` inside
//!   revision 5 after the number was set, so an artifact built from an earlier
//!   revision-5 header reports 5, passes the handshake, and does not know them.
//!   Every line here is asked before it is used, with a header spelled exactly
//!   as the column is declared — a probe whose answer depended on case-folding
//!   would not ask the same question on both sides of the boundary this file
//!   measures.
//! * Every block COUNTS what it ran and fails by name on zero. A boundary case
//!   that skips forever reads as a pass otherwise, which is the one outcome
//!   worse than a red.
//! * Codes and verdicts are asserted; ClickHouse's message text never is.
//!
//! This file deliberately never asks for a line by name and never uses a
//! "primary" library: it walks every line the registry holds and decides which
//! side of the boundary each one sits on from the line itself. `primary()` in
//! the sibling integration suite resolves to the NEWEST loaded line, which is
//! above every boundary the SDK documents.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{
    BatchResult, CODE_UNSUPPORTED, DocFlags, Format, Library, NO_SETTINGS, Outcome, Registry,
    RowOptions, RowResult, Schema, Value,
};

/// The probe schema and the two probe payloads. The header spells `id` and `p`
/// exactly as the DDL declares them, so the question is the same on every line.
const WITHNAMES_DDL: &str = "id UInt8, p String";
const CSV_PROBE: &[u8] = b"id,p\n1,a\n";
const TSV_PROBE: &[u8] = b"id\tp\n1\ta\n";

/// The column-list schema: two DEFAULTs, so "took its DEFAULT" and "took the
/// reader's zero" are different observable values rather than both being 0.
const LIST_DDL: &str = "id UInt8, p UInt8 DEFAULT 7, q UInt8 DEFAULT 9";

const DEFAULTS_FOR_OMITTED: &str = "input_format_defaults_for_omitted_fields";

/// One line of the registry: loaded, and asked whether it knows 10 and 11.
struct Line {
    minor: String,
    library: Arc<Library>,
    supported: bool,
}

/// Announce on the *real* stderr. `eprintln!` is captured by libtest for a
/// passing test, so a skip printed with it is invisible — which is how a suite
/// that silently tests nothing looks exactly like a suite that passes.
fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().flush();
}

fn registry_dir() -> PathBuf {
    match std::env::var_os(chtypes::REGISTRY_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => chtypes::default_registry_dir(),
    }
}

/// Ask ONE artifact about format 10 and format 11, separately, exactly as
/// `docs/reference/bindings.md` requires: one payload each through `chs_rows`,
/// and only `accepted` counts. These two need no era refinement — both formats
/// exist on every ClickHouse line measured, so unlike `Buffers` there is no 73
/// era to allow for.
fn probe(library: &Arc<Library>) -> bool {
    let schema = match library.compile(WITHNAMES_DDL).compile() {
        Ok(s) => s,
        Err(e) => panic!("compiling the probe schema {WITHNAMES_DDL:?}: {e}"),
    };
    for (format, body) in [
        (Format::CsvWithNames, CSV_PROBE),
        (Format::TsvWithNames, TSV_PROBE),
    ] {
        match schema.rows(format, body, NO_SETTINGS) {
            Ok(res) if res.outcome == Outcome::Accepted => {}
            Ok(_) => return false,
            Err(e) => panic!("probing format {}: {e}", format.code()),
        }
    }
    true
}

/// Every line this build can open, each asked about formats 10 and 11.
///
/// Built once per process: `chs_init` runs exactly once per loaded library and
/// `cargo test` runs tests as threads of one process.
fn lines() -> Option<&'static Vec<Line>> {
    static LINES: OnceLock<Option<Vec<Line>>> = OnceLock::new();
    LINES
        .get_or_init(|| {
            let dir = registry_dir();
            if !dir.is_dir() {
                announce(&format!(
                    "\nSKIP: no chtypes artifact registry at {} — fetch one with scripts/fetch.sh \
                     26.8 (docs/guides/fetch.md), or point ${} at a registry. Every test in this \
                     file is skipped, each by name below.\n",
                    dir.display(),
                    chtypes::REGISTRY_ENV
                ));
                return None;
            }
            // Construction reads manifests and dlopens nothing; each line is
            // asked for INDIVIDUALLY so one artifact at a refused ABI revision
            // does not take the whole registry down with it. That refusal is
            // abi_revision.rs's subject, not this file's.
            let reg = match Registry::new(&dir) {
                Ok(r) => r,
                Err(e) => {
                    announce(&format!(
                        "\nSKIP: registry at {} did not open: {e}. Every test in this file is \
                         skipped.\n",
                        dir.display()
                    ));
                    return None;
                }
            };
            let mut out = Vec::new();
            for minor in reg.versions() {
                let library = match reg.for_version(&minor) {
                    Ok(l) => l,
                    Err(e) => {
                        announce(&format!(
                            "\nNOTE: line {minor} did not open, so it is not measured here: {e}\n"
                        ));
                        continue;
                    }
                };
                let supported = probe(&library);
                if !supported {
                    announce(&format!(
                        "\nNOTE: line {minor} (ClickHouse {}) does not answer the \
                         CsvWithNames/TsvWithNames probe: a pre-value artifact, not measured for \
                         the format cases\n",
                        library.version()
                    ));
                }
                out.push(Line {
                    minor,
                    library,
                    supported,
                });
            }
            if out.is_empty() {
                announce(&format!(
                    "\nSKIP: no line on the registry {} opened in this build — fetch a current one \
                     with scripts/fetch.sh 26.8. Every test in this file is skipped, each by name \
                     below.\n",
                    dir.display()
                ));
                return None;
            }
            Some(out)
        })
        .as_ref()
}

/// The calling test's name, for the per-test skip line.
macro_rules! test_name {
    () => {{
        fn here() {}
        let full = std::any::type_name_of_val(&here);
        full.strip_suffix("::here").unwrap_or(full)
    }};
}

/// Bind the registry's lines, or skip the test — by name, on the real stderr,
/// after the one message above that says why.
macro_rules! lines {
    () => {
        match lines() {
            Some(l) => l,
            None => {
                // Leading newline: libtest writes "test name ... ok" in pieces,
                // and a line landing between them would not START a line — the
                // one place a census looks for it.
                announce(&format!(
                    "\nSKIP {}: needs an artifact registry\n",
                    test_name!()
                ));
                return;
            }
        }
    };
}

/// The lines that ANSWERED the probe. A registry of pre-value artifacts is a
/// real finding, and a suite that skipped past it would look exactly like one
/// that proved these formats work — so an empty result panics rather than
/// letting every block below record zero cases quietly.
fn supported(all: &'static [Line]) -> Vec<&'static Line> {
    let out: Vec<&Line> = all.iter().filter(|l| l.supported).collect();
    assert!(
        !out.is_empty(),
        "no artifact in {} knows chs_format 10 or 11: {} line(s) opened and every one of them \
         refused the CsvWithNames/TsvWithNames probe. These formats joined enum chs_format inside \
         ABI revision 5 without bumping it, so an artifact built from an earlier revision-5 header \
         reports 5 and still does not know them — fetch a current line (scripts/fetch.sh 26.8)",
        registry_dir().display(),
        all.len()
    );
    out
}

/// True when this line matches header names EXACTLY — through 26.4 — and false
/// from 26.5, where matching is case-insensitive, exactly as those servers do.
fn header_matching_is_exact(minor: &str) -> bool {
    let mut parts = minor.split('.');
    let major: u32 = parts
        .next()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| panic!("minor line {minor:?} is not <major>.<minor>"));
    let feature: u32 = parts
        .next()
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(|| panic!("minor line {minor:?} is not <major>.<minor>"));
    (major, feature) <= (26, 4)
}

fn column_value<'a>(row: &'a RowResult, column: &str) -> &'a Value {
    row.values
        .iter()
        .find(|v| v.column == column)
        .unwrap_or_else(|| {
            let had: Vec<&str> = row.values.iter().map(|v| v.column.as_str()).collect();
            panic!("row has no value for {column:?} (it has: {had:?})")
        })
}

/// The parts of a row this file compares — verdict, values and unknown fields,
/// never ClickHouse's message text, which the CSV reader change warns consumers
/// not to pin.
fn row_shape(row: &RowResult) -> String {
    let values: Vec<String> = row
        .values
        .iter()
        .map(|v| {
            format!(
                "{}={}/{}/null={}",
                v.column,
                v.text.to_lossy(),
                v.source,
                v.null
            )
        })
        .collect();
    let transformed: Vec<String> = row
        .transformed
        .iter()
        .map(|t| format!("{}:{}", t.column, t.reason))
        .collect();
    format!(
        "outcome={:?} code={} values=[{}] unknown=[{}] transformed=[{}]",
        row.outcome,
        row.err_code,
        values.join(" "),
        row.unknown_fields.join(" "),
        transformed.join(" ")
    )
}

fn batch_shape(batch: &BatchResult) -> String {
    let rows: Vec<String> = batch.rows.iter().map(row_shape).collect();
    format!(
        "outcome={:?} code={} rows_read={} rows_skipped={}\n    {}",
        batch.outcome,
        batch.err_code,
        batch.rows_read,
        batch.rows_skipped,
        rows.join("\n    ")
    )
}

fn compiled(library: &Arc<Library>, ddl: &str) -> Schema {
    library
        .compile(ddl)
        .compile()
        .unwrap_or_else(|e| panic!("compiling {ddl:?}: {e}"))
}

fn with_columns(settings: &[(&str, &str)], columns: &[&str]) -> RowOptions {
    RowOptions {
        settings: settings
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect(),
        columns: Some(columns.iter().map(|c| (*c).to_string()).collect()),
    }
}

// ----------------------------------------------------------------- item 1

/// A body in format 10 or 11 is accepted, and its verdicts match the headerless
/// equivalent for the same rows. The reordered-header cases are the point of
/// the formats: the data is addressed by NAME, so a header naming the columns
/// in the other order still produces the declared-order stored row.
#[test]
fn withnames_round_trip_matches_headerless() {
    let all = lines!();
    let mut cases = 0usize;
    let supported = supported(all);
    for line in &supported {
        let schema = compiled(&line.library, WITHNAMES_DDL);
        for (name, named, plain, body, control) in [
            (
                "CsvWithNames",
                Format::CsvWithNames,
                Format::Csv,
                &b"id,p\n1,a\n2,b\n"[..],
                &b"1,a\n2,b\n"[..],
            ),
            (
                "CsvWithNames/reordered header",
                Format::CsvWithNames,
                Format::Csv,
                &b"p,id\na,1\nb,2\n"[..],
                &b"1,a\n2,b\n"[..],
            ),
            (
                "TsvWithNames",
                Format::TsvWithNames,
                Format::Tsv,
                &b"id\tp\n1\ta\n2\tb\n"[..],
                &b"1\ta\n2\tb\n"[..],
            ),
            (
                "TsvWithNames/reordered header",
                Format::TsvWithNames,
                Format::Tsv,
                &b"p\tid\na\t1\nb\t2\n"[..],
                &b"1\ta\n2\tb\n"[..],
            ),
        ] {
            let where_ = format!("{} ({}) {name}", line.minor, line.library.version());
            let with_names = schema.rows(named, body, NO_SETTINGS).expect("rows");
            let headerless = schema.rows(plain, control, NO_SETTINGS).expect("rows");
            // Not vacuously equal: two identical failures would satisfy the
            // comparison below and prove nothing.
            assert!(
                headerless.outcome == Outcome::Accepted && headerless.rows.len() == 2,
                "{where_}: the headerless control did not accept two rows:\n{}",
                batch_shape(&headerless)
            );
            assert!(
                with_names.outcome == Outcome::Accepted && with_names.rows.len() == 2,
                "{where_}: format {} did not accept two rows:\n{}",
                named.code(),
                batch_shape(&with_names)
            );
            assert_eq!(
                batch_shape(&with_names),
                batch_shape(&headerless),
                "{where_}: format {} answered differently from format {} for the same rows",
                named.code(),
                plain.code()
            );
            cases += 1;
        }
    }
    assert!(
        cases > 0,
        "withnames_round_trip_matches_headerless ran ZERO cases: no line in the registry answered \
         the format 10/11 probe, so nothing was compared against a headerless body"
    );
    println!(
        "{cases} round-trip case(s) over {} line(s)",
        supported.len()
    );
}

// ----------------------------------------------------------------- item 2

/// Header-name matching is EXACT through 26.4 and case-insensitive from 26.5,
/// so the same case-differing header binds differently on either side. Both
/// sides are required — a test on one side alone does not show a boundary, it
/// shows one answer.
///
/// The header is `ID,p` (and `ID<TAB>p`) against `id UInt8, p String`, with the
/// row `1,a`:
///
/// * through 26.4 — `ID` names no column: it is an UNKNOWN FIELD and `id` takes
///   the reader's zero, absent from the input.
/// * from 26.5 — `ID` names `id` case-insensitively: `id` is 1, from input, and
///   there is no unknown field.
#[test]
fn withnames_header_case_boundary() {
    let all = lines!();
    let supported = supported(all);
    let (mut below, mut above) = (0usize, 0usize);
    let (mut exact_lines, mut folding_lines) = (0usize, 0usize);
    for line in &supported {
        let exact = header_matching_is_exact(&line.minor);
        if exact {
            exact_lines += 1;
        } else {
            folding_lines += 1;
        }
        let schema = compiled(&line.library, WITHNAMES_DDL);
        for (name, format, body) in [
            ("CsvWithNames", Format::CsvWithNames, &b"ID,p\n1,a\n"[..]),
            ("TsvWithNames", Format::TsvWithNames, &b"ID\tp\n1\ta\n"[..]),
        ] {
            let where_ = format!("ClickHouse {} {name}", line.library.version());
            let res = schema.rows(format, body, NO_SETTINGS).expect("rows");
            assert!(
                res.outcome == Outcome::Accepted && res.rows.len() == 1,
                "{where_}: want one accepted row:\n{}",
                batch_shape(&res)
            );
            let row = &res.rows[0];
            let identifier = column_value(row, "id");
            let observed = format!(
                "{}|{}|{}",
                identifier.source,
                identifier.text.to_lossy(),
                row.unknown_fields.join(" ")
            );
            if exact {
                assert_eq!(
                    observed,
                    "absent|0|ID",
                    "{where_} matches header names exactly (through 26.4), so `ID` must not bind \
                     to `id`: `id` takes the reader's zero and `ID` is an unknown field; got {}",
                    row_shape(row)
                );
                below += 1;
            } else {
                assert_eq!(
                    observed,
                    "input|1|",
                    "{where_} matches header names case-insensitively (from 26.5), so `ID` must \
                     bind to `id` and leave no unknown field; got {}",
                    row_shape(row)
                );
                above += 1;
            }
            println!("{where_}: {}", row_shape(row));
        }
    }
    // Both sides, or this proved nothing. A registry holding only lines above
    // the boundary answers every case the same way and says nothing about where
    // the behavior changes.
    assert!(
        below > 0,
        "withnames_header_case_boundary ran ZERO cases BELOW the 26.5 boundary: the registry holds \
         no line at or under 26.4 that knows formats 10 and 11 (it offered {folding_lines} line(s) \
         above it). Header matching is exact through 26.4 and case-insensitive from 26.5, and one \
         side alone cannot show that — install an older line (scripts/fetch.sh 24.8, or 26.4)"
    );
    assert!(
        above > 0,
        "withnames_header_case_boundary ran ZERO cases AT OR ABOVE the 26.5 boundary: the registry \
         holds no line from 26.5 on that knows formats 10 and 11 (it offered {exact_lines} line(s) \
         below it). Install a current line (scripts/fetch.sh 26.8)"
    );
    println!(
        "{below} case(s) below the boundary over {exact_lines} line(s), {above} at or above it \
         over {folding_lines} line(s)"
    );
}

// ----------------------------------------------------------------- item 3

/// With an INSERT column list as well as a header, the LIST decides the block
/// and the HEADER decides the layout: a listed column the header omits takes
/// its `DEFAULT` under `input_format_defaults_for_omitted_fields=1` and the
/// reader's zero under `0`; an unlisted column the header names is an unknown
/// field; an unlisted column takes its `DEFAULT` whatever that setting says.
///
/// The last rule is NOT a `WithNames` rule — the 0.3.0 CHANGELOG states it
/// applies to every format — so it is executed on `JsonEachRow` and on
/// headerless CSV and TSV as well, which a case on 10 and 11 alone would not
/// show.
#[test]
fn withnames_column_list_interplay() {
    let all = lines!();
    let supported = supported(all);
    let (mut omitted, mut unknown, mut unlisted) = (0usize, 0usize, 0usize);
    for line in &supported {
        let schema = compiled(&line.library, LIST_DDL);

        for (name, format, defaults, want) in [
            ("CsvWithNames", Format::CsvWithNames, "1", "7/default"),
            ("CsvWithNames", Format::CsvWithNames, "0", "0/absent"),
            ("TsvWithNames", Format::TsvWithNames, "1", "7/default"),
            ("TsvWithNames", Format::TsvWithNames, "0", "0/absent"),
        ] {
            let where_ = format!(
                "ClickHouse {} {name} omitted-defaults={defaults}",
                line.library.version()
            );
            let options = with_columns(&[(DEFAULTS_FOR_OMITTED, defaults)], &["id", "p"]);
            let res = schema
                .rows_with_options(format, b"id\n1\n", &options)
                .expect("rows");
            assert!(
                res.outcome == Outcome::Accepted && res.rows.len() == 1,
                "{where_}: want one accepted row:\n{}",
                batch_shape(&res)
            );
            let p = column_value(&res.rows[0], "p");
            assert_eq!(
                format!("{}/{}", p.text.to_lossy(), p.source),
                want,
                "{where_}: `p` is listed and the header omits it; got {}",
                row_shape(&res.rows[0])
            );
            omitted += 1;
        }

        for (name, format, body) in [
            ("CsvWithNames", Format::CsvWithNames, &b"id,p\n1,3\n"[..]),
            ("TsvWithNames", Format::TsvWithNames, &b"id\tp\n1\t3\n"[..]),
        ] {
            let where_ = format!("ClickHouse {} {name}", line.library.version());
            let options = with_columns(&[], &["id"]);
            let res = schema
                .rows_with_options(format, body, &options)
                .expect("rows");
            assert!(
                res.outcome == Outcome::Accepted && res.rows.len() == 1,
                "{where_}: want one accepted row:\n{}",
                batch_shape(&res)
            );
            let row = &res.rows[0];
            assert_eq!(
                row.unknown_fields.join(" "),
                "p",
                "{where_}: `p` is not in the column list, so the header naming it is an unknown \
                 field; got {}",
                row_shape(row)
            );
            let p = column_value(row, "p");
            assert_eq!(
                format!("{}/{}", p.text.to_lossy(), p.source),
                "7/default",
                "{where_}: `p` is unlisted, so it takes its DEFAULT 7; got {}",
                row_shape(row)
            );
            unknown += 1;
        }

        for (name, format, body) in [
            ("JsonEachRow", Format::JsonEachRow, &b"{\"id\":1}\n"[..]),
            ("Csv", Format::Csv, &b"1\n"[..]),
            ("Tsv", Format::Tsv, &b"1\n"[..]),
        ] {
            for defaults in ["0", "1"] {
                let where_ = format!(
                    "ClickHouse {} {name} omitted-defaults={defaults}",
                    line.library.version()
                );
                let options = with_columns(&[(DEFAULTS_FOR_OMITTED, defaults)], &["id"]);
                let res = schema
                    .rows_with_options(format, body, &options)
                    .expect("rows");
                assert!(
                    res.outcome == Outcome::Accepted && res.rows.len() == 1,
                    "{where_}: want one accepted row:\n{}",
                    batch_shape(&res)
                );
                let row = &res.rows[0];
                for (column, text) in [("p", "7"), ("q", "9")] {
                    let value = column_value(row, column);
                    assert_eq!(
                        format!("{}/{}", value.text.to_lossy(), value.source),
                        format!("{text}/default"),
                        "{where_}: `{column}` is not in the column list, so it takes its DEFAULT \
                         {text} whatever {DEFAULTS_FOR_OMITTED} says; got {}",
                        row_shape(row)
                    );
                }
                unlisted += 1;
            }
        }
    }
    assert!(
        omitted > 0 && unknown > 0 && unlisted > 0,
        "withnames_column_list_interplay ran ZERO cases in at least one block: \
         listed-column-the-header-omits={omitted}, unlisted-column-the-header-names={unknown}, \
         unlisted-column-takes-its-DEFAULT={unlisted} — each must run at least once"
    );
    println!(
        "{omitted} omitted-listed-column case(s), {unknown} unknown-field case(s), {unlisted} \
         unlisted-DEFAULT case(s) over {} line(s)",
        supported.len()
    );
}

// ----------------------------------------------------------------- item 4

/// As an `export_format`, 10 and 11 are the existing loud decline, like every
/// format other than `JsonCompactEachRow`. This one needs no probe — an
/// artifact that cannot SERIALIZE a format declines it whether or not it can
/// READ it — so it runs against every line that opened.
#[test]
fn withnames_export_format_declines() {
    let all = lines!();
    let mut cases = 0usize;
    for line in all {
        let schema = compiled(&line.library, WITHNAMES_DDL);
        for format in [Format::CsvWithNames, Format::TsvWithNames] {
            let where_ = format!(
                "ClickHouse {} export_format {}",
                line.library.version(),
                format.code()
            );
            let res = schema
                .rows_export(
                    Format::JsonEachRow,
                    b"{\"id\":1,\"p\":\"a\"}\n",
                    NO_SETTINGS,
                    Some(format),
                    DocFlags::ALL,
                )
                .expect("rows_export");
            assert_eq!(
                res.outcome,
                Outcome::Unsupported,
                "{where_} must be declined, loudly:\n{}",
                batch_shape(&res)
            );
            assert_eq!(
                res.err_code, CODE_UNSUPPORTED,
                "{where_} declined with the wrong code"
            );
            assert!(
                res.payload.is_none(),
                "{where_} was declined and still produced payload bytes"
            );
            assert!(
                res.rows.is_empty() && res.rows_read == 0,
                "{where_}: a declined export processes nothing:\n{}",
                batch_shape(&res)
            );
            cases += 1;
        }
    }
    assert!(
        cases > 0,
        "withnames_export_format_declines ran ZERO cases: no line in the registry opened, so the \
         export decline for formats 10 and 11 was never asked for"
    );
    println!("{cases} export-decline case(s) over {} line(s)", all.len());
}
