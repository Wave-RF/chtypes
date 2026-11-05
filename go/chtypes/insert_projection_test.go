package chtypes

// insert_projection_test.go — the standing projection gate of Schema.Insert:
// the rule spec/abi-v2/docs.md (chs_preview_insert, "The projection rule")
// states between an insert_result and the batch document for the same inputs,
// run over every case of tests/fixtures/abi-v2/insert-projection.json. Both
// documents of a case are read by the package's real decoders, never as raw
// JSON, so a case also proves the decoders. projectionVerdict mirrors
// scripts/abi-v2/check-insert-projection.py rule by rule and in the same order.
// A case's batch_of_t_rows, the batch over the rows the insert keeps, goes
// through the same decoder; whether it is accepted decides which of the two
// post-WHERE rules the case is held to.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"testing"
)

var projectionRules = []string{"e_row_rejects", "export_decline_is_call_decline", "post_where_cut", "post_where_refusal",
	"shared_members", "counts", "err_prefix", "err_suffix", "output_presence", "non_t_row", "line_output", "native_output"}

var insertNever = []string{"export_declined", "export_declined_b64", "export_declined_reason"}

// projectionPWC is a case's batch_of_t_rows, decoded: the indexes of the rows
// the insert keeps, and the batch over those records alone with its export.
type projectionPWC struct {
	Rows    []int
	Batch   BatchResult
	Export  []byte
	Records []Span // the case's body_records: one span per body record, in body order
	Failing int    // the case's failing_t_record: the first failing kept record's body index; -1 where the t rows' batch is accepted
}

// sameRecords: the t rows' batch reads exactly the listed records, in order,
// one row each, each input_span as long as its body record, none skipped. A
// batch refused at a row stops reading there, so then its rows end at the
// refused row.
func sameRecords(pwc *projectionPWC) bool {
	idx, sub := pwc.Rows, pwc.Batch
	n := len(idx)
	if sub.Outcome == Rejected && sub.RefusedAt == RefusedAtRow && sub.RefusedRow != nil && *sub.RefusedRow < uint64(n) {
		n = int(*sub.RefusedRow) + 1
	}
	if len(sub.Rows) != n || sub.RowsRead != uint64(n) || sub.RowsSkipped != 0 {
		return false
	}
	for j, r := range sub.Rows {
		if r.InputSpan == nil || r.InputSpan.Len != pwc.Records[idx[j]].Len {
			return false
		}
	}
	return true
}

func postWhereSite(s RefusalSite) bool { return s == RefusedAtRow || s == RefusedAtBodyStep }

// postWhereRefusal mirrors the checker's _post_where_refusal: the insert is
// rejected with the code of both batches and the t rows' batch's err, and its
// rows_read is exactly the failing kept record's body index + 1.
func postWhereRefusal(batch BatchResult, ins InsertResult, insRaw map[string]json.RawMessage, output []byte, pwc *projectionPWC) bool {
	sub := pwc.Batch
	if batch.Outcome != Rejected || !postWhereSite(batch.RefusedAt) || !sameRecords(pwc) {
		return false
	}
	if sub.Outcome != Rejected || !postWhereSite(sub.RefusedAt) {
		return false
	}
	if len(pwc.Rows) == 0 || (sub.RefusedAt == RefusedAtRow && (sub.RefusedRow == nil || *sub.RefusedRow >= uint64(len(pwc.Rows)))) {
		return false
	}
	if ins.Outcome != Rejected {
		return false
	}
	for _, k := range insertNever {
		if _, has := insRaw[k]; has {
			return false
		}
	}
	// the outcome class and code stay equal, to the whole batch and to the t rows' batch
	if ins.ErrCode != batch.ErrCode || ins.ErrCode != sub.ErrCode {
		return false
	}
	// err: the server's for the first failing kept row, numbered over the kept rows
	if ins.ErrMsg != sub.ErrMsg {
		return false
	}
	if batch.Reason != ins.Reason || batch.RefusedAt != ins.RefusedAt || batch.RowsSkipped != ins.RowsSkipped ||
		!slices.Equal(batch.UnsupportedSettings, ins.UnsupportedSettings) || !reflect.DeepEqual(batch.Framing, ins.Framing) ||
		!reflect.DeepEqual(batch.PartitionCount, ins.PartitionCount) {
		return false
	}
	// rows_read: exact. A refused insert, like the batch, stops at its refusing record, so
	// it has read through the first failing kept record, whose body index the case carries
	if pwc.Failing < 0 || ins.RowsRead != uint64(pwc.Failing)+1 {
		return false
	}
	return isZero(ins.RowsPassed) && isZero(ins.RowsCut) && isZero(ins.RowsUndecided) &&
		insRaw["export_columns"] == nil && output == nil
}

