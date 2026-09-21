package chtypes

// Issue #90: src == "skipped" (MATERIALIZED / ALIAS / EPHEMERAL, never read
// from an input row) has been excluded from RowResult.Values since before
// revision 5 — rowResultOf's Values loop special-cases it the same way it
// special-cases SourceEphemeralInput — but, like the ephemeral_input
// exclusion before #97's test, it had no test of its own. This is that test.
//
// Parses a row document the same way the production path does —
// json.Unmarshal(quoteBareDenormals(...), &rowDoc) then rowResultOf(doc) —
// rather than constructing a RowResult by hand.

import (
	"encoding/json"
	"testing"
)

// TestValuesExcludesSkippedKeepsInput covers the Values half of #90: a
// MATERIALIZED/ALIAS/EPHEMERAL column reported with src "skipped" must not
// appear in the stored-row view, while an ordinary input-supplied column
// must.
func TestValuesExcludesSkippedKeepsInput(t *testing.T) {
	const js = `{
		"outcome": "accepted",
		"cols": [
			{
				"name": "mat_col",
				"type": "UInt8",
				"base": "UInt8",
				"src": "skipped",
				"nullable": false
			},
			{
				"name": "in_col",
				"type": "UInt8",
				"base": "UInt8",
				"src": "input",
				"input": "5",
				"stored": 5,
				"nullable": false
			}
		]
	}`
	var doc rowDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		t.Fatalf("unmarshal row document: %v", err)
	}
	res := rowResultOf(doc)

	seen := map[string]bool{}
	for _, v := range res.Values {
		seen[v.Column] = true
	}
	if seen["mat_col"] {
		t.Errorf("Values contains mat_col (src %q): a column that is never read from an input row must not appear in the stored-row view", SourceSkipped)
	}
	if !seen["in_col"] {
		t.Errorf("Values is missing in_col (src \"input\"): an ordinary supplied column must stay in Values")
	}
}
