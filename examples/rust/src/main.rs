//! The Rust tour of chtypes.
//!
//! WHAT THIS IS
//!
//! chtypes answers one question: "if this row were inserted into this table on
//! this ClickHouse version, what would happen?" — without a server. The
//! answers come from ClickHouse's own C++ (vendored per release into shared
//! libraries behind a 22-function C ABI), which is why they are exact rather
//! than approximately right.
//!
//! This file is a tutorial you RUN. Fourteen numbered sections walk the whole
//! public API of the Rust SDK, from loading an artifact to tearing down, each
//! with a comment saying what it demonstrates, why an ingest pipeline cares,
//! and what to look at in the output. The same fourteen sections — same
//! numbering, same schemas, same rows — exist in go/main.go, python/demo.py
//! and ts/demo.mjs, so you can diff two tours and see only the language
//! idioms differ.
//!
//! EVERYTHING HERE IS OFFLINE. You need cargo and artifacts in the registry
//! (scripts/fetch.sh, or a core-repository build) — no Docker, no ClickHouse server, no
//! network. Even the discovery-kit section (11) runs offline, against CANNED
//! bytes shaped exactly like a real server's responses.
//!
//! ```text
//! cargo run                        # newest vendored version
//! CHTYPES_VERSION=25.8 cargo run   # pick a line
//! ../chplay.sh rust                # same, with prerequisite checks
//! ```
//!
//! Nothing here is a test — the real suites live in rust. Every
//! number printed below is produced by the run, never written down by hand.

use std::sync::Arc;

use chtypes::{
    BatchResult, CompileMode, DefaultKind, DocFlags, Error, FilterOutcome, FilterResult, Format,
    Library, Outcome, Registry, Value, NO_PARAMS, NO_SETTINGS, QUERY_CHANGED_SETTINGS,
    QUERY_SERVER_VERSION, SETTING_CLOCK_OFFSET_NANOS, SETTING_MAX_CLOCK_SKEW_NANOS,
    SETTING_NOW_EPOCH_NANOS,
};

// ------------------------------------------------------------- the fixture
//
// These constants are IDENTICAL in all four playgrounds. Change one here and
// you must change it in go/main.go, python/demo.py and ts/demo.mjs too — the
// point of this directory is that the four outputs can be diffed.

/// The tenant's table for the DEFAULT/result sections: one of everything the
/// tour needs — a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how a
/// table gets poisoned), and a MATERIALIZED column (never in SELECT *).
const DEMO_DDL: &str = "ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)";

/// A small three-column table for the outcome and format sections. Positional
/// formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
/// small schema, and both DEFAULTs give the empty-field rules something to do.
const FORMAT_DDL: &str = "device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown'";

/// Pinning the clock is what makes a demo with now64(3) in it reproducible.
/// The value is a STRING at the boundary, always: 19 digits do not survive an
/// IEEE double, and a JSON number here would be silently ignored.
const PINNED_CLOCK: &str = "1700000000000000000"; // 2023-11-14 22:13:20 UTC

// Hand-built binary payloads (hex), shared by all four tours. Each is
// explained where it is fed. The RBWD payloads are the committed fixtures
// from tests/fixtures/rbwd/, whose value bytes a real ClickHouse wrote.
const ROW_BINARY_OK: &str = "0100000007026f6b"; // UInt32 LE 1, UInt8 7, varint-len "ok"
const RBWD_MARKER: &str = "00020000000100026869"; // device_id=2 by value, seq by marker, label="hi"
const RBWD_POISON: &str = "000100000001"; // id=1 by value, e by marker -> raw 0, NO name
const RBWD_VALUE: &str = "00010000000002"; // id=1 by value, e by value 2 ("green")
/// RowBinaryWithNamesAndTypesAndDefaults: LEB128 column count, names, types,
/// then RBWD-style marker+value rows. Built by hand for FORMAT_DDL:
/// 3 cols, device_id=1 by value, seq by marker (DEFAULT 7), label="ok".
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
const NATIVE_OK: &str =
    "0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b";
const NATIVE_CAST: &str =
    "0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374";
// Buffers: uint64le n_columns, n_rows, then per column a byte size and the
// raw column. 4 bytes ff ff ff ff — declared 4 wide, then (wrongly) 8 wide.
const BUFFERS_OK: &str = "010000000000000001000000000000000400000000000000ffffffff";
const BUFFERS_WIDE: &str = "010000000000000001000000000000000800000000000000ffffffff";

// Section 11's CANNED server responses. These are not live bytes — they are
// shaped EXACTLY like a real ClickHouse's JSONEachRow answers to the three
// discovery queries (quoted UInt64s and all, matching a stock HTTP server's
// output_format_json_quote_64bit_integers=1). Swap in your own HTTP client's
// bytes and nothing else changes.
const CANNED_VERSION_RESULT: &[u8] = b"{\"version\":\"25.8.28.1\"}\n";
const CANNED_SETTINGS_RESULT: &[u8] = b"{\"name\":\"flatten_nested\",\"value\":\"0\"}\n{\"name\":\"date_time_input_format\",\"value\":\"best_effort\"}\n";
const CANNED_COLUMNS_RESULT: &[u8] = b"{\"name\":\"ts\",\"type\":\"DateTime64(3)\",\"default_kind\":\"DEFAULT\",\"default_expression\":\"now64(3)\",\"position\":\"1\"}\n{\"name\":\"device_id\",\"type\":\"UInt32\",\"default_kind\":\"\",\"default_expression\":\"\",\"position\":\"2\"}\n{\"name\":\"reading c\",\"type\":\"Float64\",\"default_kind\":\"\",\"default_expression\":\"\",\"position\":\"3\"}\n{\"name\":\"note\",\"type\":\"String\",\"default_kind\":\"DEFAULT\",\"default_expression\":\"'unset'\",\"position\":\"4\"}\n";

fn main() {
    let (registry, lib) = section1();
    section2(&lib);
    section3(&lib);
    section4(&lib);
    section5(&lib);
    section6(&lib);
    section7(&lib, &registry);
    section8(&lib);
    section9(&lib);
    section10(&lib);
    section11(&registry);
    section12(&registry);
    section13();
    section14();
    section15(&lib);
    section16(&lib);

    // The teardown section 13 narrates: the LAST thing this process does
    // with the registry. Reopening an artifact after a full shutdown is not
    // a promised operation, so the tour keeps ONE registry for its whole
    // life and shuts it down exactly once, here.
    registry.shutdown();

    blank();
    println!("Done. Every value above was measured by this run.");
    println!("The optional ONLINE demo (a real server, end to end) is go/ingest-demo/.");
}

// ---------------------------------------------------------------------------
// SECTION 1 — Load the library and check the ABI
//
// WHAT: open the artifact registry, see every ClickHouse version
// resident in this one process, pick one, and check the ABI revision.
// WHY: an ingest gateway serves tenants on different ClickHouse versions at
// once; the registry is how one process answers for all of them, exactly.
// LOOK FOR: the artifact NAMING ITSELF, and the refusal for a version that is
// not built — never a silent nearest-version fallback.
// C API: chs_clickhouse_version, chs_abi_revision, chs_init (implicit on
// load), chs_free (implicit on every returned string).
// Rust extras: Registry::from_env_or ($CHTYPES_REGISTRY or the fallback),
// registry.dir(), registry.libraries().
// ---------------------------------------------------------------------------
fn section1() -> (Registry, Arc<Library>) {
    section(1, "Load the library and check the ABI");

    // $CHTYPES_REGISTRY wins when set; the per-user cache every SDK defaults
    // to otherwise (chtypes::default_registry_dir).
    let fallback = chtypes::default_registry_dir();
    let registry = match Registry::from_env_or(&fallback) {
        Ok(r) => r,
        Err(err) => fatal(&format!(
            "open registry: {err}\n\nFetch an artifact first: `scripts/fetch.sh 25.8` (or build one in the core repository)."
        )),
    };
    let versions = registry.versions();
    kv("registry dir", &registry.dir().display().to_string());
    kv("versions resident", &versions.join("  "));
    kv("libraries loaded", &registry.libraries().len().to_string());
    note("one dlopen (RTLD_LOCAL) per version — all live in THIS process at once");
    if versions.len() == 1 {
        note("only one artifact is built; the tour still runs, and section 12's");
        note("cross-version sweeps will degrade gracefully. More: `scripts/fetch.sh 26.7`");
    }

    // Version selection: a minor line ("25.8") and an exact patch
    // ("25.8.28.1-lts") both resolve. Docker tags drift, so an
    // exact-match-only lookup would silently lose a whole version line.
    let want = match std::env::var("CHTYPES_VERSION") {
        Ok(v) if !v.is_empty() => {
            kv("version selected", &format!("{v}  (from $CHTYPES_VERSION)"));
            v
        }
        _ => {
            let v = newest_line(&versions);
            kv(
                "version selected",
                &format!("{v}  (default: newest held; set $CHTYPES_VERSION to change)"),
            );
            v
        }
    };
    let lib = match registry.for_version(&want) {
        Ok(l) => l,
        Err(err) => fatal(&err.to_string()),
    };
    kv(
        "artifact reports",
        &format!("{}  (minor line {})", lib.version(), lib.minor()),
    );
    note("the artifact names ITSELF via chs_clickhouse_version() — nothing is");
    note("ever inferred from a directory or file name");

    // The ABI revision closes the gap symbol presence cannot: a symbol proves
    // a function exists, never that its signature matches. The binding's
    // revision is a crate constant; the artifact's is a live call. A nonzero
    // disagreement is refused AT LOAD, not discovered mid-call.
    kv("ABI revision (binding)", &chtypes::ABI_REVISION.to_string());
    kv(
        "ABI revision (artifact)",
        &format!(
            "{}   (0 would mean 'predates the probe')",
            lib.abi_revision()
        ),
    );
    kv(
        "compile-settings symbol",
        &format!(
            "{}  (Library::has_compile_settings)",
            lib.has_compile_settings()
        ),
    );

    // Graceful refusal: answering 26.7 semantics out of a 25.8 artifact would
    // be a lie, so an unknown version errors, NAMING what is loaded.
    blank();
    match registry.for_version("99.9") {
        Ok(_) => kv("asking for 99.9", "(no error!?)"),
        Err(err) => kv("asking for 99.9", &err.to_string()),
    }
    note("no nearest-neighbor fallback, ever — a wrong-version answer is a");
    note("wrong answer with a green checkmark on it");

    (registry, lib)
}