// postWhereCut mirrors the checker's _post_where_cut: ok is false when the
// pair does not hold, else (kind, name) is the verdict.
func postWhereCut(batch BatchResult, ins InsertResult, insRaw map[string]json.RawMessage, output []byte, pwc *projectionPWC) (kind, name string, ok bool) {
	if pwc == nil { // an accepted insert over a refused body, with no t rows' batch
		return "", "", false
	}
	idx, sub := pwc.Rows, pwc.Batch
	if batch.Outcome != Rejected || !postWhereSite(batch.RefusedAt) {
		return "", "", false
	}
	if batch.RefusedAt == RefusedAtRow && batch.RefusedRow != nil && slices.Contains(idx, int(*batch.RefusedRow)) {
		return "", "", false
	}
	// the rows the batch read: it stops at its refusal, so a kept row past it is not among them
	for _, i := range idx {
		if i < len(batch.Rows) && batch.Rows[i].Outcome != Accepted {
			return "", "", false
		}
	}
	n := uint64(len(idx))
	if !sameRecords(pwc) {
		return "", "", false
	}
	for _, r := range sub.Rows {
		if r.Verdict == nil || string(*r.Verdict) != "t" {
			return "", "", false
		}
	}
	// the t rows' batch's rows_passed is the number of t rows, which the insert's equals
	if sub.RowsPassed != n {
		return "", "", false
	}
	if sub.Outcome != Accepted || sub.ExportDeclined != "" || sub.ExportDeclinedReason != "" {
		return "", "", false
	}
	for _, k := range insertNever {
		if _, has := insRaw[k]; has {
			return "", "", false
		}
	}
	// the t rows' insert decides these; the whole body's read decides the rest
	if sub.Outcome != ins.Outcome || sub.ErrCode != ins.ErrCode || sub.Reason != ins.Reason || sub.RefusedAt != ins.RefusedAt ||
		(sub.ExportColumns == nil) != (insRaw["export_columns"] == nil) || !slices.Equal(sub.ExportColumns, ins.ExportColumns) ||
		!reflect.DeepEqual(sub.PartitionCount, ins.PartitionCount) {
		return "", "", false
	}
	if batch.RowsSkipped != ins.RowsSkipped ||
		!slices.Equal(batch.UnsupportedSettings, ins.UnsupportedSettings) || !reflect.DeepEqual(batch.Framing, ins.Framing) {
		return "", "", false
	}
	// the insert read the whole body, which the batch, stopped at its refusal, did not
	if ins.RowsRead != uint64(len(pwc.Records)) || ins.RowsRead < ins.RowsSkipped+n {
		return "", "", false
	}
	cut := ins.RowsRead - ins.RowsSkipped - n
	if !isCount(ins.RowsPassed, n) || !isCount(ins.RowsCut, cut) || ins.RowsUndecided == nil || *ins.RowsUndecided > cut {
		return "", "", false
	}
	if ins.ErrMsg != sub.ErrMsg {
		return "", "", false
	}
	if (pwc.Export == nil) != (output == nil) {
		return "", "", false
	}
	if output == nil {
		return "equal", "", true
	}
	for _, r := range sub.Rows {
		for _, c := range r.Columns {
			if c.Source == "default_generated" {
				return "exception", "generator_draw", true
			}
		}
	}
	// every row of the t rows' batch is t, so its whole export is the t rows' output
	if bytes.Equal(output, pwc.Export) {
		return "equal", "", true
	}
	return "", "", false
}

