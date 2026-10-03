//! The Rust tour of chtypes, on the v1 API (`docs/reference/bindings-v1.md`).
//!
//! chtypes answers one question: "if this row were inserted into this table on
//! this ClickHouse version, what would happen?" without a server. The answers
//! come from ClickHouse's own C++, vendored per release into a shared library
//! behind the `chs_*` C ABI, which is why they are exact rather than
//! approximately right.
//!
//! This file is a tutorial you RUN. Seventeen numbered sections walk the public
//! API of the Rust SDK, each with a comment saying what it demonstrates, why an
//! ingest pipeline cares and what to look for in the output. The same section
//! numbers, schemas and rows exist in the Go, Python and TypeScript tours, so
//! two tours can be diffed and only the language idioms differ. A section whose
//! v0 feature the v1 API deletes (`bindings-v1.md` section 7) keeps its number
//! and prints which deletion removed it.
//!
//! It needs a real artifact: the tour opens a release through the v1 fetch
//! layer (a signed OCI artifact, verified and cached), so the first run needs
//! the network, or an artifact already in the cache.
//!
//! ```text
//! chtypes fetch 26.8                  # once, with the CLI (cargo run --bin chtypes -- fetch 26.8)
//! CHTYPES_AUTOFETCH=1 cargo run       # or let the registry fetch what it lacks
//! CHTYPES_VERSION=26.8 cargo run      # pick a line (default 26.8)
//! ../chplay.sh rust                   # the same, with prerequisite checks
//! ```
//!
//! Nothing here is a test; the real suites live in `rust/`. Every value printed
//! below is produced by the run, never written down by hand.

use chtypes::{
    BatchResult, CompileOptions, DocFlags, Error, EvalOptions, FilterOptions, FilterOutcome,
    FilterResult, Format, Library, Outcome, Registry, RegistryOptions, RowOptions, RowsOptions,
    SetupOptions, Value, Verdict,
};
use std::sync::Arc;

// ------------------------------------------------------------- the fixture
//
// These schemas are IDENTICAL in all four tours.

/// The table for the DEFAULT and result sections: a volatile DEFAULT (now64), a
/// literal DEFAULT, an Enum (how a table gets poisoned) and a MATERIALIZED
/// column (never in SELECT *).
const DEMO_COLUMNS: &str = "ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)";

/// A small three-column table for the outcome and format sections.
const FORMAT_COLUMNS: &str =
    "device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown'";

// Hand-built binary payloads (hex), shared by all four tours.
const ROW_BINARY_OK: &str = "0100000007026f6b"; // UInt32 LE 1, UInt8 7, varint-len "ok"
const RBWD_MARKER: &str = "00020000000100026869"; // device_id=2 by value, seq by marker, label="hi"
const RBWD_POISON: &str = "000100000001"; // id=1 by value, e by marker -> raw 0, no name
const RBWD_VALUE: &str = "00010000000002"; // id=1 by value, e by value 2
/// RowBinaryWithNamesAndTypesAndDefaults for FORMAT_COLUMNS: 3 columns,
/// device_id=1 by value, seq by marker (DEFAULT 7), label="ok".
const RBWNTD_ROW: &str = concat!(
    "03",
    "096465766963655f6964",
    "03736571",
    "056c6162656c",
    "0655496e743332",
    "0555496e7438",
    "06537472696e67",
    "00",
    "01000000",
    "01",
    "00",
    "026f6b"
);
// Native blocks captured from a live 25.8 (SELECT ... FORMAT Native).
const NATIVE_OK: &str = "0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b";
const NATIVE_CAST: &str = "0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374";

// Section 11's CANNED answer to the discovery query: shaped like a real
// server's JSONEachRow answer over `system.columns` (quoted UInt64s and all).
const CANNED_COLUMNS_RESULT: &[u8] = b"{\"name\":\"ts\",\"type\":\"DateTime64(3)\",\"default_kind\":\"DEFAULT\",\"default_expression\":\"now64(3)\",\"position\":\"1\"}\n{\"name\":\"device_id\",\"type\":\"UInt32\",\"default_kind\":\"\",\"default_expression\":\"\",\"position\":\"2\"}\n{\"name\":\"reading c\",\"type\":\"Float64\",\"default_kind\":\"\",\"default_expression\":\"\",\"position\":\"3\"}\n{\"name\":\"note\",\"type\":\"String\",\"default_kind\":\"DEFAULT\",\"default_expression\":\"'unset'\",\"position\":\"4\"}\n";

/// One CREATE TABLE statement over `columns`: compile takes exactly one.
fn ddl(columns: &str) -> String {
    format!("CREATE TABLE t ({columns}) ENGINE = MergeTree ORDER BY tuple()")
}

/// Owned `(name, value)` pairs from borrowed ones: settings values are strings
/// and cross the boundary exactly as written.
fn pairs(p: &[(&str, &str)]) -> Vec<(String, String)> {
    p.iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

fn rows_opts(p: &[(&str, &str)]) -> RowsOptions {
    RowsOptions {
        settings: pairs(p),
        ..RowsOptions::default()
    }
}

fn compile(lib: &Arc<Library>, columns: &str) -> chtypes::Schema {
    match lib.compile_table(ddl(columns), &CompileOptions::default()) {
        Ok(s) => s,
        Err(err) => fatal(&format!("compile {columns:?}: {err}")),
    }
}

fn main() {
    // Section 1 opens the registry and the library; everything after reuses them.
    let (registry, lib) = section1();
    section2(&lib);
    section3(&lib);
    section4(&lib);
    section5(&lib);
    section6(&lib);
    section7(&lib);
    section8(&lib);
    section9(&lib);
    section10(&lib, &registry);
    section11(&lib);
    section12(&registry);
    section13(&lib);
    section14();
    section15(&lib);
    section16(&lib);
    section17(&lib);
    blank();
    println!("Done. Every value above was measured by this run.");
}

// ---------------------------------------------------------------------------
// SECTION 1: Set up the process, open a library
//
// WHAT: the optional process setup, a registry over the v1 fetch layer, and one
// open version.
// WHY: an ingest gateway serves tenants on different ClickHouse versions; the
// registry is how one process answers for all of them. The image zone and the
// default settings are fixed once per process, before traffic.
// LOOK FOR: the library naming ITSELF (build_info), the fetch record that
// opened it (who signed it, where it came from) and the refusals: a spelling
// the fetch layer rejects, and a version nothing answers.
// ---------------------------------------------------------------------------
fn section1() -> (Registry, Arc<Library>) {
    section(1, "Set up the process, open a library");

    // `setup` is optional and first. A second, different setup is refused:
    // the zone and defaults are process-once.
    let setup = SetupOptions {
        timezone: Some("UTC".to_string()),
        defaults: Vec::new(),
    };
    kv("setup(timezone UTC)", &classify_unit(chtypes::setup(setup)));
    let other = SetupOptions {
        timezone: Some("Asia/Tokyo".to_string()),
        defaults: Vec::new(),
    };
    kv(
        "setup(timezone Asia/Tokyo)",
        &classify_unit(chtypes::setup(other)),
    );
    note("the second call disagrees with the first, so it is misuse: no two");
    note("components in one process can silently disagree about the zone");
    blank();

    // Construction opens nothing; CHTYPES_AUTOFETCH=1 lets a request that
    // nothing installed answers be fetched.
    let registry = match Registry::new(RegistryOptions::default()) {
        Ok(r) => r,
        Err(err) => fatal(&err.to_string()),
    };
    kv("libraries open", &registry.libraries().len().to_string());
    match registry.installed() {
        Ok(list) => {
            let versions: Vec<String> = list
                .iter()
                .map(|r| format!("{} ({})", r.version, r.platform))
                .collect();
            kv(
                "installed",
                &if versions.is_empty() {
                    "(nothing)".to_string()
                } else {
                    versions.join("  ")
                },
            );
        }
        Err(err) => kv("installed", &err.to_string()),
    }

    let want = std::env::var("CHTYPES_VERSION")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "26.8".to_string());
    kv("version requested", &want);
    let lib = match registry.for_version(&want) {
        Ok(l) => l,
        Err(err) => fatal(&format!(
            "{err}\n\nFetch an artifact first: `chtypes fetch {want}` (cargo run --bin chtypes -- fetch {want}), or set CHTYPES_AUTOFETCH=1."
        )),
    };
    let info = lib.build_info();
    kv(
        "library reports",
        &format!("{}  (minor line {})", lib.version(), lib.minor()),
    );
    kv(
        "build",
        &format!("{} on {}/{}", info.build, info.os, info.arch),
    );
    kv("abi_fingerprint", &info.abi_fingerprint);
    kv("path", &lib.path().display().to_string());
    if let Some(resolved) = lib.resolved() {
        kv("signed by", &resolved.signed_by);
        kv("source", &resolved.source);
        kv("already installed", &resolved.already_installed.to_string());
    }
    note("the library names ITSELF via chs_build_info; nothing is inferred from");
    note("a directory or file name");
    blank();

    kv(
        "for_version(\"v26.8\")",
        &classify_lib(registry.for_version("v26.8")),
    );
    kv(
        "for_version(\"99.9\")",
        &classify_lib(registry.for_version("99.9")),
    );
    note("a spelling the fetch layer refuses is misuse, raised before any read;");
    note("a version nothing answers is ArtifactMissing (or, with autofetch,");
    note("ArtifactUnpublished): never a silent nearest-version fallback");

    (registry, lib)
}