// ---------------------------------------------------------------------------
// SECTION 2 — Ask a build about itself
//
// WHAT: type validation and canonicalisation, straight from this build's own
// DataTypeFactory — plus the whole introspection group: the widened REFERENCE
// type, the type-family registry, and the function-flags audit. Every SDK
// exposes the trio since the 2026-08-26 parity cycle; Rust was the complete
// column the others were brought up to.
// WHY: canonicalisation is how you compare a tenant's declared type against
// what the server will actually store — and it is NOT a spelling normaliser,
// it is the server's own parse.
// LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, the error
// for an unknown family carrying ClickHouse's own code 50, and now64's row in
// the function-flags audit — the statelessness gate's raw material.
// C API: chs_validate_type, chs_reference_type, chs_registered_families,
// chs_function_flags — all four.
// ---------------------------------------------------------------------------
fn section2(lib: &Arc<Library>) {
    section(2, "Ask a build about itself");

    kv("validate_type", "input -> this build's canonical spelling");
    for t in [
        "Decimal(18,4)",
        "Variant(UInt8, String)",
        "BIGINT",
        "LowCardinality( String )",
    ] {
        match lib.validate_type(t) {
            Ok(canon) => kv(&format!("  {t}"), &format!("-> {canon}")),
            Err(err) => kv(&format!("  {t}"), &err.to_string()),
        }
    }
    note("Variant members are SORTED; surplus parameters are dropped; the");
    note("space after each comma is the library's own spelling — compare");
    note("canonical strings verbatim, never re-normalise whitespace");
    blank();

    // An unknown family is a typed error carrying ClickHouse's OWN code and
    // message — not a string you have to pattern-match.
    if let Err(Error::Schema { code, message, .. }) = lib.validate_type("NotAType") {
        kv(
            "validate_type(NotAType)",
            &format!("Error::Schema code={code}  {message}"),
        );
        note("code 50 = UNKNOWN_TYPE — the server's own code, from the server's");
        note("own registry. Section 10 is the full error taxonomy.");
    }
    blank();

    // The widened reference type: the second parse every row call already
    // runs to detect silent transformations, exposed as a diagnostic. Rust
    // spells "no wider type" as None rather than "".
    kv(
        "reference_type",
        "the widened type transformation findings compare against",
    );
    for t in ["UInt8", "DateTime", "String"] {
        match lib.reference_type(t) {
            Ok(Some(ref_ty)) => kv(&format!("  {t}"), &format!("-> Some({ref_ty:?})")),
            Ok(None) => kv(&format!("  {t}"), "-> None   (no wider type exists)"),
            Err(err) => kv(&format!("  {t}"), &err.to_string()),
        }
    }
    note("UInt8 reparses through Int256, so an overflow wrap is CAUGHT by the");
    note("reference disagreeing; String has no wider type, so None");
    blank();

    // Every type family in this build's own runtime registry.
    match lib.registered_families() {
        Ok(families) => {
            kv(
                "registered_families",
                &format!(
                    "{} type families (e.g. {})",
                    families.len(),
                    families[..3].join(", ")
                ),
            );
            note("read from the build itself, so there is no per-release list to");
            note("maintain — this is how chtypes tracks upstream type families");
        }
        Err(err) => kv("registered_families", &err.to_string()),
    }
    blank();

    // The function-flags audit: every registered function's volatility, from
    // ClickHouse's own registry. This TSV is what the build gate reads to
    // enforce that the ONLY volatile functions are the four clock reads —
    // the statelessness guarantee, build-enforced.
    match lib.function_flags() {
        Ok(tsv) => {
            let lines: Vec<&str> = tsv.lines().collect();
            kv(
                "function_flags",
                &format!("{} registered functions audited (TSV)", lines.len()),
            );
            if let Some(row) = lines.iter().find(|l| l.starts_with("now64\t")) {
                kv("  the now64 row", &row.replace('\t', "  "));
            }
            note("columns: name, deterministic, deterministic_in_query,");
            note("server_constant, stateful, resolver_error_code — ClickHouse's own");
            note("answers, and the raw material of the volatile-set build gate.");
            note("every SDK exposes this call (docs/reference/bindings.md §Introspection).");
        }
        Err(err) => kv("function_flags", &err.to_string()),
    }
}

// ---------------------------------------------------------------------------
// SECTION 3 — Compile a schema and read it back
//
// WHAT: compile a column-declaration list (NOT a CREATE TABLE) and walk the
// compiled columns: canonical types, DEFAULT kinds and expressions.
// WHY: the compiled handle IS the table, as this ClickHouse version would
// create it. Two rewrites below are things no type-string comparison could
// ever catch — the compile is schema-aware.
// LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), an ALIAS column
// whose type is INFERRED, and default_is_literal (which Go does not surface).
// C API: chs_schema_compile, chs_schema_column_count/_name/_type/
// _default_kind/_default_expr/_default_is_literal, chs_schema_free (Drop).
// Rust note: `lib.compile(ddl)` returns a builder — `.settings(...)`,
// `.mode(...)`, then `.compile()`. Rust has no optional arguments, and the
// alternative was `None, CompileMode::Declared` at every call site.
// ---------------------------------------------------------------------------
fn section3(lib: &Arc<Library>) {
    section(3, "Compile a schema and read it back");

    kv("the DDL", "");
    for ddl_line in DEMO_DDL.split(",\n") {
        raw(&format!("      {ddl_line}"));
    }
    let schema = lib.compile(DEMO_DDL).compile().expect("compile");
    blank();
    kv(
        "compiled columns",
        "name  type  (default kind + expression, literal?)",
    );
    for c in schema.columns() {
        let extra = match &c.default_kind {
            DefaultKind::None => String::new(),
            kind => format!(
                "  {:?} {}  literal={}",
                kind, c.default_expr, c.default_is_literal
            ),
        };
        kv(&format!("  {}", c.name), &format!("{}{extra}", c.ty));
    }
    note("payload_len is MATERIALIZED: compiled, introspectable, but never");
    note("read from input — watch it come back separately in section 6.");
    note("default_is_literal marks a DEFAULT applicable without the expression");
    note("interpreter (seq's 0: true; ts's now64(3): false)");
    drop(schema); // chs_schema_free — Drop does it; dropping early is explicit here
    blank();

    // Rewrite 1: a DEFAULT can change the declared TYPE. This is why
    // validate_type alone is not enough and a schema-aware compile exists.
    let schema = lib
        .compile("x Int64 DEFAULT NULL")
        .compile()
        .expect("compile");
    let c = &schema.columns()[0];
    kv(
        "x Int64 DEFAULT NULL",
        &format!("compiles as {} DEFAULT {}", c.ty, c.default_expr),
    );
    note("the DEFAULT rewrote the type to Nullable — the server does this at");
    note("CREATE, so chtypes must too or every later verdict drifts");
    drop(schema);

    // Rewrite 2: an ALIAS column's type is inferred from its expression.
    let schema = lib
        .compile("a UInt8, al ALIAS a + 1")
        .compile()
        .expect("compile");
    kv(
        "a UInt8, al ALIAS a + 1",
        &format!("al compiles as {}", schema.columns()[1].ty),
    );
    note("UInt8 + 1 widens to UInt16, ClickHouse's own inference");
    drop(schema);
    blank();

    // A failed compile is the same typed error as section 2's.
    let err = lib.compile("x NotAType").compile().map(|_| ());
    kv("x NotAType", &classify(err));
    let msg = lib
        .compile("x NotAType")
        .compile()
        .err()
        .map(|e| e.to_string())
        .unwrap_or_default();
    note(&truncate(&msg, 90));
}

// ---------------------------------------------------------------------------
// SECTION 4 — Compile under a settings profile
//
// WHAT: the same compile with a DECLARED settings profile fixed into the
// handle — the settings a real server would have had at CREATE TABLE.
// WHY: some settings change the SHAPE of a table (flatten_nested), some gate
// which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
// discovers a deployment's settings once (section 11) and declares them here;
// the handle then behaves like a table created on THAT server.
// LOOK FOR: one DDL compiling to two different column lists; a type gate
// failing with the server's own 455; a typo'd setting name failing with the
// server's own 115 INCLUDING its did-you-mean hint.
// C API: chs_schema_compile (settings_json + mode arguments).
// ---------------------------------------------------------------------------
fn section4(lib: &Arc<Library>) {
    section(4, "Compile under a settings profile");

    // (a) A compile-SHAPE setting: the same DDL, two storage shapes.
    kv(
        "(a) flatten_nested",
        "id UInt32, n Nested(a UInt8, b String)",
    );
    for value in ["1", "0"] {
        match lib
            .compile("id UInt32, n Nested(a UInt8, b String)")
            .settings([("flatten_nested", value)])
            .compile()
        {
            Ok(schema) => {
                let shapes: Vec<String> = schema
                    .columns()
                    .iter()
                    .map(|c| format!("{} {}", c.name, c.ty))
                    .collect();
                kv(&format!("  ={value}"), &shapes.join(" | "));
            }
            Err(err) => kv(&format!("  ={value}"), &err.to_string()),
        }
    }
    note("under 1 (the stock default) the Nested column is stored FLATTENED as");
    note("two Arrays; under 0 it is one Array(Tuple) column. Every downstream");
    note("answer — names, arity, the RowBinary wire — follows the compiled shape.");
    blank();

    // (b) A TYPE GATE, declared: checked ONCE at compile with the server's own
    // code, exactly where a real server checks it (at CREATE).
    kv(
        "(b) a type gate",
        "lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types",
    );
    for value in ["0", "1"] {
        match lib
            .compile("lc LowCardinality(UInt8)")
            .settings([("allow_suspicious_low_cardinality_types", value)])
            .compile()
        {
            Ok(schema) => kv(
                &format!("  ={value}"),
                &format!("compiled: {}", schema.columns()[0].ty),
            ),
            Err(Error::Schema { code, message, .. }) => {
                kv(
                    &format!("  ={value}"),
                    &format!("REFUSED, the server's own code {code}"),
                );
                note(&truncate(&message, 92));
            }
            Err(err) => kv(&format!("  ={value}"), &err.to_string()),
        }
    }
    blank();

    // (c) A typo in the profile is caught at DECLARE time. The did-you-mean
    // hint is the SERVER'S — chtypes passes it through and invents nothing.
    if let Err(Error::Schema { code, message, .. }) = lib
        .compile("a UInt8")
        .settings([("flatten_nestedd", "1")])
        .compile()
    {
        kv(
            "(c) unknown setting name",
            &format!("flatten_nestedd -> code {code} (UNKNOWN_SETTING)"),
        );
        note(&truncate(&message, 96));
    }
    blank();

    // (d) The compile MODE. One mode exists (Declared = 0), and Rust's
    // CompileMode is a #[repr(i32)] enum with exactly one variant — the
    // out-of-range mode the other three SDKs pass through to the library
    // (and get the -2 decline back for) does not even typecheck here. Same
    // contract, enforced one compiler earlier.
    kv(
        "(d) compile mode",
        &format!("CompileMode::Declared = {}", CompileMode::Declared.code()),
    );
    let ok = lib
        .compile("a UInt8")
        .mode(CompileMode::Declared)
        .compile()
        .map(|_| ());
    kv("  mode=Declared", &classify(ok));
    kv(
        "  mode=7",
        "not constructible in Rust — refused by the type system",
    );
    note("Go/Python/TS pass 7 through and print the library's -2 decline;");
    note("Rust cannot spell the call. Same rule, different enforcement point.");
}

