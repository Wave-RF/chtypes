//! The binding parity contract, checked against this binding.
//!
//! `tests/parity/manifest.json` at the repository root is the ONE
//! machine-readable source of truth for what every binding must expose;
//! `docs/reference/bindings.md` is the prose that explains why. This file is Rust's half
//! of the enforcement: it checks every spelling the manifest assigns to the
//! `rust` column against this crate's own source, and fails by NAME when one is
//! missing — naming the bindings that DO have it, because "rust is missing
//! `verify_installed`, which go/python/ts all expose" is the sentence that gets
//! the gap fixed and "parity check failed" is not.
//!
//! Three rules this file holds:
//!
//! * **It cannot pass by doing nothing.** A manifest that parsed to zero
//!   capabilities, or fewer than its own declared floors, FAILS — as does a run
//!   in which nothing resolved, or one whose value table compared nothing.
//! * **It needs no artifact.** Parity is a claim about the API surface, not
//!   about dlopening a library, so all of it runs in the artifact-free CI job.
//!   Nothing here opens a `Registry`.
//! * **A deliberate gap is still written down.** A binding that should not have
//!   a capability declares `{"absent": "<why>"}`; an absence with no reason
//!   fails the manifest's own integrity check. Rust's are the borrow checker's:
//!   `Schema`, `Filter` and `Block` implement `Drop`, so the free-before-schema
//!   ordering the other three enforce at runtime is a compile error here, and an
//!   inherent `close()` would be a second way to say it.
//!
//! **Presence is read off the source, and values off the real constants.** The
//! two halves cover each other: the value table below is a set of REAL
//! references, so renaming a constant does not fail this test — it fails the
//! BUILD, which is louder and earlier; and the source scan is what proves the
//! manifest's *spelling* still matches, which a compiling crate cannot.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value as Json;

const LANG: &str = "rust";
const OTHERS: [&str; 3] = ["go", "python", "ts"];

fn crate_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}
fn repo_root() -> PathBuf {
    crate_root()
        .parent()
        .expect("rust/ has a parent")
        .to_path_buf()
}
fn manifest_path() -> PathBuf {
    repo_root()
        .join("tests")
        .join("parity")
        .join("manifest.json")
}

fn manifest() -> Json {
    let path = manifest_path();
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "the parity manifest is missing at {}: {e}. It is the contract this suite exists to \
             enforce; an absent manifest is a failure, never a skip.",
            path.display()
        )
    });
    serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("{} is not valid JSON: {e}", path.display()))
}

// ---------------------------------------------------------------- the manifest

/// One capability's column for one language: a bare spelling, or an object that
/// may declare an absence (with its reason) or opt out of the value comparison.
struct Column {
    symbol: Option<String>,
    absent: Option<String>,
    value_check: bool,
}

fn column(cap: &Json, lang: &str) -> Column {
    match cap.get(lang) {
        Some(Json::String(s)) => Column {
            symbol: Some(s.clone()),
            absent: None,
            value_check: true,
        },
        Some(Json::Object(o)) => Column {
            symbol: o.get("symbol").and_then(Json::as_str).map(str::to_owned),
            absent: o.get("absent").and_then(Json::as_str).map(str::to_owned),
            value_check: o.get("value_check").and_then(Json::as_bool).unwrap_or(true),
        },
        _ => Column {
            symbol: None,
            absent: None,
            value_check: true,
        },
    }
}

fn spelling(cap: &Json, lang: &str) -> Option<String> {
    column(cap, lang).symbol
}

/// The other bindings that DO carry this capability, for the failure text.
fn also_in(cap: &Json) -> String {
    let have: Vec<&str> = OTHERS
        .iter()
        .copied()
        .filter(|l| spelling(cap, l).is_some())
        .collect();
    if have.is_empty() {
        "no other binding".into()
    } else {
        have.join("/")
    }
}

fn capabilities(doc: &Json) -> &Vec<Json> {
    doc["capabilities"]
        .as_array()
        .expect("capabilities is an array")
}

