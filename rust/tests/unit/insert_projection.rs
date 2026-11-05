//! The standing projection gate of `chs_preview_insert`
//! (`spec/abi-v2/docs.md`, "The projection rule"), over the shared fixture
//! `tests/fixtures/abi-v2/insert-projection.json`.
//!
//! [`evaluate`] mirrors `scripts/abi-v2/check-insert-projection.py`'s
//! `evaluate`, rule by rule and in the same order: it takes a decoded
//! [`InsertResult`], a decoded [`BatchResult`] (its export `payload` and
//! `spans` included), the RAW insert document and the raw output bytes the
//! call returned, and returns the FIRST rule the pair violates, or `Equal`, or
//! the one `Exception`. The public [`InsertResult`] has no `export_declined`
//! members and its `output` is present only for an accepted call, so the rule
//! that an insert never carries them, and the check that no output was handed
//! back for a refusal, read the raw document and the raw bytes. Both documents
//! of every case go through this crate's real decoders (`decode::batch` and
//! `decode::insert_result`), so a case proves the decoders as well as the
//! rule. Each case carries its expected result; the fixture's 26 planted reds
//! and its exception keep the gate honest: a rule that cannot fire fails here.
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

/// `: (at row N)` and a line feed: ClickHouse's own suffix, nothing else.
fn is_row_suffix(rest: &[u8]) -> bool {
    let Some(rest) = rest.strip_prefix(b": (at row ") else {
        return false;
    };
    let Some(digits) = rest.strip_suffix(b")\n") else {
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
fn evaluate(
    batch: &BatchResult,
    ins: &InsertResult,
    raw_batch: &Json,
    raw_ins: &Json,
    out: Option<&[u8]>,
    format: Option<Format>,
) -> Projection {
    let verdicts: Vec<Option<&Verdict>> = batch.rows.iter().map(|r| r.verdict.as_ref()).collect();
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

    // export_decline_is_call_decline
    let declined = batch.outcome == Outcome::Accepted
        && (raw_batch.get("export_declined").is_some()
            || raw_batch.get("export_declined_b64").is_some());
    if declined {
        let ok = ins.outcome == Outcome::Unsupported
            && ins.reason == batch.export_declined_reason
            && ins.err_code == -2
            && ins.refused_at == Some(RefusalSite::BodyStep)
            && ins.export_columns.is_none()
            && out.is_none()
            && ins.err_msg == batch.export_declined
            && (!filtered
                || (ins.rows_passed == Some(0)
                    && ins.rows_cut == Some(0)
                    && ins.rows_undecided == Some(0)));
        if !ok {
            return Projection::Violates("export_decline_is_call_decline");
        }
    }

    // shared_members: an insert never carries export_declined*; under a
    // declined output the call's own decline replaces seven members.
    if [
        "export_declined",
        "export_declined_b64",
        "export_declined_reason",
    ]
    .iter()
    .any(|k| raw_ins.get(k).is_some())
    {
        return Projection::Violates("shared_members");
    }
    let replaced = |same: bool| declined || same;
    let shared = replaced(batch.outcome == ins.outcome)
        && replaced(batch.err_code == ins.err_code)
        && replaced(batch.reason == ins.reason)
        && replaced(batch.refused_at == ins.refused_at)
        && batch.rows_read == ins.rows_read
        && batch.rows_skipped == ins.rows_skipped
        && batch.unsupported_settings == ins.unsupported_settings
        && batch.framing == ins.framing
        && batch.partition_count == ins.partition_count
        && replaced(batch.export_columns == ins.export_columns)
        && replaced(filtered.then_some(batch.rows_passed) == ins.rows_passed)
        && replaced(filtered.then_some(batch.rows_cut) == ins.rows_cut);
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
        let want = if ins.outcome == Outcome::Accepted {
            (Some(t), Some(f + d), Some(d))
        } else {
            (Some(0), Some(0), Some(0))
        };
        if !(good && (ins.rows_passed, ins.rows_cut, ins.rows_undecided) == want) {
            return Projection::Violates("counts");
        }
    } else if ins.rows_passed.is_some() || ins.rows_cut.is_some() || ins.rows_undecided.is_some() {
        return Projection::Violates("counts");
    }

    // err: a declined output's err is the decline text, checked above
    if !declined {
        let (be, ie) = (batch.err_msg.as_bytes(), ins.err_msg.as_bytes());
        if !ie.starts_with(be) {
            return Projection::Violates("err_prefix");
        }
        let rest = &ie[be.len()..];
        if !rest.is_empty() && !is_row_suffix(rest) {
            return Projection::Violates("err_suffix");
        }
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

/// Decode a case's two documents with the crate's real decoders. The insert's
/// raw output bytes are read separately (`b64(case, "insert_rows_b64")`): the
/// public `output` is absent unless the call was accepted.
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

const RULES: [&str; 10] = [
    "e_row_rejects",
    "export_decline_is_call_decline",
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
    // The fixture is shared and lives outside this crate, so a bare copy of
    // rust/ (the packaged crate) does not hold it: skip LOUDLY, by name. A
    // fixture that is present but unreadable or malformed still fails.
    if !path.exists() {
        fail_if_fixtures_required(&path);
        eprintln!(
            "SKIPPED (loudly): every_case_of_the_shared_fixture_gives_its_expected_projection_result: fixture absent ({}); nothing was exercised",
            path.display()
        );
        return;
    }
    let text = std::fs::read_to_string(&path).expect("read the projection fixture");
    let doc: Json = serde_json::from_str(&text).expect("parse the projection fixture");
    assert_eq!(doc["schema"], "chtypes.abi-v2.insert-projection-fixture/1");
    let cases = doc["cases"].as_array().expect("cases");
    assert!(
        !cases.is_empty(),
        "the fixture holds no case (a zero-run fails)"
    );

    let mut fired = std::collections::BTreeSet::new();
    let (mut equal, mut exceptions, mut reds) = (0, 0, 0);
    let mut failures = Vec::new();
    for case in cases {
        let id = case["id"].as_str().expect("case id");
        let (batch, ins) = decode_case(case);
        let got = evaluate(
            &batch,
            &ins,
            &case["batch"],
            &case["insert"],
            b64(case, "insert_rows_b64").as_deref(),
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
    assert!(is_row_suffix(b": (at row 12)\n"));
    assert!(!is_row_suffix(b": (at row 12)"));
    assert!(!is_row_suffix(b": (at row 12)\n\n"));
    assert!(!is_row_suffix(b" (at row 12)\n"));
    assert!(!is_row_suffix(b": (at row )\n"));
    assert!(!is_row_suffix(b": (at row 1x)\n"));
    assert!(!is_row_suffix(b"(at row 1)\n"));
}

/// Where `CHTYPES_REQUIRE_FIXTURES` is "1" (the repository's main CI) an absent
/// shared fixture fails instead of skipping.
fn fail_if_fixtures_required(path: &std::path::Path) {
    assert!(
        std::env::var("CHTYPES_REQUIRE_FIXTURES").as_deref() != Ok("1"),
        "CHTYPES_REQUIRE_FIXTURES=1 and the insert projection fixture is absent: {}",
        path.display()
    );
}

/// Read the shared fixture, or `None` (after a loud skip line) when it is absent.
fn fixture_cases(test: &str) -> Option<Vec<Json>> {
    let path = super::registry_stub_tests::repo_root()
        .join("tests/fixtures/abi-v2/insert-projection.json");
    if !path.exists() {
        fail_if_fixtures_required(&path);
        eprintln!(
            "SKIPPED (loudly): {test}: fixture absent ({}); nothing was exercised",
            path.display()
        );
        return None;
    }
    let doc: Json =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read the fixture"))
            .expect("parse the fixture");
    Some(doc["cases"].as_array().expect("cases").clone())
}

#[test]
fn insert_output_absent_unless_accepted() {
    let Some(cases) = fixture_cases("insert_output_absent_unless_accepted") else {
        return;
    };
    let by_id = |id: &str| -> &Json {
        cases
            .iter()
            .find(|c| c["id"] == id)
            .unwrap_or_else(|| panic!("the fixture has no case {id}"))
    };

    // 1. A declined output is the call's decline: unsupported, no output,
    // whatever bytes the library handed back.
    let case = by_id("equal-declined-output-is-the-call-decline");
    let (_, ins) = decode_case(case);
    assert_eq!(ins.outcome, Outcome::Unsupported);
    assert!(ins.output.is_none());
    assert!(ins.export_columns.is_none());
    let doc = serde_json::to_vec(&case["insert"]).unwrap();
    let stray = decode::insert_result(&doc, Some(b"stray".to_vec())).unwrap();
    assert!(
        stray.output.is_none(),
        "an output outside accepted is dropped"
    );

    // 2. An accepted call with an output keeps its bytes, present and equal.
    let mut kept = 0;
    for case in &cases {
        let Some(raw) = b64(case, "insert_rows_b64") else {
            continue;
        };
        let (_, ins) = decode_case(case);
        if ins.outcome == Outcome::Accepted {
            assert_eq!(
                ins.output.as_deref(),
                Some(raw.as_slice()),
                "{}",
                case["id"]
            );
            kept += 1;
        }
    }
    assert!(kept > 0, "no accepted case carries an output");
}