// ---------------------------------------------------------------------------
// SECTION 5 — Accept, reject, decline (and poison)
//
// WHAT: the three verdicts every row lands on — plus the fourth, poisoned,
// which looks like an accept and bites at read time.
// WHY: this is the contract of the whole product. An ingest gateway routes on
// exactly this: accepted -> insert and publish the STORED values; rejected ->
// 400 the producer with the server's own message; unsupported -> chtypes
// refuses to guess, so fall back to the real server (validate cautiously) and
// NEVER convert the decline into an accept or a reject yourself.
// LOOK FOR: the accept carrying a visible coercion (input 256, stored 0);
// the reject carrying ClickHouse's own error text; the decline carrying no
// ClickHouse code at all.
// C API: chs_rows.
// ---------------------------------------------------------------------------
fn section5(lib: &Arc<Library>) {
    section(5, "Accept, reject, decline (and poison)");
    kv("schema", FORMAT_DDL);
    blank();

    let schema = lib.compile(FORMAT_DDL).compile().expect("compile");

    // ACCEPT — with the coercion made visible. ClickHouse's readIntText wraps
    // integers mod 2^N and reports SUCCESS; chtypes derives the Transform so
    // a gateway can warn the tenant BEFORE the row ships to subscribers.
    kv("(a) ACCEPT", r#"{"device_id":1,"seq":256,"label":"ok"}"#);
    feed(
        &schema,
        "JSONEachRow",
        Format::JsonEachRow,
        br#"{"device_id":1,"seq":256,"label":"ok"}"#,
    );
    note("accepted — but look at seq: input 256, stored 0. The ~ line is the");
    note("Transform (reason overflow_wrap, LOSSY). Publish the STORED value;");
    note("publishing the payload value is how previews and tables diverge.");
    blank();

    // REJECT — the server's own refusal, code and message verbatim from the
    // vendored ClickHouse code. Nothing to retry; tell the producer.
    kv("(b) REJECT", r#"{"device_id":"abc"}"#);
    feed(
        &schema,
        "JSONEachRow",
        Format::JsonEachRow,
        br#"{"device_id":"abc"}"#,
    );
    note("code 27 and the message are ClickHouse's OWN — chtypes never");
    note("hand-writes an error, so your 400 body matches what a real INSERT");
    note("would have said");
    blank();

    // DECLINE — chtypes refuses to guess. Values falls back to the SQL
    // expression parser for non-literals; evaluating tenant SQL locally is a
    // guess this library will not make: the row is UNSUPPORTED (-2).
    kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values");
    feed(&schema, "Values", Format::Values, b"(2,7,concat('a','b'))");
    note("unsupported means 'a real server MIGHT WELL accept this; I will not");
    note("guess'. Do not 400 the producer (that manufactures an over-reject),");
    note("do not publish (that manufactures an over-accept): send it to the");
    note("real server unpreviewed and let it decide. Both mistake classes are");
    note("budgeted at zero in this repo.");
    drop(schema);
    blank();

    // POISON — the fourth verdict. The INSERT genuinely succeeds and every
    // later SELECT throws: RowBinaryWithDefaults' marker byte fills an Enum
    // with the raw zero, and Enum8('red'=1,'green'=2) has NO name for 0.
    kv("(d) POISON", "an accept that bites at read time");
    let poison = lib
        .compile("id UInt32, e Enum8('red' = 1, 'green' = 2)")
        .compile()
        .expect("compile");
    kv("  schema", "id UInt32, e Enum8('red' = 1, 'green' = 2)");
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
    note("accepted_poisoned + code 691: the marker fills with the COLUMN-level");
    note("raw zero, and raw 0 has no Enum name. The insert returns success;");
    note("every later SELECT fails. Reported as an ACCEPT variant — never a");
    note("rejection — because the insert really does succeed.");
    kv(
        "  control payload",
        &format!("{RBWD_VALUE}   (e supplied by value = 2)"),
    );
    feed(
        &poison,
        "RBWD value",
        Format::RowBinaryWithDefaults,
        &unhex(RBWD_VALUE),
    );
    drop(poison);
    blank();

    // SKIP — the fifth verdict (2026-08-27), and the only per-row-only one: a
    // batch under input_format_allow_errors_* drops a bad row and continues,
    // with the server's own machinery — and since the itemization cycle, every
    // skip keeps its place in rows with the error IRowInputFormat caught
    // before resyncing. The server logs only a count; chtypes reports what it
    // computed.
    kv(
        "(e) SKIP",
        "a bad middle row under input_format_allow_errors_num=10",
    );
    let schema = lib.compile(FORMAT_DDL).compile().expect("compile");
    let batch = schema
        .rows(
            Format::JsonEachRow,
            b"{\"device_id\":1,\"seq\":1,\"label\":\"a\"}\n{\"device_id\":\"oops\"}\n{\"device_id\":3,\"seq\":3,\"label\":\"c\"}\n",
            &[("input_format_allow_errors_num", "10")],
        )
        .expect("rows");
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
            line += &format!("  code={} {}", r.err_code, truncate(&r.err_msg, 48));
        }
        kv(&format!("  row {i}"), &line);
    }
    note("one rows() call answers per input record, IN ORDER: accepted (with");
    note("the coerced values) or skipped (with the error that caused it).");
    note("A Skipped row is never stored — route on the row outcome; forward");
    note("only survivors, and never send allow_errors to the real INSERT.");
}

