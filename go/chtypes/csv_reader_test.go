package chtypes

// Issue #119, items 3 and 4 (the revision-5 CSV/TSV reader) and item 1 (the
// two revision-5 `src` provenances), executed END TO END against a real
// revision-5 artifact rather than against a hand-built document.
//
// The three blocks below are the artifact-backed twins of tests that already
// exist as pure units: values_skipped_test.go and transform_ephemeral_test.go
// parse a document this file's own assertions never write. A test that
// hand-sets the value the code computes tests the belief, not the
// computation, so everything here comes off a loaded library.
//
// ⚠️ Nothing in this file asserts on an error MESSAGE. With revision-5
// artifacts a rejected CSV or TSV row carries ClickHouse's own wording, which
// changes whenever ClickHouse rewords an error; the CODE is the stable part
// and is the only thing pinned. 15,245 TSV records in the artifact producer's
// corpus differ in the message alone, which is exactly the fragility this
// change exists to warn consumers about.

import (
	"encoding/json"
	"testing"
)

// ---------------------------------------------------------------- the lines
//
// Every block runs against EVERY revision-5 line the test registry holds —
// CI fetches three (the newest -lts, the newest -stable and 24.8) — rather
// than against one resolved by a helper, because none of these behaviors is
// line-sensitive and a silent retarget onto a different ClickHouse would
// hide that. A line whose artifact this platform has not relinked to
// revision 5 refuses to load by design (the loader's own ABI guard); it is
// named in the log and passed over, and the block still has to run something.

// rev5Libraries opens every line of the test registry that loads at ABI
// revision 5. It skips LOUDLY, by name, when there is no registry or no
// revision-5 artifact in it — the same verdict every artifact-backed test
// here gives, since a hosted runner without artifacts must not look green for
// having tested nothing.
func rev5Libraries(t *testing.T) []*Library {
	t.Helper()
	dir := testRegistryDir(t) // skips by name when the registry is empty/absent
	reg, err := NewRegistry(dir)
	if err != nil {
		t.Fatalf("registry %s did not open: %v", dir, err)
	}
	var libs []*Library
	for _, v := range reg.Versions() {
		lib, err := reg.For(Version(v))
		if err != nil {
			// A pre-revision-5 artifact refuses to load through the loader's
			// own revision guard. That is the guard working, not a failure of
			// this test — say so and move on.
			t.Logf("line %s not exercised: %v", v, err)
			continue
		}
		if lib.ABIRevision < 5 {
			t.Logf("line %s not exercised: artifact reports ABI revision %d, these cases need 5", v, lib.ABIRevision)
			continue
		}
		libs = append(libs, lib)
	}
	if len(libs) == 0 {
		t.Skipf("registry %s holds no ABI revision-5 artifact: every case in this file needs one — fetch one with scripts/fetch.sh (docs/guides/fetch.md)", dir)
	}
	return libs
}

// ------------------------------------------- item 3: codes, not messages
//
// A CSV row the library rejects returns the same code as the pre-revision-5
// path, and so does a TSV one; only the wording moved. A rejected CSV row
// also now carries an EMPTY per-column list where the previous reader could
// report a partial one — a consumer reading per-column detail off a rejected
// row sees this. That detail was measured for CSV rejections and is asserted
// for CSV; the TSV row's own emptiness is logged, not pinned.

// The two codes, measured on 24.8, 26.7 and 26.8 (linux-arm64) and on 26.7
// and 26.8 (darwin-arm64), identical on every line: ClickHouse's own
// INCORRECT_DATA for the CSV reader's trailing-garbage refusal and
// CANNOT_PARSE_INPUT_ASSERTION_FAILED for the TSV reader's.
const (
	csvRejectionCode = 117
	tsvRejectionCode = 27
)

