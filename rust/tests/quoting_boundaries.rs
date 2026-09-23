//! The quoting trio, measured against real artifacts (issue #119, from #52).
//!
//! Four things are pinned here, and none of them is a spelling:
//!
//! 1. **The per-line boundaries.** [`chtypes::Library::quote_identifier_if_needed`]
//!    answers the LOADED BUILD's own rule, and that rule moves between
//!    ClickHouse lines. The 0.3.0 CHANGELOG states which names move where;
//!    these cases execute that statement against whatever lines the registry
//!    holds, on BOTH sides of every boundary.
//! 2. **An empty identifier comes back quoted, never bare** (`include/chtypes.h`).
//! 3. **A literal carrying a NUL byte survives end to end** — the one case
//!    where the counted-input contract is load-bearing, because a `strlen`
//!    would truncate at the NUL and nothing else would say so.
//! 4. **Two loaded libraries answer for themselves**, asked in one process and
//!    interleaved. Per-line divergence is the entire reason these calls hang
//!    off a library rather than off the crate, and one library cannot show it.
//!
//! HOW A SPELLING IS NEVER WRITTEN DOWN HERE. Every expectation is stated as
//! one of the library's OWN two answers: `quote_identifier` always quotes, so
//! it IS this build's quoted spelling, and the input itself is the bare
//! spelling. A case asserts `answer == lib.quote_identifier(name)` or
//! `answer == name` and never names a quote character — which is what keeps
//! `scripts/check-quoting-passthrough.py` true and keeps these cases from
//! re-asserting the hand-written rule issue #52 deleted. Each line is first
//! checked to spell the two forms DIFFERENTLY, so "quoted" and "bare" cannot
//! both be satisfied by the same bytes.
//!
//! THE TWO ENUMERATIONS BELOW ARE EXPECTATIONS, NOT A RULE. Nothing here
//! computes which names a build quotes; the sets say what the release
//! MEASURED, per line, and a disagreement is a finding about the artifact
//! rather than a test to adjust. They are closed downward on purpose: the
//! lines named are the published lines that sit below a boundary, and any
//! other line is at or above the last one, so a line published after this was
//! written reads as "quotes all three" rather than silently dropping out of
//! the count.
//!
//! COUNTS, AND WHY A QUIET RUN IS A FAILURE. Every case counts what it
//! actually ran and asserts the total. Without a registry every case here
//! skips, loudly, by name, on the real stderr; WITH one, a case that observed
//! only one side of a boundary — or only one library — PANICS by name rather
//! than passing on the half it could see. A boundary case that skips forever
//! is exactly the failure these tests exist to prevent.
//!
//! `primary()` in `tests/integration.rs` deliberately resolves the NEWEST
//! loaded line; nothing here goes through it, because a boundary needs the
//! oldest and the newest at once and both must be named.

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

use chtypes::{Library, Registry};

/// The 0.3.0 CHANGELOG's claim, split into the part that holds everywhere and
/// the part that moves between lines.
const QUOTED_ON_EVERY_LINE: [&str; 4] = ["all", "distinct", "table", "null"];
const BARE_ON_EVERY_LINE: [&str; 1] = ["where"];
const MOVES_BETWEEN_LINES: [&str; 3] = ["select", "from", "values"];
const EVERY_NAME: [&str; 8] = [
    "all", "distinct", "table", "null", "where", "select", "from", "values",
];

/// Published lines below every boundary: all three of the moving names bare.
const LINES_BELOW_EVERY_BOUNDARY: [&str; 5] = ["24.8", "25.3", "25.8", "25.10", "26.2"];
/// Published lines that quote `select` and not yet `from`/`values`.
const LINES_QUOTING_SELECT_ONLY: [&str; 2] = ["26.3", "26.4"];

