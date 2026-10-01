package chtypes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// Most of this file covers what can be exercised WITHOUT a loaded artifact:
// the pure document-decoding logic (verdictOf, rowResultOf, batchResultOf
// against hand-built documents shaped exactly as the C ABI contract §Rows
// describes chs_rows' attached-filter document), RowsOption assembly, and
// the Go-level cross-library refusal RowsExportWith performs before any C
// call. TestRowsExportWithColumnsAndFilterComposeEndToEnd and the three
// chtypes#298 regression tests at the end are the exceptions: each needs a
// revision-5 artifact and skips loudly without one (rev5Libraries,
// csv_reader_test.go).

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
		o.applyRowsExportWith(&cfg)
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
		o.applyRowsExportWith(&cfg)
	}
	if cfg.filter != nil {
		t.Fatalf("cfg.filter = %v, want nil with no WithRowFilter", cfg.filter)
	}
}

// TestRowsOptionAssemblyWithColumns is issue #304's own regression: WithColumns
// (a RowOption) rides RowsOption via RowOption.applyRowsExportWith, exactly as
// it already rides RowsExportOption for RowsExport, so a column list composes
// with an attached filter in one RowsExportWith call — the gap the C ABI's
// chs_rows never had (it already takes columns_json and filter on the same
// call) but Go's RowsExportWith did, until now.
func TestRowsOptionAssemblyWithColumns(t *testing.T) {
	f := &LoadedFilter{Expr: "tenant = 1"}
	var cfg rowsExportWithConfig
	for _, o := range []RowsOption{WithColumns([]string{"tenant", "id"}), WithRowFilter(f)} {
		o.applyRowsExportWith(&cfg)
	}
	if cfg.filter != f {
		t.Fatalf("cfg.filter = %v, want %v", cfg.filter, f)
	}
	want := []string{"tenant", "id"}
	if len(cfg.columns) != len(want) {
		t.Fatalf("cfg.columns = %v, want %v", cfg.columns, want)
	}
	for i, c := range want {
		if cfg.columns[i] != c {
			t.Fatalf("cfg.columns = %v, want %v", cfg.columns, want)
		}
	}
}

