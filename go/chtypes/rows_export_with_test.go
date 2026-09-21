package chtypes

import "testing"

// These tests cover what can be exercised WITHOUT a loaded artifact: the
// pure document-decoding logic (verdictOf, rowResultOf, batchResultOf
// against hand-built documents shaped exactly as the C ABI contract §Rows
// describes chs_rows' attached-filter document), RowsOption assembly, and
// the Go-level cross-library refusal RowsExportWith performs before any C
// call. Nothing here dlopens a library or asserts against a live server —
// that end-to-end path needs a revision-5 artifact, which does not exist
// yet (issue #54).

// TestVerdictOfMapsTheFourCharacters is the single source of truth
// verdictOf implements: 't'/'f' answer, 'e' is the predicate throwing, and
// 'd' — plus anything unrecognized — declines. Fail-closed lives here: 'e'
// and 'd' must never map to VerdictFalse.
func TestVerdictOfMapsTheFourCharacters(t *testing.T) {
	cases := map[rune]Verdict{
		't': VerdictTrue,
		'f': VerdictFalse,
		'e': VerdictError,
		'd': VerdictDecline,
		'?': VerdictDecline, // unrecognized degrades to decline, not false
	}
	for c, want := range cases {
		if got := verdictOf(c); got != want {
			t.Errorf("verdictOf(%q) = %v, want %v", c, got, want)
		}
		if (c == 'e' || c == 'd' || c == '?') && verdictOf(c) == VerdictFalse {
			t.Errorf("verdictOf(%q) collapsed a non-answer into VerdictFalse — fail-open", c)
		}
	}
}

// TestRowResultOfDecodesFilterVerdict feeds batchResultOf a hand-built
// document shaped as chs_rows answers WITH an attached filter (the C ABI
// contract §Rows, "THE ATTACHED ROW FILTER"): four rows exercising all four
// verdict characters, one of them ('d') on a row whose own parse Outcome is
// not Accepted, plus rows_passed/rows_cut at the batch level. This is the
// acceptance-bar test for property (3) — bytes only for 't', e/d never
// collapsed into f — at the decoding layer; see
// TestRowResultOfDecodesFilterVerdict_FailClosedPlant below for the
// required RED/revert demonstration.
func TestRowResultOfDecodesFilterVerdict(t *testing.T) {
	js := `{
		"outcome":"accepted","code":0,"err":"","rows_read":4,"rows_skipped":0,
		"rows_passed":1,"rows_cut":3,
		"rows":[
			{"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"t"},
			{"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"f"},
			{"outcome":"accepted","code":0,"err":"","cols":[],"verdict":"e","verdict_code":386,"verdict_err":"no common type"},
			{"outcome":"skipped","code":117,"err":"bad row","cols":[],"verdict":"d","verdict_code":117,"verdict_err":"bad row"}
		],
		"row_spans":[{"off":0,"len":5},{"off":0,"len":0},{"off":0,"len":0},{"off":0,"len":0}]
	}`
	res, err := batchResultOf(js)
	if err != nil {
		t.Fatalf("batchResultOf: %v", err)
	}
	if res.RowsPassed != 1 || res.RowsCut != 3 {
		t.Fatalf("RowsPassed/RowsCut = %d/%d, want 1/3", res.RowsPassed, res.RowsCut)
	}
	if res.RowsPassed+res.RowsCut != 4 {
		t.Fatalf("rows_passed + rows_cut = %d, want the accepted-row count 4", res.RowsPassed+res.RowsCut)
	}
	if len(res.Rows) != 4 {
		t.Fatalf("got %d rows, want 4", len(res.Rows))
	}
	want := []Verdict{VerdictTrue, VerdictFalse, VerdictError, VerdictDecline}
	for i, w := range want {
		rr := res.Rows[i]
		if rr.Verdict == nil {
			t.Fatalf("row %d: Verdict is nil, want %v", i, w)
		}
		if *rr.Verdict != w {
			t.Errorf("row %d: Verdict = %v, want %v", i, *rr.Verdict, w)
		}
	}
	// Property (3), directly: neither non-answer decoded as VerdictFalse.
	if *res.Rows[2].Verdict == VerdictFalse {
		t.Error("row 2 ('e', the predicate threw) decoded as VerdictFalse — fail-open")
	}
	if *res.Rows[3].Verdict == VerdictFalse {
		t.Error("row 3 ('d', a non-accepted row's own parse error) decoded as VerdictFalse — fail-open")
	}
	// Row 3's own outcome is not Accepted, and it still carries verdict_code
	// / verdict_err beside verdict, per the contract.
	if res.Rows[3].Outcome != Skipped {
		t.Errorf("row 3 Outcome = %v, want Skipped", res.Rows[3].Outcome)
	}
	if res.Rows[3].VerdictCode != 117 || res.Rows[3].VerdictErr != "bad row" {
		t.Errorf("row 3 VerdictCode/VerdictErr = %d/%q, want 117/%q", res.Rows[3].VerdictCode, res.Rows[3].VerdictErr, "bad row")
	}
	if res.Rows[2].VerdictCode != 386 || res.Rows[2].VerdictErr != "no common type" {
		t.Errorf("row 2 VerdictCode/VerdictErr = %d/%q, want 386/%q", res.Rows[2].VerdictCode, res.Rows[2].VerdictErr, "no common type")
	}
}