func TestCSVAndTSVRejectionsKeepTheirCodes(t *testing.T) {
	libs := rev5Libraries(t)
	cases := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt8, n UInt8")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		// `abc` in the second field: the first field parses, so a reader that
		// reported per-column detail for what it managed to read would report
		// one column here. That is the shape the empty-cols assertion pins.
		for _, c := range []struct {
			name   string
			format Format
			row    []byte
			code   int
			// emptyCols: the measured CSV-scoped claim. Asserted for CSV only.
			emptyCols bool
		}{
			{"CSV", CSV, []byte("1,abc\n"), csvRejectionCode, true},
			{"TSV", TSV, []byte("1\tabc\n"), tsvRejectionCode, false},
		} {
			r, err := s.Row(c.format, c.row)
			if err != nil {
				t.Fatalf("%s %s: Row: %v", lib.Version, c.name, err)
			}
			if r.Outcome != Rejected {
				t.Errorf("%s %s: Outcome = %v, want %v (the row must be refused, not coerced)", lib.Version, c.name, r.Outcome, Rejected)
			}
			// The CODE, and only the code. r.ErrMsg is deliberately not
			// asserted anywhere — it is ClickHouse's own text now.
			if r.ErrCode != c.code {
				t.Errorf("%s %s: ErrCode = %d, want %d (message, not asserted, was %q)", lib.Version, c.name, r.ErrCode, c.code, r.ErrMsg)
			}
			if c.emptyCols {
				if len(r.Values) != 0 {
					t.Errorf("%s %s: a rejected row reported %d per-column value(s), want 0 — the revision-5 reader reports no partial column list for a row it refused: %+v", lib.Version, c.name, len(r.Values), r.Values)
				}
				if len(r.Transformed) != 0 {
					t.Errorf("%s %s: a rejected row reported %d transform(s), want 0 — there is no stored row to have changed: %+v", lib.Version, c.name, len(r.Transformed), r.Transformed)
				}
			} else {
				t.Logf("%s %s rejected row carries %d value(s) (not pinned: the empty-cols measurement is CSV-scoped)", lib.Version, c.name, len(r.Values))
			}
			cases++

			// The same refusal through the batch path, where an allowed error
			// budget turns the refusal into a skipped row rather than a
			// rejected batch. Same code, again by code only.
			b, err := s.Rows(c.format, c.row, map[string]string{"input_format_allow_errors_num": "10"})
			if err != nil {
				t.Fatalf("%s %s: Rows: %v", lib.Version, c.name, err)
			}
			if len(b.Rows) != 1 {
				t.Fatalf("%s %s: got %d row documents, want 1", lib.Version, c.name, len(b.Rows))
			}
			if b.Rows[0].Outcome != Skipped {
				t.Errorf("%s %s: batch row Outcome = %v, want %v", lib.Version, c.name, b.Rows[0].Outcome, Skipped)
			}
			if b.Rows[0].ErrCode != c.code {
				t.Errorf("%s %s: batch row ErrCode = %d, want %d (message, not asserted, was %q)", lib.Version, c.name, b.Rows[0].ErrCode, c.code, b.Rows[0].ErrMsg)
			}
			if c.emptyCols && len(b.Rows[0].Values) != 0 {
				t.Errorf("%s %s: a skipped batch row reported %d per-column value(s), want 0", lib.Version, c.name, len(b.Rows[0].Values))
			}
			cases++
		}
		s.Close()
	}
	// The count assertion. A block that exercised a library and then ran
	// nothing must say so by name rather than pass: four cases per line is
	// the whole of this block, and zero is a broken test, not a green one.
	if want := 4 * len(libs); cases != want {
		t.Fatalf("TestCSVAndTSVRejectionsKeepTheirCodes ran %d case(s) over %d line(s), want %d", cases, len(libs), want)
	}
	if cases == 0 {
		t.Fatalf("TestCSVAndTSVRejectionsKeepTheirCodes ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestCSVAndTSVRejectionsKeepTheirCodes: %d cases over %d line(s)", cases, len(libs))
}

