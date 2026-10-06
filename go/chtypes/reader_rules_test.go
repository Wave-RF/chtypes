package chtypes

// reader_rules_test.go — ABI v2's reader rules (spec/abi-v2/docs.md), through
// the public API, over the generated v2 stub:
//
//   - r2: a member the description does not name is ignored at every object
//     level, `_b64` members included, in every result document and in
//     build_info (stub variant "r2-unknown-members");
//   - r3: a value a vocabulary does not list is kept as that vocabulary's
//     unknown(n), for that field alone, and never fails the document, the row
//     or the batch; the fallback's fail-closed reading stays (stub variant
//     "r3-unknown-values", one planted value per "!E:<id>" body, and
//     "r3-unknown-capabilities"); a call status outside the closed set is an
//     internal error naming unknown(n).
//
// The documents and the planted values are scripts/abi-v1/emit/_stubshared.py's
// R2_DOCS and R3_MUTATIONS, the shapes public pull request #509 measured on the
// released 1.0.4 bindings. TestR3EveryDescribedVocabulary covers the enums no
// document carries (chs_format, discover_query_param) from the generated list
// of every enum the description defines. Without CHTYPES_ABI2_STUBS the stub
// tests skip loudly by name.

import (
	"errors"
	"slices"
	"strconv"
	"strings"
	"testing"
)

func TestR2UnknownMembersAreIgnored(t *testing.T) {
	lib := openStub(t, "r2-unknown-members")

	bi := lib.BuildInfo()
	if bi.ClickHouseVersion != "26.8.15.10" || bi.Channel != "lts" || !slices.Contains(bi.Capabilities.Features, "default_generators") {
		t.Errorf("build_info with unknown members = %+v", bi)
	}
	if h, err := lib.LiveHandles(); err != nil {
		t.Errorf("live_handles with an unknown key: %v", err)
	} else if _, ok := h["chs_schema"]; !ok {
		t.Errorf("live handles = %v", h)
	}
	if tab, err := lib.ErrorCodes(); err != nil {
		t.Errorf("error_code_table with unknown members: %v", err)
	} else if n, ok := tab.Name(53); !ok || n != "TYPE_MISMATCH" {
		t.Errorf("Name(53) = %q, %v", n, ok)
	}
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	if d, err := schema.Describe(); err != nil {
		t.Errorf("schema_description with unknown members: %v", err)
	} else if len(d.Columns) != 1 || d.Columns[0].Name != "x" || d.Columns[0].Type != "Int32" {
		t.Errorf("description = %+v", d)
	}
	if r, err := schema.Row(JSONEachRow, []byte(`{"x":1}`)); err != nil {
		t.Errorf("row with unknown members: %v", err)
	} else if r.Outcome != Accepted || len(r.Columns) != 1 || r.Columns[0].Text != "abc" || r.InputSpan == nil || r.InputSpan.Len != 3 ||
		len(r.Computed) != 1 || len(r.Transformed) != 1 || len(r.UnknownFields) != 1 || len(r.UnsupportedSettings) != 1 {
		t.Errorf("row = %+v", r)
	}
	if b, err := schema.Rows(JSONEachRow, []byte(`{"x":1}`), WithExport(JSONEachRow)); err != nil {
		t.Errorf("batch with unknown members: %v", err)
	} else if b.Outcome != Accepted || b.RowsRead != 1 || len(b.Rows) != 1 || len(b.Rows[0].Columns) != 1 ||
		len(b.EngineRows) != 1 || len(b.Spans) != 1 || len(b.Unconsumed) != 1 || b.Framing == nil ||
		b.Framing.Header == nil || len(b.Framing.Header.Names) != 1 || string(b.Payload) != "{\"s\":\"abc\"}\n" {
		t.Errorf("batch = %+v", b)
	}
	filter, err := schema.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	if f, err := filter.Rows(JSONEachRow, []byte(`{"x":1}`)); err != nil {
		t.Errorf("filter_result with unknown members: %v", err)
	} else if f.Outcome != FilterOK || f.RowsRead != 2 || len(f.Verdicts) != 2 || len(f.Errors) != 1 || len(f.UnsupportedSettings) != 1 {
		t.Errorf("filter result = %+v", f)
	}
	if d, err := lib.DiscoverColumns([]byte(`{}`)); err != nil {
		t.Errorf("discovery with unknown members: %v", err)
	} else if len(d.Columns) != 1 || d.Columns[0].Declaration != "c String" || d.ColumnsSQL != "c String" {
		t.Errorf("discovery = %+v", d)
	}
}