// --------------------------------------------------- this crate's own surface

/// Every public item this crate declares, in the manifest's spelling grammar:
/// `name` for a crate-root item, `Type::method` for an inherent method,
/// `module::NAME` for an item inside a `pub mod`, `Enum::Variant` for a variant,
/// and `fetch::name` for an item of the `fetch` module's own files.
///
/// Read off the source rather than from reflection, because Rust has none — and
/// the source is what a rename actually moves. The value table below is the
/// other half: it holds real references, so a rename breaks the build first.
fn source_surface() -> (BTreeSet<String>, BTreeSet<String>) {
    let src = crate_root().join("src");
    let mut all = BTreeSet::new();
    let mut top = BTreeSet::new();
    let mut files = Vec::new();
    collect_rs(&src, &mut files);
    assert!(
        files.len() > 5,
        "only {} source files found under {} — the surface scan cannot run and must not be \
         reported as passing",
        files.len(),
        src.display()
    );

    for file in &files {
        // An item of src/fetch/*.rs is reached as `fetch::name`; everything else
        // is re-exported at the crate root by lib.rs.
        let in_fetch = file.parent().is_some_and(|p| p.ends_with("fetch"));
        let text = fs::read_to_string(file).expect("readable source file");
        // `impl <Type>`, `pub mod <name>` and `pub enum <Name>` each open a
        // namespace; track the innermost by brace depth. A trait impl
        // (`impl … for T`) opens none: its items are the trait's, not the type's.
        let mut scopes: Vec<(usize, Scope)> = Vec::new();
        let mut depth = 0usize;
        for line in text.lines() {
            let trimmed = line.trim();
            let opens = line.matches('{').count();
            let closes = line.matches('}').count();
            let scope = scopes.last().map(|(_, s)| s.clone()).unwrap_or(Scope::None);

            if let Some(name) = declared_item(trimmed) {
                let path = match scope.name() {
                    Some(prefix) => format!("{prefix}::{name}"),
                    None if in_fetch => format!("fetch::{name}"),
                    None => name.clone(),
                };
                all.insert(path.clone());
                if scope.name().is_none() {
                    top.insert(path);
                }
                // A crate-root item of src/fetch/ is also reachable unprefixed
                // when lib.rs re-exports it.
                if scope.name().is_none() && in_fetch {
                    all.insert(name);
                }
            }
            if let Scope::Enum(owner) = &scope {
                if let Some(variant) = enum_variant(trimmed) {
                    all.insert(format!("{owner}::{variant}"));
                }
            }
            if opens > closes {
                scopes.push((depth, opened_scope(trimmed)));
            }
            depth = (depth + opens).saturating_sub(closes);
            while let Some(&(at, _)) = scopes.last() {
                if depth <= at {
                    scopes.pop();
                } else {
                    break;
                }
            }
        }
    }
    (top, all)
}

#[derive(Clone, PartialEq)]
enum Scope {
    None,
    /// An inherent `impl Type` or a `pub mod name`: its items are `Type::item`.
    Named(String),
    /// A `pub enum Name`: its lines are variants, not items.
    Enum(String),
}

impl Scope {
    fn name(&self) -> Option<&str> {
        match self {
            Scope::None => None,
            Scope::Named(n) | Scope::Enum(n) => Some(n),
        }
    }
}

