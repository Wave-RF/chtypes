package chtypes

// PROBE (do not merge): do the RELEASED 1.0.4 decoders tolerate members no
// 1.0 description names? The "ok-x" stub (scripts/abi-v1/emit/_stubshared.py
// PROBE_X_DOCS) answers every document-returning call with a document that
// carries unknown members at every object level, and its build_info and
// live_handles carry unknown members too. Each subtest decodes one document
// through the public API and checks the known fields still decode.

import "testing"

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