// ---------------------------------------------------------------------------
// SECTION 2: Ask a build about itself
//
// WHAT: type canonicalization, identifier and literal quoting, the build's
// error-code table and its capabilities, all from the build's own code.
// WHY: canonicalization is how you compare a tenant's declared type against
// what the server will store; it is the server's own parse, not a spelling
// normalizer.
// LOOK FOR: Variant members being SORTED, an unknown family refused with
// ClickHouse's own code, and the quoting spelled by THIS build.
// The v0 introspection trio (registered families, function flags, reference
// type) is deleted in v1 (bindings-v1.md section 7: tooling exports, not
// binding surface).
// ---------------------------------------------------------------------------
fn section2(lib: &Arc<Library>) {
    section(2, "Ask a build about itself");

    for t in [
        "Variant(String, UInt64, Array(UInt8))",
        "BIGINT",
        "Nullable(Int8)",
        "NoSuchType",
    ] {
        match lib.validate_type(t) {
            Ok(canon) => kv(t, &format!("-> {canon}")),
            Err(Error::Schema(c)) => kv(
                t,
                &format!("REFUSED, ClickHouse code {} {}", c.ch_code, c.ch_name),
            ),
            Err(err) => kv(t, &err.to_string()),
        }
    }
    blank();
    kv(
        "quote_identifier(\"reading c\")",
        &lib.quote_identifier("reading c")
            .map_or_else(|e| e.to_string(), |q| q.to_string()),
    );
    kv(
        "quote_identifier_if_needed(\"abc\")",
        &lib.quote_identifier_if_needed("abc")
            .map_or_else(|e| e.to_string(), |q| q.to_string()),
    );
    kv(
        "quote_literal(\"it's\")",
        &lib.quote_literal("it's")
            .map_or_else(|e| e.to_string(), |q| q.to_string()),
    );
    note("the quoting is the artifact's own backQuote and string quoting, not");
    note("a rule in the binding");
    blank();
    match lib.error_codes() {
        Ok(table) => {
            kv("error codes in this build", &table.len().to_string());
            kv("code 27", table.name(27).unwrap_or("(none)"));
            kv("code 53", table.name(53).unwrap_or("(none)"));
        }
        Err(err) => kv("error_codes", &err.to_string()),
    }
    let caps = &lib.build_info().capabilities;
    kv("input formats", &caps.input_formats.join(" "));
    kv("export formats", &caps.export_formats.join(" "));
    kv("features", &caps.features.join(" "));
    note("what a build supports is read from capabilities, never probed by calling");
}

// ---------------------------------------------------------------------------
// SECTION 3: Compile a schema and read it back
//
// WHAT: compile exactly one CREATE TABLE statement, then describe the columns.
// WHY: the compiled schema is the server's view of the table, including the
// rewrites only a schema-aware compile can see.
// LOOK FOR: `DEFAULT NULL` making a type Nullable, and an ALIAS type being
// inferred.
// ---------------------------------------------------------------------------
fn section3(lib: &Arc<Library>) {
    section(3, "Compile a schema and read it back");

    kv("the columns", "");
    for line in DEMO_COLUMNS.split(",\n") {
        raw(&format!("      {line}"));
    }
    let schema = compile(lib, DEMO_COLUMNS);
    blank();
    kv(
        "compiled columns",
        "name  type  (default kind + expression)",
    );
    print_columns(&schema);
    note("payload_len is MATERIALIZED: compiled and introspectable, but never");
    note("read from input; watch it come back separately in section 6");
    blank();

    for cols in ["x Int64 DEFAULT NULL", "a UInt8, al ALIAS a + 1"] {
        kv(cols, "");
        print_columns(&compile(lib, cols));
    }
    note("the DEFAULT rewrote the type to Nullable, and UInt8 + 1 widened to");
    note("UInt16: the server does both at CREATE, so chtypes must too");
    blank();

    let err = lib.compile_table(ddl("x NotAType"), &CompileOptions::default());
    kv("x NotAType", &classify(err.map(|_| ())));
    if let Err(e) = lib.compile_table(ddl("x NotAType"), &CompileOptions::default()) {
        note(&truncate(&e.to_string(), 90));
    }
}