// ---------------------------------------------------------------------------
// SECTION 6 — DEFAULT evaluation: where every value comes from
//
// WHAT: one row through the demo table with the clock pinned, then reading
// back WHERE each stored value came from (Value.source), which values chtypes
// substituted itself, and which it computed.
// WHY: an INSERT is mostly values the row did NOT supply. A gateway that
// cannot answer "what will the table hold for this column?" cannot preview an
// insert. The volatile-DEFAULT rule is the sharp edge: chtypes resolved
// now64() from ITS clock, so the caller MUST send that column explicitly —
// otherwise the server stamps its own clock and preview != stored, always.
// LOOK FOR: different source values in one row; the substituted warning;
// payload_len under computed (never in values); "bogus" under unknown_fields;
// and a skew-budget DECLINE at the end.
// C API: chs_row (via Schema::row_with_settings), the chtypes_* clock
// settings (the crate names them: SETTING_NOW_EPOCH_NANOS and friends).
// ---------------------------------------------------------------------------
fn section6(lib: &Arc<Library>) {
    section(6, "DEFAULT evaluation: where every value comes from");

    let schema = lib.compile(DEMO_DDL).compile().expect("compile");
    let row = br#"{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}"#;
    kv("row fed", std::str::from_utf8(row).unwrap());
    kv(
        "clock pinned",
        &format!("chtypes_now_epoch_nanos={PINNED_CLOCK}  (2023-11-14 22:13:20 UTC)"),
    );
    note("ts and label are OMITTED on purpose; bogus matches no column");
    let r = schema
        .row_with_settings(
            Format::JsonEachRow,
            row,
            &[(SETTING_NOW_EPOCH_NANOS, PINNED_CLOCK)],
        )
        .expect("row");
    blank();

    kv(
        "outcome / err_code",
        &format!("{} / {}", r.outcome, r.err_code),
    );
    kv(
        "values[]  (the stored row)",
        "column = stored text  (source)",
    );
    for v in &r.values {
        kv(
            &format!("  {}", v.column),
            &format!("{:<28} ({})", text_or(v), v.source),
        );
    }
    note("source values: input (the row supplied it), default (a DEFAULT");
    note("expression evaluated through ClickHouse's own CAST path),");
    note("default_substituted (a VOLATILE default resolved from the pinned");
    note("clock), absent (no DEFAULT: the type's own zero). text is");
    note("ClickHouse's OWN JSON rendering — never re-serialized here, because");
    note("18446744073709551615 through a double comes back ...552000.");

    blank();
    kv(
        "substituted[]",
        "volatile DEFAULTs chtypes resolved from ITS clock",
    );
    for s in &r.substituted {
        kv(
            &format!("  {}", s.column),
            &format!("{}  ->  {}", s.expr, s.text),
        );
    }
    note("SEND THESE AS EXPLICIT COLUMNS IN THE REAL INSERT. If the server");
    note("evaluates now64() itself, preview and stored differ every time —");
    note("ClickHouse reads the clock once per BLOCK, not once per statement.");

    blank();
    kv(
        "computed[]",
        "MATERIALIZED values — durable, but never in SELECT *",
    );
    for c in &r.computed {
        kv(
            &format!("  {}", c.column),
            &format!("{}  =  {}", c.kind, c.text),
        );
    }
    note("reported separately from values so the preview matches what a");
    note("subscriber reading the table will actually see");

    blank();
    kv(
        "transformed[]",
        "every silent change, with a machine-readable reason",
    );
    for t in &r.transformed {
        let input = if t.input.is_empty() {
            "(absent)"
        } else {
            &t.input
        };
        kv(
            &format!("  {}", t.reason),
            &format!(
                "{}: {} -> {}   lossy={}",
                t.column,
                input,
                t.stored,
                t.lossy()
            ),
        );
    }
    note("lossy() is false for exactly four reasons (reformat, default_filled,");
    note("zero_filled, default_materialized) and true for everything else");

    blank();
    kv("unknown_fields[]", &format!("{:?}", r.unknown_fields));
    kv(
        "unsupported_settings[]",
        &format!("{:?}", r.unsupported_settings),
    );
    note("unknown fields are reported, not judged — whether to 400 on them is");
    note("gateway policy. A non-empty unsupported_settings promotes the row to");
    note("unsupported: a declined setting must never score as agreement.");
    blank();

    // A DEFAULT can read OTHER columns of the same row.
    let dep = lib
        .compile("a UInt8, d UInt8 DEFAULT a + 1")
        .compile()
        .expect("compile");
    kv(
        "row-dependent DEFAULT",
        "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`",
    );
    feed(&dep, "CSV", Format::Csv, b"7,");
    note("the bare empty CSV field takes the DEFAULT, and the DEFAULT reads");
    note("a=7 from the same row — d stores 8, exactly as the server computes it");
    drop(dep);
    blank();

    // The clock-skew budget: the one place a DEFAULT becomes a DECLINE. Past
    // the budget the only safe answer is "unsupported" — a substituted
    // timestamp too far in the past under a TTL is silently deleted at merge
    // time, with no error at any point.
    let r2 = schema
        .row_with_settings(
            Format::JsonEachRow,
            br#"{"device_id":1,"seq":1,"payload":"x","grade":"b"}"#,
            &[
                (SETTING_CLOCK_OFFSET_NANOS, "5000000000"),
                (SETTING_MAX_CLOCK_SKEW_NANOS, "1"),
            ],
        )
        .expect("row");
    kv("skew budget decline", "offset=5s, budget=1ns");
    kv(
        "  outcome / err_code",
        &format!("{} / {}", r2.outcome, r2.err_code),
    );
    for v in &r2.values {
        if v.column == "ts" {
            kv("  ts.source", &v.source);
        }
    }
    note("default_volatile_unresolved: chtypes refuses to substitute a");
    note("volatile DEFAULT when the measured clock offset exceeds the");
    note("caller's budget (chtypes_max_clock_skew_nanos)");
}