func TestR3UnknownValuesAreKept(t *testing.T) {
	lib := openStub(t, "r3-unknown-values")
	clean, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	row := func(id string) RowResult {
		t.Helper()
		r, err := clean.Row(JSONEachRow, []byte("!E:"+id))
		if err != nil {
			t.Fatalf("%s: the row failed: %v (rule r3: an unlisted value never fails it)", id, err)
		}
		return r
	}
	batch := func(id string) BatchResult {
		t.Helper()
		b, err := clean.Rows(JSONEachRow, []byte("!E:"+id), WithExport(JSONEachRow))
		if err != nil {
			t.Fatalf("%s: the batch failed: %v (rule r3)", id, err)
		}
		return b
	}

	// The controls: the clean documents decode with every value listed.
	if r, err := clean.Row(JSONEachRow, []byte(`{}`)); err != nil || r.Outcome != Accepted || !r.Columns[0].Source.Known() {
		t.Fatalf("control row = %+v, %v", r, err)
	}

	if r := row("row.outcome"); r.Outcome != "x_future_outcome" || r.Outcome.Known() || r.Outcome == Accepted || len(r.Columns) != 1 {
		t.Errorf("row.outcome: %q, known %v, %d columns; want unknown(x_future_outcome), never accepted, the rest decoded", r.Outcome, r.Outcome.Known(), len(r.Columns))
	}
	if r := row("row.cols.src"); r.Columns[0].Source != "x_future_src" || r.Columns[0].Source.Known() || r.Columns[0].IsStored || r.Columns[0].Text != "abc" || r.Outcome != Accepted {
		t.Errorf("row.cols.src: %+v; want unknown(x_future_src) kept, not stored, the column and row decoded", r.Columns[0])
	}
	if r := row("row.transformed.reason"); r.Transformed[0].Reason != "x_future_reason" || r.Transformed[0].Reason.Known() ||
		r.Transformed[0].Lossy != ReasonValueChanged.Lossy() {
		t.Errorf("row.transformed.reason: %+v; want unknown(x_future_reason) with the fallback's lossy fact", r.Transformed[0])
	}
	if r := row("row.verdict"); r.Verdict == nil || *r.Verdict != "x" || r.Verdict.Known() || r.Verdict.Answered() {
		t.Errorf("row.verdict: %v; want unknown(x), never answered", r.Verdict)
	}

	if b := batch("batch.outcome"); b.Outcome != "x_future_outcome" || b.Outcome.Known() || b.Outcome == Accepted || len(b.Rows) != 1 {
		t.Errorf("batch.outcome: %q; want unknown(x_future_outcome), the rows decoded", b.Outcome)
	}
	if b := batch("batch.rows.outcome"); b.Rows[0].Outcome != "x_future_outcome" || b.Rows[0].Outcome.Known() || b.Outcome != Accepted {
		t.Errorf("batch.rows.outcome: %q (batch %q); want the row's unknown(n), the batch's own value untouched", b.Rows[0].Outcome, b.Outcome)
	}
	if b := batch("batch.rows.cols.src"); b.Rows[0].Columns[0].Source != "x_future_src" || b.Rows[0].Columns[0].Source.Known() || len(b.Rows) != 1 {
		t.Errorf("batch.rows.cols.src: %+v; want unknown(x_future_src) for that column, the batch whole", b.Rows[0].Columns)
	}
	if b := batch("batch.transformed.reason"); b.Transformed[0].Reason != "x_future_reason" || b.Transformed[0].Reason.Known() {
		t.Errorf("batch.transformed.reason: %+v", b.Transformed)
	}
	if b := batch("batch.storage_transforms.reason"); b.Outcome != Accepted || len(b.Rows) != 1 {
		t.Errorf("batch.storage_transforms.reason: the batch = %+v; an unlisted reason in a member this binding does not read must not fail it", b)
	}
	if b := batch("batch.framing.container"); b.Framing == nil || b.Framing.Container != "x_future_container" {
		t.Errorf("batch.framing.container: %+v; the schema's enum constrains the writer, never the reader (r3)", b.Framing)
	}

	filter, err := clean.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	if f, err := filter.Rows(JSONEachRow, []byte("!E:filter.outcome")); err != nil || f.Outcome != "x_future_outcome" || f.Outcome.Known() || f.Outcome == FilterOK {
		t.Errorf("filter.outcome: %q, %v; want unknown(x_future_outcome), never ok", f.Outcome, err)
	}
	if f, err := filter.Rows(JSONEachRow, []byte("!E:filter.verdicts")); err != nil || len(f.Verdicts) != 2 ||
		f.Verdicts[0] != VerdictTrue || f.Verdicts[1] != "x" || f.Verdicts[1].Known() || f.Verdicts[1].Answered() {
		t.Errorf("filter.verdicts: %q, %v; want t then unknown(x), never answered", f.Verdicts, err)
	}

	// chs_schema_describe has no body: the stub answers the mutation the
	// schema's own statement named.
	mutated, err := lib.CompileTable("!E:describe.default_kind")
	if err != nil {
		t.Fatal(err)
	}
	if d, err := mutated.Describe(); err != nil || d.Columns[0].DefaultKind != "X_FUTURE" || d.Columns[0].DefaultKind.Known() || d.Columns[0].Name != "x" {
		t.Errorf("describe.default_kind: %+v, %v; want unknown(X_FUTURE), the column decoded", d, err)
	}
	if d, err := lib.DiscoverColumns([]byte("!E:discovery.default_kind")); err != nil || len(d.Columns) != 1 || d.Columns[0].Declaration != "c String" {
		t.Errorf("discovery.default_kind: %+v, %v; want the document decoded", d, err)
	}
}