var projectionSuffix = regexp.MustCompile(`^: \(at row [0-9]+\)\n$`)

// projectionVerdict returns ("equal", ""), ("exception", name) or
// ("violates", rule). batchExport is the batch's export bytes (nil when no
// export ran) and output the insert's output bytes (nil when none exists); they
// are passed apart from the decoded results because the public InsertResult
// keeps an output only for an accepted call, and the rule must see what the
// library handed back. insRaw is the insert document's raw members: the public
// InsertResult has no export_declined member, so a document that carries one is
// seen there. pwc is the case's batch_of_t_rows, nil when it carries none.
func projectionVerdict(outFormat string, batch BatchResult, batchExport []byte, ins InsertResult, insRaw map[string]json.RawMessage, output []byte, pwc *projectionPWC) (string, string) {
	verdicts := make([]string, len(batch.Rows))
	for i, r := range batch.Rows {
		if r.Verdict != nil {
			verdicts[i] = string(*r.Verdict)
		}
	}
	count := func(v string) uint64 {
		var n uint64
		for _, x := range verdicts {
			if x == v {
				n++
			}
		}
		return n
	}
	// BatchResult models rows_passed and rows_cut as plain numbers, so the batch
	// carries a filter's counts exactly when a row has a verdict or a count is
	// nonzero (a filtered batch of no rows reads as unfiltered).
	filtered := batch.RowsPassed != 0 || batch.RowsCut != 0
	for _, v := range verdicts {
		if v != "" {
			filtered = true
		}
	}

	// e_row_rejects
	if batch.Outcome == Accepted && count("e") > 0 {
		first := batch.Rows[slices.Index(verdicts, "e")]
		if !(ins.Outcome == Rejected && ins.ErrCode == first.VerdictCode && ins.ErrMsg == first.VerdictErr &&
			ins.RefusedAt == RefusedAtRow && isZero(ins.RowsPassed) && isZero(ins.RowsCut) && isZero(ins.RowsUndecided) &&
			ins.RowsRead == batch.RowsRead && output == nil) {
			return "violates", "e_row_rejects"
		}
		return "equal", ""
	}

	// export_decline_is_call_decline
	declined := batch.Outcome == Accepted && batch.ExportDeclined != ""
	if declined {
		if !(ins.Outcome == Unsupported && ins.Reason == batch.ExportDeclinedReason && ins.ErrCode == CodeDeclined &&
			ins.RefusedAt == RefusedAtBodyStep && insRaw["export_columns"] == nil && output == nil &&
			ins.ErrMsg == batch.ExportDeclined &&
			(!filtered || (isZero(ins.RowsPassed) && isZero(ins.RowsCut) && isZero(ins.RowsUndecided)))) {
			return "violates", "export_decline_is_call_decline"
		}
	}

	// post_where_cut and post_where_refusal split on the t rows' batch, accepted or refused
	if pwc != nil && pwc.Batch.Outcome != Accepted {
		if postWhereRefusal(batch, ins, insRaw, output, pwc) {
			return "equal", ""
		}
		return "violates", "post_where_refusal"
	}
	if pwc != nil || (filtered && batch.Outcome == Rejected && ins.Outcome == Accepted) {
		if kind, name, ok := postWhereCut(batch, ins, insRaw, output, pwc); ok {
			return kind, name
		}
		return "violates", "post_where_cut"
	}

	// shared_members
	for _, k := range insertNever {
		if _, ok := insRaw[k]; ok {
			return "violates", "shared_members"
		}
	}
	eqCount := func(batchValue uint64, p *uint64) bool {
		if !filtered {
			return p == nil
		}
		return p != nil && *p == batchValue
	}
	if batch.RowsRead != ins.RowsRead || batch.RowsSkipped != ins.RowsSkipped ||
		!slices.Equal(batch.UnsupportedSettings, ins.UnsupportedSettings) ||
		!reflect.DeepEqual(batch.Framing, ins.Framing) || !reflect.DeepEqual(batch.PartitionCount, ins.PartitionCount) {
		return "violates", "shared_members"
	}
	if !declined {
		// A declined output replaces these with the insert's own values.
		if batch.Outcome != ins.Outcome || batch.ErrCode != ins.ErrCode || batch.Reason != ins.Reason ||
			batch.RefusedAt != ins.RefusedAt ||
			!eqCount(batch.RowsPassed, ins.RowsPassed) || !eqCount(batch.RowsCut, ins.RowsCut) ||
			(batch.ExportColumns == nil) != (insRaw["export_columns"] == nil) || !slices.Equal(batch.ExportColumns, ins.ExportColumns) {
			return "violates", "shared_members"
		}
	}

	// counts
	t, f, d := count("t"), count("f"), count("d")
	if filtered {
		var good bool
		if batch.Outcome == Accepted {
			good = batch.RowsPassed == t && batch.RowsCut == f+d
		} else {
			good = batch.RowsPassed == 0 && batch.RowsCut == 0
		}
		// #t, #f + #d and #d on an accepted call, 0 on any other outcome.
		wantPassed, wantCut, wantUndecided := uint64(0), uint64(0), uint64(0)
		if ins.Outcome == Accepted {
			wantPassed, wantCut, wantUndecided = t, f+d, d
		}
		if !(good && isCount(ins.RowsPassed, wantPassed) && isCount(ins.RowsCut, wantCut) && isCount(ins.RowsUndecided, wantUndecided)) {
			return "violates", "counts"
		}
	} else if ins.RowsPassed != nil || ins.RowsCut != nil || ins.RowsUndecided != nil {
		return "violates", "counts"
	}

	// err: the batch's, plus a remainder that is empty or the row-reader suffix
	// (a declined output's err is the decline text, checked above)
	if !declined {
		if !bytes.HasPrefix([]byte(ins.ErrMsg), []byte(batch.ErrMsg)) {
			return "violates", "err_prefix"
		}
		if rest := ins.ErrMsg[len(batch.ErrMsg):]; rest != "" && !projectionSuffix.MatchString(rest) {
			return "violates", "err_suffix"
		}
	}

	// output
	if (batch.Payload == nil) != (output == nil) {
		return "violates", "output_presence"
	}
	if output == nil {
		return "equal", ""
	}
	for _, r := range batch.Rows {
		for _, c := range r.Columns {
			if c.Source == "default_generated" {
				return "exception", "generator_draw"
			}
		}
	}
	if outFormat == "Native" {
		if bytes.Equal(output, batchExport) {
			return "equal", ""
		}
		return "violates", "native_output"
	}
	var want []byte
	var tLines int
	for i, s := range batch.Spans {
		if verdicts[i] == "" || verdicts[i] == "t" {
			if s != nil {
				want = append(want, batchExport[s.Off:s.Off+s.Len]...)
			}
			tLines++
		}
	}
	// A non-t row's span is null, so a stray row is seen as a line beyond the t rows.
	if countLines(output) > tLines {
		return "violates", "non_t_row"
	}
	if bytes.Equal(output, want) {
		return "equal", ""
	}
	return "violates", "line_output"
}