// TestRowsOptionAssemblyWithColumnsEmptyLeavesNil confirms an empty WithColumns
// slice leaves cfg.columns empty, the same "no list" reading columnsCArgLoaded
// gives it elsewhere — never an empty JSON array sent to the server.
func TestRowsOptionAssemblyWithColumnsEmptyLeavesNil(t *testing.T) {
	var cfg rowsExportWithConfig
	for _, o := range []RowsOption{WithColumns(nil)} {
		o.applyRowsExportWith(&cfg)
	}
	if len(cfg.columns) != 0 {
		t.Fatalf("cfg.columns = %v, want empty", cfg.columns)
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

// TestRowsExportWithColumnsAndFilterComposeEndToEnd is issue #304's own
// end-to-end regression, against a real revision-5 artifact: WithColumns and
// WithRowFilter attached to the SAME RowsExportWith call compose exactly as
// the C ABI's chs_rows already allows — columns_json and the attached filter
// are two independent trailing parameters on one call — and exactly as
// Python and TypeScript already let a caller combine them. Before this fix,
// Go's RowsExportWith hardcoded the columns_json argument to NULL, so a
// listed EPHEMERAL column's value never reached the DEFAULT the filter then
// reads — the issue's own measurement ("tenant is empty and the filter
// answers f").
//
// _t is EPHEMERAL: its value is read only when WithColumns lists it, never
// stored, never exported. tenant's DEFAULT reads _t, so tenant resolves to
// "acme"/"other" only when _t was actually carried through — proof the
// column list reached the same chs_rows call as the filter, not a second,
// column-list-less one.
func TestRowsExportWithColumnsAndFilterComposeEndToEnd(t *testing.T) {
	libs := rev5Libraries(t)
	ran := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt32, _t String EPHEMERAL, tenant String DEFAULT _t")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		f, err := s.CompileFilter("tenant = 'acme'")
		if err != nil {
			s.Close()
			t.Fatalf("%s: CompileFilter: %v", lib.Version, err)
		}

		body := []byte(`{"id":1,"_t":"acme"}` + "\n" + `{"id":2,"_t":"other"}` + "\n")
		b, err := s.RowsExportWith(JSONEachRow, body, nil, JSONCompactEachRow,
			WithColumns([]string{"id", "_t"}), WithRowFilter(f))
		if err != nil {
			f.Close()
			s.Close()
			t.Fatalf("%s: RowsExportWith: %v", lib.Version, err)
		}
		if b.Outcome != Accepted {
			t.Fatalf("%s: Outcome = %v (code %d, %q), want %v", lib.Version, b.Outcome, b.ErrCode, b.ErrMsg, Accepted)
		}
		if len(b.Rows) != 2 {
			t.Fatalf("%s: got %d row(s), want 2", lib.Version, len(b.Rows))
		}
		if b.Rows[0].Verdict == nil || *b.Rows[0].Verdict != VerdictTrue {
			t.Fatalf("%s: row 0 Verdict = %v, want VerdictTrue (tenant == \"acme\")", lib.Version, b.Rows[0].Verdict)
		}
		if b.Rows[1].Verdict == nil || *b.Rows[1].Verdict != VerdictFalse {
			t.Fatalf("%s: row 1 Verdict = %v, want VerdictFalse (tenant == \"other\")", lib.Version, b.Rows[1].Verdict)
		}
		if b.RowsPassed != 1 || b.RowsCut != 1 {
			t.Fatalf("%s: RowsPassed/RowsCut = %d/%d, want 1/1", lib.Version, b.RowsPassed, b.RowsCut)
		}
		if len(b.Spans) != 2 {
			t.Fatalf("%s: got %d span(s), want 2", lib.Version, len(b.Spans))
		}
		if b.Spans[1] != (Span{}) {
			t.Fatalf("%s: cut row's span = %+v, want the zero span", lib.Version, b.Spans[1])
		}

		passed := b.Payload[b.Spans[0].Off : b.Spans[0].Off+b.Spans[0].Len]
		var fields []json.RawMessage
		if err := json.Unmarshal(passed, &fields); err != nil {
			t.Fatalf("%s: exported row %q is not one JSON array: %v", lib.Version, string(passed), err)
		}
		if len(fields) != 2 {
			t.Fatalf("%s: exported row has %d field(s) (%q), want 2 — [id, tenant], the stored columns in declared order", lib.Version, len(fields), string(passed))
		}
		if string(fields[0]) != "1" || string(fields[1]) != `"acme"` {
			t.Errorf("%s: exported row = %q, want [1,\"acme\"]", lib.Version, string(passed))
		}

		f.Close()
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestRowsExportWithColumnsAndFilterComposeEndToEnd ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestRowsExportWithColumnsAndFilterComposeEndToEnd ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestRowsExportWithColumnsAndFilterComposeEndToEnd: %d line(s)", ran)
}

// ----------------------------------------------------- chtypes#298 (G1-G5)
//
// The artifact producer's relink served at chtypes_build 1790845279 changed
// four document fields by design, measured against that build and against
// the immediately preceding build (1790783214) of the SAME ClickHouse patch
// on darwin-arm64 — same ABI revision, only the library rebuilt:
//
//   - G2: verdict_code/verdict_err are now populated on a non-answered
//     verdict whose row is not itself accepted (measured: 0/"" before,
//     the row's own code/message after).
//   - G3: rows_passed/rows_cut are now 0 for a batch whose own Outcome is
//     not Accepted, even when an individual row before the one that aborted
//     it was itself accepted with a 't' verdict (measured: 1/0 before, 0/0
//     after).
//   - G5: export_declined is now populated for every non-accepted batch
//     that requested an export, including a batch with ZERO rows (measured
//     on a CSVWithNames body naming an unknown header column under
//     input_format_skip_unknown_fields=0: "" before, a reason after).
//
// G4 (a skipped row counts in neither RowsPassed nor RowsCut) has no
// library change — it is a docs-only clarification — so it is not measured
// here; see docs/guides/filters.md.
//
// The fix reached only the SUPPORTED lines' artifacts (docs/support.md):
// 26.3, 26.7, 26.8 and 26.9, at chtypes_build 1790845279 or later. A served,
// unsupported (retired) line gets no new build or ABI revision, ever, so its
// existing artifact keeps the pre-relink behavior permanently — measured:
// 26.6's newest served build, 1790767905, predates this relink and was
// never republished. rev5Libraries also opens such a line (anything ABI
// revision 5+), so these three tests gate on the artifact's OWN
// chtypes_build (relinkedLibraries) rather than a hand-typed line list — a
// hand-typed list goes stale the moment a new supported line ships, and
// silently stops exercising it.

// relinkBuild298 is the chtypes_build the chtypes#298 relink was served at.
// It is the one constant relinkedLibraries is built from: the relink is a
// property of the BUILD, not of which lines happened to be supported the
// day this file was written.
const relinkBuild298 int64 = 1790845279

// isRelinkedBuild is relinkedLibraries' predicate, factored out so it can be
// pinned against fabricated build numbers without a loaded artifact (see
// TestIsRelinkedBuildThreshold). A missing or zero chtypes_build — 0 is what
// a manifest that predates the field reads as — must never be treated as
// relinked.
func isRelinkedBuild(build int64) bool {
	return build >= relinkBuild298
}

// chtypesBuildOf reads lib's own manifest.json for its chtypes_build field,
// the same file and the same place scripts/lib/provenance.py reads it from:
// next to the loaded library. There is no public accessor for this field —
// the loader's own artifactManifest does not carry it — so this reads the
// manifest directly rather than guessing. A manifest that cannot be read or
// parsed fails the helper loudly: this gate must never default a line it
// could not actually measure to "relinked".
func chtypesBuildOf(t *testing.T, lib *Library) int64 {
	t.Helper()
	path := filepath.Join(filepath.Dir(lib.Path), "manifest.json")
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%s: reading %s: %v", lib.Version, path, err)
	}
	var m struct {
		ChtypesBuild int64 `json:"chtypes_build"`
	}
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("%s: %s: %v", lib.Version, path, err)
	}
	return m.ChtypesBuild
}

