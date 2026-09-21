package chtypes

// Issue #122: five more results-group rules the four bindings agree on by
// construction, declared nowhere in tests/parity/manifest.json until this
// change. Each test below parses a document through the same production path
// rowResultOf/filterResultOf already use — json.Unmarshal(quoteBareDenormals(...))
// then the assembler — rather than constructing a result struct by hand.

import (
	"encoding/json"
	"testing"
)

// TestUnsupportedSettingsForcesUnsupportedUnlessRejected covers rule 1:
// results.unsupported-settings-forces-unsupported. A non-empty
// UnsupportedSettings forces Outcome to Unsupported, UNLESS it is already
// Rejected — the precedence is part of the rule.
func TestUnsupportedSettingsForcesUnsupportedUnlessRejected(t *testing.T) {
	cases := []struct {
		name    string
		outcome string
		want    Outcome
	}{
		{"accepted row with unsupported settings degrades to Unsupported", "accepted", Unsupported},
		{"rejected row with unsupported settings stays Rejected", "rejected", Rejected},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			js := `{"outcome": "` + tc.outcome + `", "unsupported_settings": ["some_setting"], "cols": []}`
			var doc rowDoc
			if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
				t.Fatalf("unmarshal row document: %v", err)
			}
			res := rowResultOf(doc)
			if res.Outcome != tc.want {
				t.Errorf("Outcome = %v, want %v (unsupported_settings %v, wire outcome %q)",
					res.Outcome, tc.want, res.UnsupportedSettings, tc.outcome)
			}
		})
	}
}

// TestUnknownOutcomeDegradesToUnsupported covers rule 2, the outcome half:
// results.unknown-outcome-degrades-to-unsupported. An outcome string this
// binding does not recognize MUST map to Unsupported, never to Rejected, for
// both RowResult and FilterResult.
func TestUnknownOutcomeDegradesToUnsupported(t *testing.T) {
	t.Run("RowResult.Outcome", func(t *testing.T) {
		js := `{"outcome": "totally-unknown-future-outcome", "cols": []}`
		var doc rowDoc
		if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
			t.Fatalf("unmarshal row document: %v", err)
		}
		res := rowResultOf(doc)
		if res.Outcome != Unsupported {
			t.Errorf("Outcome = %v, want Unsupported (never Rejected) for an unrecognized outcome string", res.Outcome)
		}
	})
	t.Run("FilterResult.Outcome", func(t *testing.T) {
		js := `{"outcome": "totally-unknown-future-outcome", "verdicts": "", "errors": []}`
		res, err := filterResultOf(js)
		if err != nil {
			t.Fatalf("filterResultOf: %v", err)
		}
		if res.Outcome != FilterUnsupported {
			t.Errorf("Outcome = %v, want FilterUnsupported (never FilterRejected) for an unrecognized outcome string", res.Outcome)
		}
	})
}

// TestUnknownVerdictDegradesToDecline covers rule 2, the verdict half:
// results.unknown-verdict-degrades-to-decline. An unrecognized verdict
// character MUST degrade to VerdictDecline, never be collapsed into
// VerdictFalse (an invented answer).
func TestUnknownVerdictDegradesToDecline(t *testing.T) {
	t.Run("row-level verdict", func(t *testing.T) {
		js := `{"outcome": "accepted", "cols": [], "verdict": "z"}`
		var doc rowDoc
		if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
			t.Fatalf("unmarshal row document: %v", err)
		}
		res := rowResultOf(doc)
		if res.Verdict == nil {
			t.Fatal("Verdict is nil, want VerdictDecline")
		}
		if *res.Verdict != VerdictDecline {
			t.Errorf("Verdict = %v, want VerdictDecline for an unrecognized character 'z'", *res.Verdict)
		}
	})
	t.Run("filter verdicts string", func(t *testing.T) {
		js := `{"outcome": "ok", "verdicts": "z", "errors": []}`
		res, err := filterResultOf(js)
		if err != nil {
			t.Fatalf("filterResultOf: %v", err)
		}
		if len(res.Verdicts) != 1 || res.Verdicts[0] != VerdictDecline {
			t.Errorf("Verdicts = %v, want [VerdictDecline] for an unrecognized character 'z'", res.Verdicts)
		}
	})
}

