package chtypes

// PROBE (do not merge): do the RELEASED 1.0.4 decoders tolerate members no
// 1.0 description names? The "ok-x" stub (scripts/abi-v1/emit/_stubshared.py
// PROBE_X_DOCS) answers every document-returning call with a document that
// carries unknown members at every object level, and its build_info and
// live_handles carry unknown members too. Each subtest decodes one document
// through the public API and checks the known fields still decode.

import (
	"fmt"
	"testing"
)

func TestProbeUnknownFields(t *testing.T) {
	lib := openStub(t, "ok-x")

	t.Run("build_info", func(t *testing.T) {
		bi := lib.BuildInfo()
		if bi.ClickHouseVersion != "26.8.15.10" || bi.Channel != "lts" || bi.ABI != 1 {
			t.Errorf("build info = %+v", bi)
		}
		found := false
		for _, f := range bi.Capabilities.Features {
			if f == "default_generators" {
				found = true
			}
		}
		if !found {
			t.Errorf("capabilities = %+v", bi.Capabilities)
		}
	})
	t.Run("live_handles", func(t *testing.T) {
		h, err := lib.LiveHandles()
		if err != nil {
			t.Fatalf("PROBE-FAIL live_handles: %v", err)
		}
		if _, ok := h["chs_schema"]; !ok {
			t.Errorf("live handles = %v", h)
		}
		t.Logf("live handles decoded as %v", h)
	})
	t.Run("error_code_table", func(t *testing.T) {
		tab, err := lib.ErrorCodes()
		if err != nil {
			t.Fatalf("PROBE-FAIL error_code_table: %v", err)
		}
		if n, ok := tab.Name(53); !ok || n != "TYPE_MISMATCH" {
			t.Errorf("Name(53) = %q, %v", n, ok)
		}
	})
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	t.Run("schema_description", func(t *testing.T) {
		d, err := schema.Describe()
		if err != nil {
			t.Fatalf("PROBE-FAIL schema_description: %v", err)
		}
		if len(d.Columns) != 1 || d.Columns[0].Name != "x" || d.Columns[0].Type != "Int32" {
			t.Errorf("description = %+v", d)
		}
	})
	t.Run("row", func(t *testing.T) {
		r, err := schema.Row(JSONEachRow, []byte(`{"x":1}`))
		if err != nil {
			t.Fatalf("PROBE-FAIL row: %v", err)
		}
		if r.Outcome != Accepted || len(r.Columns) != 1 || r.Columns[0].Text != "abc" || r.InputSpan == nil || r.InputSpan.Len != 3 ||
			len(r.Computed) != 1 || len(r.Transformed) != 1 || len(r.UnknownFields) != 1 || len(r.UnsupportedSettings) != 1 {
			t.Errorf("row = %+v", r)
		}
	})
	t.Run("batch", func(t *testing.T) {
		b, err := schema.Rows(JSONEachRow, []byte(`{"x":1}`), WithExport(JSONEachRow))
		if err != nil {
			t.Fatalf("PROBE-FAIL batch: %v", err)
		}
		if b.Outcome != Accepted || b.RowsRead != 1 || len(b.Rows) != 1 || len(b.Rows[0].Columns) != 1 ||
			len(b.EngineRows) != 1 || len(b.Spans) != 1 || len(b.Unconsumed) != 1 || b.Framing == nil ||
			b.Framing.Header == nil || len(b.Framing.Header.Names) != 1 || string(b.Payload) != "{\"s\":\"abc\"}\n" {
			t.Errorf("batch = %+v", b)
		}
	})
	filter, err := schema.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	t.Run("filter_result", func(t *testing.T) {
		f, err := filter.Rows(JSONEachRow, []byte(`{"x":1}`))
		if err != nil {
			t.Fatalf("PROBE-FAIL filter_result: %v", err)
		}
		if f.Outcome != FilterOK || f.RowsRead != 2 || len(f.Verdicts) != 2 || len(f.Errors) != 1 || len(f.UnsupportedSettings) != 1 {
			t.Errorf("filter result = %+v", f)
		}
	})
	t.Run("discovery", func(t *testing.T) {
		d, err := lib.DiscoverColumns([]byte(`{}`))
		if err != nil {
			t.Fatalf("PROBE-FAIL discovery: %v", err)
		}
		if len(d.Columns) != 1 || d.Columns[0].Declaration != "c String" || d.ColumnsSQL != "c String" {
			t.Errorf("discovery = %+v", d)
		}
	})
}

