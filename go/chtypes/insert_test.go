package chtypes

// insert_test.go — Schema.Insert over the ABI v2 test stub, and the
// insert_result decoder on a document that carries every member. The stub
// echoes the arguments of chs_preview_insert into both its buffers, so the
// out_rows buffer Insert returns as Output is the argument echo.

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// wireOf reads the stub's echo of an Insert call: the scalar arguments and the
// bytes arguments by position (chs_preview_insert: schema, format, body,
// settings, columns, filter, output_format, options).
func wireOf(t *testing.T, res InsertResult) (args []json.RawMessage) {
	t.Helper()
	var e struct {
		Fn   string            `json:"fn"`
		Out  string            `json:"out"`
		Args []json.RawMessage `json:"args"`
	}
	if err := json.Unmarshal(res.Output, &e); err != nil || e.Fn != "chs_preview_insert" || e.Out != "out_rows" || len(e.Args) != 8 {
		t.Fatalf("the out_rows echo %q: %v", res.Output, err)
	}
	return e.Args
}

// insertEcho makes an Insert call and returns the out_rows buffer as the stub
// echoed it in Output. The stub's document carries no outcome, so the public
// Insert would (rightly) hide that buffer; the wire tests read it raw.
func insertEcho(s *Schema, format Format, body []byte, out Format, opts ...InsertCallOption) (InsertResult, error) {
	_, rows, err := s.insertRaw(format, body, out, opts...)
	return InsertResult{Output: rows}, err
}

func wireNum(t *testing.T, raw json.RawMessage) float64 {
	t.Helper()
	var n float64
	if err := json.Unmarshal(raw, &n); err != nil {
		t.Fatalf("%s is not a number: %v", raw, err)
	}
	return n
}

func wireBytes(t *testing.T, raw json.RawMessage) string {
	t.Helper()
	var b struct {
		HeadHex string `json:"head_hex"`
		Len     int    `json:"len"`
	}
	if err := json.Unmarshal(raw, &b); err != nil {
		t.Fatalf("%s is not a bytes echo: %v", raw, err)
	}
	h, _ := hex.DecodeString(b.HeadHex)
	if b.Len <= 16 && len(h) != b.Len { // the echo keeps a head of 16 bytes
		t.Fatalf("echo %s: head shorter than len", raw)
	}
	return string(h)
}

func stubInsertSchema(t *testing.T) (*Schema, *Filter) {
	t.Helper()
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { schema.Close() })
	flt, err := schema.CompileFilter("x < 5")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { flt.Close() })
	return schema, flt
}

func TestStubInsertFormatsReachTheABI(t *testing.T) {
	schema, _ := stubInsertSchema(t)
	for _, tc := range []struct{ in, out Format }{{JSONEachRow, CSV}, {CSV, Native}, {JSONEachRow, ExportNone}} {
		res, err := insertEcho(schema, tc.in, []byte("ab"), tc.out)
		if err != nil {
			t.Fatal(err)
		}
		a := wireOf(t, res)
		if got := wireNum(t, a[1]); got != float64(tc.in) {
			t.Errorf("format on the wire = %v, want %d", got, tc.in)
		}
		if got := wireNum(t, a[6]); got != float64(tc.out) {
			t.Errorf("output format on the wire = %v, want %d", got, tc.out)
		}
		if got := wireBytes(t, a[2]); got != "ab" {
			t.Errorf("body on the wire = %q", got)
		}
	}
}