// relinkedLibraries is rev5Libraries narrowed to the lines whose own
// artifact's chtypes_build is at least relinkBuild298 — the chtypes#298
// relink. A loaded line below that build — a served, unsupported (retired)
// line that will never receive it (docs/support.md), or simply a supported
// line fetched before the relink shipped — is logged and passed over, the
// same verdict every artifact-backed test here gives for a line it cannot
// exercise.
func relinkedLibraries(t *testing.T) []*Library {
	t.Helper()
	all := rev5Libraries(t)
	var libs []*Library
	for _, lib := range all {
		build := chtypesBuildOf(t, lib)
		if !isRelinkedBuild(build) {
			t.Logf("skipping %s at build %d: predates the relink %d", lib.Minor, build, relinkBuild298)
			continue
		}
		libs = append(libs, lib)
	}
	if len(libs) == 0 {
		t.Skipf("registry holds no artifact at chtypes_build %d or later (the chtypes#298 relink) — fetch one with scripts/fetch.sh (docs/guides/fetch.md)", relinkBuild298)
	}
	return libs
}

// TestIsRelinkedBuildThreshold pins isRelinkedBuild's own threshold against
// fabricated build numbers, independent of any loaded artifact: the relink's
// own build is kept, one build short of it is not, and a manifest predating
// chtypes_build (0) is not — it must never read as relinked by default.
func TestIsRelinkedBuildThreshold(t *testing.T) {
	cases := []struct {
		build int64
		want  bool
	}{
		{relinkBuild298, true},
		{relinkBuild298 - 1, false},
		{0, false},
	}
	for _, c := range cases {
		if got := isRelinkedBuild(c.build); got != c.want {
			t.Errorf("isRelinkedBuild(%d) = %v, want %v", c.build, got, c.want)
		}
	}
}

// TestNonAcceptedBatchZeroesCountsAndCarriesVerdictCode is G2 and G3
// together, because the same strict (no input_format_allow_errors_*) body
// exercises both at once: row 0 parses and is individually Accepted with
// verdict 't'; row 1 is the CSV reader's own trailing-garbage refusal (code
// 117), which aborts the batch — so the batch's own Outcome is Rejected
// even though row 0, in isolation, was accepted and passed the filter.
func TestNonAcceptedBatchZeroesCountsAndCarriesVerdictCode(t *testing.T) {
	libs := relinkedLibraries(t)
	ran := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt8, n UInt8")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		f, err := s.CompileFilter("id >= 0")
		if err != nil {
			s.Close()
			t.Fatalf("%s: CompileFilter: %v", lib.Version, err)
		}

		body := []byte("1,2\n1,abc\n") // row 1: "abc" in a UInt8 column — the CSV reader's own refusal
		b, err := s.RowsExportWith(CSV, body, nil, ExportNone, WithRowFilter(f))
		if err != nil {
			f.Close()
			s.Close()
			t.Fatalf("%s: RowsExportWith: %v", lib.Version, err)
		}
		if b.Outcome != Rejected {
			t.Fatalf("%s: batch Outcome = %v, want %v (the aborting row must still reject the whole batch)", lib.Version, b.Outcome, Rejected)
		}
		// G3: the batch's own Outcome is not Accepted, so BOTH counts must be
		// zero — regardless of row 0's own accepted-and-'t' verdict.
		if b.RowsPassed != 0 || b.RowsCut != 0 {
			t.Errorf("%s: RowsPassed/RowsCut = %d/%d, want 0/0 for a batch whose own Outcome (%v) is not Accepted", lib.Version, b.RowsPassed, b.RowsCut, b.Outcome)
		}
		if len(b.Rows) != 2 {
			t.Fatalf("%s: got %d row document(s), want 2", lib.Version, len(b.Rows))
		}
		// G2: row 1's own parse outcome is not Accepted (Rejected, code 117),
		// so its verdict is 'd' and must carry that same code/message beside
		// it — never the 0/"" the pre-relink library left there.
		row1 := b.Rows[1]
		if row1.Verdict == nil || *row1.Verdict != VerdictDecline {
			t.Fatalf("%s: row 1 Verdict = %v, want %v (its own outcome is not Accepted)", lib.Version, row1.Verdict, VerdictDecline)
		}
		if row1.VerdictCode != csvRejectionCode || row1.VerdictErr == "" {
			t.Errorf("%s: row 1 VerdictCode/VerdictErr = %d/%q, want %d/<non-empty> — a non-accepted row's own error, beside its decline verdict", lib.Version, row1.VerdictCode, row1.VerdictErr, csvRejectionCode)
		}
		if row1.VerdictCode != row1.ErrCode {
			t.Errorf("%s: row 1 VerdictCode %d != its own ErrCode %d — the decline must carry the SAME error the row's own Outcome already reports", lib.Version, row1.VerdictCode, row1.ErrCode)
		}

		f.Close()
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestNonAcceptedBatchZeroesCountsAndCarriesVerdictCode ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestNonAcceptedBatchZeroesCountsAndCarriesVerdictCode ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestNonAcceptedBatchZeroesCountsAndCarriesVerdictCode: %d line(s)", ran)
}