// TestProbeUnknownEnumValues: the second question. The "ok-e" stub answers a
// clean document, or (on a "!E:<id>" body) the same document with one field
// set to a value its vocabulary does not list; "ok-e-bi" carries an unknown
// value in every capabilities list of build_info; chs_type_validate("!U:")
// answers status 99. Each probe RECORDS what the released decoder did (an
// error and its class, or the decoded value) as a PROBE-ENUM line; it asserts
// nothing beyond "no crash", because every outcome is a finding.
func TestProbeUnknownEnumValues(t *testing.T) {
	report := func(id, got string, err error) {
		if err != nil {
			t.Logf("PROBE-ENUM go %s => ERROR %T: %v", id, err, err)
			return
		}
		t.Logf("PROBE-ENUM go %s => %s", id, got)
	}

	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	bi, err := OpenUnverified(stubFile(t, "ok-e-bi"), true)
	if err != nil {
		report("build_info.capabilities", "", err)
	} else {
		report("build_info.capabilities", fmt.Sprintf("%+v", bi.BuildInfo().Capabilities), nil)
	}

	lib := openStub(t, "ok-e")
	_, err = lib.ValidateType("!U:")
	report("status.unknown", "no error", err)

	schema, err := lib.CompileTable("!E:describe.default_kind")
	if err != nil {
		t.Fatal(err)
	}
	d, err := schema.Describe()
	if err == nil {
		report("describe.default_kind", fmt.Sprintf("default_kind=%q", d.Columns[0].DefaultKind), nil)
	} else {
		report("describe.default_kind", "", err)
	}
	clean, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	if d, err := clean.Describe(); err == nil {
		report("describe.control", fmt.Sprintf("default_kind=%q", d.Columns[0].DefaultKind), nil)
	} else {
		report("describe.control", "", err)
	}

	row := func(id string, pick func(RowResult) string) {
		r, err := clean.Row(JSONEachRow, []byte("!E:"+id))
		if err != nil {
			report(id, "", err)
			return
		}
		report(id, pick(r), nil)
	}
	row("row.outcome", func(r RowResult) string { return fmt.Sprintf("outcome=%q", r.Outcome) })
	row("row.cols.src", func(r RowResult) string {
		return fmt.Sprintf("source=%q is_stored=%v", r.Columns[0].Source, r.Columns[0].IsStored)
	})
	row("row.transformed.reason", func(r RowResult) string {
		return fmt.Sprintf("reason=%q lossy=%v", r.Transformed[0].Reason, r.Transformed[0].Lossy)
	})
	row("row.verdict", func(r RowResult) string {
		if r.Verdict == nil {
			return "verdict=nil"
		}
		return fmt.Sprintf("verdict=%q answered=%v", *r.Verdict, r.Verdict.Answered())
	})
	if r, err := clean.Row(JSONEachRow, []byte(`{}`)); err == nil {
		report("row.control", fmt.Sprintf("outcome=%q source=%q", r.Outcome, r.Columns[0].Source), nil)
	} else {
		report("row.control", "", err)
	}

	batch := func(id string, pick func(BatchResult) string) {
		b, err := clean.Rows(JSONEachRow, []byte("!E:"+id), WithExport(JSONEachRow))
		if err != nil {
			report(id, "", err)
			return
		}
		report(id, pick(b), nil)
	}
	batch("batch.outcome", func(b BatchResult) string { return fmt.Sprintf("outcome=%q", b.Outcome) })
	batch("batch.rows.outcome", func(b BatchResult) string { return fmt.Sprintf("outcome=%q", b.Rows[0].Outcome) })
	batch("batch.rows.cols.src", func(b BatchResult) string {
		c := b.Rows[0].Columns[0]
		return fmt.Sprintf("source=%q is_stored=%v", c.Source, c.IsStored)
	})
	batch("batch.transformed.reason", func(b BatchResult) string {
		return fmt.Sprintf("reason=%q lossy=%v", b.Transformed[0].Reason, b.Transformed[0].Lossy)
	})
	batch("batch.framing.container", func(b BatchResult) string {
		if b.Framing == nil {
			return "framing=nil"
		}
		return fmt.Sprintf("container=%q", b.Framing.Container)
	})
	batch("batch.control", func(b BatchResult) string { return fmt.Sprintf("outcome=%q", b.Outcome) })

	filter, err := clean.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"filter.outcome", "filter.verdicts", "filter.control"} {
		body := []byte("!E:" + id)
		if id == "filter.control" {
			body = []byte(`{}`)
		}
		f, err := filter.Rows(JSONEachRow, body)
		if err != nil {
			report(id, "", err)
			continue
		}
		answered := make([]bool, len(f.Verdicts))
		for i, v := range f.Verdicts {
			answered[i] = v.Answered()
		}
		report(id, fmt.Sprintf("outcome=%q verdicts=%q answered=%v", f.Outcome, f.Verdicts, answered), nil)
	}

	if dc, err := lib.DiscoverColumns([]byte("!E:discovery.default_kind")); err == nil {
		report("discovery.default_kind", fmt.Sprintf("%+v", dc.Columns), nil)
	} else {
		report("discovery.default_kind", "", err)
	}
}