fn print_columns(schema: &chtypes::Schema) {
    match schema.describe() {
        Ok(desc) => {
            for c in &desc.columns {
                let extra = if c.default_expr.is_empty() {
                    String::new()
                } else {
                    format!("  {} {}", c.default_kind, c.default_expr)
                };
                kv(&format!("  {}", c.name), &format!("{}{extra}", c.r#type));
            }
        }
        Err(err) => kv("  describe", &err.to_string()),
    }
}

// ---------------------------------------------------------------------------
// SECTION 4: Compile under a settings profile
//
// WHAT: the settings a compile takes (CompileOptions::settings): flatten_nested
// changing the storage shape, a type gate refusing with the server's own code,
// and an unknown setting name refused with ClickHouse's own message.
// WHY: a tenant's server runs with its own settings; a stock compile answers
// for a server they do not have.
// LOOK FOR: the same DDL compiling to different columns, and the refusals
// carrying ClickHouse's own codes. Settings values are STRINGS, only strings.
// The v0 compile mode is deleted (bindings-v1.md section 7: compile takes
// exactly one CREATE TABLE statement).
// ---------------------------------------------------------------------------
fn section4(lib: &Arc<Library>) {
    section(4, "Compile under a settings profile");

    kv(
        "(a) flatten_nested",
        "id UInt32, n Nested(a UInt8, b String)",
    );
    for value in ["1", "0"] {
        let options = CompileOptions {
            settings: pairs(&[("flatten_nested", value)]),
            ..CompileOptions::default()
        };
        match lib.compile_table(ddl("id UInt32, n Nested(a UInt8, b String)"), &options) {
            Ok(schema) => {
                let shapes: Vec<String> = schema
                    .describe()
                    .map(|d| {
                        d.columns
                            .iter()
                            .map(|c| format!("{} {}", c.name, c.r#type))
                            .collect()
                    })
                    .unwrap_or_default();
                kv(&format!("  ={value}"), &shapes.join(" | "));
            }
            Err(err) => kv(&format!("  ={value}"), &err.to_string()),
        }
    }
    blank();

    kv("(b) a type gate", "lc LowCardinality(UInt8)");
    for value in ["0", "1"] {
        let options = CompileOptions {
            settings: pairs(&[("allow_suspicious_low_cardinality_types", value)]),
            ..CompileOptions::default()
        };
        match lib.compile_table(ddl("lc LowCardinality(UInt8)"), &options) {
            Ok(_) => kv(&format!("  ={value}"), "compiled"),
            Err(Error::Schema(c)) => kv(
                &format!("  ={value}"),
                &format!("REFUSED, the server's own code {}", c.ch_code),
            ),
            Err(err) => kv(&format!("  ={value}"), &err.to_string()),
        }
    }
    blank();

    let options = CompileOptions {
        settings: pairs(&[("flatten_nestedd", "1")]),
        ..CompileOptions::default()
    };
    match lib.compile_table(ddl("a UInt8"), &options) {
        Err(Error::Schema(c)) => {
            kv(
                "(c) unknown setting name",
                &format!("flatten_nestedd -> code {} {}", c.ch_code, c.ch_name),
            );
            note(&truncate(&c.message.to_string(), 90));
        }
        other => kv("(c) unknown setting name", &classify(other.map(|_| ()))),
    }
}

// ---------------------------------------------------------------------------
// SECTION 5: Accept, reject, decline (and poison)
//
// WHAT: the whole verdict contract, each outcome shown concretely.
// WHY: a gateway must know what to do with each answer.
// LOOK FOR: accepted with a silent change (seq 256 stored as 0), a rejection
// carrying ClickHouse's own code and message, a DECLINE ("a real server might
// well have accepted this"), a poisoned accept, and a skipped row in a batch.
// ---------------------------------------------------------------------------
fn section5(lib: &Arc<Library>) {
    section(5, "Accept, reject, decline (and poison)");
    kv("schema", FORMAT_COLUMNS);
    blank();
    let schema = compile(lib, FORMAT_COLUMNS);

    kv("(a) ACCEPT", r#"{"device_id":1,"seq":256,"label":"ok"}"#);
    feed(
        &schema,
        "JSONEachRow",
        Format::JsonEachRow,
        br#"{"device_id":1,"seq":256,"label":"ok"}"#,
    );
    note("accepted, but look at seq: input 256, stored 0. The ~ line is the");
    note("Transform (reason overflow_wrap, LOSSY): publish the STORED value");
    blank();

    kv("(b) REJECT", r#"{"device_id":"abc"}"#);
    feed(
        &schema,
        "JSONEachRow",
        Format::JsonEachRow,
        br#"{"device_id":"abc"}"#,
    );
    note("the code and message are ClickHouse's OWN; chtypes never hand-writes");
    note("an error, so your 400 body matches what a real INSERT would say");
    blank();

    kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values");
    feed(&schema, "Values", Format::Values, b"(2,7,concat('a','b'))");
    note("unsupported means 'a real server MIGHT WELL accept this; I will not");
    note("guess'. Never map it to a rejection or an acceptance: send the row to");
    note("the real server unpreviewed and let it decide");
    blank();

    kv("(d) POISON", "an accept that bites at read time");
    let poison = compile(lib, "id UInt32, e Enum8('red' = 1, 'green' = 2)");
    kv(
        "  payload (RBWD)",
        &format!("{RBWD_POISON}   (id=1 by value, e by marker byte)"),
    );
    feed(
        &poison,
        "RBWD marker",
        Format::RowBinaryWithDefaults,
        &unhex(RBWD_POISON),
    );
    feed(
        &poison,
        "RBWD value",
        Format::RowBinaryWithDefaults,
        &unhex(RBWD_VALUE),
    );
    note("accepted_poisoned: the marker fills with the column-level raw zero and");
    note("raw 0 has no Enum name, so the insert succeeds and later SELECTs fail;");
    note("the control payload supplies e by value");
    blank();

    kv(
        "(e) SKIP",
        "a bad middle row under input_format_allow_errors_num=10",
    );
    let batch = schema.rows(
        Format::JsonEachRow,
        b"{\"device_id\":1,\"seq\":1,\"label\":\"a\"}\n{\"device_id\":\"oops\"}\n{\"device_id\":3,\"seq\":3,\"label\":\"c\"}\n",
        &rows_opts(&[("input_format_allow_errors_num", "10")]),
    );
    match batch {
        Ok(batch) => {
            kv(
                "  batch",
                &format!(
                    "{}  rows_read={} rows_skipped={}",
                    batch.outcome, batch.rows_read, batch.rows_skipped
                ),
            );
            for (i, r) in batch.rows.iter().enumerate() {
                let mut line = r.outcome.to_string();
                if r.outcome == Outcome::Skipped {
                    line += &format!(
                        "  code={} {}",
                        r.err_code,
                        truncate(&r.err_msg.to_string(), 48)
                    );
                }
                kv(&format!("  row {i}"), &line);
            }
        }
        Err(err) => kv("  batch", &err.to_string()),
    }
    note("one rows() call answers per input record, IN ORDER; a skipped row is");
    note("never stored, so route on the row outcome");
}

// ---------------------------------------------------------------------------
// SECTION 6: DEFAULT evaluation: where every value comes from
//
// WHAT: each stored value's source (input, default, default_substituted,
// default_generated, absent, ...), MATERIALIZED values (computed), unknown
// fields and the transforms.
// WHY: a preview that omits a computed DEFAULT is not the row the server stores.
// LOOK FOR: the `source` column, the `computed` list and the transforms with
// their reasons. A DEFAULT that calls an admitted generator is drawn by the
// library (source default_generated): insert the library's own export (section
// 15), never the original body.
// ---------------------------------------------------------------------------
fn section6(lib: &Arc<Library>) {
    section(6, "DEFAULT evaluation: where every value comes from");
    let schema = compile(lib, DEMO_COLUMNS);
    let body = br#"{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}"#;
    kv("row fed", std::str::from_utf8(body).unwrap_or("?"));
    let r = match schema.row(Format::JsonEachRow, body, &RowOptions::default()) {
        Ok(r) => r,
        Err(err) => {
            kv("row", &err.to_string());
            return;
        }
    };
    blank();
    kv(
        "outcome / err_code",
        &format!("{} / {}", r.outcome, r.err_code),
    );
    kv(
        "columns[]  (every entry)",
        "column = text  (source, stored?)",
    );
    for v in &r.columns {
        kv(
            &format!("  {}", v.column),
            &format!("{:<28} ({}, stored={})", text_or(v), v.source, v.is_stored),
        );
    }
    kv("values[]  (the stored row)", &r.values.len().to_string());
    blank();
    kv(
        "computed[]",
        "MATERIALIZED values: durable, never in SELECT *",
    );
    for c in &r.computed {
        kv(
            &format!("  {}", c.column),
            &format!("{}  =  {}", c.kind, c.text),
        );
    }
    blank();
    kv("transformed[]", "every silent change, with a reason");
    for t in &r.transformed {
        kv(
            &format!("  {}", t.reason),
            &format!(
                "{}: {} -> {}   lossy={}",
                t.column, t.input, t.stored, t.lossy
            ),
        );
    }
    blank();
    kv("unknown_fields[]", &format!("{:?}", r.unknown_fields));
    kv(
        "unsupported_settings[]",
        &format!("{:?}", r.unsupported_settings),
    );
    blank();

    let dep = compile(lib, "a UInt8, d UInt8 DEFAULT a + 1");
    kv(
        "row-dependent DEFAULT",
        "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`",
    );
    feed(&dep, "CSV", Format::Csv, b"7,");
    note("the DEFAULT is evaluated against the row's own earlier column");
}

// ---------------------------------------------------------------------------
// SECTION 7: One schema, every format
//
// WHAT: the input formats with accepts, rejects and each format's signature
// behavior. Binary payloads are hand-built hex.
// WHY: real pipelines carry different encodings; each has its own coercion
// rules, and chtypes runs the library's reader for each.
// LOOK FOR: the empty-field rule in CSV, header formats naming columns (and
// reordering them), Native CASTing, and Buffers having no self-description.
// ---------------------------------------------------------------------------
fn section7(lib: &Arc<Library>) {
    section(7, "One schema, every format");
    kv("schema", FORMAT_COLUMNS);
    blank();
    let schema = compile(lib, FORMAT_COLUMNS);

    group("text formats (one row per line)");
    feed(
        &schema,
        "JSONEachRow  accept",
        Format::JsonEachRow,
        br#"{"device_id":1,"seq":7,"label":"ok"}"#,
    );
    feed(
        &schema,
        "JSONEachRow  reject",
        Format::JsonEachRow,
        br#"{"device_id":"abc"}"#,
    );
    feed(&schema, "CSV          accept", Format::Csv, b"1,7,ok");
    feed(&schema, "CSV          reject", Format::Csv, b"x,7,ok");
    feed(&schema, "TSV          accept", Format::Tsv, b"1\t7\tok");
    feed(&schema, "TSV          reject", Format::Tsv, b"y\t7\tok");
    feed(
        &schema,
        "JSONCompact  accept",
        Format::JsonCompactEachRow,
        br#"[1,7,"ok"]"#,
    );
    feed(
        &schema,
        "JSONCompact  reject",
        Format::JsonCompactEachRow,
        br#"["z",7,"ok"]"#,
    );
    feed(
        &schema,
        "Values       accept",
        Format::Values,
        b"(1,7,'ok')",
    );
    feed(
        &schema,
        "Values       DEFAULT",
        Format::Values,
        b"(3,DEFAULT,'d')",
    );
    feed(
        &schema,
        "Values       decline",
        Format::Values,
        b"(2,7,concat('a','b'))",
    );
    blank();

    kv(
        "CSV empty-field rule",
        "bare empty takes the DEFAULT; quoted empty is ''",
    );
    feed(&schema, "CSV   bare   2,7,", Format::Csv, b"2,7,");
    feed(&schema, r#"CSV   quoted 3,7,"""#, Format::Csv, br#"3,7,"""#);
    blank();

    group("header formats (the first row NAMES the columns)");
    feed(
        &schema,
        "CSVWithNames accept",
        Format::CsvWithNames,
        b"device_id,seq,label\n1,7,ok",
    );
    feed(
        &schema,
        "CSVWithNames reorder",
        Format::CsvWithNames,
        b"label,device_id,seq\nok,1,7",
    );
    feed(
        &schema,
        "TSVWithNames accept",
        Format::TsvWithNames,
        b"device_id\tseq\tlabel\n1\t7\tok",
    );
    blank();

    group("binary formats (bytes, COUNTED)");
    kv("  RowBinary payload", ROW_BINARY_OK);
    feed(
        &schema,
        "RowBinary    accept",
        Format::RowBinary,
        &unhex(ROW_BINARY_OK),
    );
    feed(
        &schema,
        "RowBinary    reject",
        Format::RowBinary,
        &[0x01, 0x00],
    );
    kv("  RBWD payload", RBWD_MARKER);
    feed(
        &schema,
        "RBWD  marker byte",
        Format::RowBinaryWithDefaults,
        &unhex(RBWD_MARKER),
    );
    feed(
        &schema,
        "RBWNTD       accept",
        Format::RowBinaryWithNamesAndTypesAndDefaults,
        &unhex(RBWNTD_ROW),
    );
    blank();

    group("Native: self-describing, and it CASTs");
    feed(
        &schema,
        "Native       accept",
        Format::Native,
        &unhex(NATIVE_OK),
    );
    feed(
        &schema,
        "Native       CAST",
        Format::Native,
        &unhex(NATIVE_CAST),
    );
    blank();

    group("Buffers: no self-description at all");
    let buffers = compile(lib, "x Int32");
    kv("  payload", "4 bytes ff ff ff ff, declared x Int32");
    feed(
        &buffers,
        "Buffers reinterpret",
        Format::Buffers,
        &unhex("010000000000000001000000000000000400000000000000ffffffff"),
    );
    note("a build that predates the format answers with a decline or a refusal;");
    note("capabilities (section 2) says which formats this build reads");
}

// ---------------------------------------------------------------------------
// SECTION 8: Engines, MergeTree settings, and TTL
//
// WHAT: the engine, its settings, the partition key and the TTL are part of the
// one CREATE TABLE statement now; this section feeds three statements.
// WHY: SummingMergeTree merges at insert time, a MergeTree SETTINGS list is
// validated by the server, and a TTL can make a row stored nowhere.
// LOOK FOR: engine_rows (the stored rows after the engine's insert-time merge),
// the refusal for an unknown MergeTree setting, and the TTL transform.
// The v0 SetEngine, SetTTL, SetPartitionBy and WithMergeTreeSettings are
// deleted (bindings-v1.md section 7).
// ---------------------------------------------------------------------------
fn section8(lib: &Arc<Library>) {
    section(8, "Engines, MergeTree settings, and TTL");

    let summing = "CREATE TABLE t (day Date, key UInt32, v UInt64) ENGINE = SummingMergeTree ORDER BY (day, key)";
    kv("(a) SummingMergeTree", "ORDER BY (day, key)");
    match lib.compile_table(summing, &CompileOptions::default()) {
        Ok(schema) => {
            let body = br#"{"day":"2026-01-01","key":1,"v":5}
{"day":"2026-01-01","key":1,"v":7}"#;
            match schema.rows(Format::JsonEachRow, body, &RowsOptions::default()) {
                Ok(batch) => {
                    kv("  rows in / rows_read", &format!("2 / {}", batch.rows_read));
                    for (i, row) in batch.engine_rows.iter().flatten().enumerate() {
                        let cells: Vec<String> = row
                            .iter()
                            .map(|c| format!("{}={}", c.column, c.text))
                            .collect();
                        kv(&format!("  engine row {i}"), &cells.join(" "));
                    }
                }
                Err(err) => kv("  rows", &err.to_string()),
            }
        }
        Err(err) => kv("  compile", &err.to_string()),
    }
    blank();

    kv("(b) MergeTree SETTINGS", "refusal vs accepted");
    for setting in [
        "index_granularityy = 8192",
        "index_granularity = 4096",
        "index_granularity = 8192",
    ] {
        let stmt = format!(
            "CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple() SETTINGS {setting}"
        );
        kv(
            &format!("  {setting}"),
            &classify(
                lib.compile_table(stmt, &CompileOptions::default())
                    .map(|_| ()),
            ),
        );
    }
    blank();

    kv("(c) TTL", "TTL ts + INTERVAL 1 DAY, a row dated 2020");
    let ttl = "CREATE TABLE t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL ts + INTERVAL 1 DAY";
    match lib.compile_table(ttl, &CompileOptions::default()) {
        Ok(schema) => match schema.rows(
            Format::JsonEachRow,
            br#"{"ts":"2020-01-01 00:00:00","v":9}"#,
            &RowsOptions::default(),
        ) {
            Ok(batch) => {
                kv(
                    "  row-level outcome",
                    &batch
                        .rows
                        .first()
                        .map_or("(none)".to_string(), |r| r.outcome.to_string()),
                );
                kv(
                    "  engine rows stored",
                    &batch
                        .engine_rows
                        .as_ref()
                        .map_or("(not reported)".to_string(), |e| e.len().to_string()),
                );
                for t in &batch.transformed {
                    kv(
                        "  batch transform",
                        &format!(
                            "row={} column={} reason={} lossy={}",
                            t.row, t.column, t.reason, t.lossy
                        ),
                    );
                }
            }
            Err(err) => kv("  rows", &err.to_string()),
        },
        Err(err) => kv("  compile", &err.to_string()),
    }
    note("a row can PARSE fine and still be stored nowhere: the TTL expired it");
}

// ---------------------------------------------------------------------------
// SECTION 9: Settings precedence: who wins
//
// WHAT: the layers a call's settings come from: ClickHouse's own defaults, the
// process setup's defaults (fixed once, section 1) and the per-call settings,
// plus the per-call zone.
// WHY: parsing depends on settings; the same body can be accepted or rejected.
// LOOK FOR: one body flipping verdict with date_time_input_format, and the
// refusal when a call spells its zone twice (the session_timezone option AND a
// session_timezone key).
// The v0 per-handle profile and the runtime set_default_settings are deleted
// (bindings-v1.md section 7): defaults are fixed at setup and never change.
// ---------------------------------------------------------------------------
fn section9(lib: &Arc<Library>) {
    section(9, "Settings precedence: who wins");
    kv(
        "the probe",
        r#"ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}"#,
    );
    blank();
    let iso = br#"{"ts":"2026-01-15T10:30:00Z"}"#;
    let schema = compile(lib, "ts DateTime");
    let run = |label: &str, options: &RowsOptions| {
        kv(
            label,
            &match schema.rows(Format::JsonEachRow, iso, options) {
                Ok(b) => verdict(&b),
                Err(err) => err.to_string(),
            },
        );
    };
    run("1. ClickHouse defaults", &RowsOptions::default());
    run(
        "2. per-call best_effort",
        &rows_opts(&[("date_time_input_format", "best_effort")]),
    );
    run(
        "3. per-call basic",
        &rows_opts(&[("date_time_input_format", "basic")]),
    );
    blank();

    let both = RowsOptions {
        settings: pairs(&[("session_timezone", "UTC")]),
        session_timezone: Some("UTC".to_string()),
        ..RowsOptions::default()
    };
    run("4. zone option AND key", &both);
    note("a call carries exactly one spelling of its zone, even when the two agree");
    let zoned = RowsOptions {
        session_timezone: Some("Asia/Tokyo".to_string()),
        ..RowsOptions::default()
    };
    run("5. per-call zone Asia/Tokyo", &zoned);
}

// ---------------------------------------------------------------------------
// SECTION 10: The error taxonomy
//
// WHAT: the four call classes and the artifact classes, caught by variant,
// never by string matching.
// WHY: a refusal means "the server would say no"; a decline means "ask the
// server"; misuse and internal errors are yours to fix. Mixing them up is how
// over-rejects and over-accepts are manufactured.
// LOOK FOR: each variant, with its ClickHouse code where it has one, and that
// is_unsupported() is a sibling check, not a subtype.
// ---------------------------------------------------------------------------
fn section10(lib: &Arc<Library>, registry: &Registry) {
    section(10, "The error taxonomy");
    raw("      match err {");
    raw("          Error::Schema(c)      => a REFUSAL: the server would say no (c.ch_code)");
    raw("          Error::Unsupported(c) => a DECLINE: a server might well accept");
    raw("          Error::Usage(c)       => misuse of the API");
    raw("          Error::Internal(c)    => a library bug");
    raw("          other                 => an artifact or source problem (err.code())");
    raw("      }");
    blank();
    let samples: Vec<(&str, Result<(), Error>)> = vec![
        (
            "compile `x NotAType`",
            lib.compile_table(ddl("x NotAType"), &CompileOptions::default())
                .map(|_| ()),
        ),
        (
            "two zone spellings",
            compile(lib, "ts DateTime")
                .row(
                    Format::JsonEachRow,
                    br#"{"ts":"2026-01-15 10:30:00"}"#,
                    &RowOptions {
                        settings: pairs(&[("session_timezone", "UTC")]),
                        session_timezone: Some("UTC".to_string()),
                        ..RowOptions::default()
                    },
                )
                .map(|_| ()),
        ),
        (
            "open a version nothing answers",
            registry.for_version("99.9").map(|_| ()),
        ),
        ("open `v26.8`", registry.for_version("v26.8").map(|_| ())),
    ];
    for (what, result) in samples {
        describe_error(result, what);
    }
}

// ---------------------------------------------------------------------------
// SECTION 11: The discovery kit, offline
//
// WHAT: the discovery query the library hands you, and the library reading a
// server's answer back into column declarations.
// WHY: a tenant's table already exists on a server; discovery rebuilds a
// schema from `system.columns` without the binding holding any SQL.
// LOOK FOR: the query (run it with your own client, binding `database` and
// `table`), the declarations the library spells (the `reading c` column comes
// back QUOTED, by this build's own quoting) and the rebuilt table compiling.
// The responses below are CANNED, shaped like a real server's. The v0 version
// and changed-settings queries are deleted (bindings-v1.md section 7): a
// caller supplies its server's settings itself.
// ---------------------------------------------------------------------------
fn section11(lib: &Arc<Library>) {
    section(11, "The discovery kit, offline");
    kv(
        "NOTE",
        "the answer below is CANNED, shaped like a real server's",
    );
    blank();
    match lib.discover_query() {
        Ok(q) => kv("discover_query()", &truncate(&q.to_string(), 100)),
        Err(err) => kv("discover_query()", &err.to_string()),
    }
    match lib.discover_columns(CANNED_COLUMNS_RESULT) {
        Ok(found) => {
            for col in &found.columns {
                kv(&format!("  {}", col.name), &col.declaration.to_string());
            }
            kv(
                "columns_sql",
                &truncate(&found.columns_sql.to_string(), 100),
            );
            let stmt = format!(
                "CREATE TABLE t ({}) ENGINE = MergeTree ORDER BY tuple()",
                found.columns_sql
            );
            match lib.compile_table(stmt, &CompileOptions::default()) {
                Ok(schema) => {
                    blank();
                    kv("rebuilt table compiles", "yes");
                    let row = br#"{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}"#;
                    kv(
                        "the same row, twice",
                        std::str::from_utf8(row).unwrap_or("?"),
                    );
                    for (label, settings) in [
                        ("  stock settings", pairs(&[])),
                        (
                            "  date_time_input_format=best_effort",
                            pairs(&[("date_time_input_format", "best_effort")]),
                        ),
                    ] {
                        let options = RowOptions {
                            settings,
                            ..RowOptions::default()
                        };
                        kv(
                            label,
                            &match schema.row(Format::JsonEachRow, row, &options) {
                                Ok(r) => format!(
                                    "{}  {}",
                                    r.outcome,
                                    truncate(&r.err_msg.to_string(), 44)
                                ),
                                Err(err) => err.to_string(),
                            },
                        );
                    }
                    note("a server declared with best_effort takes the ISO-8601 timestamp; the");
                    note("caller passes the settings its server has, because 1.0 has no call");
                    note("that reads them (docs/limitations.md, Known gaps in 1.0)");
                }
                Err(err) => kv("rebuilt table", &err.to_string()),
            }
        }
        Err(err) => kv("discover_columns", &err.to_string()),
    }
}

// ---------------------------------------------------------------------------
// SECTION 12: Version pinning: same input, different answers
//
// WHAT: the same statement answered by every line this registry can open.
// WHY: ClickHouse versions differ, and a gateway serving tenants on several
// must answer for each exactly.
// LOOK FOR: a line that refuses a DEFAULT its neighbors accept. With one line
// installed there is nothing to compare, and the section says so.
// ---------------------------------------------------------------------------
fn section12(registry: &Registry) {
    section(12, "Version pinning: same input, different answers");
    let installed = registry.installed().unwrap_or_default();
    let mut minors: Vec<String> = Vec::new();
    for r in &installed {
        let minor = r.version.split('.').take(2).collect::<Vec<_>>().join(".");
        if !minors.contains(&minor) {
            minors.push(minor);
        }
    }
    kv("lines installed", &minors.join("  "));
    if minors.len() < 2 {
        note("only one line is installed, so there is nothing to sweep; fetch another");
        note("(`chtypes fetch 26.7`) and re-run to see the answers diverge");
        return;
    }
    kv(
        "(a) a mixed-type DEFAULT",
        "a UInt8, x Int64 DEFAULT if(1,2,'a')",
    );
    for minor in &minors {
        match registry.for_version(minor) {
            Ok(lib) => {
                let result = lib.compile_table(
                    ddl("a UInt8, x Int64 DEFAULT if(1,2,'a')"),
                    &CompileOptions::default(),
                );
                kv(&format!("  {minor}"), &classify(result.map(|_| ())));
            }
            Err(err) => kv(&format!("  {minor}"), &format!("SKIPPED  {err}")),
        }
    }
}

// ---------------------------------------------------------------------------
// SECTION 13: Teardown
//
// WHAT: nothing to tear down. v0's Registry::shutdown and Library::shutdown are
// deleted (bindings-v1.md section 7): no binding ever unloads a library.
// WHY: unloading a ClickHouse image is not safe; handles are what you release.
// LOOK FOR: live_handles() counting the handles this library has outstanding,
// and falling when a handle drops (Drop frees it).
// ---------------------------------------------------------------------------
fn section13(lib: &Arc<Library>) {
    section(13, "Teardown");
    let before = lib.live_handles().unwrap_or_default();
    kv("live handles now", &format!("{before:?}"));
    {
        let _schema = compile(lib, "a UInt8");
        let during = lib.live_handles().unwrap_or_default();
        kv("with one schema alive", &format!("{during:?}"));
    }
    let after = lib.live_handles().unwrap_or_default();
    kv("after the schema drops", &format!("{after:?}"));
    note("Drop frees every handle; a library itself is never unloaded");
}

// ---------------------------------------------------------------------------
// SECTION 14: The static path (Go only)
//
// WHAT: the cgo-linked single-version shape only Go has (OpenLinked). The v1
// API has no Rust equivalent by design (bindings-v1.md section 2): every
// binding but Go loads with dlopen.
// ---------------------------------------------------------------------------
fn section14() {
    section(14, "The static path (Go only)");
    note("Go's OpenLinked links one version into the binary; Rust always dlopens.");
    note("Library::open_unverified(path, allow) opens a LOCAL build instead, and");
    note("needs both the argument and CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1.");
}

// ---------------------------------------------------------------------------
// SECTION 15: Export: bytes + spans
//
// WHAT: the same rows() call serializing the accepted rows to wire bytes in a
// format you ask for, addressed per row by spans; the lean document flags; the
// export declined for a poisoned batch.
// WHY: when a row carries a generated DEFAULT, the stored value exists only in
// the library's own output. Insert the export, never the original body.
// LOOK FOR: one line per ACCEPTED row, spans of zero length for rows not
// accepted, and export_declined naming why a poisoned batch emits nothing.
// ---------------------------------------------------------------------------
fn section15(lib: &Arc<Library>) {
    section(15, "Export: bytes + spans");
    let schema = compile(lib, FORMAT_COLUMNS);
    let body: &[u8] = concat!(
        r#"{"device_id":1,"label":"ok"}"#,
        "\n",
        r#"{"device_id":"zap"}"#,
        "\n",
        r#"{"device_id":3,"label":"hi"}"#,
        "\n",
    )
    .as_bytes();
    let options = RowsOptions {
        settings: pairs(&[("input_format_allow_errors_num", "10")]),
        export: Some(Format::JsonCompactEachRow),
        ..RowsOptions::default()
    };
    let batch = match schema.rows(Format::JsonEachRow, body, &options) {
        Ok(b) => b,
        Err(err) => {
            kv("rows with export", &err.to_string());
            return;
        }
    };
    let payload = batch.payload.clone().unwrap_or_default();
    kv(
        "batch",
        &format!(
            "{}  rows_read={} rows_skipped={}",
            batch.outcome, batch.rows_read, batch.rows_skipped
        ),
    );
    kv(
        "payload",
        &format!(
            "{:?}  ({} bytes, one line per ACCEPTED row)",
            String::from_utf8_lossy(&payload),
            payload.len()
        ),
    );
    for (i, span) in batch.spans.iter().flatten().enumerate() {
        let line = if span.len > 0 {
            let (off, len) = (span.off as usize, span.len as usize);
            format!(
                "{:?}",
                String::from_utf8_lossy(payload.get(off..off + len).unwrap_or_default())
            )
        } else {
            "(no bytes: row not accepted)".to_string()
        };
        let outcome = batch
            .rows
            .get(i)
            .map_or("?".to_string(), |r| r.outcome.to_string());
        kv(
            &format!("  span[{i}] {{off:{} len:{}}}", span.off, span.len),
            &format!("{outcome} -> {line}"),
        );
    }
    blank();

    let lean = RowsOptions {
        doc_flags: Some(DocFlags::from_bits(0)),
        ..options.clone()
    };
    match schema.rows(Format::JsonEachRow, body, &lean) {
        Ok(l) => kv(
            "lean (no doc flags)",
            &format!(
                "outcome {}, {} values on row 0, {} transformed; payload identical: {}",
                l.outcome,
                l.rows.first().map_or(0, |r| r.values.len()),
                l.transformed.len(),
                l.payload == batch.payload
            ),
        ),
        Err(err) => kv("lean", &err.to_string()),
    }
    blank();

    let poison = compile(lib, "e Enum8('a' = 1, 'b' = 2)");
    let poisoned = RowsOptions {
        settings: pairs(&[("input_format_defaults_for_omitted_fields", "0")]),
        export: Some(Format::JsonCompactEachRow),
        ..RowsOptions::default()
    };
    match poison.rows(Format::JsonEachRow, b"{\"e\":null}\n", &poisoned) {
        Ok(p) => kv(
            "poisoned batch",
            &format!(
                "{} -> payload={} export_declined={:?}",
                p.outcome,
                p.payload
                    .as_ref()
                    .map_or("None (declined)".to_string(), |b| format!(
                        "{} bytes",
                        b.len()
                    )),
                truncate(&p.export_declined.to_string(), 48)
            ),
        ),
        Err(err) => kv("poisoned batch", &err.to_string()),
    }
    let empty = RowsOptions {
        export: Some(Format::JsonCompactEachRow),
        ..RowsOptions::default()
    };
    match schema.rows(Format::JsonEachRow, b"", &empty) {
        Ok(e) => kv(
            "empty batch",
            &format!(
                "payload is_some={} len={}  (emitted-empty is not declined)",
                e.payload.is_some(),
                e.payload.as_deref().unwrap_or_default().len()
            ),
        ),
        Err(err) => kv("empty batch", &err.to_string()),
    }
}

// ---------------------------------------------------------------------------
// SECTION 16: Filters: WHERE semantics at the edge
//
// WHAT: one boolean expression compiled against the schema and evaluated per
// row: promotion instead of wrapping, NULL-is-not-true, the compile-then-throw
// class, refusals at compile, query parameters, and the block twin.
// WHY: a gateway filters events with a tenant's expression and needs the
// server's own answer.
// LOOK FOR: `x = 256` over UInt8 comparing 256 as 256 (never wrapping), a
// hostile parameter value printed as an inert literal, and one parsed block
// evaluated by two filters.
// ---------------------------------------------------------------------------
fn section16(lib: &Arc<Library>) {
    section(16, "Filters: WHERE semantics at the edge");
    let schema = compile(lib, "x UInt8");
    let eval = EvalOptions::default();

    match schema.compile_filter("x = 256", &FilterOptions::default()) {
        Ok(f) => kv(
            "filter `x = 256` over UInt8",
            &filter_line(f.rows(Format::JsonEachRow, b"{\"x\":0}\n{\"x\":255}\n", &eval)),
        ),
        Err(err) => kv("filter `x = 256`", &err.to_string()),
    }
    blank();

    let nullable = compile(lib, "lvl Nullable(UInt8)");
    if let Ok(f) = nullable.compile_filter("lvl = 1", &FilterOptions::default()) {
        kv(
            "`lvl = 1` on [null, 1]",
            &filter_line(f.rows(Format::JsonEachRow, b"{\"lvl\":null}\n{\"lvl\":1}\n", &eval)),
        );
        note("NULL is not true, as WHERE hides it");
    }
    blank();

    let strings = compile(lib, "s String");
    if let Ok(f) = strings.compile_filter("s = 257", &FilterOptions::default()) {
        match f.rows(Format::JsonEachRow, b"{\"s\":\"hi\"}\n", &eval) {
            Ok(fr) => {
                kv("`s = 257` over String", &verdict_string(&fr));
                for fe in &fr.errors {
                    kv(
                        &format!("  row {}", fe.row),
                        &format!("code {}  {}", fe.code, truncate(&fe.msg.to_string(), 56)),
                    );
                }
            }
            Err(err) => kv("`s = 257`", &err.to_string()),
        }
    }
    blank();

    if let Ok(f) = schema.compile_filter("x < 5", &FilterOptions::default()) {
        kv(
            "`x < 5` on [1, bad, 9]",
            &filter_line(f.rows(
                Format::JsonEachRow,
                b"{\"x\":1}\n{\"x\":\"zap\"}\n{\"x\":9}\n",
                &eval,
            )),
        );
        note("the bad row cannot swallow the tail");
    }
    blank();

    for expr in ["now() > x", "x = {p:UInt8}", "nosuch = 1"] {
        kv(
            &format!("compile `{expr}`"),
            &classify(
                schema
                    .compile_filter(expr, &FilterOptions::default())
                    .map(|_| ()),
            ),
        );
    }
    blank();

    group("query parameters: values are STRINGS, never escaped");
    let event_body: &[u8] = b"{\"tenant\":\"acme\",\"role\":\"admin\",\"x\":1}\n{\"tenant\":\"evil\",\"role\":\"viewer\",\"x\":2}\n{\"tenant\":\"' OR 1=1 --\",\"role\":\"admin\",\"x\":3}\n";
    let events = compile(lib, "tenant String, role String, x UInt8");
    for value in ["acme", "' OR 1=1 --"] {
        let options = FilterOptions {
            params: pairs(&[("t", value)]),
            ..FilterOptions::default()
        };
        match events.compile_filter("tenant = {t:String}", &options) {
            Ok(f) => kv(
                &format!("t = `{value}`"),
                &filter_line(f.rows(Format::JsonEachRow, event_body, &eval)),
            ),
            Err(err) => kv(&format!("t = `{value}`"), &err.to_string()),
        }
    }
    note("the hostile value compares as exactly that literal: no escaping anywhere");
    blank();

    group("the block twin: parse ONCE, evaluate K filters");
    match events.parse_block(Format::JsonEachRow, event_body, &RowOptions::default()) {
        Ok(block) => {
            for role in ["admin", "viewer"] {
                match events.compile_filter(format!("role = '{role}'"), &FilterOptions::default()) {
                    Ok(f) => kv(
                        &format!("eval `role = '{role}'`"),
                        &filter_line(f.eval(&block)),
                    ),
                    Err(err) => kv(&format!("compile role {role}"), &err.to_string()),
                }
            }
        }
        Err(err) => kv("parse_block", &err.to_string()),
    }
}

// ---------------------------------------------------------------------------
// SECTION 17: The INSERT column list
//
// WHAT: the `columns` option: a list naming an EPHEMERAL column, whose value is
// read, feeds the DEFAULT that references it and is never stored; then one
// refusal, a list naming a column the table does not have.
// WHY: an INSERT with a column list is a different statement from one without.
// LOOK FOR: d computed from the ephemeral e, and ClickHouse's own refusal.
// ---------------------------------------------------------------------------
fn section17(lib: &Arc<Library>) {
    section(17, "The INSERT column list");
    let schema = compile(lib, "id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1");
    let with = |cols: &[&str]| RowOptions {
        columns: cols.iter().map(|c| c.as_bytes().to_vec()).collect(),
        ..RowOptions::default()
    };
    match schema.row(
        Format::JsonEachRow,
        br#"{"id":3,"e":5}"#,
        &with(&["id", "e"]),
    ) {
        Ok(row) => {
            let vals: Vec<String> = row
                .values
                .iter()
                .map(|v| format!("{}={}({})", v.column, text_or(v), v.source))
                .collect();
            kv(
                "columns [id, e]  row {id:3,e:5}",
                &format!("{}  {}", row.outcome, vals.join(" ")),
            );
        }
        Err(err) => kv("columns [id, e]", &err.to_string()),
    }
    blank();
    match schema.row(
        Format::JsonEachRow,
        br#"{"id":1,"nosuch":2}"#,
        &with(&["id", "nosuch"]),
    ) {
        Ok(refused) => kv(
            "columns [id, nosuch]",
            &format!(
                "{}  code={}  {}",
                refused.outcome,
                refused.err_code,
                truncate(&refused.err_msg.to_string(), 56)
            ),
        ),
        Err(err) => kv("columns [id, nosuch]", &err.to_string()),
    }
}

// ------------------------------------------------------------- plumbing
//
// Printing helpers only: no chtypes call hides here beyond reading results.

fn section(n: u32, title: &str) {
    println!("\n=== {n}. {title} ===");
}

fn kv(key: &str, value: &str) {
    println!("  {key:<30} {value}");
}

fn note(text: &str) {
    println!("      . {text}");
}

fn raw(text: &str) {
    println!("{text}");
}

fn blank() {
    println!();
}

fn group(title: &str) {
    println!("  -- {title}");
}

fn fatal(message: &str) -> ! {
    eprintln!("\nplayground: {message}");
    std::process::exit(1);
}

/// Run one payload through rows() and print the verdict on one line, plus a
/// "~" line per Transform (a silent change ClickHouse made).
fn feed(schema: &chtypes::Schema, label: &str, format: Format, body: &[u8]) {
    let batch = match schema.rows(format, body, &RowsOptions::default()) {
        Ok(b) => b,
        Err(err) => {
            kv(&format!("  {label}"), &format!("call failed: {err}"));
            return;
        }
    };
    let mut parts = vec![batch.outcome.as_str().to_string()];
    if batch.err_code != 0 {
        parts.push(format!("code={}", batch.err_code));
    }
    if let Some(row) = batch.rows.first() {
        let vals: Vec<String> = row
            .values
            .iter()
            .map(|v| format!("{}={}({})", v.column, text_or(v), v.source))
            .collect();
        if !vals.is_empty() {
            parts.push(vals.join(" "));
        }
    }
    if !batch.err_msg.is_empty() {
        parts.push(truncate(&batch.err_msg.to_string(), 56));
    }
    kv(&format!("  {label}"), &parts.join("  "));
    for t in &batch.transformed {
        raw(&format!(
            "      ~ {}: {} -> {} ({}{})",
            t.column,
            t.input,
            t.stored,
            t.reason,
            if t.lossy { ", LOSSY" } else { "" }
        ));
    }
}

/// Print which variant a Result carries, with its code.
fn describe_error(result: Result<(), Error>, what: &str) {
    match result {
        Ok(()) => kv(what, "(no error)"),
        Err(err) => match &err {
            Error::Schema(c) => {
                kv(
                    what,
                    &format!(
                        "Error::Schema (a REFUSAL), ClickHouse code {} {}",
                        c.ch_code, c.ch_name
                    ),
                );
                kv("  message", &truncate(&c.message.to_string(), 84));
                kv("  is_unsupported()?", &err.is_unsupported().to_string());
            }
            Error::Unsupported(_) => {
                kv(what, "Error::Unsupported (a DECLINE)");
                kv("  message", &truncate(&err.to_string(), 84));
            }
            Error::Usage(c) => {
                kv(what, "Error::Usage (misuse)");
                kv("  message", &truncate(&c.message.to_string(), 84));
            }
            other => {
                kv(
                    what,
                    &format!(
                        "{} ({})",
                        other.code().unwrap_or("internal"),
                        truncate(&other.to_string(), 60)
                    ),
                );
            }
        },
    }
}

/// Name a Result's error KIND in one word.
fn classify(result: Result<(), Error>) -> String {
    match result {
        Ok(()) => "accepted".to_string(),
        Err(err) => match &err {
            Error::Schema(c) => format!("REFUSED   (Error::Schema, code {})", c.ch_code),
            Error::Unsupported(_) => "DECLINED  (Error::Unsupported)".to_string(),
            _ => truncate(&err.to_string(), 80),
        },
    }
}

fn classify_unit(result: Result<(), Error>) -> String {
    match result {
        Ok(()) => "ok".to_string(),
        Err(err) => truncate(&err.to_string(), 80),
    }
}

fn classify_lib(result: Result<Arc<Library>, Error>) -> String {
    classify(result.map(|_| ()))
}

fn verdict(batch: &BatchResult) -> String {
    if batch.err_code != 0 {
        return format!(
            "{:<9} code={}  {}",
            batch.outcome.as_str(),
            batch.err_code,
            truncate(&batch.err_msg.to_string(), 44)
        );
    }
    if let Some(v) = batch.rows.first().and_then(|r| r.values.first()) {
        return format!("{:<9} {} = {}", batch.outcome.as_str(), v.column, v.text);
    }
    batch.outcome.as_str().to_string()
}

fn filter_line(result: Result<FilterResult, Error>) -> String {
    match result {
        Ok(fr) => verdict_string(&fr),
        Err(err) => err.to_string(),
    }
}

fn verdict_string(fr: &FilterResult) -> String {
    if fr.outcome != FilterOutcome::Ok {
        return format!(
            "{} code={} {}",
            fr.outcome,
            fr.err_code,
            truncate(&fr.err_msg.to_string(), 40)
        );
    }
    let cells: Vec<String> = fr
        .verdicts
        .iter()
        .map(|v| match v {
            Verdict::True => "true".to_string(),
            Verdict::False => "false".to_string(),
            other => other.as_str().to_string(),
        })
        .collect();
    format!("[{}]", cells.join(", "))
}

fn text_or(value: &Value) -> String {
    if value.text.is_empty() && !value.null {
        "<unreadable>".to_string()
    } else {
        value.text.to_string()
    }
}

fn truncate(text: &str, limit: usize) -> String {
    let flat = text.replace('\n', " ");
    if flat.chars().count() <= limit {
        return flat;
    }
    let cut: String = flat.chars().take(limit).collect();
    format!("{cut}...")
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).unwrap_or(0))
        .collect()
}
