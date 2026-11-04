//! The standing projection gate of `chs_preview_insert`
//! (`spec/abi-v2/docs.md`, "The projection rule"), over the shared fixture
//! `tests/fixtures/abi-v2/insert-projection.json`.
//!
//! [`evaluate`] mirrors `scripts/abi-v2/check-insert-projection.py`'s
//! `evaluate`, rule by rule and in the same order: it takes a decoded
//! [`InsertResult`] (its `output` bytes included) and a decoded
//! [`BatchResult`] (its export `payload` and `spans` included) and returns the
//! FIRST rule the pair violates, or `Equal`, or the one `Exception`. Both
//! documents of every case go through this crate's real decoders
//! (`decode::batch` and `decode::insert_result`), never raw JSON, so a case
//! proves the decoders as well as the rule. Each case carries its expected
//! result; the fixture's 21 planted reds and its exception keep the gate
//! honest: a rule that cannot fire fails here.
//!
//! This is a unit test of the crate (src/lib.rs declares it with `#[path]`)
//! because the decoders are crate-internal. It needs no library.
//!
//! `BatchResult` does not model the absence of `rows_passed` and `rows_cut`
//! (they read 0), so whether the batch ran with a filter is read from what a
//! filter leaves in the batch: a row verdict, or a non-zero count.

use base64::Engine as _;
use serde_json::Value as Json;

use crate::decode;
use crate::result::{BatchResult, InsertResult};
use crate::{Format, Outcome, RefusalSite, Source, Verdict};

