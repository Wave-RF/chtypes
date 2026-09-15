//! The ABI-revision handshake, run against a wrong-revision fixture (#36).
//!
//! `docs/reference/artifact.md` step 5 requires an artifact whose
//! `chs_abi_revision()` answers a value that is neither `0` nor this crate's
//! own [`chtypes::ABI_REVISION`] to be REJECTED, naming both numbers. This
//! suite runs that handshake against a generated fixture pair rather than
//! assuming it: `wrong-revision/` answers one revision higher than the
//! header, `at-revision/` answers the header's own revision.
//!
//! The fixture root comes ONLY from `$CHTYPES_ABI_FIXTURES` — no fallback
//! build. Without it, each test announces a loud skip on the real stderr
//! (`std::io::stderr().write_all`, not `println!`/`eprintln!`) and returns:
//! `cargo test` captures the macros' output on a passing test, and a census
//! reading plain `cargo test` (no `--nocapture`) would see nothing from them.
//! Expected numbers come only from `fixture.json`, never from
//! `chtypes::ABI_REVISION` — a case that read the constant under test could
//! not catch it being wrong.

use std::path::PathBuf;

use chtypes::Registry;

/// A loud announcement on the REAL stderr, uncaptured by `cargo test` even
/// without `--nocapture` (unlike `println!`/`eprintln!`, which libtest
/// swallows on a passing test) — the same mechanism `rust/tests/fetch.rs`
/// uses for its own skip messages.
fn announce(message: &str) {
    use std::io::Write;
    let _ = std::io::stderr().write_all(message.as_bytes());
    let _ = std::io::stderr().write_all(b"\n");
    let _ = std::io::stderr().flush();
}

/// One parsed `fixture.json`, or `None` when `$CHTYPES_ABI_FIXTURES` is unset
/// — the one condition under which a test here may skip.
struct Fixture {
    root: PathBuf,
    abi_revision: i64,
    mismatch_revision: i64,
    clickhouse_version: String,
}

fn fixture() -> Option<Fixture> {
    let Some(root) = std::env::var_os("CHTYPES_ABI_FIXTURES").map(PathBuf::from) else {
        announce("SKIPPED: no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset");
        return None;
    };

    let path = root.join("fixture.json");
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "$CHTYPES_ABI_FIXTURES={}: no fixture.json: {e}",
            root.display()
        )
    });
    let doc: serde_json::Value = serde_json::from_str(&text).unwrap_or_else(|e| {
        panic!(
            "$CHTYPES_ABI_FIXTURES={}: fixture.json does not parse: {e}",
            root.display()
        )
    });

    let abi_revision = doc["abi_revision"].as_i64().unwrap_or_else(|| {
        panic!(
            "{}: fixture.json carries no integer abi_revision",
            path.display()
        )
    });
    let mismatch_revision = doc["mismatch_revision"].as_i64().unwrap_or_else(|| {
        panic!(
            "{}: fixture.json carries no integer mismatch_revision",
            path.display()
        )
    });
    let clickhouse_version = doc["clickhouse_version"]
        .as_str()
        .unwrap_or_else(|| {
            panic!(
                "{}: fixture.json carries no clickhouse_version",
                path.display()
            )
        })
        .to_string();

    assert!(
        abi_revision > 0,
        "{}: abi_revision must be positive, got {abi_revision}",
        path.display()
    );
    assert_eq!(
        mismatch_revision,
        abi_revision + 1,
        "{}: mismatch_revision ({mismatch_revision}) must be abi_revision ({abi_revision}) + 1",
        path.display()
    );

    Some(Fixture {
        root,
        abi_revision,
        mismatch_revision,
        clickhouse_version,
    })
}

/// "The message says N", not "the message contains the digits of N": a
/// refusal naming revision 4 must not be satisfied by the 4 in a path like
/// `.../24.8/`, and one naming 5 must not be satisfied by 15. Equivalent to
/// the regex `(^|[^0-9])N([^0-9]|$)`, hand-rolled because this crate takes no
/// regex dependency.
fn names_number(message: &str, n: i64) -> bool {
    let needle = n.to_string();
    let bytes = message.as_bytes();
    let mut from = 0usize;
    while let Some(off) = message[from..].find(&needle) {
        let start = from + off;
        let end = start + needle.len();
        let before_ok = start == 0 || !bytes[start - 1].is_ascii_digit();
        let after_ok = end == bytes.len() || !bytes[end].is_ascii_digit();
        if before_ok && after_ok {
            return true;
        }
        from = start + 1;
    }
    false
}

/// A registry over `<root>/wrong-revision` is refused — Rust's `Registry`
/// loads every artifact in a one-directory registry EAGERLY, at construction
/// (`Registry::new`/`Registry::with_timezone` scans the directory and calls
/// `Library::load` before returning), so the ABI-revision gate in
/// `ffi::Api::open` fires from the construction call itself, never from a
/// later `for_version`.
#[test]
fn wrong_revision_is_refused() {
    let Some(fx) = fixture() else { return };
    let wrong = fx.root.join("wrong-revision");

    let err = match Registry::new(&wrong) {
        Ok(r) => {
            let loaded: Vec<String> = r
                .libraries()
                .iter()
                .map(|l| format!("{} at ABI revision {}", l.version(), l.abi_revision()))
                .collect();
            panic!(
                "the registry accepted an artifact reporting ABI revision {} (it loaded {}); \
                 docs/reference/artifact.md step 5 requires it be rejected",
                fx.mismatch_revision,
                loaded.join(", ")
            );
        }
        Err(e) => e,
    };
    let message = err.to_string();

    for n in [fx.mismatch_revision, fx.abi_revision] {
        assert!(
            names_number(&message, n),
            "the refusal does not name {n} as a whole number, which step 5 requires: {message}"
        );
    }

    announce("ABI fixture: ran wrong_revision_is_refused");
}

/// A registry over `<root>/at-revision` loads line `0.0`, and the library's
/// reported ABI revision and ClickHouse version match the fixture. Without
/// this control, `wrong_revision_is_refused` would also pass for a crate
/// whose own `ABI_REVISION` had drifted and that therefore refused
/// everything.
#[test]
fn matching_revision_loads() {
    let Some(fx) = fixture() else { return };
    let at_revision = fx.root.join("at-revision");

    let reg = Registry::new(&at_revision).unwrap_or_else(|e| {
        panic!(
            "the control artifact at {} was refused: {e}",
            at_revision.display()
        )
    });
    let lib = reg
        .for_version("0.0")
        .unwrap_or_else(|e| panic!("line 0.0 did not load from {}: {e}", at_revision.display()));

    assert_eq!(
        i64::from(lib.abi_revision()),
        fx.abi_revision,
        "the control artifact was built at ABI revision {} but the crate read {}",
        fx.abi_revision,
        lib.abi_revision()
    );
    assert_eq!(lib.version(), fx.clickhouse_version);

    announce("ABI fixture: ran matching_revision_loads");
}