// r3: a call status outside the closed set is unknown(n), and the call still
// fails, as an internal error naming n.
func TestR3UnknownStatusIsInternalNamingIt(t *testing.T) {
	lib := openStub(t, "r3-unknown-values")
	_, err := lib.ValidateType("!U:")
	var ie *InternalError
	if !errors.As(err, &ie) || ie.Status != 99 || ie.Status.Known() || ie.Status.String() != "unknown(99)" || !strings.Contains(err.Error(), "unknown(99)") {
		t.Errorf("status 99 = %v; want an *InternalError whose Status is unknown(99)", err)
	}
}

// r3: build_info's capabilities lists keep a value no vocabulary lists.
func TestR3UnknownCapabilitiesAreKept(t *testing.T) {
	lib := openStub(t, "r3-unknown-capabilities")
	c := lib.BuildInfo().Capabilities
	if !slices.Contains(c.InputFormats, "XFutureFormat") || !slices.Contains(c.ExportFormats, "XFutureFormat") ||
		!slices.Contains(c.DocFlags, "x_future_flag") || !slices.Contains(c.Features, "x_future_feature") ||
		!slices.Contains(c.Features, "default_generators") {
		t.Errorf("capabilities = %+v; want every unlisted value kept beside the listed ones", c)
	}
}

// r3, every enum the description defines (the generated describedVocabulary
// list, so a new enum is covered without an edit here): an unlisted value
// builds that vocabulary's unknown(n), Known() is false for it, and the raw
// value reads back unchanged; every listed value is Known().
func TestR3EveryDescribedVocabulary(t *testing.T) {
	names := describedVocabularyNames()
	if len(names) < 10 {
		t.Fatalf("the generated list names %d enums: %v", len(names), names)
	}
	for _, name := range names {
		for _, raw := range []string{"x_unlisted_" + name, "2147483000", "-7"} {
			known, back, ok := describedVocabulary(name, raw)
			if !ok {
				if _, err := strconv.Atoi(raw); err != nil {
					continue // a string spelling is not an int32 enum's raw value
				}
				t.Errorf("%s: no vocabulary built from %q", name, raw)
				continue
			}
			if known || back != raw {
				t.Errorf("%s(%q): Known() = %v, reads back %q; want unknown(n) carrying %q", name, raw, known, back, raw)
			}
		}
	}
	if _, _, ok := describedVocabulary("no_such_enum", "1"); ok {
		t.Error("describedVocabulary built a value for an enum the description does not define")
	}
	for _, v := range []interface{ Known() bool }{StatusOK, JSONEachRow, ReasonValueChanged, SourceInput, Accepted, FilterOK, VerdictTrue, QueryParamDatabase, KindNone} {
		if !v.Known() {
			t.Errorf("%v: a listed value is not Known()", v)
		}
	}
}