/// The result of the projection rule for one pair of documents.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Projection {
    /// The documents agree.
    Equal,
    /// Equal by the docs' own carve-out; the name of the exception.
    Exception(&'static str),
    /// The first rule violated.
    Violates(&'static str),
}

/// A batch carries a filter's counts exactly where a filter was attached.
fn batch_filtered(batch: &BatchResult) -> bool {
    batch.rows.iter().any(|r| r.verdict.is_some()) || batch.rows_passed != 0 || batch.rows_cut != 0
}

/// ` (at row N)`: ClickHouse's own suffix, nothing else.
fn is_row_suffix(rest: &[u8]) -> bool {
    let Some(rest) = rest.strip_prefix(b" (at row ") else {
        return false;
    };
    let Some(digits) = rest.strip_suffix(b")") else {
        return false;
    };
    !digits.is_empty() && digits.iter().all(u8::is_ascii_digit)
}

/// How many lines `bytes.splitlines()` yields: `\n`, `\r` and `\r\n` end one.
fn line_count(bytes: &[u8]) -> usize {
    let mut n = 0;
    let mut i = 0;
    let mut open = false;
    while i < bytes.len() {
        match bytes[i] {
            b'\n' => {
                n += 1;
                open = false;
            }
            b'\r' => {
                n += 1;
                open = false;
                if bytes.get(i + 1) == Some(&b'\n') {
                    i += 1;
                }
            }
            _ => open = true,
        }
        i += 1;
    }
    n + usize::from(open)
}

/// The projection rule, in `check-insert-projection.py`'s order. `format` is
/// the output format (`None` for NONE).
fn evaluate(batch: &BatchResult, ins: &InsertResult, format: Option<Format>) -> Projection {
    let verdicts: Vec<Option<&Verdict>> = batch.rows.iter().map(|r| r.verdict.as_ref()).collect();
    let out = ins.output.as_deref();
    let exp = batch.payload.as_deref();
    let filtered = batch_filtered(batch);

    // e_row_rejects
    if batch.outcome == Outcome::Accepted && verdicts.contains(&Some(&Verdict::Error)) {
        let first = batch
            .rows
            .iter()
            .find(|r| r.verdict == Some(Verdict::Error))
            .expect("an e verdict was just seen");
        let ok = ins.outcome == Outcome::Rejected
            && ins.err_code == first.verdict_code
            && ins.err_msg == first.verdict_err
            && ins.refused_at == Some(RefusalSite::Row)
            && ins.rows_passed == Some(0)
            && ins.rows_cut == Some(0)
            && ins.rows_undecided == Some(0)
            && ins.rows_read == batch.rows_read
            && out.is_none();
        return if ok {
            Projection::Equal
        } else {
            Projection::Violates("e_row_rejects")
        };
    }

    // shared_members
    let shared = batch.outcome == ins.outcome
        && batch.err_code == ins.err_code
        && batch.reason == ins.reason
        && batch.refused_at == ins.refused_at
        && batch.rows_read == ins.rows_read
        && batch.rows_skipped == ins.rows_skipped
        && batch.unsupported_settings == ins.unsupported_settings
        && batch.framing == ins.framing
        && batch.partition_count == ins.partition_count
        && batch.export_columns == ins.export_columns
        && batch.export_declined == ins.export_declined
        && batch.export_declined_reason == ins.export_declined_reason
        && filtered.then_some(batch.rows_passed) == ins.rows_passed
        && filtered.then_some(batch.rows_cut) == ins.rows_cut;
    if !shared {
        return Projection::Violates("shared_members");
    }

    // counts
    let count = |v: Verdict| verdicts.iter().filter(|x| **x == Some(&v)).count() as u64;
    let (t, f, d) = (
        count(Verdict::True),
        count(Verdict::False),
        count(Verdict::Decline),
    );
    if filtered {
        let good = if batch.outcome == Outcome::Accepted {
            batch.rows_passed == t && batch.rows_cut == f + d
        } else {
            batch.rows_passed == 0 && batch.rows_cut == 0
        };
        if !(good && ins.rows_undecided == Some(d)) {
            return Projection::Violates("counts");
        }
    } else if ins.rows_passed.is_some() || ins.rows_cut.is_some() || ins.rows_undecided.is_some() {
        return Projection::Violates("counts");
    }

    // err
    let (be, ie) = (batch.err_msg.as_bytes(), ins.err_msg.as_bytes());
    if !ie.starts_with(be) {
        return Projection::Violates("err_prefix");
    }
    let rest = &ie[be.len()..];
    if !rest.is_empty() && !is_row_suffix(rest) {
        return Projection::Violates("err_suffix");
    }

    // output
    if exp.is_none() != out.is_none() {
        return Projection::Violates("output_presence");
    }
    let (Some(out), Some(exp)) = (out, exp) else {
        return Projection::Equal;
    };
    if batch.rows.iter().any(|r| {
        r.columns
            .iter()
            .any(|c| c.source == Source::DefaultGenerated)
    }) {
        return Projection::Exception("generator_draw");
    }
    if format == Some(Format::Native) {
        return if out == exp {
            Projection::Equal
        } else {
            Projection::Violates("native_output")
        };
    }
    let spans = batch.spans.as_deref().unwrap_or(&[]);
    let mut t_lines: Vec<u8> = Vec::new();
    let mut t_rows = 0;
    for (span, v) in spans.iter().zip(&verdicts) {
        if matches!(v, None | Some(Verdict::True)) {
            let s = span.expect("a t row has a span");
            let (off, len) = (s.off as usize, s.len as usize);
            t_lines.extend_from_slice(&exp[off..off + len]);
            t_rows += 1;
        }
    }
    // The spans of non-t rows are null, so a stray row is seen as a line
    // beyond the t rows.
    if line_count(out) > t_rows {
        return Projection::Violates("non_t_row");
    }
    if out == t_lines {
        Projection::Equal
    } else {
        Projection::Violates("line_output")
    }
}

fn format_of(name: &str) -> Option<Format> {
    match name {
        "NONE" => None,
        "Native" => Some(Format::Native),
        "JSONCompactEachRow" => Some(Format::JsonCompactEachRow),
        "CSV" => Some(Format::Csv),
        other => panic!("the fixture names an output format this gate does not know: {other}"),
    }
}

fn b64(case: &Json, key: &str) -> Option<Vec<u8>> {
    case[key].as_str().map(|s| {
        base64::engine::general_purpose::STANDARD
            .decode(s)
            .expect("the fixture's base64")
    })
}

fn expected(case: &Json) -> Projection {
    let e = &case["expect"];
    if e == "equal" {
        return Projection::Equal;
    }
    let rule = |k: &str| -> &'static str {
        // The names are the checker's own; leak is fine in a test.
        Box::leak(
            e[k].as_str()
                .expect("a rule name")
                .to_string()
                .into_boxed_str(),
        )
    };
    if e.get("exception").is_some() {
        Projection::Exception(rule("exception"))
    } else {
        Projection::Violates(rule("violates"))
    }
}