func TestStubInsertFilterIsNullableOnTheWire(t *testing.T) {
	schema, flt := stubInsertSchema(t)
	res, err := insertEcho(schema, JSONEachRow, []byte("ab"), CSV)
	if err != nil {
		t.Fatal(err)
	}
	if got := string(wireOf(t, res)[5]); got != "null" {
		t.Errorf("filter without WithInsertFilter = %s, want null", got)
	}
	res, err = insertEcho(schema, JSONEachRow, []byte("ab"), CSV, WithInsertFilter(flt))
	if err != nil {
		t.Fatal(err)
	}
	var h struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(wireOf(t, res)[5], &h); err != nil || h.Kind != "chs_filter" {
		t.Errorf("filter with WithInsertFilter = %s (%v), want a chs_filter handle", wireOf(t, res)[5], err)
	}
	// A closed filter is the binding's usage error, raised before the call.
	flt.Close()
	var ue *UsageError
	if _, err := insertEcho(schema, JSONEachRow, []byte("ab"), CSV, WithInsertFilter(flt)); !errors.As(err, &ue) {
		t.Errorf("a closed filter = %v, want a *UsageError", err)
	}
}

func TestStubInsertOptionsSettingsColumnsReachTheABI(t *testing.T) {
	schema, _ := stubInsertSchema(t)
	res, err := insertEcho(schema, JSONEachRow, []byte("ab"), CSV)
	if err != nil {
		t.Fatal(err)
	}
	a := wireOf(t, res)
	if s, c, o := wireBytes(t, a[3]), wireBytes(t, a[4]), wireBytes(t, a[7]); s != "" || c != "" || o != "" {
		t.Errorf("with no options, settings %q, columns %q and options %q must be empty", s, c, o)
	}
	res, err = insertEcho(schema, JSONEachRow, []byte("ab"), CSV,
		WithSettings(map[string]string{"a": "b"}), WithColumns([]string{"x"}), WithMemoryLimit(1048576), WithDeferDefaults(DeferDefaultsNone))
	if err != nil {
		t.Fatal(err)
	}
	a = wireOf(t, res)
	if got := wireBytes(t, a[3]); got == "" {
		t.Errorf("settings missing on the wire")
	}
	if got := wireBytes(t, a[4]); got != `[{"name":"x"}]` {
		t.Errorf("columns on the wire = %s", got)
	}
	// The stub's echo keeps a bytes argument's first 16 bytes and its length.
	var e struct {
		Args []json.RawMessage `json:"args"`
	}
	if err := json.Unmarshal(res.Output, &e); err != nil {
		t.Fatal(err)
	}
	var o7 struct {
		Len int `json:"len"`
	}
	if err := json.Unmarshal(e.Args[7], &o7); err != nil {
		t.Fatal(err)
	}
	_, _, options, err := callInputs(insertConfig([]InsertCallOption{WithMemoryLimit(1048576), WithDeferDefaults(DeferDefaultsNone)}))
	if err != nil {
		t.Fatal(err)
	}
	if got := wireBytes(t, e.Args[7]); o7.Len != len(options) || got != string(options[:16]) {
		t.Errorf("insert options on the wire = %d bytes starting %q, want %s", o7.Len, got, options)
	}
	if !json.Valid(options) || !containsAll(string(options), `"memory_limit_bytes":"1048576"`, `"defer_defaults":"none"`) {
		t.Errorf("options document = %s", options)
	}
}

func containsAll(s string, parts ...string) bool {
	for _, p := range parts {
		if !strings.Contains(s, p) {
			return false
		}
	}
	return true
}

// memory_limit_bytes is an insert option: it reaches every call that models an
// INSERT of a body, with the same document.
func TestMemoryLimitIsAnInsertOptionOfEveryCall(t *testing.T) {
	var _ RowOption = WithMemoryLimit(1)
	var _ RowsOption = WithMemoryLimit(1)
	var _ BlockOption = WithMemoryLimit(1)
	var _ EvalOption = WithMemoryLimit(1)
	var _ InsertCallOption = WithMemoryLimit(1)
	for _, n := range []struct {
		limit uint64
		want  string
	}{{0, `{"memory_limit_bytes":"0"}`}, {1048576, `{"memory_limit_bytes":"1048576"}`}, {999999999999999999, `{"memory_limit_bytes":"999999999999999999"}`}} {
		_, _, options, err := callInputs(insertConfig([]InsertCallOption{WithMemoryLimit(n.limit)}))
		if err != nil || string(options) != n.want {
			t.Errorf("WithMemoryLimit(%d) = %s, %v, want %s", n.limit, options, err, n.want)
		}
		_, _, options, err = callInputs(rowsConfig([]RowsOption{WithMemoryLimit(n.limit)}))
		if err != nil || string(options) != n.want {
			t.Errorf("Rows: WithMemoryLimit(%d) = %s, %v", n.limit, options, err)
		}
	}
}