// -------------------------------- item 4: the empty CSV field under `=0`
//
// An empty CSV field under input_format_defaults_for_omitted_fields=0 is
// reported as an INPUT and its coercion as a TRANSFORM, where the previous
// reader reported no transform: the field's reference value is the empty
// string rather than absent. The STORED VALUE is unchanged — this changes
// what is reported, not what is stored.
//
// ⚠️ The setting is set explicitly on every call. Under the default (`1`) a
// bare empty field still takes the column's DEFAULT, so a case that forgot
// the setting would pass without ever exercising this.
//
// ⚠️ The three reasons below are WHAT WAS MEASURED, not a closed set. The
// detector's switch would assign date_clamp, ip_mangle and others for other
// column types, and nothing has measured whether an empty field reaches
// them. Nothing here asserts that the set is exactly three, and a fourth
// column type arriving must not make this test red.
//
// ⚠️ TSV is NOT affected by the setting. Its reader takes an empty field
// through the typed parse whether the setting is on or off, exactly as the
// previous splitter did, so what is pinned for TSV is that the two settings
// give the IDENTICAL answer — that is the asymmetry, and a later change
// making TSV setting-sensitive like CSV turns it red. Note, measured: TSV
// does report fixedstring_pad for a FixedString empty field under BOTH
// settings, and has always done so; "TSV is not affected" means the reader
// swap did not change TSV's answer, never that TSV reports no transform at
// all. Do not "fix" that by asserting TSV reports nothing.
func TestEmptyCSVFieldUnderDefaultsZeroReportsAnInputAndATransform(t *testing.T) {
	libs := rev5Libraries(t)

	type emptyFieldCase struct {
		name string
		ddl  string
		// reason is the transform reason MEASURED for this column type when a
		// CSV empty field arrives under `=0`. Not a claim about any other
		// column type.
		reason string
	}
	// The Enum carries a member at value 0: an empty field is not a member
	// spelling, the reader's zero is, and the coercion between the two is
	// what enum_coerce names.
	cases := []emptyFieldCase{
		{"Enum8", "id UInt8, c Enum8('a' = 0, 'b' = 1)", ReasonEnumCoerce},
		{"FixedString", "id UInt8, c FixedString(4)", ReasonFixedStringPad},
		{"UUID", "id UInt8, c UUID", ReasonUUIDMangle},
	}

	const (
		csvRow = "1,\n"
		tsvRow = "1\t\n"
	)
	defaultsOff := map[string]string{"input_format_defaults_for_omitted_fields": "0"}
	defaultsOn := map[string]string{"input_format_defaults_for_omitted_fields": "1"}

	// reasonsOn returns the reasons reported for column `c`, and the text
	// stored for it ("" when the row carried no value for it at all).
	reasonsFor := func(t *testing.T, res RowResult) (reasons []string, stored string, found bool) {
		t.Helper()
		for _, tr := range res.Transformed {
			if tr.Column == "c" {
				reasons = append(reasons, tr.Reason)
			}
		}
		for _, v := range res.Values {
			if v.Column == "c" {
				stored, found = v.Text, true
			}
		}
		return
	}

	ran := 0
	for _, lib := range libs {
		for _, c := range cases {
			s, err := lib.CompileDDL(c.ddl)
			if err != nil {
				t.Fatalf("%s %s: compile: %v", lib.Version, c.name, err)
			}

			off, err := s.RowWithSettings(CSV, []byte(csvRow), defaultsOff)
			if err != nil {
				t.Fatalf("%s %s: CSV =0: %v", lib.Version, c.name, err)
			}
			on, err := s.RowWithSettings(CSV, []byte(csvRow), defaultsOn)
			if err != nil {
				t.Fatalf("%s %s: CSV =1: %v", lib.Version, c.name, err)
			}
			offReasons, offStored, offFound := reasonsFor(t, off)
			onReasons, onStored, onFound := reasonsFor(t, on)

			// The empty field is reported as an INPUT: it is in the stored
			// row, with src `input` — not absent, not a default.
			if !offFound {
				t.Fatalf("%s %s: CSV =0 reported no value at all for the empty field, want one with src %q", lib.Version, c.name, "input")
			}
			for _, v := range off.Values {
				if v.Column == "c" && v.Source != "input" {
					t.Errorf("%s %s: CSV =0 value source = %q, want %q — the empty field is an input, not an omitted column", lib.Version, c.name, v.Source, "input")
				}
			}

			// ... and its coercion as a TRANSFORM, with the measured reason.
			if !containsString(offReasons, c.reason) {
				t.Errorf("%s %s: CSV =0 reported reasons %v, want %q among them", lib.Version, c.name, offReasons, c.reason)
			}
			// Under the default the same field reports no such transform —
			// which is what makes the setting load-bearing rather than
			// decorative. (Not "no transforms at all": that would be a claim
			// about column types nobody measured.)
			if containsString(onReasons, c.reason) {
				t.Errorf("%s %s: CSV =1 reported %q too — under the default an empty field still takes the column's DEFAULT and this reason must not appear, or the =0 case proves nothing", lib.Version, c.name, c.reason)
			}

			// THE STORED VALUE IS UNCHANGED. This changes what is reported,
			// not what is stored.
			if !onFound || offStored != onStored {
				t.Errorf("%s %s: stored value differs between =0 (%q) and =1 (%q, present=%v) — this change is about what is REPORTED, never about what is stored", lib.Version, c.name, offStored, onStored, onFound)
			}

			// TSV: the setting reaches nothing. The two answers must be the
			// same answer, whatever that answer is.
			tsvOff, err := s.RowWithSettings(TSV, []byte(tsvRow), defaultsOff)
			if err != nil {
				t.Fatalf("%s %s: TSV =0: %v", lib.Version, c.name, err)
			}
			tsvOn, err := s.RowWithSettings(TSV, []byte(tsvRow), defaultsOn)
			if err != nil {
				t.Fatalf("%s %s: TSV =1: %v", lib.Version, c.name, err)
			}
			tsvOffReasons, tsvOffStored, tsvOffFound := reasonsFor(t, tsvOff)
			tsvOnReasons, tsvOnStored, tsvOnFound := reasonsFor(t, tsvOn)
			if tsvOff.Outcome != tsvOn.Outcome || tsvOff.ErrCode != tsvOn.ErrCode {
				t.Errorf("%s %s: TSV answered =0 as %v/%d and =1 as %v/%d — TSV's reader takes an empty field through the typed parse whether the setting is on or off; a difference here means TSV has started behaving like CSV", lib.Version, c.name, tsvOff.Outcome, tsvOff.ErrCode, tsvOn.Outcome, tsvOn.ErrCode)
			}
			if !sameStrings(tsvOffReasons, tsvOnReasons) {
				t.Errorf("%s %s: TSV reported %v under =0 and %v under =1 — the setting must make NO difference to TSV; the CSV-only transform is the asymmetry this pins", lib.Version, c.name, tsvOffReasons, tsvOnReasons)
			}
			if tsvOffFound != tsvOnFound || tsvOffStored != tsvOnStored {
				t.Errorf("%s %s: TSV stored %q (present=%v) under =0 and %q (present=%v) under =1 — the setting must make no difference to TSV", lib.Version, c.name, tsvOffStored, tsvOffFound, tsvOnStored, tsvOnFound)
			}
			t.Logf("%s %-11s CSV =0 %v / =1 %v   TSV =0 %v / =1 %v", lib.Version, c.name, offReasons, onReasons, tsvOffReasons, tsvOnReasons)

			s.Close()
			ran++
		}
	}
	if want := len(cases) * len(libs); ran != want {
		t.Fatalf("TestEmptyCSVFieldUnderDefaultsZeroReportsAnInputAndATransform ran %d column type(s) over %d line(s), want %d", ran, len(libs), want)
	}
	if ran == 0 {
		t.Fatalf("TestEmptyCSVFieldUnderDefaultsZeroReportsAnInputAndATransform ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestEmptyCSVFieldUnderDefaultsZeroReportsAnInputAndATransform: %d cases over %d line(s)", ran, len(libs))
}

// ------------------------------------- item 1: the two `src` provenances
//
// A listed EPHEMERAL column is READ — it is in scope for the DEFAULT
// expressions that reference it — and is never stored and never exported. A
// listed MATERIALIZED column under insert_allow_materialized_columns=1 has
// its supplied value REPLACE the column's expression and IS stored.
//
// The plant that makes this a test rather than a hope: delete
// `|| c.Src == SourceEphemeralInput` from rowResultOf's Values loop
// (chtypes.go) and TestEphemeralInputIsReadNeverStoredNeverExported goes red
// on its "Values must not contain e" assertion, naming the source the
// artifact really reported.
func TestEphemeralInputIsReadNeverStoredNeverExported(t *testing.T) {
	libs := rev5Libraries(t)
	ran := 0
	for _, lib := range libs {
		// e is EPHEMERAL: it has no value at all outside a column list, and d
		// reads it. d == 6 is the proof the value was read.
		s, err := lib.CompileDDL("id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		r, err := s.Row(JSONEachRow, []byte(`{"id":3,"e":5}`), WithColumns([]string{"id", "e"}))
		if err != nil {
			t.Fatalf("%s: Row: %v", lib.Version, err)
		}
		if r.Outcome != Accepted {
			t.Fatalf("%s: Outcome = %v (code %d), want %v", lib.Version, r.Outcome, r.ErrCode, Accepted)
		}
		seen := map[string]string{} // column -> src
		for _, v := range r.Values {
			seen[v.Column] = v.Source
		}
		if got := valueText(r, "d"); got != "6" {
			t.Errorf("%s: d = %q, want \"6\" — a listed EPHEMERAL column's value must reach the DEFAULT expressions that reference it", lib.Version, got)
		}
		if src, ok := seen["e"]; ok {
			t.Errorf("%s: Values contains e with src %q — a listed EPHEMERAL column is never stored, so it must never sit where a caller reads the stored row", lib.Version, src)
		}
		for col, src := range seen {
			if src == SourceEphemeralInput {
				t.Errorf("%s: Values contains %s with src %q — %q is excluded from the stored-row view exactly as %q is", lib.Version, col, src, SourceEphemeralInput, SourceSkipped)
			}
		}

		// ... and never EXPORTED. The exported tuple is the wire tuple —
		// declared order minus MATERIALIZED/ALIAS/EPHEMERAL — so the row is
		// [id, d] and the ephemeral 5 is nowhere in it. Parsed rather than
		// string-compared: the separator spacing is the vendored writer's,
		// not this repository's, and pinning it would test the wrong thing.
		b, err := s.RowsExport(JSONEachRow, []byte(`{"id":3,"e":5}`+"\n"), nil, JSONCompactEachRow, WithColumns([]string{"id", "e"}))
		if err != nil {
			t.Fatalf("%s: RowsExport: %v", lib.Version, err)
		}
		if b.Outcome != Accepted {
			t.Fatalf("%s: export outcome = %v, declined %q", lib.Version, b.Outcome, b.ExportDeclined)
		}
		var fields []json.RawMessage
		if err := json.Unmarshal(b.Payload, &fields); err != nil {
			t.Fatalf("%s: exported row %q is not one JSON array: %v", lib.Version, string(b.Payload), err)
		}
		if len(fields) != 2 {
			t.Fatalf("%s: exported row has %d field(s) (%q), want 2 — the wire tuple is declared order minus MATERIALIZED/ALIAS/EPHEMERAL", lib.Version, len(fields), string(b.Payload))
		}
		if string(fields[0]) != "3" || string(fields[1]) != "6" {
			t.Errorf("%s: exported row = %q, want the id and the computed d", lib.Version, string(b.Payload))
		}
		for i, f := range fields {
			if string(f) == "5" {
				t.Errorf("%s: exported field %d is the EPHEMERAL value 5 — a listed EPHEMERAL column is never exported (%q)", lib.Version, i, string(b.Payload))
			}
		}
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestEphemeralInputIsReadNeverStoredNeverExported ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestEphemeralInputIsReadNeverStoredNeverExported ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestEphemeralInputIsReadNeverStoredNeverExported: %d line(s)", ran)
}

// TestMaterializedInputIsStoredAndStaysInValues is the other half: the two
// provenances get OPPOSITE treatment, and folding them together is the
// mistake this guards.
//
// ⚠️ The supplied value must be the stored one: `m Int64 MATERIALIZED id +
// 10` with id = 1 would compute 11, and 99 is what was sent. Reading 11 here
// would mean the supplied value never replaced the expression.
//
// ⚠️ The export channel DECLINES this row, loudly, and that is the C ABI
// contract's own rule rather than a defect: the exported tuple is the wire
// tuple (declared order minus MATERIALIZED/ALIAS/EPHEMERAL), which has no
// position for a MATERIALIZED column, so bytes that carried the supplied
// value could not be re-INSERTed. Fail-closed with the reason in
// ExportDeclined is what the header specifies, and it is asserted here so a
// later silent emission would show up.
func TestMaterializedInputIsStoredAndStaysInValues(t *testing.T) {
	libs := rev5Libraries(t)
	allow := map[string]string{"insert_allow_materialized_columns": "1"}
	ran := 0
	for _, lib := range libs {
		s, err := lib.CompileDDL("id UInt32, p UInt8 DEFAULT 3, m Int64 MATERIALIZED id + 10")
		if err != nil {
			t.Fatalf("%s: compile: %v", lib.Version, err)
		}
		r, err := s.RowWithSettings(JSONEachRow, []byte(`{"id":1,"m":99}`), allow, WithColumns([]string{"id", "m"}))
		if err != nil {
			t.Fatalf("%s: Row: %v", lib.Version, err)
		}
		if r.Outcome != Accepted {
			t.Fatalf("%s: Outcome = %v (code %d, %q), want %v", lib.Version, r.Outcome, r.ErrCode, r.ErrMsg, Accepted)
		}
		var src, text string
		found := false
		for _, v := range r.Values {
			if v.Column == "m" {
				src, text, found = v.Source, v.Text, true
			}
		}
		if !found {
			t.Fatalf("%s: Values has no m — a listed MATERIALIZED column's supplied value IS stored and stays IN the stored-row view, unlike %q", lib.Version, SourceEphemeralInput)
		}
		if src != SourceMaterializedInput {
			t.Errorf("%s: m src = %q, want %q", lib.Version, src, SourceMaterializedInput)
		}
		// ⚠️ 24.8 renders a stored Int64 as a JSON string and 25.8/26.8 as a
		// number — ClickHouse's own 64-bit quoting changing between lines,
		// which belongs to the artifact and is never normalized here. Both
		// spellings are the same stored value; neither is 11.
		if text != "99" && text != `"99"` {
			t.Errorf("%s: m = %q, want the SUPPLIED 99 (as a number or, on a line that quotes 64-bit integers, as \"99\") — not the expression's 11", lib.Version, text)
		}
		// It is also reported as a computed column, because that is what it
		// is: durable, and absent from SELECT *.
		computed := ""
		for _, c := range r.Computed {
			if c.Column == "m" {
				computed = c.Text
			}
		}
		if computed != text {
			t.Errorf("%s: Computed m = %q, Values m = %q — the same stored value, reported twice", lib.Version, computed, text)
		}

		// The export channel's documented refusal.
		b, err := s.RowsExport(JSONEachRow, []byte(`{"id":1,"m":99}`+"\n"), allow, JSONCompactEachRow, WithColumns([]string{"id", "m"}))
		if err != nil {
			t.Fatalf("%s: RowsExport: %v", lib.Version, err)
		}
		if len(b.Payload) != 0 {
			t.Errorf("%s: export emitted %q — the wire tuple has no position for a MATERIALIZED column, so bytes carrying the supplied value could not be re-INSERTed; the contract is fail-closed", lib.Version, string(b.Payload))
		}
		if b.ExportDeclined == "" {
			t.Errorf("%s: export withheld its bytes without saying why — a decline carries its reason", lib.Version)
		}
		s.Close()
		ran++
	}
	if ran != len(libs) {
		t.Fatalf("TestMaterializedInputIsStoredAndStaysInValues ran %d line(s), want %d", ran, len(libs))
	}
	if ran == 0 {
		t.Fatalf("TestMaterializedInputIsStoredAndStaysInValues ran ZERO cases — a block that asserts nothing is not a pass")
	}
	t.Logf("TestMaterializedInputIsStoredAndStaysInValues: %d line(s)", ran)
}

// ------------------------------------------------------------- tiny helpers

func containsString(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func valueText(r RowResult, column string) string {
	for _, v := range r.Values {
		if v.Column == column {
			return v.Text
		}
	}
	return ""
}