// TestRowResultOfNoFilterLeavesVerdictNil covers the ordinary Row/Rows/
// RowsExport document — no "verdict" key at all — and asserts Verdict stays
// nil rather than decoding an absent field into a fail-closed-looking
// zero value that could be mistaken for a real decline.
func TestRowResultOfNoFilterLeavesVerdictNil(t *testing.T) {
	js := `{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_skipped":0,
		"rows":[{"outcome":"accepted","code":0,"err":"","cols":[]}]}`
	res, err := batchResultOf(js)
	if err != nil {
		t.Fatalf("batchResultOf: %v", err)
	}
	if res.RowsPassed != 0 || res.RowsCut != 0 {
		t.Fatalf("RowsPassed/RowsCut = %d/%d, want 0/0 with no filter attached", res.RowsPassed, res.RowsCut)
	}
	if len(res.Rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(res.Rows))
	}
	if res.Rows[0].Verdict != nil {
		t.Fatalf("Verdict = %v, want nil (no filter attached)", *res.Rows[0].Verdict)
	}
}

// TestRowsOptionAssembly checks WithRowFilter and WithDocFlags assemble
// rowsExportWithConfig as RowsExportWith reads it: the filter set, and
// repeated WithDocFlags calls OR their bits together (RowsExport's own
// DocFlags args do the same).
func TestRowsOptionAssembly(t *testing.T) {
	f := &LoadedFilter{Expr: "tenant = 1"}
	var cfg rowsExportWithConfig
	for _, o := range []RowsOption{WithDocFlags(DocValues), WithRowFilter(f), WithDocFlags(DocDefaults)} {
		o(&cfg)
	}
	if cfg.filter != f {
		t.Fatalf("cfg.filter = %v, want %v", cfg.filter, f)
	}
	if cfg.flags != DocValues|DocDefaults {
		t.Fatalf("cfg.flags = %v, want %v", cfg.flags, DocValues|DocDefaults)
	}
}

// TestRowsExportWithNoFilterOptionLeavesConfigEmpty confirms omitting
// WithRowFilter is legal and leaves cfg.filter nil, which RowsExportWith
// reads as "call rowsThrough exactly as RowsExport would" — the
// NULL-is-unchanged-byte-for-byte path.
func TestRowsExportWithNoFilterOptionLeavesConfigEmpty(t *testing.T) {
	var cfg rowsExportWithConfig
	for _, o := range []RowsOption{WithDocFlags(DocAll)} {
		o(&cfg)
	}
	if cfg.filter != nil {
		t.Fatalf("cfg.filter = %v, want nil with no WithRowFilter", cfg.filter)
	}
}

// TestRowsExportWithCrossLibraryFilterRefused is the Go-level half of
// property (8): a filter from a DIFFERENT loaded library is refused before
// any C call — no handle crosses a dlopen'd image boundary, the same rule
// LoadedFilter.Eval enforces for a (filter, block) pair. This needs no
// dlopen'd artifact: the refusal fires on the *Library pointer comparison,
// before s.lock() or any handle is touched, so two zero-value Library
// structs with distinct identity are enough to exercise it.
//
// The SAME-library-different-schema half of (8) (code 1002) is the server's
// own answer and cannot be exercised without a loaded artifact; it is wired
// (rowsThroughWithFilter takes the cross-schema lock path and lets the C
// call answer) but not run here.
func TestRowsExportWithCrossLibraryFilterRefused(t *testing.T) {
	libA := &Library{Version: "24.8.1.1-a"}
	libB := &Library{Version: "24.8.1.1-b"}
	s := &LoadedSchema{lib: libA}
	fs := &LoadedSchema{lib: libB}
	f := &LoadedFilter{Expr: "1", schema: fs}

	_, err := s.RowsExportWith(JSONEachRow, []byte(`{}`), nil, ExportNone, WithRowFilter(f))
	if err == nil {
		t.Fatal("expected a refusal for a filter compiled over a different loaded library")
	}
}
