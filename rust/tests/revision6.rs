//! Revision 6 against a loaded artifact: the error-code table and the
//! partition key, end to end.
//!
//! The no-artifact halves live beside the code they pin — `src/error_codes.rs`
//! (parsing, lookups, the success-only cache), `src/result.rs` (the two
//! partition fields) and `src/ffi.rs` (the setter's sign rule). This file
//! needs a revision-6 artifact and SKIPS LOUDLY, on the real stderr, by name,
//! without one: the registry default for this crate is the revision-6
//! directory, which is empty until the artifact producer publishes revision-6
//! artifacts.
//!
//! One case runs before any such artifact exists:
//! `error_codes_through_the_abi_fixture` drives `chs_error_codes` through the
//! REAL `dlopen` path on the ABI fixture's at-revision stub, built from this
//! header, when `$CHTYPES_ABI_FIXTURES` names one. It lives here, not in
//! `tests/abi_revision.rs`, so nothing it prints can land inside that binary's
//! census lines.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{Error, Format, Library, NO_SETTINGS, Outcome, Registry};

/// Announce on the *real* stderr. `eprintln!` is captured by libtest for a
/// passing test, so a skip printed with it is invisible.
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

static REV6: OnceLock<Vec<(String, Arc<Library>)>> = OnceLock::new();

/// Every line the registry holds that loads at ABI revision 6. Empty (and
/// announced) when there is none.
fn rev6() -> &'static [(String, Arc<Library>)] {
    REV6.get_or_init(|| {
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
        let mut out = Vec::new();
        for line in reg.versions() {
            match reg.for_version(&line) {
                Ok(lib) if lib.abi_revision() >= 6 => out.push((line, lib)),
                Ok(lib) => announce(&format!(
                    "\nNOTE: line {line} reports ABI revision {} and is not measured here\n",
                    lib.abi_revision()
                )),
                Err(e) => announce(&format!(
                    "\nNOTE: line {line} did not open and is not measured here: {e}\n"
                )),
            }
        }
        if out.is_empty() {
            announce(&format!(
                "\nSKIP: registry {} holds no ABI revision-6 artifact. Every test in this file \
                 is skipped, each by name below.\n",
                dir.display()
            ));
        }
        out
    })
}

fn skip(test: &str) {
    announce(&format!(
        "SKIP revision6::{test}: no ABI revision-6 artifact\n"
    ));
}

/// The at-revision stub answers by return type, not with a table, so this
/// pins the wiring rather than an answer: the symbol resolves (never a
/// decline, never a refusal), and whatever it answers takes the rule — a
/// `NULL` is [`Error::NoDocument`] and is not kept, a document is kept.
#[test]
fn error_codes_through_the_abi_fixture() {
    let Some(root) = std::env::var_os("CHTYPES_ABI_FIXTURES").filter(|v| !v.is_empty()) else {
        announce(
            "SKIP revision6::error_codes_through_the_abi_fixture: no ABI revision fixture: \
             $CHTYPES_ABI_FIXTURES is unset\n",
        );
        return;
    };
    let root = PathBuf::from(root);
    let fixture = std::fs::read_to_string(root.join("fixture.json")).expect("fixture.json");
    let doc: serde_json::Value = serde_json::from_str(&fixture).expect("fixture.json parses");
    let minor = doc["clickhouse_minor"].as_str().expect("clickhouse_minor");
    let reg = Registry::new(root.join("at-revision")).expect("the control registry opens");
    let lib = reg.for_version(minor).expect("the control artifact loads");
    match lib.error_codes() {
        Ok(first) => {
            let again = lib.error_codes().expect("the kept table");
            assert!(std::ptr::eq(first, again), "a built table was not kept");
        }
        Err(e @ Error::NoDocument { .. }) => {
            assert!(!e.is_unsupported(), "a NULL answer is not a decline");
            assert_eq!(e.code(), None, "and not a refusal");
            // Not kept: the next call asks the library again, and gets NULL again.
            assert!(
                matches!(lib.error_codes(), Err(Error::NoDocument { .. })),
                "the second call did not ask the library again"
            );
        }
        Err(other) => panic!(
            "error_codes() on a stub built from this header = {other:?}; the symbol is declared \
             there, so neither a decline nor a refusal is a possible answer"
        ),
    }
}

