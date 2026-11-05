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
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"testing"
)

var projectionRules = []string{"e_row_rejects", "export_decline_is_call_decline", "shared_members", "counts", "err_prefix", "err_suffix",
	"output_presence", "non_t_row", "line_output", "native_output"}

var projectionSuffix = regexp.MustCompile(`^: \(at row [0-9]+\)\n$`)

// projectionVerdict returns ("equal", ""), ("exception", name) or
// ("violates", rule). batchExport is the batch's export bytes (nil when no
// export ran) and output the insert's output bytes (nil when none exists); they
// are passed apart from the decoded results because the public InsertResult
// keeps an output only for an accepted call, and the rule must see what the
// library handed back. insRaw is the insert document's raw members: the public
// InsertResult has no export_declined member, so a document that carries one is
// seen there.
func projectionVerdict(outFormat string, batch BatchResult, batchExport []byte, ins InsertResult, insRaw map[string]json.RawMessage, output []byte) (string, string) {
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

	// shared_members
	for _, k := range []string{"export_declined", "export_declined_b64", "export_declined_reason"} {
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
	if len(cases) != 44 {
		t.Fatalf("the fixture holds %d cases, want 44", len(cases))
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
			kind, name := projectionVerdict(c.OutputFormat, batch, export, ins, insRaw, output)
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
	wantReds := map[string]int{"e_row_rejects": 8, "export_decline_is_call_decline": 3, "shared_members": 3, "counts": 2,
		"line_output": 2, "non_t_row": 2, "output_presence": 2, "err_prefix": 1, "err_suffix": 2, "native_output": 1}
	if !reflect.DeepEqual(reds, wantReds) {
		t.Errorf("planted reds per rule = %v, want %v", reds, wantReds)
	}
	if exceptions != 1 || equal != 17 {
		t.Errorf("equal=%d exceptions=%d, want 17 and 1", equal, exceptions)
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