/// Decode a case's two documents with the crate's real decoders.
fn decode_case(case: &Json) -> (BatchResult, InsertResult) {
    let batch = decode::batch(
        &serde_json::to_vec(&case["batch"]).unwrap(),
        b64(case, "batch_export_b64"),
    )
    .unwrap_or_else(|e| panic!("{}: the batch does not decode: {e}", case["id"]));
    let ins = decode::insert_result(
        &serde_json::to_vec(&case["insert"]).unwrap(),
        b64(case, "insert_rows_b64"),
    )
    .unwrap_or_else(|e| panic!("{}: the insert_result does not decode: {e}", case["id"]));
    (batch, ins)
}

const RULES: [&str; 9] = [
    "e_row_rejects",
    "shared_members",
    "counts",
    "err_prefix",
    "err_suffix",
    "output_presence",
    "non_t_row",
    "line_output",
    "native_output",
];

#[test]
fn every_case_of_the_shared_fixture_gives_its_expected_projection_result() {
    let path = super::registry_stub_tests::repo_root()
        .join("tests/fixtures/abi-v2/insert-projection.json");
    let text = std::fs::read_to_string(&path).expect("read the projection fixture");
    let doc: Json = serde_json::from_str(&text).expect("parse the projection fixture");
    assert_eq!(doc["schema"], "chtypes.abi-v2.insert-projection-fixture/1");
    let cases = doc["cases"].as_array().expect("cases");
    assert!(!cases.is_empty(), "the fixture holds no case");

    let mut fired = std::collections::BTreeSet::new();
    let (mut equal, mut exceptions, mut reds) = (0, 0, 0);
    let mut failures = Vec::new();
    for case in cases {
        let id = case["id"].as_str().expect("case id");
        let (batch, ins) = decode_case(case);
        let got = evaluate(
            &batch,
            &ins,
            format_of(case["output_format"].as_str().unwrap()),
        );
        let want = expected(case);
        eprintln!("projection case {id}: {got:?} (expected {want:?})");
        if got != want {
            failures.push(format!("{id}: got {got:?}, expected {want:?}"));
        }
        match &want {
            Projection::Equal => equal += 1,
            Projection::Exception(_) => exceptions += 1,
            Projection::Violates(r) => {
                reds += 1;
                fired.insert(*r);
            }
        }
    }
    assert!(
        failures.is_empty(),
        "projection results differ:\n{}",
        failures.join("\n")
    );
    eprintln!(
        "projection gate: {} cases ({equal} equal, {exceptions} exception, {reds} planted reds)",
        cases.len()
    );
    // A rule with no planted red is a rule the gate cannot see fail.
    for rule in RULES {
        assert!(fired.contains(rule), "no planted red for rule {rule}");
    }
    assert!(exceptions >= 1, "no case for the generator_draw exception");
    for fmt in ["JSONCompactEachRow", "CSV", "Native", "NONE"] {
        assert!(
            cases
                .iter()
                .any(|c| c["output_format"] == fmt && c["expect"] == "equal"),
            "no equal case for {fmt}"
        );
    }
}

#[test]
fn the_line_counter_and_the_suffix_match_the_checkers_primitives() {
    // bytes.splitlines(keepends=True), as the checker counts a line output.
    assert_eq!(line_count(b""), 0);
    assert_eq!(line_count(b"a"), 1);
    assert_eq!(line_count(b"a\n"), 1);
    assert_eq!(line_count(b"a\nb"), 2);
    assert_eq!(line_count(b"a\r\nb\r\n"), 2);
    assert_eq!(line_count(b"a\rb"), 2);
    assert_eq!(line_count(b"\n\n"), 2);
    assert!(is_row_suffix(b" (at row 12)"));
    assert!(!is_row_suffix(b" (at row )"));
    assert!(!is_row_suffix(b" (at row 1x)"));
    assert!(!is_row_suffix(b"(at row 1)"));
    assert!(!is_row_suffix(b" (at row 1) "));
}