func isCount(p *uint64, want uint64) bool { return p != nil && *p == want }

func isZero(p *uint64) bool { return isCount(p, 0) }

// countLines counts lines as Python's bytes.splitlines does: \n, \r and \r\n end one.
func countLines(b []byte) int {
	n := 0
	for i := 0; i < len(b); i++ {
		if b[i] == '\r' && i+1 < len(b) && b[i+1] == '\n' {
			i++
		}
		if b[i] == '\n' || b[i] == '\r' {
			n++
			if i+1 == len(b) {
				return n
			}
			continue
		}
		if i+1 == len(b) {
			n++
		}
	}
	return n
}

type projectionCase struct {
	ID             string          `json:"id"`
	OutputFormat   string          `json:"output_format"`
	Batch          json.RawMessage `json:"batch"`
	BatchExportB64 *string         `json:"batch_export_b64"`
	Insert         json.RawMessage `json:"insert"`
	InsertRowsB64  *string         `json:"insert_rows_b64"`
	Expect         json.RawMessage `json:"expect"`
	BatchOfTRows   *struct {
		Rows      []int           `json:"rows"`
		Batch     json.RawMessage `json:"batch"`
		ExportB64 *string         `json:"export_b64"`
	} `json:"batch_of_t_rows"`
	BodyRecords    []map[string]json.RawMessage `json:"body_records"`
	FailingTRecord json.RawMessage              `json:"failing_t_record"`
	Provenance     map[string]json.RawMessage   `json:"provenance"`
}