func TestDecodeInsertResultEveryMember(t *testing.T) {
	doc := `{"outcome":"rejected","code":27,"err_b64":"Yf8=","reason":"some-reason","refused_at":"row",
	"rows_read":7,"rows_skipped":2,"rows_passed":3,"rows_cut":4,"rows_undecided":1,
	"unsupported_settings":[{"name":"s1"},{"name_b64":"/w=="}],
	"framing":{"bom_skipped":true,"container":"array","header":{"consumed":true,"lines":1,"names":[{"name":"x"}]}},
	"partition_count":5,"export_columns":[{"name":"x"}]}`
	res, err := decodeInsertResult([]byte(doc), []byte("bytes"))
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Rejected || res.ErrCode != 27 || res.ErrMsg != "a\xff" || res.Reason != "some-reason" || res.RefusedAt != RefusedAtRow ||
		res.RowsRead != 7 || res.RowsSkipped != 2 || *res.RowsPassed != 3 || *res.RowsCut != 4 || *res.RowsUndecided != 1 {
		t.Errorf("scalars = %+v", res)
	}
	if res.ErrMsg != "a\xff" {
		t.Errorf("err_b64 = %q", res.ErrMsg)
	}
	if len(res.UnsupportedSettings) != 2 || res.UnsupportedSettings[1] != "\xff" {
		t.Errorf("unsupported_settings = %q", res.UnsupportedSettings)
	}
	if res.Framing == nil || res.Framing.Container != "array" || res.Framing.BomSkipped == nil || !*res.Framing.BomSkipped || res.Framing.Header == nil || res.Framing.Header.Lines != 1 {
		t.Errorf("framing = %+v", res.Framing)
	}
	if res.PartitionCount == nil || *res.PartitionCount != 5 {
		t.Errorf("partition_count = %v", res.PartitionCount)
	}
	if res.Output != nil || res.ExportColumns != nil {
		t.Errorf("a rejected call = %q %q: Output and ExportColumns must be absent unless accepted", res.Output, res.ExportColumns)
	}
	// Absent counts are nil, not 0; an unlisted outcome is kept, not accepted.
	res, err = decodeInsertResult([]byte(`{"outcome":"future(9)","code":0,"rows_read":0,"rows_skipped":0,"unsupported_settings":[],"framing":{"bom_skipped":null,"container":null,"header":null},"export_columns":[{"name":"x"}]}`), []byte{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome.Known() || res.Outcome == Accepted || res.RowsPassed != nil || res.RowsCut != nil || res.RowsUndecided != nil || res.Output != nil || res.ExportColumns != nil {
		t.Errorf("minimal result = %+v", res)
	}
	// An accepted call with an output keeps it, empty or not, and its columns.
	res, err = decodeInsertResult([]byte(`{"outcome":"accepted","code":0,"rows_read":0,"rows_skipped":0,"unsupported_settings":[],"framing":{"bom_skipped":null,"container":null,"header":null},"export_columns":[{"name":"x"}]}`), []byte{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Output == nil || len(res.Output) != 0 || len(res.ExportColumns) != 1 {
		t.Errorf("accepted result = %+v", res)
	}
	// A duplicate key is an internal error, as in every document.
	if _, err := decodeInsertResult([]byte(`{"outcome":"accepted","outcome":"accepted"}`), nil); err == nil {
		t.Errorf("a duplicate key decoded")
	}
}