// TestRejectedZeroRowBatchNamesTheExportDecline is G5: a CSVWithNames body
// naming a header column no schema column matches, read under
// input_format_skip_unknown_fields=0, is refused before a single row is
// admitted — Rejected, rows_read 0, zero row documents — and an export was
// requested. The pre-relink library left ExportDeclined empty here even
// though bytes were withheld; the relinked one names the batch's own
// outcome as the reason.
func TestRejectedZeroRowBatchNamesTheExportDecline(t *testing.T) {
	libs := relinkedLibraries(t)
	ran := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt8, p String")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		body := []byte("id,unknown_col\n1,a\n")
		b, err := s.RowsExport(CSVWithNames, body, map[string]string{"input_format_skip_unknown_fields": "0"}, JSONCompactEachRow)
		if err != nil {
			s.Close()
			t.Fatalf("%s: RowsExport: %v", lib.Version, err)
		}
		if b.Outcome != Rejected {
			t.Fatalf("%s: Outcome = %v (code %d, %q), want %v", lib.Version, b.Outcome, b.ErrCode, b.ErrMsg, Rejected)
		}
		if b.RowsRead != 0 || len(b.Rows) != 0 {
			t.Fatalf("%s: RowsRead=%d, %d row document(s) — want a call-level refusal with none", lib.Version, b.RowsRead, len(b.Rows))
		}
		if len(b.Payload) != 0 {
			t.Errorf("%s: Payload = %q, want none — a rejected batch exports nothing", lib.Version, string(b.Payload))
		}
		if b.ExportDeclined == "" {
			t.Errorf("%s: ExportDeclined is empty on a rejected zero-row batch that requested an export — a decline must always carry its reason (chtypes#298, G5)", lib.Version)
		}
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestRejectedZeroRowBatchNamesTheExportDecline ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestRejectedZeroRowBatchNamesTheExportDecline ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestRejectedZeroRowBatchNamesTheExportDecline: %d line(s)", ran)
}

// TestSessionTimezoneSettingIsDeclinedNotIgnored is G1: ClickHouse's
// session_timezone is a real, known setting name — never the server's own
// code 115 — but this library resolves bare-DateTime timezone once,
// process-wide, at chs_init, and never re-reads it per call. Before the
// relink, a per-call session_timezone was silently accepted and had no
// effect, indistinguishable from agreement. The relinked library declines
// it the same way an unmodeled MergeTree setting is declined
// (docs/guides/settings.md): it comes back in UnsupportedSettings, which
// promotes the row's own Outcome to Unsupported.
func TestSessionTimezoneSettingIsDeclinedNotIgnored(t *testing.T) {
	libs := relinkedLibraries(t)
	ran := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt8, t DateTime")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		r, err := s.RowWithSettings(JSONEachRow, []byte(`{"id":1,"t":"2024-01-01 00:00:00"}`), map[string]string{"session_timezone": "Europe/Berlin"})
		if err != nil {
			s.Close()
			t.Fatalf("%s: RowWithSettings: %v", lib.Version, err)
		}
		if !containsString(r.UnsupportedSettings, "session_timezone") {
			t.Errorf("%s: UnsupportedSettings = %v, want it to name session_timezone — sent on a per-call map, this is a decline, never a silent admission", lib.Version, r.UnsupportedSettings)
		}
		if r.Outcome != Unsupported {
			t.Errorf("%s: Outcome = %v, want %v — a non-empty UnsupportedSettings must promote the row (docs/guides/settings.md)", lib.Version, r.Outcome, Unsupported)
		}
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestSessionTimezoneSettingIsDeclinedNotIgnored ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestSessionTimezoneSettingIsDeclinedNotIgnored ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestSessionTimezoneSettingIsDeclinedNotIgnored: %d line(s)", ran)
}