// bodyRecords reads a case's body_records: each exactly {off, len}, integers,
// in body order and not overlapping, and every row the whole batch read
// carrying its record's span. A malformed one is a fixture error.
func bodyRecords(t *testing.T, c projectionCase, batch BatchResult) []Span {
	t.Helper()
	var recs []Span
	var end uint64
	for i, r := range c.BodyRecords {
		var sp Span
		if len(r) != 2 || json.Unmarshal(r["off"], &sp.Off) != nil || json.Unmarshal(r["len"], &sp.Len) != nil {
			t.Fatalf("body_records[%d] is not {off, len} with integer members", i)
		}
		if sp.Len < 1 || sp.Off < end {
			t.Fatalf("body_records[%d] is empty or overlaps the record before it", i)
		}
		end = sp.Off + sp.Len
		recs = append(recs, sp)
	}
	if len(batch.Rows) > len(recs) || batch.RowsRead > uint64(len(recs)) {
		t.Fatalf("the batch read more rows than body_records holds")
	}
	for i, row := range batch.Rows {
		if row.InputSpan == nil || *row.InputSpan != recs[i] {
			t.Fatalf("batch row %d's input_span is not body_records[%d]", i, i)
		}
	}
	return recs
}

// checkProvenance: provenance names exactly the case's documents, each
// recorded ({build, line}) or marked "unrecorded" or "planted". A malformed
// one is a fixture error.
func checkProvenance(t *testing.T, c projectionCase) {
	t.Helper()
	if c.Provenance == nil {
		if c.BatchOfTRows != nil {
			t.Fatalf("batch_of_t_rows without provenance")
		}
		return
	}
	want := []string{"batch", "insert"}
	if c.BatchOfTRows != nil {
		want = append(want, "batch_of_t_rows")
	}
	if len(c.Provenance) != len(want) {
		t.Fatalf("provenance does not name exactly %v", want)
	}
	for _, k := range want {
		raw, ok := c.Provenance[k]
		if !ok {
			t.Fatalf("provenance does not name %s", k)
		}
		var mark string
		if json.Unmarshal(raw, &mark) == nil {
			if mark != "unrecorded" && mark != "planted" {
				t.Fatalf("provenance.%s is the mark %q", k, mark)
			}
			continue
		}
		var rec map[string]string
		if json.Unmarshal(raw, &rec) != nil || len(rec) != 2 || rec["build"] == "" || rec["line"] == "" {
			t.Fatalf("provenance.%s is not {build, line}, \"unrecorded\" or \"planted\"", k)
		}
	}
}