/// Whether this line is expected to spell `name` bare, per the CHANGELOG.
fn expected_bare(line: &str, name: &str) -> bool {
    if QUOTED_ON_EVERY_LINE.contains(&name) {
        return false;
    }
    if BARE_ON_EVERY_LINE.contains(&name) {
        return true;
    }
    // Anything left is one of the names the release says MOVES between lines,
    // and nothing else may reach the per-line answer below.
    assert!(
        MOVES_BETWEEN_LINES.contains(&name),
        "{name:?} is not one of the names this file has an expectation for"
    );
    if LINES_BELOW_EVERY_BOUNDARY.contains(&line) {
        return true;
    }
    if LINES_QUOTING_SELECT_ONLY.contains(&line) {
        return name != "select";
    }
    false
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

type Loaded = Vec<(String, Arc<Library>)>;

static LOADED: OnceLock<Option<Loaded>> = OnceLock::new();

/// Every line this registry can answer for, OPENED, in release order.
///
/// A line that refuses to open — an artifact from an older ABI revision left
/// on the search path, say — is announced and left out rather than failing
/// every case here: whether enough lines opened is decided by each test, by
/// name, where the reason can be stated.
fn loaded() -> Option<&'static Loaded> {
    LOADED
        .get_or_init(|| {
            let dir = registry_dir();
            if !dir.is_dir() {
                announce(&format!(
                    "\nSKIP: no chtypes artifact registry at {} — fetch the lines with \
                     scripts/fetch.sh 24.8 and scripts/fetch.sh 26.8 (docs/guides/fetch.md), or \
                     point ${} at a registry. Every test in this file is skipped, each by name \
                     below.\n",
                    dir.display(),
                    chtypes::REGISTRY_ENV
                ));
                return None;
            }
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
            let mut out: Loaded = Vec::new();
            for line in reg.versions() {
                match reg.for_version(&line) {
                    Ok(lib) => out.push((line, lib)),
                    Err(e) => announce(&format!(
                        "\nNOTE: line {line} did not open and is not measured here: {e}\n"
                    )),
                }
            }
            if out.is_empty() {
                announce(&format!(
                    "\nSKIP: registry {} holds no line this binding can open — fetch one with \
                     scripts/fetch.sh 26.8 (docs/guides/fetch.md). Every test in this file is \
                     skipped, each by name below.\n",
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

/// Bind the loaded lines, or skip the test — by name, on the real stderr,
/// after the one message above that says why.
macro_rules! lines {
    () => {
        match loaded() {
            Some(l) => l,
            None => {
                // Leading newline: libtest writes "test name ... ok" in
                // pieces, and a line landing between them would not START a
                // line — the one place a census looks for it.
                announce(&format!(
                    "\nSKIP {}: needs an artifact registry\n",
                    test_name!()
                ));
                return;
            }
        }
    };
}

/// `(the always-quoted spelling, the if-needed answer)` for one name. The
/// first is this build's own quoted form — that is what `quote_identifier` IS
/// — so a case never has to know what quoting looks like.
fn both_forms(lib: &Library, name: &str) -> (String, String) {
    (
        lib.quote_identifier(name)
            .unwrap_or_else(|e| panic!("quote_identifier({name:?}): {e}")),
        lib.quote_identifier_if_needed(name)
            .unwrap_or_else(|e| panic!("quote_identifier_if_needed({name:?}): {e}")),
    )
}

fn line_names(loaded: &Loaded) -> Vec<&str> {
    loaded.iter().map(|(line, _)| line.as_str()).collect()
}

#[test]
fn the_per_line_boundaries_are_what_the_release_documents() {
    let loaded = lines!();
    let (mut ran, mut below, mut above) = (0usize, 0usize, 0usize);
    for (line, lib) in loaded {
        if LINES_BELOW_EVERY_BOUNDARY.contains(&line.as_str()) {
            below += 1;
        } else if !LINES_QUOTING_SELECT_ONLY.contains(&line.as_str()) {
            above += 1;
        }
        for name in EVERY_NAME {
            let (always, answer) = both_forms(lib, name);
            assert_ne!(
                always, name,
                "{line}: the always-quoted form of {name:?} is the bare name, so this case \
                 cannot tell quoted from bare"
            );
            if expected_bare(line, name) {
                assert_eq!(answer, name, "{line}: {name:?} is documented bare");
            } else {
                assert_eq!(
                    answer, always,
                    "{line}: {name:?} is documented as this build's quoted form"
                );
            }
            ran += 1;
        }
    }
    assert_eq!(
        ran,
        EVERY_NAME.len() * loaded.len(),
        "a case was skipped inside the loop"
    );
    assert!(ran > 0, "no line was examined");
    // Both sides, or nothing is being measured. CI fetches 24.8 alongside the
    // newest -lts and -stable precisely so this holds.
    assert!(
        below > 0,
        "the registry holds no line below every documented boundary (one of {:?}), so the \
         boundary cannot be observed at all — scripts/fetch.sh 24.8 installs one. Lines loaded: \
         {:?}",
        LINES_BELOW_EVERY_BOUNDARY,
        line_names(loaded)
    );
    assert!(
        above > 0,
        "the registry holds no line at or above the last documented boundary, so the quoted side \
         of it cannot be observed — scripts/fetch.sh 26.8 installs one. Lines loaded: {:?}",
        line_names(loaded)
    );
    println!(
        "{ran} cases over {:?} ({below} below every boundary, {above} at or above the last)",
        line_names(loaded)
    );
}

#[test]
fn an_empty_identifier_comes_back_quoted_never_bare() {
    let loaded = lines!();
    let mut ran = 0usize;
    for (line, lib) in loaded {
        let (always, answer) = both_forms(lib, "");
        assert!(
            !answer.is_empty(),
            "{line}: an empty identifier came back empty, which is bare"
        );
        assert_eq!(
            answer, always,
            "{line}: an empty identifier must come back as this build's quoted form"
        );
        ran += 1;
    }
    assert_eq!(
        ran,
        loaded.len(),
        "no line answered for an empty identifier"
    );
    assert!(ran > 0, "no line answered for an empty identifier");
}

#[test]
fn a_literal_carrying_a_nul_byte_survives_end_to_end() {
    let loaded = lines!();
    let mut ran = 0usize;
    for (line, lib) in loaded {
        let plain = lib.quote_literal("a").expect("quote_literal");
        let with_nul = lib
            .quote_literal("a\0b")
            .expect("quote_literal with a NUL byte");
        // A strlen'd input would quote just the leading "a" and say nothing.
        assert_ne!(
            with_nul, plain,
            "{line}: the input was truncated at the NUL byte"
        );
        assert!(
            with_nul.len() > plain.len(),
            "{line}: {with_nul:?} is no longer than {plain:?}"
        );
        assert!(
            with_nul.contains('b'),
            "{line}: the byte after the NUL is missing from {with_nul:?}"
        );
        // The header's other half: every byte the server escapes comes back
        // escaped, so the ANSWER is NUL-free even when the input was not.
        assert!(
            !with_nul.contains('\0'),
            "{line}: the answer {with_nul:?} carries a NUL byte"
        );
        // Same delimiters as an ordinary value: this is one literal, not two.
        assert_eq!(
            (
                with_nul.as_bytes()[0],
                with_nul.as_bytes()[with_nul.len() - 1]
            ),
            (plain.as_bytes()[0], plain.as_bytes()[plain.len() - 1]),
            "{line}: {with_nul:?} is not delimited like {plain:?}"
        );
        ran += 1;
    }
    assert_eq!(
        ran,
        loaded.len(),
        "no line quoted a literal carrying a NUL byte"
    );
    assert!(ran > 0, "no line quoted a literal carrying a NUL byte");
}

#[test]
fn two_loaded_libraries_each_answer_for_themselves() {
    let loaded = lines!();
    assert!(
        loaded.len() >= 2,
        "this case needs TWO libraries in one process — a single library cannot show a \
         cache-across-versions bug, which is the whole reason these calls hang off a library. \
         Lines loaded: {:?}",
        line_names(loaded)
    );
    let (older_line, older) = &loaded[0];
    let (newer_line, newer) = &loaded[loaded.len() - 1];

    let mut ran = 0usize;
    let mut rounds: Vec<Vec<(String, String)>> = Vec::new();
    // Interleaved, and then interleaved again: each library is asked the same
    // name after the other one has answered it, so an answer cached across
    // versions would show up as one library repeating the other's.
    for _ in 0..2 {
        let mut round = Vec::new();
        for name in EVERY_NAME {
            let a = older
                .quote_identifier_if_needed(name)
                .unwrap_or_else(|e| panic!("{older_line}: {e}"));
            let b = newer
                .quote_identifier_if_needed(name)
                .unwrap_or_else(|e| panic!("{newer_line}: {e}"));
            round.push((a, b));
            ran += 2;
        }
        rounds.push(round);
    }
    assert_eq!(
        rounds[0], rounds[1],
        "{older_line}/{newer_line}: asking one library changed the other's answer"
    );
    assert_eq!(
        ran,
        2 * 2 * EVERY_NAME.len(),
        "a name was skipped inside the loop"
    );

    // And where the two lines sit on opposite sides of a boundary, they must
    // DISAGREE — the divergence the library-scoped API exists for.
    let mut divergent = 0usize;
    for (i, name) in EVERY_NAME.iter().enumerate() {
        if expected_bare(older_line, name) == expected_bare(newer_line, name) {
            continue;
        }
        divergent += 1;
        let (a, b) = &rounds[0][i];
        assert_ne!(
            a, b,
            "{name:?} is documented as differing between {older_line} and {newer_line}"
        );
    }
    assert!(
        divergent > 0,
        "{older_line} and {newer_line} sit on the same side of every documented boundary, so no \
         divergence can be observed — fetch a line below 26.3 (24.8) alongside the newest one"
    );
    println!(
        "{ran} interleaved calls over {older_line} and {newer_line}, {divergent} names documented \
         as divergent"
    );
}