fn collect_rs(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_rs(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// The name a `pub fn` / `pub const` / `pub struct` / `pub enum` / `pub type`
/// line declares.
fn declared_item(line: &str) -> Option<String> {
    let rest = line.strip_prefix("pub ")?;
    // `pub(crate)` / `pub(super)` / `pub(in …)` are not public surface.
    if rest.starts_with('(') {
        return None;
    }
    for kw in [
        "fn ", "const ", "struct ", "enum ", "type ", "trait ", "static ", "mod ",
    ] {
        if let Some(tail) = rest.strip_prefix(kw) {
            let name = ident(tail);
            if !name.is_empty() {
                return Some(name);
            }
        }
    }
    None
}

/// A variant line inside a `pub enum` scope: `Accepted,`, `JsonEachRow = 0,`,
/// `Other(String),` or `Schema { … }`. The explicit discriminant matters —
/// `Format`'s variants all carry the frozen `chs_format` number.
fn enum_variant(line: &str) -> Option<String> {
    let line = line.trim_start_matches("#[default]").trim();
    if line.starts_with('#') || line.starts_with("//") {
        return None;
    }
    let name = ident(line);
    if name.is_empty() || !name.starts_with(|c: char| c.is_uppercase()) {
        return None;
    }
    let after = line[name.len()..].trim_start();
    let opens_variant = after.is_empty()
        || after.starts_with(',')
        || after.starts_with('{')
        || after.starts_with('(')
        || after.starts_with('=');
    opens_variant.then_some(name)
}

/// The namespace a braced line opens. Only an inherent `impl Type` — never a
/// trait impl, whose items belong to the trait — and a `pub mod` / `pub enum`
/// name one.
fn opened_scope(line: &str) -> Scope {
    if let Some(tail) = line.strip_prefix("impl") {
        // `impl<'a, T: Bound> Type<'a> { … }` and `impl Type { … }` both land here.
        let tail = tail.trim_start();
        let tail = skip_generics(tail).trim_start();
        if tail.contains(" for ") {
            return Scope::None;
        }
        // The type path's last segment, without its own generic arguments.
        let path: String = tail
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_' || *c == ':')
            .collect();
        let name = path.rsplit("::").next().unwrap_or("").to_string();
        return if name.is_empty() {
            Scope::None
        } else {
            Scope::Named(name)
        };
    }
    for kw in ["pub enum ", "enum "] {
        if let Some(tail) = line.strip_prefix(kw) {
            let name = ident(tail);
            return if name.is_empty() {
                Scope::None
            } else {
                Scope::Enum(name)
            };
        }
    }
    if let Some(tail) = line.strip_prefix("pub mod ") {
        let name = ident(tail);
        return if name.is_empty() {
            Scope::None
        } else {
            Scope::Named(name)
        };
    }
    Scope::None
}

/// `<'a, T: Bound>` at the head of a string, skipped by angle-bracket depth.
fn skip_generics(s: &str) -> &str {
    if !s.starts_with('<') {
        return s;
    }
    let mut depth = 0usize;
    for (idx, ch) in s.char_indices() {
        match ch {
            '<' => depth += 1,
            '>' => {
                depth -= 1;
                if depth == 0 {
                    return &s[idx + 1..];
                }
            }
            _ => {}
        }
    }
    s
}

fn ident(s: &str) -> String {
    s.chars()
        .take_while(|c| c.is_alphanumeric() || *c == '_')
        .collect()
}

// ------------------------------------------------------------- the value table
//
// Every entry is a REAL reference to the item it names, so a rename does not
// fail this test — it fails the BUILD. The keys are the manifest's own `rust`
// spellings, and the value test asserts the table covers every valued capability
// assigned to Rust: a contract that grows a value Rust never compares would
// otherwise pass in silence.
#[derive(Debug, PartialEq)]
enum Val {
    Int(i64),
    Text(String),
}
fn i(v: i64) -> Val {
    Val::Int(v)
}
fn s(v: &str) -> Val {
    Val::Text(v.to_string())
}

fn rust_values() -> BTreeMap<&'static str, Val> {
    use chtypes::{CompileMode, DocFlags, Format, Outcome, reason};
    let mut m = BTreeMap::new();

    // chs_format — the numbers are frozen (the core repository's C ABI specification §Types and schemas).
    m.insert("Format::JsonEachRow", i(Format::JsonEachRow as i64));
    m.insert("Format::Csv", i(Format::Csv as i64));
    m.insert("Format::Tsv", i(Format::Tsv as i64));
    m.insert("Format::Values", i(Format::Values as i64));
    m.insert(
        "Format::JsonCompactEachRow",
        i(Format::JsonCompactEachRow as i64),
    );
    m.insert("Format::RowBinary", i(Format::RowBinary as i64));
    m.insert(
        "Format::RowBinaryWithDefaults",
        i(Format::RowBinaryWithDefaults as i64),
    );
    m.insert(
        "Format::RowBinaryWithNamesAndTypesAndDefaults",
        i(Format::RowBinaryWithNamesAndTypesAndDefaults as i64),
    );
    m.insert("Format::Native", i(Format::Native as i64));
    m.insert("Format::Buffers", i(Format::Buffers as i64));

    // The row/batch verdict vocabulary, as the result document spells it.
    m.insert("Outcome::Accepted", s(Outcome::Accepted.as_str()));
    m.insert("Outcome::Rejected", s(Outcome::Rejected.as_str()));
    m.insert(
        "Outcome::AcceptedPoisoned",
        s(Outcome::AcceptedPoisoned.as_str()),
    );
    m.insert("Outcome::Unsupported", s(Outcome::Unsupported.as_str()));
    m.insert("Outcome::Skipped", s(Outcome::Skipped.as_str()));

    // The call-level filter verdict.
    m.insert("FilterOutcome::Ok", s(chtypes::FilterOutcome::Ok.as_str()));
    m.insert(
        "FilterOutcome::Rejected",
        s(chtypes::FilterOutcome::Rejected.as_str()),
    );
    m.insert(
        "FilterOutcome::Unsupported",
        s(chtypes::FilterOutcome::Unsupported.as_str()),
    );

    // The transform reason vocabulary — stable strings; the harness groups on them.
    m.insert("reason::OVERFLOW_WRAP", s(reason::OVERFLOW_WRAP));
    m.insert("reason::NULL_TO_DEFAULT", s(reason::NULL_TO_DEFAULT));
    m.insert("reason::NULL_LOSS", s(reason::NULL_LOSS));
    m.insert("reason::DECIMAL_TRUNCATE", s(reason::DECIMAL_TRUNCATE));
    m.insert("reason::DATE_CLAMP", s(reason::DATE_CLAMP));
    m.insert("reason::DATETIME_WRAP", s(reason::DATETIME_WRAP));
    m.insert("reason::DATE_SHIFT", s(reason::DATE_SHIFT));
    m.insert("reason::UUID_MANGLE", s(reason::UUID_MANGLE));
    m.insert("reason::IP_MANGLE", s(reason::IP_MANGLE));
    m.insert("reason::FLOAT_PRECISION", s(reason::FLOAT_PRECISION));
    m.insert("reason::LOSSY_NUMERIC", s(reason::LOSSY_NUMERIC));
    m.insert("reason::FIXEDSTRING_PAD", s(reason::FIXEDSTRING_PAD));
    m.insert("reason::EMPTIED", s(reason::EMPTIED));
    m.insert("reason::ELEMENT_CHANGED", s(reason::ELEMENT_CHANGED));
    m.insert("reason::ENUM_COERCE", s(reason::ENUM_COERCE));
    m.insert("reason::VALUE_CHANGED", s(reason::VALUE_CHANGED));
    m.insert("reason::POISONED", s(reason::POISONED));
    m.insert(
        "reason::DUPLICATE_KEY_DROPPED",
        s(reason::DUPLICATE_KEY_DROPPED),
    );
    m.insert("reason::REFORMAT", s(reason::REFORMAT));
    m.insert("reason::DEFAULT_FILLED", s(reason::DEFAULT_FILLED));
    m.insert("reason::ZERO_FILLED", s(reason::ZERO_FILLED));
    m.insert(
        "reason::DEFAULT_MATERIALIZED",
        s(reason::DEFAULT_MATERIALIZED),
    );
    m.insert("reason::TTL_EXPIRED", s(reason::TTL_EXPIRED));
    m.insert("reason::TTL_COLUMN_EXPIRED", s(reason::TTL_COLUMN_EXPIRED));

    // ABI identity and the document/compile channels.
    m.insert("ABI_REVISION", i(chtypes::ABI_REVISION as i64));
    m.insert("CODE_UNSUPPORTED", i(chtypes::CODE_UNSUPPORTED as i64));
    m.insert(
        "CompileMode::Declared",
        i(CompileMode::Declared.code() as i64),
    );
    m.insert("DocFlags::VALUES", i(DocFlags::VALUES.bits() as i64));
    m.insert(
        "DocFlags::TRANSFORMS",
        i(DocFlags::TRANSFORMS.bits() as i64),
    );
    m.insert("DocFlags::DEFAULTS", i(DocFlags::DEFAULTS.bits() as i64));
    m.insert("DocFlags::ALL", i(DocFlags::ALL.bits() as i64));

    // docs/fetch.md §6 — the machine-readable codes the four CLIs print.
    m.insert("CODE_ARTIFACT_MISSING", s(chtypes::CODE_ARTIFACT_MISSING));
    m.insert(
        "CODE_ARTIFACT_UNTRUSTED",
        s(chtypes::CODE_ARTIFACT_UNTRUSTED),
    );
    m.insert("CODE_ARTIFACT_CORRUPT", s(chtypes::CODE_ARTIFACT_CORRUPT));
    m.insert("CODE_ARTIFACT_PINNED", s(chtypes::CODE_ARTIFACT_PINNED));
    m.insert(
        "CODE_ARTIFACT_UNPUBLISHED",
        s(chtypes::CODE_ARTIFACT_UNPUBLISHED),
    );
    m.insert(
        "CODE_SOURCE_UNREACHABLE",
        s(chtypes::CODE_SOURCE_UNREACHABLE),
    );

    // The canonical discovery queries, carried verbatim (docs/reference/bindings.md §Discovery).
    m.insert("QUERY_SERVER_VERSION", s(chtypes::QUERY_SERVER_VERSION));
    m.insert("QUERY_CHANGED_SETTINGS", s(chtypes::QUERY_CHANGED_SETTINGS));
    m.insert("QUERY_TABLE_COLUMNS", s(chtypes::QUERY_TABLE_COLUMNS));

    // The artifact source and its trust anchor.
    #[cfg(feature = "fetch")]
    {
        m.insert("fetch::RELEASE_KEY_ID", s(chtypes::fetch::RELEASE_KEY_ID));
        m.insert(
            "fetch::RELEASE_PUBLIC_KEY_HEX",
            s(chtypes::fetch::RELEASE_PUBLIC_KEY_HEX),
        );
        m.insert(
            "fetch::DEFAULT_ARTIFACTS_URL",
            s(chtypes::fetch::DEFAULT_ARTIFACTS_URL),
        );
        m.insert("fetch::DEFAULT_TAG", s(chtypes::fetch::DEFAULT_TAG));
        m.insert(
            "fetch::DEFAULT_LOCK_FILE",
            s(chtypes::fetch::DEFAULT_LOCK_FILE),
        );
    }
    m.insert("REGISTRY_ENV", s(chtypes::REGISTRY_ENV));
    m.insert("AUTOFETCH_ENV", s(chtypes::AUTOFETCH_ENV));
    m.insert("FETCH_COMMAND", s(chtypes::FETCH_COMMAND));
    m
}

// ---------------------------------------------------------------------- tests

#[test]
fn parity_manifest_meets_its_own_floors() {
    let doc = manifest();
    assert_eq!(doc["schema"], 1, "unknown parity manifest schema");
    let caps = capabilities(&doc);
    let floors = &doc["floors"];
    let floor = floors["capabilities"].as_u64().unwrap() as usize;
    assert!(
        caps.len() >= floor,
        "the parity manifest declares {} capabilities, below its own floor of {floor}. A \
         shrinking contract is how this check passes by doing nothing.",
        caps.len()
    );
    let valued = caps.iter().filter(|c| c.get("value").is_some()).count();
    let valued_floor = floors["valued"].as_u64().unwrap() as usize;
    assert!(
        valued >= valued_floor,
        "only {valued} capabilities carry a shared value, below the floor of {valued_floor}. \
         Values are what prove the four bindings answer the same bytes."
    );
    let group_floor = floors["per_group"].as_u64().unwrap() as usize;
    for group in doc["groups"].as_object().unwrap().keys() {
        let n = caps.iter().filter(|c| c["group"] == *group).count();
        assert!(
            n >= group_floor,
            "group {group:?} has {n} capabilities, below the floor of {group_floor}"
        );
    }
}

#[test]
fn parity_manifest_is_fully_declared() {
    let doc = manifest();
    let langs: Vec<&str> = doc["languages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l.as_str().unwrap())
        .collect();
    let groups = doc["groups"].as_object().unwrap();
    let mut seen = BTreeSet::new();
    let mut problems = Vec::new();
    for cap in capabilities(&doc) {
        let id = cap["id"].as_str().unwrap_or("<no id>");
        if !seen.insert(id.to_string()) {
            problems.push(format!("{id}: duplicate id"));
        }
        if cap["what"].as_str().unwrap_or("").trim().is_empty() {
            problems.push(format!(
                "{id}: no `what` — a capability with no description is a name, not a contract"
            ));
        }
        if !groups.contains_key(cap["group"].as_str().unwrap_or("")) {
            problems.push(format!("{id}: unknown group {:?}", cap["group"]));
        }
        let mut absent = 0;
        for lang in &langs {
            let col = column(cap, lang);
            match (&col.symbol, &col.absent) {
                (Some(sym), _) if !sym.trim().is_empty() => {}
                (_, Some(why)) if !why.trim().is_empty() => absent += 1,
                (_, Some(_)) => problems.push(format!(
                    "{id}: {lang} is declared absent with no reason. A gap that nobody had to \
                     justify in writing is how parity rots."
                )),
                _ => problems.push(format!(
                    "{id}: {lang} column has neither a symbol nor a declared absence"
                )),
            }
        }
        if absent == langs.len() {
            problems.push(format!(
                "{id}: absent in every binding — that is a note, not a contract"
            ));
        }
    }
    assert!(
        problems.is_empty(),
        "the parity manifest is not internally consistent:\n  {}",
        problems.join("\n  ")
    );
}

#[test]
fn the_go_copy_of_the_manifest_is_byte_identical() {
    // scripts/check-standalone.sh runs the Go suite from a bare copy of go/ with
    // no repository root beside it, so Go embeds the manifest. This asserts the
    // cache has not drifted from the one source of truth.
    let canonical = fs::read(manifest_path()).expect("the parity manifest");
    let copy_path = repo_root()
        .join("go")
        .join("chtypes")
        .join("testdata")
        .join("parity.json");
    let copy = fs::read(&copy_path).unwrap_or_else(|e| {
        panic!(
            "the Go copy of the parity manifest is missing at {}: {e}",
            copy_path.display()
        )
    });
    assert!(
        copy == canonical,
        "{} has drifted from {}. The manifest is one file and this is its cache. Re-sync it:\n\n    \
         cp tests/parity/manifest.json go/chtypes/testdata/parity.json\n",
        copy_path.display(),
        manifest_path().display()
    );
}

#[test]
fn rust_exposes_every_capability_the_contract_assigns_it() {
    let doc = manifest();
    let (_, all) = source_surface();
    let mut missing = Vec::new();
    let mut resolved = 0usize;
    for cap in capabilities(&doc) {
        let Some(sym) = spelling(cap, LANG) else {
            continue;
        };
        if cap["kind"] == "cli" {
            continue;
        }
        if all.contains(&sym) {
            resolved += 1;
        } else {
            missing.push(format!(
                "{}: rust is missing `{sym}`, which {} expose — {}",
                cap["id"].as_str().unwrap_or("?"),
                also_in(cap),
                cap["what"].as_str().unwrap_or("")
            ));
        }
    }
    assert!(
        resolved > 0,
        "no rust spelling resolved at all — the check asserted nothing"
    );
    assert!(
        missing.is_empty(),
        "rust does not carry {} capability/capabilities the parity contract assigns it:\n  {}",
        missing.len(),
        missing.join("\n  ")
    );
    eprintln!("{resolved} rust spellings resolved against the crate's own source");
}

#[test]
fn rust_answers_the_same_values_as_the_other_bindings() {
    let doc = manifest();
    let table = rust_values();
    let mut wrong = Vec::new();
    let mut uncovered = Vec::new();
    let mut checked = 0usize;
    for cap in capabilities(&doc) {
        let Some(want_raw) = cap.get("value") else {
            continue;
        };
        if cap["kind"] == "cli" {
            continue;
        }
        let col = column(cap, LANG);
        let Some(sym) = col.symbol else { continue };
        if !col.value_check {
            continue;
        }
        let id = cap["id"].as_str().unwrap_or("?");
        let Some(got) = table.get(sym.as_str()) else {
            uncovered.push(format!(
                "{id}: the contract gives `{sym}` a shared value and the rust value table does not carry it"
            ));
            continue;
        };
        checked += 1;
        let want = match want_raw {
            Json::Number(n) => Val::Int(n.as_i64().expect("an integer value")),
            Json::String(t) => Val::Text(t.clone()),
            other => panic!("{id}: unsupported value kind in the manifest: {other}"),
        };
        if *got != want {
            wrong.push(format!(
                "{id}: rust `{sym}` is {got:?}, the contract says {want:?}"
            ));
        }
    }
    // A value table that stopped covering the contract is the quiet failure this
    // guards: the check would still "pass", having compared less and less.
    assert!(
        uncovered.is_empty(),
        "the rust value table has fallen behind the contract:\n  {}",
        uncovered.join("\n  ")
    );
    let floor = doc["floors"]["valued"].as_u64().unwrap() as usize;
    assert!(
        checked >= floor,
        "only {checked} shared values were actually compared, below the floor of {floor} — a value \
         check that checks nothing is not a check"
    );
    assert!(
        wrong.is_empty(),
        "rust answers differently from the contract:\n  {}",
        wrong.join("\n  ")
    );
}

#[test]
fn the_rust_cli_offers_every_contract_subcommand() {
    let doc = manifest();
    let main = crate_root().join("src").join("main.rs");
    let src = fs::read_to_string(&main).unwrap_or_else(|e| {
        panic!(
            "cannot read the CLI at {}: {e} — the subcommand check cannot run and must not be \
             reported as passing",
            main.display()
        )
    });
    let mut commands = 0;
    let mut missing = Vec::new();
    for cap in capabilities(&doc) {
        if cap["kind"] != "cli" || spelling(cap, LANG).is_none() {
            continue;
        }
        commands += 1;
        let want = cap["value"].as_str().unwrap_or("");
        if !src.contains(&format!("\"{want}\"")) {
            missing.push(format!(
                "{}: the rust CLI has no `{want}` subcommand, which {} offer",
                cap["id"].as_str().unwrap_or("?"),
                also_in(cap)
            ));
        }
    }
    assert!(commands > 0, "the contract names no CLI subcommands");
    assert!(missing.is_empty(), "{}", missing.join("\n  "));
}

#[test]
fn the_rust_unlisted_allowlist_has_not_rotted() {
    // An allowlist entry for an item this crate no longer exports is an excuse
    // with nothing behind it, and it hides the next real gap.
    let doc = manifest();
    let (_, all) = source_surface();
    let mut gone = Vec::new();
    for name in doc["unlisted"][LANG]
        .as_object()
        .expect("unlisted.rust")
        .keys()
    {
        if !all.contains(name) {
            gone.push(name.clone());
        }
    }
    assert!(
        gone.is_empty(),
        "`unlisted.rust` in the parity manifest excuses items this crate no longer exports:\n  {}",
        gone.join("\n  ")
    );
}