// failingTRecord reads a case's failing_t_record: required where the t rows'
// batch is refused (the post_where_refusal rule handles the case), absent
// otherwise; an integer, not a boolean, that is one of the t rows and, where that
// batch is refused at one of its rows, the body record that row is. It returns -1
// where there is none. A violation is a fixture error.
func failingTRecord(t *testing.T, c projectionCase, pwc *projectionPWC) int {
	t.Helper()
	refused := pwc != nil && pwc.Batch.Outcome != Accepted
	if c.FailingTRecord == nil {
		if refused {
			t.Fatalf("batch_of_t_rows is refused and the case carries no failing_t_record")
		}
		return -1
	}
	if !refused {
		t.Fatalf("failing_t_record where post_where_refusal does not apply")
	}
	var n int
	if err := json.Unmarshal(c.FailingTRecord, &n); err != nil || string(c.FailingTRecord) == "null" {
		t.Fatalf("failing_t_record %s is not an integer", c.FailingTRecord)
	}
	if !slices.Contains(pwc.Rows, n) {
		t.Fatalf("failing_t_record %d is not one of batch_of_t_rows.rows", n)
	}
	if sub := pwc.Batch; sub.RefusedAt == RefusedAtRow && sub.RefusedRow != nil && *sub.RefusedRow < uint64(len(pwc.Rows)) &&
		pwc.Rows[*sub.RefusedRow] != n {
		t.Fatalf("failing_t_record %d is not the record the t rows' batch refused at", n)
	}
	return n
}

func b64Or(t *testing.T, s *string) []byte {
	t.Helper()
	if s == nil {
		return nil
	}
	b, err := base64.StdEncoding.DecodeString(*s)
	if err != nil {
		t.Fatal(err)
	}
	if b == nil {
		b = []byte{}
	}
	return b
}

// loadProjectionFixture reads the shared fixture. It skips loudly, by name,
// when the file is absent (a bare copy of the module, as
// scripts/check-standalone.sh builds, has no tests/ tree); a file that is
// present but unreadable or malformed still fails. Where
// CHTYPES_REQUIRE_FIXTURES is "1" (the repository's main CI), an absent
// fixture fails instead of skipping.
func loadProjectionFixture(t *testing.T) []projectionCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "abi-v2", "insert-projection.json"))
	if errors.Is(err, fs.ErrNotExist) {
		if os.Getenv("CHTYPES_REQUIRE_FIXTURES") == "1" {
			t.Fatalf("CHTYPES_REQUIRE_FIXTURES=1 and the insert-projection fixture (tests/fixtures/abi-v2/insert-projection.json) is absent from this copy")
		}
		t.Skipf("SKIP %s: the insert-projection fixture (tests/fixtures/abi-v2/insert-projection.json) is absent from this copy", t.Name())
	}
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Cases []projectionCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Cases) == 0 {
		t.Fatal("the insert-projection fixture holds no cases")
	}
	return fx.Cases
}