#[test]
fn error_codes_from_the_loaded_library() {
    let libs = rev6();
    if libs.is_empty() {
        return skip("error_codes_from_the_loaded_library");
    }
    for (line, lib) in libs {
        let table = lib.error_codes().expect("error_codes");
        assert!(!table.is_empty(), "{line}: the table is empty");
        let codes: Vec<i32> = table.iter().map(|e| e.code).collect();
        assert!(
            codes.windows(2).all(|w| w[0] < w[1]),
            "{line}: iter() is not strictly ascending"
        );
        for e in table {
            assert_eq!(table.name(e.code), Some(e.name.as_str()));
            assert_eq!(table.code(&e.name), Some(e.code));
        }
        assert_eq!(table.name(252), Some("TOO_MANY_PARTS"), "{line}");
        assert_eq!(table.name(-1), None);
        assert_eq!(table.name(-2), None);
        let again = lib.error_codes().expect("error_codes, again");
        assert!(std::ptr::eq(table, again), "{line}: the table was not kept");
    }
}

/// The header's own example of one number naming different errors on
/// different lines, per the producer's measurement of every served line.
/// `None` is ABSENT (on 25.10), never a synthesized name. A line not named here
/// (26.10 and later) has no expectation and is not checked.
fn error_code_903(line: &str) -> Option<Option<&'static str>> {
    match line {
        "25.3" | "25.8" => Some(Some("LICENSE_EXPIRED")),
        "25.10" => Some(None),
        "26.2" | "26.3" | "26.4" | "26.5" | "26.6" | "26.7" | "26.8" | "26.9" => {
            Some(Some("DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN"))
        }
        _ => None,
    }
}

#[test]
fn code_903_differs_across_lines() {
    let libs = rev6();
    if libs.is_empty() {
        return skip("code_903_differs_across_lines");
    }
    let mut checked = 0;
    for (line, lib) in libs {
        let Some(want) = error_code_903(line) else {
            continue;
        };
        assert_eq!(
            lib.error_codes().expect("error_codes").name(903),
            want,
            "{line}"
        );
        checked += 1;
    }
    if checked == 0 {
        announce(
            "NOTE revision6::code_903_differs_across_lines: no loaded line has a documented expectation\n",
        );
    }
}

const BODY: &[u8] = b"{\"ts\":\"2026-01-15 10:00:00\",\"tenant\":\"a\"}\n\
{\"ts\":\"2026-01-20 10:00:00\",\"tenant\":\"b\"}\n\
{\"ts\":\"2026-02-01 10:00:00\",\"tenant\":\"a\"}\n";

#[test]
fn partition_key_end_to_end() {
    let libs = rev6();
    if libs.is_empty() {
        return skip("partition_key_end_to_end");
    }
    for (line, lib) in libs {
        let mut schema = lib
            .compile("ts DateTime, tenant String")
            .compile()
            .expect("compile");

        let plain = schema
            .rows(Format::JsonEachRow, BODY, NO_SETTINGS)
            .expect("rows");
        assert_eq!(plain.partition_count, None, "{line}");
        assert_eq!(plain.rows[0].partition_id, None, "{line}");

        schema
            .set_partition_by("toYYYYMM(ts)")
            .expect("set_partition_by");
        let keyed = schema
            .rows(Format::JsonEachRow, BODY, NO_SETTINGS)
            .expect("rows");
        assert_eq!(keyed.outcome, Outcome::Accepted, "{line}");
        assert_eq!(keyed.partition_count, Some(2), "{line}");
        let ids: Vec<Option<&str>> = keyed
            .rows
            .iter()
            .map(|r| r.partition_id.as_deref())
            .collect();
        assert!(
            ids[0].is_some() && ids[0] == ids[1] && ids[0] != ids[2],
            "{line}: {ids:?}"
        );

        let over = schema
            .rows(
                Format::JsonEachRow,
                BODY,
                &[("max_partitions_per_insert_block", "1")],
            )
            .expect("rows");
        assert_eq!(over.outcome, Outcome::Rejected, "{line}");
        assert_eq!(over.err_code, 252, "{line}");
        assert_eq!(over.rows.len(), 3, "{line}");

        schema.set_partition_by("").expect("clear");
        let cleared = schema
            .rows(Format::JsonEachRow, BODY, NO_SETTINGS)
            .expect("rows");
        assert_eq!(cleared.partition_count, None, "{line}");
        assert_eq!(cleared.rows[0].partition_id, None, "{line}");

        // A non-deterministic key is the server's own rejection — 36
        // BAD_ARGUMENTS on every served line — not a decline.
        match schema.set_partition_by("rand()") {
            Err(Error::Schema { code: 36, .. }) => {}
            other => panic!("{line}: set_partition_by(rand()) = {other:?}, want Error::Schema 36"),
        }
    }
}
