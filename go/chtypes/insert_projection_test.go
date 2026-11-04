package chtypes

// insert_projection_test.go — the standing projection gate of Schema.Insert:
// the rule spec/abi-v2/docs.md (chs_preview_insert, "The projection rule")
// states between an insert_result and the batch document for the same inputs,
// run over every case of tests/fixtures/abi-v2/insert-projection.json. Both
// documents of a case are read by the package's real decoders, never as raw
// JSON, so a case also proves the decoders. projectionVerdict mirrors
// scripts/abi-v2/check-insert-projection.py rule by rule and in the same order.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"testing"
)

var projectionRules = []string{"e_row_rejects", "shared_members", "counts", "err_prefix", "err_suffix",
	"output_presence", "non_t_row", "line_output", "native_output"}

var projectionSuffix = regexp.MustCompile(`^ \(at row [0-9]+\)$`)

// projectionVerdict returns ("equal", ""), ("exception", name) or
// ("violates", rule). batchExport is the batch's export bytes (nil when no
// export ran) and output the insert's output bytes (nil when none exists); they
// are passed apart from the decoded results because a decoder drops the bytes
// of a declined export, which is the very thing the rule must see.
func projectionVerdict(outFormat string, batch BatchResult, batchExport []byte, ins InsertResult, output []byte) (string, string) {
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
		zero := func(p *uint64) bool { return p != nil && *p == 0 }
		if !(ins.Outcome == Rejected && ins.ErrCode == first.VerdictCode && ins.ErrMsg == first.VerdictErr &&
			ins.RefusedAt == RefusedAtRow && zero(ins.RowsPassed) && zero(ins.RowsCut) && zero(ins.RowsUndecided) &&
			ins.RowsRead == batch.RowsRead && output == nil) {
			return "violates", "e_row_rejects"
		}
		return "equal", ""
	}

	// shared_members
	eqCount := func(batchValue uint64, p *uint64) bool {
		if !filtered {
			return p == nil
		}
		return p != nil && *p == batchValue
	}
	if batch.Outcome != ins.Outcome || batch.ErrCode != ins.ErrCode || batch.Reason != ins.Reason ||
		batch.RefusedAt != ins.RefusedAt || batch.RowsRead != ins.RowsRead || batch.RowsSkipped != ins.RowsSkipped ||
		!slices.Equal(batch.UnsupportedSettings, ins.UnsupportedSettings) ||
		!reflect.DeepEqual(batch.Framing, ins.Framing) || !reflect.DeepEqual(batch.PartitionCount, ins.PartitionCount) ||
		(batch.ExportColumns == nil) != (ins.ExportColumns == nil) || !slices.Equal(batch.ExportColumns, ins.ExportColumns) ||
		batch.ExportDeclined != ins.ExportDeclined || batch.ExportDeclinedReason != ins.ExportDeclinedReason ||
		!eqCount(batch.RowsPassed, ins.RowsPassed) || !eqCount(batch.RowsCut, ins.RowsCut) {
		return "violates", "shared_members"
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
		if !(good && ins.RowsUndecided != nil && *ins.RowsUndecided == d) {
			return "violates", "counts"
		}
	} else if ins.RowsPassed != nil || ins.RowsCut != nil || ins.RowsUndecided != nil {
		return "violates", "counts"
	}

	// err
	if !bytes.HasPrefix([]byte(ins.ErrMsg), []byte(batch.ErrMsg)) {
		return "violates", "err_prefix"
	}
	if rest := ins.ErrMsg[len(batch.ErrMsg):]; rest != "" && !projectionSuffix.MatchString(rest) {
		return "violates", "err_suffix"
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

func TestInsertProjectionFixture(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "abi-v2", "insert-projection.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Cases []projectionCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Cases) != 33 {
		t.Fatalf("the fixture holds %d cases, want 33", len(fx.Cases))
	}
	reds := map[string]bool{}
	var equal, exceptions int
	for _, c := range fx.Cases {
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
			kind, name := projectionVerdict(c.OutputFormat, batch, export, ins, output)
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
					reds[wantName] = true
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
		if !reds[r] {
			t.Errorf("no planted red for rule %s", r)
		}
	}
	if exceptions != 1 || equal != 11 {
		t.Errorf("equal=%d exceptions=%d, want 11 and 1", equal, exceptions)
	}
}

func TestInsertDeclinedOutputIsAbsent(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "abi-v2", "insert-projection.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		Cases []projectionCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	byID := map[string]projectionCase{}
	for _, c := range fx.Cases {
		byID[c.ID] = c
	}
	// A declined export that still came with bytes: the public output is absent.
	declined := byID["red-e-row-output-present"]
	rawOut := b64Or(t, declined.InsertRowsB64)
	if len(rawOut) == 0 {
		t.Fatal("the planted case carries no output bytes")
	}
	res, err := decodeInsertResult(declined.Insert, rawOut)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExportDeclined == "" {
		t.Fatal("the planted case is not declined")
	}
	if res.Output != nil {
		t.Errorf("declined output = %q, want absent", res.Output)
	}
	// A case that was not declined keeps its bytes.
	ok := byID["equal-csv-filter-mixed"]
	want := b64Or(t, ok.InsertRowsB64)
	res, err = decodeInsertResult(ok.Insert, want)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExportDeclined != "" || len(want) == 0 || !bytes.Equal(res.Output, want) {
		t.Errorf("non-declined output = %q, want %q", res.Output, want)
	}
}