func TestInsertProjectionFixture(t *testing.T) {
	cases := loadProjectionFixture(t)
	if len(cases) != 60 {
		t.Fatalf("the fixture holds %d cases, want 60", len(cases))
	}
	reds := map[string]int{}
	var equal, exceptions int
	for _, c := range cases {
		t.Run(c.ID, func(t *testing.T) {
			export, output := b64Or(t, c.BatchExportB64), b64Or(t, c.InsertRowsB64)
			batch, err := decodeBatch(c.Batch, export)
			if err != nil {
				t.Fatalf("decoding the batch document: %v", err)
			}
			ins, err := decodeInsertResult(c.Insert, output)
			if err != nil {
				t.Fatalf("decoding the insert_result document: %v", err)
			}
			var insRaw map[string]json.RawMessage
			if err := json.Unmarshal(c.Insert, &insRaw); err != nil {
				t.Fatal(err)
			}
			checkProvenance(t, c)
			var records []Span
			if c.BodyRecords != nil {
				records = bodyRecords(t, c, batch)
			}
			var pwc *projectionPWC
			if ev := c.BatchOfTRows; ev != nil {
				// a malformed batch_of_t_rows is a fixture error, never a verdict
				if c.BodyRecords == nil {
					t.Fatalf("batch_of_t_rows without body_records")
				}
				if !slices.IsSorted(ev.Rows) || len(slices.Compact(slices.Clone(ev.Rows))) != len(ev.Rows) ||
					(len(ev.Rows) > 0 && (ev.Rows[0] < 0 || ev.Rows[len(ev.Rows)-1] >= len(records))) {
					t.Fatalf("batch_of_t_rows.rows %v is not increasing indexes into body_records", ev.Rows)
				}
				subExport := b64Or(t, ev.ExportB64)
				sub, err := decodeBatch(ev.Batch, subExport)
				if err != nil {
					t.Fatalf("decoding batch_of_t_rows.batch: %v", err)
				}
				pwc = &projectionPWC{Rows: ev.Rows, Batch: sub, Export: subExport, Records: records, Failing: -1}
			}
			if failing := failingTRecord(t, c, pwc); pwc != nil {
				pwc.Failing = failing
			}
			kind, name := projectionVerdict(c.OutputFormat, batch, export, ins, insRaw, output, pwc)
			var want struct {
				Exception string `json:"exception"`
				Violates  string `json:"violates"`
			}
			wantKind, wantName := "equal", ""
			if string(c.Expect) != `"equal"` {
				if err := json.Unmarshal(c.Expect, &want); err != nil {
					t.Fatal(err)
				}
				if want.Exception != "" {
					wantKind, wantName = "exception", want.Exception
					exceptions++
				} else {
					wantKind, wantName = "violates", want.Violates
					reds[wantName]++
				}
			} else {
				equal++
			}
			if kind != wantKind || name != wantName {
				t.Errorf("got (%s, %s), want (%s, %s)", kind, name, wantKind, wantName)
			}
		})
	}
	for _, r := range projectionRules {
		if reds[r] == 0 {
			t.Errorf("no planted red for rule %s", r)
		}
	}
	wantReds := map[string]int{"e_row_rejects": 8, "export_decline_is_call_decline": 3, "post_where_cut": 4, "post_where_refusal": 7, "shared_members": 3, "counts": 2,
		"line_output": 2, "non_t_row": 2, "output_presence": 2, "err_prefix": 1, "err_suffix": 2, "native_output": 1}
	if !reflect.DeepEqual(reds, wantReds) {
		t.Errorf("planted reds per rule = %v, want %v", reds, wantReds)
	}
	if exceptions != 1 || equal != 22 {
		t.Errorf("equal=%d exceptions=%d, want 22 and 1", equal, exceptions)
	}
}

func TestInsertOutputAbsentUnlessAccepted(t *testing.T) {
	byID := map[string]projectionCase{}
	for _, c := range loadProjectionFixture(t) {
		byID[c.ID] = c
	}
	// A declined output is the call's decline: unsupported, and the public
	// output is absent even though the case hands back bytes.
	declined, ok := byID["equal-declined-output-is-the-call-decline"]
	if !ok {
		t.Fatal("the fixture has no equal-declined-output-is-the-call-decline case")
	}
	res, err := decodeInsertResult(declined.Insert, []byte("bytes the library handed back"))
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Unsupported || res.ErrCode != CodeDeclined || res.RefusedAt != RefusedAtBodyStep {
		t.Errorf("declined insert = %v %d %q, want unsupported -2 body_step", res.Outcome, res.ErrCode, res.RefusedAt)
	}
	if res.Output != nil || res.ExportColumns != nil {
		t.Errorf("declined output = %q columns %q, want absent", res.Output, res.ExportColumns)
	}
	// A case accepted with output keeps its bytes.
	acc := byID["equal-csv-filter-mixed"]
	want := b64Or(t, acc.InsertRowsB64)
	res, err = decodeInsertResult(acc.Insert, want)
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Accepted || len(want) == 0 || !bytes.Equal(res.Output, want) {
		t.Errorf("accepted output = %q, want %q", res.Output, want)
	}
}
