package chtypes

// Issue #97: classify()'s early-return list names "skipped",
// "default_expr_unsupported", "default_volatile_unresolved" and
// "default_pending" but not "ephemeral_input" — a LISTED EPHEMERAL column's
// value is read (it is in scope for other columns' DEFAULT expressions) but
// never stored, so it fell past the early return into the value-comparison
// detectors and could be reported as a "transform" for a column the table
// never held. "materialized_input" must NOT gain the same early return: under
// insert_allow_materialized_columns=1 that value IS stored and must keep
// classifying.
//
// Both tests below parse a row document the same way the production path
// does — json.Unmarshal(quoteBareDenormals(...), &rowDoc) then
// rowResultOf(doc), exactly as multiversion.go's Row() does with a real
// chs_lib_row response — rather than constructing a RowResult by hand.

import (
	"encoding/json"
	"testing"
)

// ephemeralAndMaterializedDoc builds a row document with one ephemeral_input
// column and one materialized_input column, each carrying a RefType/ref pair
// that disagrees with its rendered stored value (a classic UInt8 overflow:
// input 256, stored 0, reference-widened 256) so that detector 2 (the
// reference-type detector, which is not gated on c.Src) would flag both if
// classify() did not stop the ephemeral one first.
//
//   - eph_col:  src "ephemeral_input", no "stored" key at all — EPHEMERAL
//     columns are never written, so colDoc.Stored() reports "".
//   - mat_col:  src "materialized_input", "stored": 0 — the supplied value
//     DID get stored (replacing the column's expression) and overflowed.
func ephemeralAndMaterializedDoc(t *testing.T) rowDoc {
	t.Helper()
	const js = `{
		"outcome": "accepted",
		"cols": [
			{
				"name": "eph_col",
				"type": "UInt8",
				"base": "UInt8",
				"src": "ephemeral_input",
				"input": "256",
				"ref_type": "Int256",
				"ref": 256,
				"nullable": false
			},
			{
				"name": "mat_col",
				"type": "UInt8",
				"base": "UInt8",
				"src": "materialized_input",
				"input": "256",
				"stored": 0,
				"ref_type": "Int256",
				"ref": 256,
				"nullable": false
			}
		]
	}`
	var doc rowDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		t.Fatalf("unmarshal row document: %v", err)
	}
	return doc
}

// TestValuesExcludesEphemeralInputKeepsMaterializedInput covers the Values
// half of #97: ephemeral_input must not appear in the stored-row view,
// materialized_input must. This behavior was already correct before #97's
// fix (rowResultOf's Values loop already special-cased SourceEphemeralInput
// the same way as "skipped") — it had no test at all, which is the other
// half of the issue. Expect PASS both before and after the classify() fix
// below; it is not what #97's fix changes.
func TestValuesExcludesEphemeralInputKeepsMaterializedInput(t *testing.T) {
	doc := ephemeralAndMaterializedDoc(t)
	res := rowResultOf(doc)

	seen := map[string]bool{}
	for _, v := range res.Values {
		seen[v.Column] = true
	}
	if seen["eph_col"] {
		t.Errorf("Values contains eph_col (src ephemeral_input): a column that is never stored must not appear in the stored-row view")
	}
	if !seen["mat_col"] {
		t.Errorf("Values is missing mat_col (src materialized_input): it IS stored under insert_allow_materialized_columns=1 and must stay in Values")
	}
}

// TestClassifyExcludesEphemeralInputStillClassifiesMaterializedInput covers
// the classify() half of #97 — classify() ITSELF, called directly on a
// colDoc parsed off the wire, not through rowResultOf. rowResultOf's own
// column loop already `continue`s past SourceEphemeralInput before it ever
// reaches its `classify(c)` call (see the Values test above), so driving
// this through rowResultOf would never exercise classify()'s own missing
// case — it would pass before the fix for the wrong reason, by never running
// the code under test. classify() must be correct standing alone: it is the
// one piece #53 deliberately left as "a behavior judgement" per the issue,
// and nothing stops a future caller (or a refactor of that loop) from
// invoking it on an ephemeral_input column without the same guard.
//
// Before the fix, eph_col's early-return switch does not include
// "ephemeral_input", so classify() falls through to the reference-type
// detector (not gated on Src), sees stored ("") disagree with the
// reference-widened value ("256"), and reports a phantom overflow_wrap
// transform for a column that was never stored. Expect this assertion to
// FAIL before the fix and PASS after.
//
// mat_col carries the identical overflow shape but src "materialized_input",
// which must keep classifying either way — it is the guard against folding
// the two early-return lists together (the issue's explicit ⚠️).
func TestClassifyExcludesEphemeralInputStillClassifiesMaterializedInput(t *testing.T) {
	doc := ephemeralAndMaterializedDoc(t)

	var ephGot, matGot []Transform
	for _, c := range doc.Cols {
		switch c.Name {
		case "eph_col":
			ephGot = classify(c)
		case "mat_col":
			matGot = classify(c)
		}
	}

	if len(ephGot) != 0 {
		t.Errorf("classify() reported %d transform(s) for eph_col (src ephemeral_input), want 0 — a column that is never stored has no stored value to have silently changed: %+v", len(ephGot), ephGot)
	}

	if len(matGot) != 1 {
		t.Fatalf("classify() reported %d transform(s) for mat_col (src materialized_input), want exactly 1 — a materialized column's supplied value IS stored and must still be classified", len(matGot))
	}
	if matGot[0].Reason != ReasonOverflowWrap {
		t.Errorf("mat_col transform reason = %q, want %q", matGot[0].Reason, ReasonOverflowWrap)
	}
}