// TestValueNullFalseWhenPoisoned covers rule 3:
// results.null-false-when-poisoned. Value.Null is true only when the stored
// text is literally "null" AND the column is not poisoned; poison silently
// overrides a textual null.
func TestValueNullFalseWhenPoisoned(t *testing.T) {
	cases := []struct {
		name   string
		poison string
		want   bool
	}{
		{"a genuine stored null (not poisoned) is Null == true", "false", true},
		{"a poisoned column reporting textual null is Null == false", "true", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			js := `{"outcome": "accepted", "cols": [
				{"name": "c", "type": "UInt8", "base": "UInt8", "src": "input", "input": "", "stored": null, "poison": ` + tc.poison + `, "nullable": true}
			]}`
			var doc rowDoc
			if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
				t.Fatalf("unmarshal row document: %v", err)
			}
			res := rowResultOf(doc)
			if len(res.Values) != 1 {
				t.Fatalf("Values has %d entries, want 1", len(res.Values))
			}
			if res.Values[0].Null != tc.want {
				t.Errorf("Values[0].Null = %v, want %v (poison=%s)", res.Values[0].Null, tc.want, tc.poison)
			}
		})
	}
}

// TestDefaultSubstitutedPopulatesSubstituted covers rule 4:
// results.default-substituted-populates-substituted. src ==
// SourceDefaultSubstituted is the only src value that populates
// RowResult.Substituted.
func TestDefaultSubstitutedPopulatesSubstituted(t *testing.T) {
	if SourceDefaultSubstituted != "default_substituted" {
		t.Fatalf("SourceDefaultSubstituted = %q, want \"default_substituted\"", SourceDefaultSubstituted)
	}
	js := `{"outcome": "accepted", "cols": [
		{"name": "ts", "type": "DateTime", "base": "DateTime", "src": "default_substituted", "input": "now()", "stored": "2026-09-21 00:00:00", "nullable": false},
		{"name": "in_col", "type": "UInt8", "base": "UInt8", "src": "input", "input": "5", "stored": 5, "nullable": false}
	]}`
	var doc rowDoc
	if err := json.Unmarshal(quoteBareDenormals([]byte(js)), &doc); err != nil {
		t.Fatalf("unmarshal row document: %v", err)
	}
	res := rowResultOf(doc)
	if len(res.Substituted) != 1 {
		t.Fatalf("Substituted has %d entries, want exactly 1 (only the default_substituted column): %+v", len(res.Substituted), res.Substituted)
	}
	sub := res.Substituted[0]
	if sub.Column != "ts" || sub.Expr != "now()" || sub.Text != `"2026-09-21 00:00:00"` {
		t.Errorf("Substituted[0] = %+v, want {Column: ts, Expr: now(), Text: \"2026-09-21 00:00:00\"}", sub)
	}
}

// TestNonOkFilterResultForcesEmptyVerdictsAndErrors covers rule 5:
// results.non-ok-filter-result-forces-empty. A FilterResult whose Outcome is
// anything but FilterOK forces Verdicts and Errors empty, even if the wire
// document carried bytes for them alongside a non-OK outcome — no partial
// answers.
func TestNonOkFilterResultForcesEmptyVerdictsAndErrors(t *testing.T) {
	js := `{"outcome": "rejected", "code": 115, "err": "unknown setting", "verdicts": "tfed", "errors": [{"row": 0, "code": 27, "err": "boom"}]}`
	res, err := filterResultOf(js)
	if err != nil {
		t.Fatalf("filterResultOf: %v", err)
	}
	if res.Outcome != FilterRejected {
		t.Fatalf("Outcome = %v, want FilterRejected", res.Outcome)
	}
	if len(res.Verdicts) != 0 {
		t.Errorf("Verdicts = %v, want empty: a non-OK FilterResult must not leak partial answers", res.Verdicts)
	}
	if len(res.Errors) != 0 {
		t.Errorf("Errors = %v, want empty: a non-OK FilterResult must not leak partial answers", res.Errors)
	}
}