// ---------------------------------------------------------------------------
// SECTION 7 — One schema, every format
//
// WHAT: the same three-column schema fed in all ten chs_format encodings —
// accept and reject for each text format, then the binary tier with
// hand-built bytes.
// WHY: format is not cosmetic. Each format has signature behaviors (CSV's
// bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
// Buffers' silent reinterpret) that change what the table ends up holding.
// LOOK FOR: the same logical row giving format-specific verdicts, and the
// byte-level payloads in the comments — every binary payload is explained.
// C API: chs_rows with format codes 0..9 (frozen integers: JsonEachRow=0,
// Csv=1, Tsv=2, Values=3, JsonCompactEachRow=4, RowBinary=5,
// RowBinaryWithDefaults=6, RowBinaryWithNamesAndTypesAndDefaults=7,
// Native=8, Buffers=9).
// ---------------------------------------------------------------------------
fn section7(lib: &Arc<Library>, registry: &Registry) {
    section(7, "One schema, every format");
    kv("schema", FORMAT_DDL);
    blank();

    let schema = lib.compile(FORMAT_DDL).compile().expect("compile");

    group("text formats (one row per line; positional or named)");
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
    note("Values has an explicit DEFAULT keyword; an SQL expression is a");
    note("DECLINE (section 5c) — evaluated by a real server, guessed by nobody");
    blank();

    // CSV's signature rule deserves its own two lines.
    kv(
        "CSV empty-field rule",
        "bare empty takes the DEFAULT; quoted empty is ''",
    );
    feed(&schema, "CSV   bare   2,7,", Format::Csv, b"2,7,");
    feed(&schema, r#"CSV   quoted 3,7,"""#, Format::Csv, br#"3,7,"""#);
    blank();

    group("binary formats (bytes, COUNTED — never NUL-terminated)");
    kv("  RowBinary payload", ROW_BINARY_OK);
    feed(
        &schema,
        "RowBinary    accept",
        Format::RowBinary,
        &unhex(ROW_BINARY_OK),
    );
    note("4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String (\"ok\")");
    note("— no framing, no names, no self-description");
    feed(
        &schema,
        "RowBinary    reject",
        Format::RowBinary,
        &[0x01, 0x00],
    );
    note("truncated mid-row: framing faults are all-or-nothing per batch");
    kv("  RBWD payload", RBWD_MARKER);
    feed(
        &schema,
        "RBWD  marker byte",
        Format::RowBinaryWithDefaults,
        &unhex(RBWD_MARKER),
    );
    note("RowBinaryWithDefaults prefixes each column with a marker byte:");
    note("00 = 'value follows', nonzero = 'compute the DEFAULT, read no value");
    note("bytes'. Above, seq's marker is 01 -> stored 7 (its DEFAULT).");
    kv(
        "  RBWNTD payload",
        "(hand-built: LEB128 count, names, types, then a marker row)",
    );
    feed(
        &schema,
        "RBWNTD       accept",
        Format::RowBinaryWithNamesAndTypesAndDefaults,
        &unhex(RBWNTD_ROW),
    );
    note("format 7 arrives with ClickHouse 26.x — on an older artifact the line");
    note("above is the server's own 73 UNKNOWN_FORMAT, not a chtypes error.");
    note("Section 12 turns exactly this into the version-pinning lesson.");
    blank();

    group("Native: self-describing, and it CASTs");
    feed(
        &schema,
        "Native       accept",
        Format::Native,
        &unhex(NATIVE_OK),
    );
    note("a Native block declares its OWN column names and types (captured");
    note("from a live 25.8 SELECT ... FORMAT Native)");
    feed(
        &schema,
        "Native       CAST",
        Format::Native,
        &unhex(NATIVE_CAST),
    );
    note("this block declares seq as String \"200\" while the table says UInt8:");
    note("the disagreement is CAST silently (input_format_native_allow_types_");
    note("conversion defaults to true on every vendored era) — visible here as");
    note("a Transform, invisible on a real server");
    drop(schema);
    blank();

    group("Buffers: NO self-description at all");
    let newest = registry
        .for_version(&newest_line(&registry.versions()))
        .expect("newest");
    let buffers = newest.compile("x Int32").compile().expect("compile");
    kv(
        "  artifact",
        &format!(
            "{}  (Buffers arrives at 26.5; this block uses the newest held)",
            newest.minor()
        ),
    );
    kv("  payload", "4 bytes ff ff ff ff, declared x Int32");
    feed(
        &buffers,
        "Buffers reinterpret",
        Format::Buffers,
        &unhex(BUFFERS_OK),
    );
    note("a UInt32 producer's 4294967295 reads back as -1: same width, no");
    note("metadata, so no check CAN fire. The over-accept class in a format");
    note("that cannot detect it — the schema is entirely out of band.");
    feed(
        &buffers,
        "Buffers width",
        Format::Buffers,
        &unhex(BUFFERS_WIDE),
    );
    note("the same 4 bytes declared 8 wide IS caught: size accounting disagrees");
}

// ---------------------------------------------------------------------------
// SECTION 8 — Engines, MergeTree settings, and TTL
//
// WHAT: declare the table's engine and TTL, then watch the STORAGE layer
// change what a batch stores — including storing nothing at all.
// WHY: a row can be accepted per row and absent per batch. SummingMergeTree
// folds rows at insert; a TTL already in the past deletes them at merge, with
// no error at any point. A gateway reading only per-row verdicts previews
// rows the table will never hold.
// LOOK FOR: engine_rows (the stored truth) being SHORTER than the input; the
// TTL batch whose row is accepted and whose engine_rows is empty; and the
// refusal-vs-decline pair on MergeTree settings (the sign of the ABI return
// decides which).
// C API: chs_schema_engine, chs_schema_ttl, chs_rows.
// ---------------------------------------------------------------------------
fn section8(lib: &Arc<Library>) {
    section(8, "Engines, MergeTree settings, and TTL");

    // (a) A specialised engine changes what the table STORES.
    let mut schema = lib
        .compile("day Date, key UInt32, v UInt64")
        .compile()
        .expect("compile");
    kv("(a) set_engine", "SummingMergeTree ORDER BY (day, key)");
    schema
        .set_engine("SummingMergeTree", "(day, key)", NO_SETTINGS)
        .expect("set_engine");
    let body = br#"{"day":"2026-01-01","key":1,"v":5}
{"day":"2026-01-01","key":1,"v":7}"#;
    let batch = schema
        .rows(Format::JsonEachRow, body, NO_SETTINGS)
        .expect("rows");
    kv(
        "  rows in / rows_read",
        &format!("2 / {}   (v=5 and v=7, same key)", batch.rows_read),
    );
    kv(
        "  engine_rows (stored)",
        &format!(
            "[{}]",
            batch.engine_rows.clone().unwrap_or_default().join(", ")
        ),
    );
    note("two rows in, ONE row out, v summed — engine_rows is the post-merge");
    note("preview and, when present, the truth to believe over rows");
    drop(schema);
    blank();

    // (b) MergeTree-namespace settings: two failures, two KINDS. The sign of
    // the ABI return decides — a positive code is the SERVER refusing, a
    // negative one is this LIBRARY declining. Never flatten them.
    kv("(b) MergeTree settings", "refusal vs decline vs inert");
    let mut s2 = lib.compile("a UInt8").compile().expect("compile");
    let verdict =
        classify(s2.set_engine("MergeTree", "tuple()", &[("index_granularityy", "8192")]));
    kv(
        "  unknown NAME",
        &format!("index_granularityy -> {verdict}"),
    );
    note("the server's own 115: this DDL can never exist — tell the tenant");
    let mut s3 = lib.compile("a UInt8").compile().expect("compile");
    let verdict = classify(s3.set_engine("MergeTree", "tuple()", &[("index_granularity", "4096")]));
    kv(
        "  known, non-default",
        &format!("index_granularity=4096 -> {verdict}"),
    );
    note("a DECLINE: no MergeTree setting's behavior is modeled yet, and");
    note("silently ignoring a declared value would fake the profile being in");
    note("force. A real server might well accept it — validate cautiously.");
    let mut s4 = lib.compile("a UInt8").compile().expect("compile");
    let verdict = classify(s4.set_engine("MergeTree", "tuple()", &[("index_granularity", "8192")]));
    kv(
        "  known, AT default",
        &format!("index_granularity=8192 -> {verdict} (inert)"),
    );
    blank();

    // (c) TTL: accepted per row, gone per batch.
    let mut schema = lib
        .compile("ts DateTime, v UInt8")
        .compile()
        .expect("compile");
    schema
        .set_engine("MergeTree", "ts", NO_SETTINGS)
        .expect("set_engine");
    schema.set_ttl("ts + INTERVAL 1 DAY").expect("set_ttl");
    kv(
        "(c) set_ttl",
        "MergeTree ORDER BY ts, TTL ts + INTERVAL 1 DAY",
    );
    let batch = schema
        .rows(
            Format::JsonEachRow,
            br#"{"ts":"2020-01-01 00:00:00","v":9}"#,
            &[(SETTING_NOW_EPOCH_NANOS, PINNED_CLOCK)],
        )
        .expect("rows");
    kv(
        "  row fed",
        r#"{"ts":"2020-01-01 00:00:00","v":9}  with the clock pinned to 2023"#,
    );
    kv(
        "  row-level outcome",
        &format!("{}   <- the row PARSED fine", batch.rows[0].outcome),
    );
    let engine_rows = batch.engine_rows.clone().unwrap_or_default();
    kv(
        "  engine_rows (stored)",
        &format!(
            "[{}]  (length {})",
            engine_rows.join(", "),
            engine_rows.len()
        ),
    );
    for t in &batch.transformed {
        kv(
            "  batch transform",
            &format!(
                "row={} column={:?} reason={} lossy={}",
                t.row,
                t.column,
                t.reason,
                t.lossy()
            ),
        );
    }
    note("accepted per ROW, stored nowhere per BATCH: the 2020 timestamp is");
    note("already past the TTL, so the part holds nothing. On a real server");
    note("this is a silent merge-time delete — the ttl_expired transform is");
    note("the only warning anyone gets.");
    blank();

    // (d) A TTL this library will not guess at.
    let declined = schema.set_ttl("now() + INTERVAL 1 DAY");
    let msg = declined
        .as_ref()
        .err()
        .map(|e| e.to_string())
        .unwrap_or_default();
    kv("(d) set_ttl now()+1 DAY", &classify(declined));
    note(&truncate(&msg, 90));
    note("a clock-reading TTL is DECLINED, not guessed");
}

// ---------------------------------------------------------------------------
// SECTION 9 — Settings precedence: who wins
//
// WHAT: the same row and the same handle, answered differently as settings
// are supplied at each layer:
//
//     per-call  >  handle profile  >  library defaults  >  ClickHouse's own
//
// WHY: this is how a gateway declares a deployment's settings ONCE (at
// compile) yet still lets one INSERT override per call. If precedence were
// fuzzy, the declared profile would not actually be in force.
// LOOK FOR: layers 3 vs 4 — the SAME handle, the SAME bytes, passing under
// the handle profile and failing the moment a per-call value overrides it.
// Then the library-defaults layer measured via set_default_settings,
// including a WHOLESALE refusal that commits nothing.
// C API: chs_rows (settings_json), chs_set_default_settings.
// Rust note: set_default_settings lives on the dlopen'd Library itself (Go's
// Registry path structurally cannot make this call; only its static cgo path
// can). It takes the library's lock exclusively, so it can never swap the
// seed under a running row call.
// ---------------------------------------------------------------------------
fn section9(lib: &Arc<Library>) {
    section(9, "Settings precedence: who wins");
    kv(
        "the probe",
        r#"ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}"#,
    );
    note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts");
    note("ISO-8601 — so the verdict TELLS you which setting value won");
    blank();

    let iso = br#"{"ts":"2026-01-15T10:30:00Z"}"#;
    let basic: &[(&str, &str)] = &[("date_time_input_format", "basic")];
    let best_effort: &[(&str, &str)] = &[("date_time_input_format", "best_effort")];
    kv(
        "artifact",
        &format!(
            "{}  (the selected version; Go's tour uses its static path here)",
            lib.version()
        ),
    );

    let plain = lib.compile("ts DateTime").compile().expect("compile");
    let profiled = lib
        .compile("ts DateTime")
        .settings([("date_time_input_format", "best_effort")])
        .compile()
        .expect("compile");

    let b1 = plain
        .rows(Format::JsonEachRow, iso, NO_SETTINGS)
        .expect("rows");
    kv("1. ClickHouse defaults", &verdict(&b1));
    note("nothing declared anywhere — the stock default decides (it rejected");
    note("ISO-8601 through 25.10 and accepts it from 26.5)");
    let b2 = plain.rows(Format::JsonEachRow, iso, basic).expect("rows");
    kv("2.  + per-call basic", &verdict(&b2));
    let b3 = profiled
        .rows(Format::JsonEachRow, iso, NO_SETTINGS)
        .expect("rows");
    kv("3. handle profile best_effort", &verdict(&b3));
    note("the profile declared at COMPILE reaches every later row call");
    let b4 = profiled
        .rows(Format::JsonEachRow, iso, basic)
        .expect("rows");
    kv("4.  + per-call basic", &verdict(&b4));
    note("3 vs 4 is the requirement, measured: the same row PASSES under the");
    note("handle profile and FAILS when the per-call value overrides it");
    blank();

    // The library-defaults layer, and its two safety properties: the seed is
    // REPLACED wholesale on every call, and a payload with any unknown name
    // is refused wholesale — nothing committed.
    kv(
        "set_default_settings",
        "the library-defaults layer, measured",
    );
    lib.set_default_settings(best_effort).expect("seed");
    kv(
        "5. seed best_effort",
        &verdict(
            &plain
                .rows(Format::JsonEachRow, iso, NO_SETTINGS)
                .expect("rows"),
        ),
    );
    lib.set_default_settings(basic).expect("seed");
    kv(
        "6. seed basic",
        &verdict(
            &plain
                .rows(Format::JsonEachRow, iso, NO_SETTINGS)
                .expect("rows"),
        ),
    );
    note("each call REPLACES the whole seed — 5's value did not linger");
    kv(
        "7. seed basic + per-call best_effort",
        &verdict(
            &plain
                .rows(Format::JsonEachRow, iso, best_effort)
                .expect("rows"),
        ),
    );
    note("per-call still outranks the seed");
    match lib.set_default_settings(&[("made_up_setting_xyz", "1")]) {
        Ok(()) => kv("8. seed an unknown name", "(no error!?)"),
        Err(err) => kv("8. seed an unknown name", &truncate(&err.to_string(), 100)),
    }
    kv(
        "   verdict after refusal",
        &format!(
            "{}   <- unchanged: NOTHING was committed",
            verdict(
                &plain
                    .rows(Format::JsonEachRow, iso, NO_SETTINGS)
                    .expect("rows")
            )
        ),
    );
    note("the 115 and its message are the server's own; a gateway can never");
    note("believe a default profile is in force when part of it never applied");
    lib.set_default_settings(NO_SETTINGS).expect("reset");
    kv(
        "9. seed {} (reset)",
        &verdict(
            &plain
                .rows(Format::JsonEachRow, iso, NO_SETTINGS)
                .expect("rows"),
        ),
    );
}

// ---------------------------------------------------------------------------
// SECTION 10 — The error taxonomy
//
// WHAT: every kind of answer this SDK gives, told apart BY TYPE — never by
// string matching.
// WHY: the three kinds demand three different reactions (tell the tenant /
// fall back cautiously / fix the deployment). Rust spells them as SIBLING
// VARIANTS of one #[non_exhaustive] enum — never a subtype relationship, so
// a decline cannot be mistaken for a refusal by any match arm.
// LOOK FOR: the same match you would write in production, err.code() /
// err.is_unsupported(), and the reminder that ROW verdicts are data
// (RowResult.outcome), not Err.
// ---------------------------------------------------------------------------
fn section10(lib: &Arc<Library>) {
    section(10, "The error taxonomy");

    kv(
        "the Rust idiom",
        "match on sibling variants of one #[non_exhaustive] enum",
    );
    raw("      match err {");
    raw("          Error::Schema { code, message, .. } => reject_with(code, message),");
    raw("          e if e.is_unsupported() => validate_cautiously(e),");
    raw("          e => fix_deployment(e),  // Registry/Load/NotAnArtifact/...");
    raw("      }");
    blank();

    // A REFUSAL: ClickHouse's own code rides on Error::Schema.
    describe_error(
        lib.compile("x NotAType").compile().map(|_| ()),
        "compile x NotAType",
    );
    // A DECLINE: Error::Unsupported — code() answers None, by design.
    let mut schema = lib
        .compile("ts DateTime, v UInt8")
        .compile()
        .expect("compile");
    schema
        .set_engine("MergeTree", "ts", NO_SETTINGS)
        .expect("set_engine");
    describe_error(
        schema.set_ttl("now() + INTERVAL 1 DAY"),
        "set_ttl now()+1 DAY",
    );
    // A registry miss: its own variants (a deployment problem, not a verdict).
    describe_error(
        Registry::new("/nonexistent-registry").map(|_| ()),
        "Registry::new(\"/nonexistent-registry\")",
    );
    blank();

    kv("row verdicts are DATA", "RowResult.outcome, not Err");
    note("a row the server would reject comes back Ok with outcome Rejected");
    note("and err_code = the server's code — the Result is about whether the");
    note("question could be asked; the verdict lives in the answer.");
    kv(
        "CODE_UNSUPPORTED",
        &format!(
            "{}  (the wire sentinel; never a real ClickHouse code)",
            chtypes::CODE_UNSUPPORTED
        ),
    );
}

// ---------------------------------------------------------------------------
// SECTION 11 — The discovery kit, offline
//
// WHAT: the three canonical queries chtypes ships for learning who a
// deployment is, their typed parsers, and reconstruct_ddl — run here against
// CANNED bytes shaped exactly like a real server's JSONEachRow responses.
// WHY: chtypes NEVER opens a socket. You run these queries with whatever
// client you already have; the kit gives you the SQL and parses the results.
// The payoff is the last step: the server's own version string resolves an
// artifact, and the discovered settings become the compile profile — so the
// handle behaves like a table created on THAT deployment.
// LOOK FOR: the reconstructed DDL (backticks where needed, DEFAULTs carried),
// and the SAME ROW accepted under the discovered profile but rejected under a
// stock compile — the measurable reason discovery matters.
// C API: none until the compile at the end — the kit is pure client-side.
// (The ONLINE version of this flow, against a real server, is
// go/ingest-demo/ — the optional demo chplay.sh never runs.)
// ---------------------------------------------------------------------------
fn section11(registry: &Registry) {
    section(11, "The discovery kit, offline");
    kv(
        "NOTE",
        "responses below are CANNED — shaped exactly like a real",
    );
    kv(
        "",
        "server's, so the parsers cannot tell. Swap in your HTTP client.",
    );
    blank();

    // Query 1: who are you? (version)
    kv("QUERY_SERVER_VERSION", QUERY_SERVER_VERSION);
    kv(
        "  canned response",
        String::from_utf8_lossy(CANNED_VERSION_RESULT).trim(),
    );
    let version = chtypes::parse_version_result(CANNED_VERSION_RESULT).expect("parse");
    kv("  parsed", &version);
    blank();

    // Query 2: which settings did this deployment change from stock?
    kv("QUERY_CHANGED_SETTINGS", QUERY_CHANGED_SETTINGS);
    for canned_line in String::from_utf8_lossy(CANNED_SETTINGS_RESULT)
        .trim()
        .split('\n')
    {
        kv("  canned response", canned_line);
    }
    let settings = chtypes::parse_changed_settings_result(CANNED_SETTINGS_RESULT).expect("parse");
    kv(
        "  parsed",
        &format!("{} changed settings -> the compile profile", settings.len()),
    );
    blank();

    // Query 3: what does the table look like AS STORED?
    kv(
        "QUERY_TABLE_COLUMNS",
        "(system.columns for one table; see the constant)",
    );
    let cols = chtypes::parse_columns_result(CANNED_COLUMNS_RESULT).expect("parse");
    for col in &cols {
        let kind = if col.default_kind.is_empty() {
            String::new()
        } else {
            format!("  {} {}", col.default_kind, col.default_expression)
        };
        kv(
            &format!("  [{}] {}", col.position, col.name),
            &format!("{}{kind}", col.r#type),
        );
    }
    note("default_kind/default_expression are CARRIED — dropping them would");
    note("silently lose the DEFAULT semantics sections 6 and 8 run on");
    let ddl = chtypes::reconstruct_ddl(&cols).expect("reconstruct");
    kv("  reconstruct_ddl", &ddl);
    note("`reading c` came back BACKTICKED — identifiers are quoted exactly");
    note("where ClickHouse requires it");
    blank();

    // The payoff: version -> artifact, settings -> profile, and a measurable
    // difference the discovered profile makes.
    let lib = match registry.for_version(&version) {
        Ok(l) => l,
        Err(err) => {
            kv(
                &format!("registry.for_version({version})"),
                &err.to_string(),
            );
            note("no artifact for this line — `scripts/fetch.sh 25.8` would add it; the");
            note("rest of this section needs it and is skipped");
            return;
        }
    };
    kv(
        &format!("registry.for_version({version})"),
        &format!(
            "artifact {}  (exact patch -> the {} line)",
            lib.version(),
            lib.minor()
        ),
    );
    let row = br#"{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}"#;
    kv("the same row, twice", std::str::from_utf8(row).unwrap());
    let pairs: Vec<(&str, &str)> = settings
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    let profiled = lib
        .compile(&ddl)
        .settings(pairs)
        .compile()
        .expect("compile");
    kv(
        "  under the discovered profile",
        &verdict(
            &profiled
                .rows(Format::JsonEachRow, row, NO_SETTINGS)
                .expect("rows"),
        ),
    );
    let plain = lib.compile(&ddl).compile().expect("compile");
    kv(
        "  under a stock compile",
        &verdict(
            &plain
                .rows(Format::JsonEachRow, row, NO_SETTINGS)
                .expect("rows"),
        ),
    );
    note("the deployment declared date_time_input_format=best_effort, so ITS");
    note("server takes the ISO-8601 timestamp — a stock compile answers for a");
    note("server the tenant does not have. Discovery is what closes that gap.");
}

// ---------------------------------------------------------------------------
// SECTION 12 — Version pinning: same input, different answers
//
// WHAT: the same DDL and the same bytes, swept across every artifact resident
// in this process.
// WHY: version differences are the reason the registry exists. They are not
// monotonic — newer is NOT always more permissive — so no rule can predict
// them; only the real per-version artifact can answer.
// LOOK FOR: 25.10 rejecting a DEFAULT that both 25.8 and 26.5 accept; and the
// Buffers format simply not existing before 26.5 (the server's own 73).
// ---------------------------------------------------------------------------
fn section12(registry: &Registry) {
    section(12, "Version pinning: same input, different answers");
    let versions = registry.versions();
    if versions.len() < 2 {
        kv("versions resident", &versions.join("  "));
        note("only one artifact is built, so there is nothing to sweep — the");
        note("point of this section needs at least two. Build another line");
        note("(e.g. `scripts/fetch.sh 26.7`) and re-run to see the answers diverge.");
        return;
    }

    kv(
        "(a) a mixed-type DEFAULT",
        "a UInt8, x Int64 DEFAULT if(1,2,'a')",
    );
    for v in &versions {
        let Ok(lib) = registry.for_version(v) else {
            continue;
        };
        match lib
            .compile("a UInt8, x Int64 DEFAULT if(1,2,'a')")
            .compile()
        {
            Ok(schema) => {
                let col = &schema.columns()[1];
                kv(
                    &format!("  {v}"),
                    &format!("compiled  ({} DEFAULT {})", col.ty, col.default_expr),
                );
            }
            Err(Error::Schema { code, message, .. }) => {
                kv(
                    &format!("  {v}"),
                    &format!("REJECTED code {code}  {}", truncate(&message, 52)),
                );
            }
            Err(err) => kv(&format!("  {v}"), &err.to_string()),
        }
    }
    note("NEWER IS NOT ALWAYS MORE PERMISSIVE — no monotonic rule predicts");
    note("this, which is exactly why one real artifact per line exists");
    blank();

    kv(
        "(b) a format's arrival",
        "Buffers (code 9), added in ClickHouse 26.5",
    );
    kv(
        "  payload",
        "1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32",
    );
    let payload = unhex(BUFFERS_OK);
    for v in &versions {
        let Ok(lib) = registry.for_version(v) else {
            continue;
        };
        let Ok(schema) = lib.compile("x Int32").compile() else {
            continue;
        };
        match schema.rows(Format::Buffers, &payload, NO_SETTINGS) {
            Ok(b) if b.outcome == chtypes::Outcome::Accepted && !b.rows.is_empty() => {
                kv(
                    &format!("  {v}"),
                    &format!("accepted  x = {}", b.rows[0].values[0].text),
                );
            }
            Ok(b) => kv(
                &format!("  {v}"),
                &format!(
                    "{}  code={}  {}",
                    b.outcome,
                    b.err_code,
                    truncate(&b.err_msg, 40)
                ),
            ),
            Err(err) => kv(&format!("  {v}"), &format!("call failed: {err}")),
        }
    }
    note("73 UNKNOWN_FORMAT is the SERVER'S own answer on the older lines —");
    note("probe the artifact (one payload through rows) instead of trusting");
    note("your own version arithmetic");
}

// ---------------------------------------------------------------------------
// SECTION 13 — Teardown
//
// WHAT: what to release, and when.
// WHY: schema handles are C allocations (chs_schema_free — Schema's Drop).
// Process teardown is chs_shutdown, which Rust exposes as
// Registry::shutdown() (and per-library as Library::shutdown()).
// C API: chs_schema_free, chs_shutdown.
// ---------------------------------------------------------------------------
fn section13() {
    section(13, "Teardown");
    kv(
        "schema handles",
        "dropped = freed (chs_schema_free runs in Schema's Drop)",
    );
    kv(
        "registry.shutdown()",
        "chs_shutdown for every loaded library",
    );
    kv(
        "  when",
        "this tour runs it at the very END (sections 15-16 still need",
    );
    kv(
        "",
        "the libraries); teardown is the LAST thing a process does with a",
    );
    kv(
        "",
        "registry — reopening a shut-down artifact is not a promised operation",
    );
    note("chs_init also registers chs_shutdown with atexit(), so an ordinary");
    note("process would be fine without this — shutdown() is for callers that");
    note("control their own teardown order. Go's Registry deliberately has NO");
    note("teardown (it never dlcloses; docs/reference/bindings.md §Teardown); Python has");
    note("close() + context manager; TS has close()/Symbol.dispose.");
}

// ---------------------------------------------------------------------------
// SECTION 14 — The static path (Go only)
//
// Go's cgo build can additionally LINK one artifact directly (no dlopen) and
// use it through package-level functions; that is also the only place Go
// exposes SetDefaultSettings and RegisteredFamilies. Rust cannot have that
// shape here: the loader is dlopen (libloading), so Registry is the only
// loader — and it is the product path anyway. docs/reference/bindings.md makes the
// static shape explicitly optional. See go/main.go section 14.
// ---------------------------------------------------------------------------
fn section14() {
    section(14, "The static path (Go only)");
    kv(
        "not offered in Rust",
        "the loader is dlopen (libloading); Registry is the only loader",
    );
    note("see go/main.go section 14 — docs/reference/bindings.md §The object model makes");
    note("the statically-linked single-version shape explicitly optional");
}

// ---------------------------------------------------------------------------
// SECTION 15 — Export: bytes + spans
//
// WHAT: the SAME chs_rows call that judges a batch can also serialize its
// accepted rows to wire bytes (JSONCompactEachRow this revision), addressed
// per row by index-aligned spans — rows_export, one C call.
// WHY: every consumer of an accepted row wants the stored bytes ready to
// publish or INSERT without rebuilding them from the document — reassembly
// is where caller bugs live (the invalid-JSON-on-poisoned-rows class).
// LOOK FOR: the skipped row's {0,0} span; a span slice BEING the row's line;
// the poisoned batch DECLINING the export and saying why; emitted-EMPTY (an
// answer) vs declined (not one); and the lean document (DocFlags::NONE — the
// default — keeping every verdict).
// C API: chs_rows with export_format/doc_flags/out_bytes (ABI revision 3).
// ---------------------------------------------------------------------------
fn section15(lib: &Arc<Library>) {
    section(15, "Export: bytes + spans");
    let schema = lib.compile(FORMAT_DDL).compile().expect("compile");
    let body: &[u8] = concat!(
        r#"{"device_id":1,"label":"ok"}"#,
        "\n",
        r#"{"device_id":"zap"}"#,
        "\n",
        r#"{"device_id":3,"label":"hi"}"#,
        "\n",
    )
    .as_bytes();
    let allow = &[("input_format_allow_errors_num", "10")];
    let batch = match schema.rows_export(
        Format::JsonEachRow,
        body,
        allow,
        Some(Format::JsonCompactEachRow),
        DocFlags::ALL,
    ) {
        Ok(b) => b,
        Err(err) if err.is_unsupported() => {
            note("this artifact predates the export surface (relink it to ABI");
            note("revision 3, `just refresh <version>`); the section degrades");
            note(&format!(
                "here rather than failing the tour: {}",
                truncate(&err.to_string(), 48)
            ));
            return;
        }
        Err(err) => panic!("rows_export: {err}"),
    };
    if batch.outcome == Outcome::Unsupported {
        kv(
            "rows_export",
            &format!("declined: {}", truncate(&batch.err_msg, 64)),
        );
        note("this artifact answers the export request unsupported; relink the");
        note("fleet (`just refresh`) to see the live bytes. Degrading.");
        return;
    }
    let payload = batch.payload.as_deref().unwrap_or_default();
    kv(
        "batch",
        &format!(
            "{}  rows_read={} rows_skipped={}",
            batch.outcome.as_str(),
            batch.rows_read,
            batch.rows_skipped
        ),
    );
    kv(
        "payload",
        &format!(
            "{:?}  ({} bytes, one line per ACCEPTED row)",
            String::from_utf8_lossy(payload),
            payload.len()
        ),
    );
    note("wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these");
    note("bytes are directly INSERT-able with no column list; DEFAULTs (seq=7,");
    note("label='unknown') are already applied — preview == stored");
    if let Some(spans) = &batch.spans {
        for (i, span) in spans.iter().enumerate() {
            let line = if span.len > 0 {
                format!(
                    "{:?}",
                    String::from_utf8_lossy(&payload[span.off..span.off + span.len])
                )
            } else {
                "(no bytes — row not accepted)".to_string()
            };
            kv(
                &format!("  span[{i}] {{off:{} len:{}}}", span.off, span.len),
                &format!("{} -> {line}", batch.rows[i].outcome.as_str()),
            );
        }
    }
    note("spans are INDEX-ALIGNED with rows; slicing spans out of the payload");
    note("IS the per-row payload, and concatenating non-zero spans reproduces");
    note("it exactly — batches merge by byte concatenation");
    blank();

    // The lean document: DocFlags::NONE (the default) — every verdict, none
    // of the description, same bytes.
    let lean = schema
        .rows_export(
            Format::JsonEachRow,
            body,
            allow,
            Some(Format::JsonCompactEachRow),
            DocFlags::NONE,
        )
        .expect("rows_export lean");
    kv(
        "lean (DocFlags::NONE)",
        &format!(
            "outcome {}, {} verdict rows, {} values, {} transformed — payload identical: {}",
            lean.outcome.as_str(),
            lean.rows.len(),
            lean.rows[0].values.len(),
            lean.transformed.len(),
            lean.payload == batch.payload
        ),
    );
    note("flags thin the DESCRIPTION, never the VERDICT; DocFlags::VALUES /");
    note("TRANSFORMS / DEFAULTS pick groups a la carte (BitOr composes them)");
    blank();

    // Fail-closed: a poisoned batch holds a value ClickHouse itself cannot
    // read back — no writer can honestly serialize it, so no bytes.
    let poison_schema = lib
        .compile("e Enum8('a' = 1, 'b' = 2)")
        .compile()
        .expect("compile");
    let poi = poison_schema
        .rows_export(
            Format::JsonEachRow,
            b"{\"e\":null}\n",
            &[("input_format_defaults_for_omitted_fields", "0")],
            Some(Format::JsonCompactEachRow),
            DocFlags::ALL,
        )
        .expect("rows_export poison");
    let payload_desc = match &poi.payload {
        None => "None (declined)".to_string(),
        Some(p) => format!("{} bytes", p.len()),
    };
    kv(
        "poisoned batch",
        &format!(
            "{} -> payload={} export_declined={:?}",
            poi.outcome.as_str(),
            payload_desc,
            truncate(&poi.export_declined, 48)
        ),
    );

    // Emitted-empty is an ANSWER (zero accepted rows), not a decline.
    let emp = schema
        .rows_export(
            Format::JsonEachRow,
            b"",
            NO_SETTINGS,
            Some(Format::JsonCompactEachRow),
            DocFlags::NONE,
        )
        .expect("rows_export empty");
    kv(
        "empty batch",
        &format!(
            "payload is_some={} len={}  (emitted-empty != declined)",
            emp.payload.is_some(),
            emp.payload.as_deref().unwrap_or_default().len()
        ),
    );
}

// ---------------------------------------------------------------------------
// SECTION 16 — Filters: WHERE semantics at the edge
//
// WHAT: compile one boolean expression against a schema (compile_filter) and
// evaluate it per row of a body — ClickHouse's own comparison functions, so
// the answers are WHERE-side by construction.
// WHY: read-side row visibility (who may SEE this row) is a WHERE question,
// and WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES
// (false for every row) where an insert would wrap 256 to 0. Reusing the
// insert answer would silently match every legitimate zero.
// LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the
// 'e' class (compiles, then THROWS per row — a server fails the WHOLE query
// here); clock reads and {p:Type} parameters REFUSED at compile, never
// guessed; and the enforcement gate at the end. Rust bonus: the borrow
// checker makes the filter-before-schema free order a COMPILE-TIME fact.
// C API: chs_filter_compile / chs_filter_rows / chs_filter_free (revision 3).
// ---------------------------------------------------------------------------
fn section16(lib: &Arc<Library>) {
    section(16, "Filters: WHERE semantics at the edge");
    let schema = lib.compile("x UInt8").compile().expect("compile");
    let filt = match schema.compile_filter("x = 256", NO_PARAMS) {
        Ok(f) => f,
        Err(err) if err.is_unsupported() => {
            note("this artifact predates the filter surface (relink it to ABI");
            note("revision 3, `just refresh <version>`); the section degrades");
            note(&format!(
                "here rather than failing the tour: {}",
                truncate(&err.to_string(), 48)
            ));
            return;
        }
        Err(err) => panic!("compile_filter: {err}"),
    };
    let fr = filt
        .rows(
            Format::JsonEachRow,
            b"{\"x\":0}\n{\"x\":255}\n",
            NO_SETTINGS,
        )
        .expect("filter rows");
    drop(filt); // Drop = chs_filter_free; the borrow makes the order structural
    kv(
        "filter `x = 256` over UInt8",
        &format!("verdicts {}", verdict_string(&fr)),
    );
    note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side of");
    note("this same library stores 256 as 0 (section 5's overflow_wrap) —");
    note("which is why predicate constants must never be folded through");
    note("insert coercion (docs/reference/bindings.md §Constants are not payloads)");
    blank();

    // NULL is not true — three-valued logic collapsed at the WHERE boundary.
    {
        let nullable_schema = lib
            .compile("lvl Nullable(UInt8)")
            .compile()
            .expect("compile");
        let nf = nullable_schema
            .compile_filter("lvl = 1", NO_PARAMS)
            .expect("compile_filter");
        let fr = nf
            .rows(
                Format::JsonEachRow,
                b"{\"lvl\":null}\n{\"lvl\":1}\n",
                NO_SETTINGS,
            )
            .expect("filter rows");
        kv(
            "`lvl = 1` on [null, 1]",
            &format!(
                "verdicts {}   (NULL is not true, as WHERE hides it)",
                verdict_string(&fr)
            ),
        );
    }
    blank();

    // The 'e' class: compiles clean, then THROWS on every row's values.
    {
        let str_schema = lib.compile("s String").compile().expect("compile");
        let sf = str_schema
            .compile_filter("s = 257", NO_PARAMS)
            .expect("compile_filter");
        let fr = sf
            .rows(Format::JsonEachRow, b"{\"s\":\"hi\"}\n", NO_SETTINGS)
            .expect("filter rows");
        kv(
            "`s = 257` over String",
            &format!("verdicts {}", verdict_string(&fr)),
        );
        for fe in &fr.errors {
            kv(
                &format!("  row {}", fe.row),
                &format!("code {}  {}", fe.code, truncate(&fe.err, 56)),
            );
        }
        note("on a real server this WHERE fails the WHOLE query — 'e' is NOT an");
        note("answer, and neither is 'd' (a row this library declines): an");
        note("enforcing caller fails CLOSED on both, or NOT(decline-as-false)");
        note("inverts fail-closed into fail-open — the measured leak class");
    }
    blank();

    // A bad row declines ('d'), itemized, and the tail keeps its indexes.
    {
        let lt = schema
            .compile_filter("x < 5", NO_PARAMS)
            .expect("compile_filter");
        let fr = lt
            .rows(
                Format::JsonEachRow,
                b"{\"x\":1}\n{\"x\":\"zap\"}\n{\"x\":9}\n",
                NO_SETTINGS,
            )
            .expect("filter rows");
        kv(
            "`x < 5` on [1, bad, 9]",
            &format!(
                "verdicts {}   (the bad row cannot swallow the tail)",
                verdict_string(&fr)
            ),
        );
    }
    blank();

    // Refused at compile, never guessed — and the two REASONS are two ARMS.
    kv(
        "compile `now() > x`",
        &classify(schema.compile_filter("now() > x", NO_PARAMS).map(|_| ())),
    );
    kv(
        "compile `x = {p:UInt8}`",
        &classify(
            schema
                .compile_filter("x = {p:UInt8}", NO_PARAMS)
                .map(|_| ()),
        ),
    );
    note("clock reads would be answered with THIS process's clock, not the");
    note("server's; an UNBOUND {p:Type} is the SERVER's own 456 since ABI");
    note("revision 4 (\"Substitution `p` is not set\") — bind it instead");
    kv(
        "compile `nosuch = 1`",
        &classify(schema.compile_filter("nosuch = 1", NO_PARAMS).map(|_| ())),
    );
    blank();

    // -- revision 4 sub-demo: query parameters ---------------------------
    // One 3-row "event" body, shared with the twin sub-demo below.
    group("query parameters (ABI revision 4) — values are STRINGS, never escaped");
    let event_body: &[u8] = b"{\"tenant\":\"acme\",\"role\":\"admin\",\"x\":1}\n{\"tenant\":\"evil\",\"role\":\"viewer\",\"x\":2}\n{\"tenant\":\"' OR 1=1 --\",\"role\":\"admin\",\"x\":3}\n";
    let ps = lib
        .compile("tenant String, role String, x UInt8")
        .compile()
        .expect("compile");
    match ps.compile_filter("tenant = {t:String}", &[("t", "acme")]) {
        Err(err) if err.is_unsupported() => {
            note("this artifact predates the params surface (relink it to ABI");
            note("revision 4, `just refresh <version>`); the sub-demo degrades");
            note("here rather than failing the tour — the surface is additive.");
        }
        Err(err) => panic!("params compile: {err}"),
        Ok(tf) => {
            let fr = tf
                .rows(Format::JsonEachRow, event_body, NO_SETTINGS)
                .expect("filter rows");
            kv(
                "`tenant = {t:String}`, t=acme",
                &format!("verdicts {}", verdict_string(&fr)),
            );
            drop(tf);
            note("compiled ONCE per (schema, expr, params) — the value is baked in;");
            note("a per-tenant cache MUST be a bounded LRU + a compile throttle");

            let hf = ps
                .compile_filter("tenant = {t:String}", &[("t", "' OR 1=1 --")])
                .expect("hostile compile");
            let hr = hf
                .rows(Format::JsonEachRow, event_body, NO_SETTINGS)
                .expect("filter rows");
            let hostile_ok = hr.outcome == FilterOutcome::Ok
                && hr.verdicts
                    == [
                        chtypes::Verdict::False,
                        chtypes::Verdict::False,
                        chtypes::Verdict::True,
                    ];
            kv(
                "t = `' OR 1=1 --` (hostile)",
                &format!(
                    "verdicts {}   hostile-value-inert: {hostile_ok}",
                    verdict_string(&hr)
                ),
            );
            drop(hf);
            note("the value became a typed LITERAL after SQL parsing — it matches");
            note("only the row holding exactly that string; no OR 1=1 semantics,");
            note("and NOTHING was escaped to get there (never hand-escape values)");
            note("traps: size the {brace type} for the value's domain ({p:UInt8}");
            note("given \"256\" BINDS 0 — the reader wraps); and never NAME a param");
            note("`limit`/`offset` — a real server's TCP channel refuses those");
        }
    }
    blank();

    // -- revision 4 sub-demo: the block twin -----------------------------
    group("the block twin (ABI revision 4) — parse ONCE, evaluate K filters");
    match ps.parse_block(Format::JsonEachRow, event_body, NO_SETTINGS) {
        Err(err) if err.is_unsupported() => {
            note("this artifact predates the block twin (relink it to ABI");
            note("revision 4, `just refresh <version>`); the sub-demo degrades");
            note("here rather than failing the tour — the surface is additive.");
        }
        Err(err) => panic!("parse_block: {err}"),
        Ok(block) => {
            let admin_f = ps
                .compile_filter("role = 'admin'", NO_PARAMS)
                .expect("compile_filter");
            let viewer_f = ps
                .compile_filter("role = 'viewer'", NO_PARAMS)
                .expect("compile_filter");
            let fr = admin_f.eval(&block).expect("eval");
            kv(
                "eval `role = 'admin'`",
                &format!("verdicts {}", verdict_string(&fr)),
            );
            let fr = viewer_f.eval(&block).expect("eval");
            kv(
                "eval `role = 'viewer'`",
                &format!("verdicts {}", verdict_string(&fr)),
            );
            note("ONE parse of the 3-row event fed BOTH filters: eval neither");
            note("consumes nor mutates the block, and eval(parse_block(body)) ≡");
            note("rows(body) for every verdict class — the live-SSE hot path is");
            note("K per-principal filters × 1 event, and the re-parse is shed");
        }
    }
    blank();

    note("THREADS: a filter call is ALSO a use of its schema handle — two");
    note("filters over one schema never run concurrently (the SDK enforces it)");
    note("LIFETIME: filters AND blocks free before their schema; in Rust the");
    note("borrow checker makes the wrong order a COMPILE error (both borrow)");
    note("ENFORCEMENT GATE: nothing may enforce read-side security on this");
    note("API until the WHERE-truth rig gates green (zero over-admit, zero");
    note("over-hide). Until that run of record exists this is a shadow/replay");
    note("surface: log disagreements, enforce with what enforced yesterday —");
    note("the twin is a call shape, not an enforcement opening.");
}

/// Render a FilterResult's verdicts as the document's compact t/f/e/d string.
fn verdict_string(fr: &FilterResult) -> String {
    if fr.outcome != FilterOutcome::Ok {
        return format!(
            "(call-level: outcome={:?} code={} {})",
            fr.outcome,
            fr.err_code,
            truncate(&fr.err_msg, 40)
        );
    }
    let chars: String = fr.verdicts.iter().map(|v| v.as_char()).collect();
    format!("\"{chars}\"")
}

// ------------------------------------------------------------- plumbing
//
// Everything below is printing helpers — no chtypes calls hide here.

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
    let batch = match schema.rows(format, body, NO_SETTINGS) {
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
        parts.push(truncate(&batch.err_msg, 56));
    }
    kv(&format!("  {label}"), &parts.join("  "));
    for t in &batch.transformed {
        raw(&format!(
            "      ~ {}: {} -> {} ({}{})",
            t.column,
            t.input,
            t.stored,
            t.reason,
            if t.lossy() { ", LOSSY" } else { "" }
        ));
    }
}

/// Print which sibling variant a Result carries, with its code.
fn describe_error(result: Result<(), Error>, what: &str) {
    match result {
        Ok(()) => kv(what, "(no error)"),
        Err(err) => match &err {
            Error::Schema { code, message, .. } => {
                kv(what, &format!("Error::Schema  (a REFUSAL), code={code}"));
                kv("  message", &truncate(message, 84));
                kv(
                    "  is_unsupported()?",
                    &format!("{}   <- siblings, not a hierarchy", err.is_unsupported()),
                );
            }
            other if other.is_unsupported() => {
                kv(what, "Error::Unsupported  (a DECLINE)");
                kv("  message", &truncate(&err.to_string(), 84));
                kv(
                    "  code()?",
                    &format!(
                        "{:?}   (the -2 sentinel — never a real ClickHouse code)",
                        err.code()
                    ),
                );
            }
            _ => {
                kv(what, "a loader/deployment error (not a verdict)");
                kv("  variant", &truncate(&format!("{err:?}"), 84));
            }
        },
    }
}

/// Name a Result's error KIND in one word.
fn classify(result: Result<(), Error>) -> String {
    match result {
        Ok(()) => "accepted".to_string(),
        Err(err) => match &err {
            Error::Schema { code, .. } => format!("REFUSED   (Error::Schema, code {code})"),
            other if other.is_unsupported() => "DECLINED  (Error::Unsupported)".to_string(),
            _ => err.to_string(),
        },
    }
}

fn verdict(batch: &BatchResult) -> String {
    if batch.err_code != 0 {
        return format!(
            "{:<9} code={}  {}",
            batch.outcome.as_str(),
            batch.err_code,
            truncate(&batch.err_msg, 44)
        );
    }
    if let Some(row) = batch.rows.first() {
        if let Some(v) = row.values.first() {
            return format!("{:<9} {} = {}", batch.outcome.as_str(), v.column, v.text);
        }
    }
    batch.outcome.as_str().to_string()
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
        .map(|i| u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).expect("hex"))
        .collect()
}

/// Pick the numerically highest minor line: "25.10" > "25.3", which a string
/// sort gets exactly backwards.
fn newest_line(lines: &[String]) -> String {
    lines
        .iter()
        .max_by_key(|line| {
            line.split('.')
                .map(|p| p.parse::<i64>().unwrap_or(-1))
                .collect::<Vec<_>>()
        })
        .cloned()
        .unwrap_or_default()
}
