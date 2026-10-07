package chtypes

import (
	"errors"
	"strings"
	"testing"
)

// The decoders are driven with documents written by hand in the shape of
// spec/abi-v2/docs: what they prove is the decoding rules of bindings-v1.md
// section 5 (names as bytes whichever way they arrive, unknown as nil, vocab
// facts read from the generated tables, duplicate keys refused), not the
// library's behavior.

const rowDoc = `{
  "outcome": "accepted",
  "code": 0,
  "err": "",
  "input_span": {"off": 0, "len": 12},
  "cols": [
    {"name": "a", "src": "input", "null": false, "input": "1", "stored": "1"},
    {"name_b64": "/wA=", "src": "default_generated", "null": true, "stored": "x"},
    {"name": "e", "src": "ephemeral_input", "null": false, "stored": "9"},
    {"name": "s", "src": "input", "null": false, "stored_b64": "/wCA", "value_b64": "/wCA"},
    {"name": "a\u0000b", "src": "absent", "null": false, "stored_b64": "/wE="}
  ],
  "transformed": [
    {"column": "a", "input": "300", "stored": "44", "reason": "overflow_wrap", "row": 0},
    {"column_b64": "/w==", "input": "1.0", "stored": "1", "reason": "reformat", "row": 2},
    {"column": "z", "input": "q", "stored": "r", "reason": "a_reason_nobody_listed", "row": 3}
  ],
  "unknown_fields": [{"name": "u1"}, {"name_b64": "/v8="}],
  "unsupported_settings": [{"name": "s1"}],
  "computed": [{"name": "m", "kind": "materialized", "stored": "5"}],
  "verdict": "t",
  "verdict_code": 0,
  "partition_id": "all"
}`

func TestDecodeRow(t *testing.T) {
	r, err := decodeRow([]byte(rowDoc))
	if err != nil {
		t.Fatal(err)
	}
	if r.Outcome != Accepted {
		t.Errorf("Outcome = %q", r.Outcome)
	}
	if len(r.Columns) != 5 {
		t.Fatalf("Columns = %d, want 5", len(r.Columns))
	}
	// A name is bytes whichever way it arrives: base64 raw bytes, and NUL.
	if got := r.Columns[1].Column; got != "\xff\x00" {
		t.Errorf("name_b64 = %q, want the decoded raw bytes", got)
	}
	if got := r.Columns[4].Column; got != "a\x00b" {
		t.Errorf("name with NUL = %q", got)
	}
	if got := r.Columns[4].Text; got != "\xff\x01" {
		t.Errorf("stored_b64 = %q", got)
	}
	// Values is the subset whose is_stored is true, in order: ephemeral_input
	// is the one entry that is not.
	if len(r.Values) != 4 {
		t.Fatalf("Values = %d, want 4 (every entry but ephemeral_input)", len(r.Values))
	}
	for _, v := range r.Values {
		if v.Source == SourceEphemeralInput {
			t.Errorf("Values holds an unstored entry: %+v", v)
		}
	}
	if !r.Columns[1].IsStored || r.Columns[1].Source != SourceDefaultGenerated || !r.Columns[1].Null {
		t.Errorf("default_generated entry = %+v, want stored and null", r.Columns[1])
	}
	if r.Columns[2].IsStored {
		t.Errorf("ephemeral_input read as stored")
	}
	if v := r.Columns[3]; v.Value == nil || *v.Value != "\xff\x00\x80" || v.Text != "\xff\x00\x80" {
		t.Errorf("a String value carries its raw bytes beside the rendering: %+v", v)
	}
	if r.Columns[0].Value != nil {
		t.Errorf("an entry with no value_b64 has no Value: %+v", r.Columns[0])
	}
	if len(r.Transformed) != 3 {
		t.Fatalf("Transformed = %d", len(r.Transformed))
	}
	if tr := r.Transformed[0]; tr.Reason != ReasonOverflowWrap || !tr.Lossy || tr.Row != 0 {
		t.Errorf("transform 0 = %+v", tr)
	}
	if tr := r.Transformed[1]; tr.Reason != ReasonReformat || tr.Lossy || tr.Column != "\xff" || tr.Row != 2 {
		t.Errorf("transform 1 = %+v, want a lossless reformat on a raw-byte column", tr)
	}
	// A reason the table does not list keeps its spelling and takes the
	// fallback's lossy fact (value_changed is lossy).
	if tr := r.Transformed[2]; tr.Reason != "a_reason_nobody_listed" || !tr.Lossy {
		t.Errorf("transform 2 = %+v, want the fallback's lossy fact", tr)
	}
	if len(r.UnknownFields) != 2 || r.UnknownFields[0] != "u1" || r.UnknownFields[1] != "\xfe\xff" {
		t.Errorf("UnknownFields = %q", r.UnknownFields)
	}
	if len(r.Computed) != 1 || r.Computed[0].Text != "5" {
		t.Errorf("Computed = %+v", r.Computed)
	}
	if r.Verdict == nil || *r.Verdict != VerdictTrue || !r.Verdict.Answered() {
		t.Errorf("Verdict = %v", r.Verdict)
	}
	if r.PartitionID == nil || *r.PartitionID != "all" {
		t.Errorf("PartitionID = %v", r.PartitionID)
	}
	if r.InputSpan == nil || r.InputSpan.Len != 12 {
		t.Errorf("InputSpan = %v", r.InputSpan)
	}
}

func TestDecodeRowAbsentIsDefault(t *testing.T) {
	r, err := decodeRow([]byte(`{"outcome": "rejected", "code": 53, "err": "boom", "future_field": [1,2]}`))
	if err != nil {
		t.Fatal(err)
	}
	if r.Outcome != Rejected || r.ErrCode != 53 || r.ErrMsg != "boom" {
		t.Errorf("row = %+v", r)
	}
	if r.Verdict != nil || r.PartitionID != nil || r.InputSpan != nil {
		t.Errorf("an absent optional is nil, got %+v", r)
	}
}

func TestDecodeRowRefusals(t *testing.T) {
	cases := map[string]string{
		"unknown field as a bare string": `{"unknown_fields": ["u"]}`,
		"duplicate key":                  `{"outcome": "accepted", "outcome": "rejected"}`,
		"duplicate key, nested":          `{"cols": [{"name": "a", "name": "b", "src": "input"}]}`,
		"name both ways":                 `{"cols": [{"name": "a", "name_b64": "YQ==", "src": "input"}]}`,
		"name neither way":               `{"cols": [{"src": "input"}]}`,
		"name, bad base64":               `{"cols": [{"name_b64": "!!", "src": "input"}]}`,
		"wrong type":                     `{"code": "7x"}`,
		"cols not an array":              `{"cols": {"name": "a"}}`,
		"source absent":                  `{"cols": [{"name": "a"}]}`,
		"not an object":                  `[1]`,
		"not JSON":                       `{`,
		"trailing data":                  `{} {}`,
		"stored both ways":               `{"cols": [{"name": "a", "src": "input", "stored": "1", "stored_b64": "MQ=="}]}`,
	}
	for name, doc := range cases {
		_, err := decodeRow([]byte(doc))
		var ie *InternalError
		if !errors.As(err, &ie) {
			t.Errorf("%s: err = %v, want an *InternalError", name, err)
			continue
		}
		if !strings.Contains(ie.Error(), "row") {
			t.Errorf("%s: the message does not name the document: %v", name, ie)
		}
	}
}

func TestDecodeBatch(t *testing.T) {
	doc := `{
	  "outcome": "accepted_poisoned", "code": 0, "err": "",
	  "rows_read": 2, "rows_skipped": 1,
	  "rows": [{"outcome": "accepted", "input_span": {"off": 0, "len": 3}}, {"outcome": "skipped", "input_span": {"off": 3, "len": 4}, "err": "bad"}],
	  "transformed": [{"column": "a", "input": "1", "stored": "2", "reason": "ttl_expired", "row": 1}],
	  "engine_rows": [[{"name":"a","stored":"1","null":false},{"name_b64":"/w==","stored_b64":"/wA=","value_b64":"/wA=","null":false},{"name":"n","null":true}]],
	  "row_spans": [{"off": 0, "len": 5}],
	  "export_declined": "",
	  "rows_passed": 1, "rows_cut": 0,
	  "partition_count": 4,
	  "unconsumed": [{"off": 9, "len": 2}],
	  "framing": {"bom_skipped": null, "container": null, "header": null}
	}`
	b, err := decodeBatch([]byte(doc), []byte("payload"))
	if err != nil {
		t.Fatal(err)
	}
	if b.Outcome != AcceptedPoisoned || b.RowsRead != 2 || b.RowsSkipped != 1 || len(b.Rows) != 2 {
		t.Errorf("batch = %+v", b)
	}
	if b.Rows[1].Outcome != Skipped || b.Rows[1].ErrMsg != "bad" {
		t.Errorf("row 1 = %+v", b.Rows[1])
	}
	if string(b.Payload) != "payload" || len(b.Spans) != 1 || b.Spans[0].Len != 5 {
		t.Errorf("payload/spans = %q %v", b.Payload, b.Spans)
	}
	if len(b.Transformed) != 1 || b.Transformed[0].Reason != ReasonTTLExpired || b.Transformed[0].Row != 1 {
		t.Errorf("Transformed = %+v: the library's flat list, not stamped here", b.Transformed)
	}
	if len(b.EngineRows) != 1 || len(b.EngineRows[0]) != 3 {
		t.Fatalf("EngineRows = %+v: each engine row is a list of cells", b.EngineRows)
	}
	if c := b.EngineRows[0][1]; c.Column != "\xff" || c.Text != "\xff\x00" || c.Value == nil || *c.Value != "\xff\x00" || c.Null {
		t.Errorf("cell 1 = %+v", c)
	}
	if c := b.EngineRows[0][2]; !c.Null || c.Value != nil {
		t.Errorf("cell 2 = %+v", c)
	}
	if b.PartitionCount == nil || *b.PartitionCount != 4 {
		t.Errorf("PartitionCount = %v", b.PartitionCount)
	}
	if len(b.Unconsumed) != 1 || b.Unconsumed[0].Off != 9 {
		t.Errorf("Unconsumed = %v", b.Unconsumed)
	}
	// Unknown is never false and never empty.
	if b.Framing == nil || b.Framing.BomSkipped != nil || b.Framing.Header != nil || b.Framing.Container != "" {
		t.Errorf("Framing = %+v, want BomSkipped and Header nil (unknown)", b.Framing)
	}
}

// atMergeBatchDoc is a batch carrying ABI v2's at_merge list in every shape
// the spec gives an entry, and the batch's own unsupported_settings (public
// issue #544). It has no unknown member, so the fast decoder answers it too
// (decode_equivalence_test.go compares the two on it).
const atMergeBatchDoc = `{
  "outcome": "accepted",
  "unsupported_settings": [{"name": "session_timezone"}, {"name_b64": "/w=="}],
  "at_merge": [
    {"row": 0, "reason": "ttl_delete"},
    {"row": 1, "reason": "ttl_column_reset", "column": "c", "stored": "0"},
    {"row": 2, "reason": "ttl_column_reset", "column": "t"},
    {"row": 3, "reason": "ttl_column_reset", "column_b64": "/wA=", "stored_b64": "/w=="},
    {"row": 4, "reason": "ttl_delete", "input_rows": [4, 7]},
    {"row": 5, "reason": "a_reason_nobody_listed"},
    {"row": 6, "reason": "ttl_column_reset", "column": "e", "stored": "", "input_rows": []}
  ]
}`

// ABI v2's at_merge (public issue #544): each entry decodes one to one, an
// absent column, stored or input_rows is nil (never an empty value), an
// unlisted reason is its unknown(n) and never fails the batch (r3), and an
// unknown member is ignored (r2).
func TestDecodeBatchAtMerge(t *testing.T) {
	b, err := decodeBatch([]byte(atMergeBatchDoc), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(b.AtMerge) != 7 {
		t.Fatalf("AtMerge = %+v, want 7 entries", b.AtMerge)
	}
	str := func(p *string) string {
		if p == nil {
			return "<nil>"
		}
		return *p
	}
	for i, tc := range []struct {
		reason        MergeReason
		column        string
		stored        string
		inputRows     []int
		inputRowsNil  bool
		known         bool
		columnPresent bool
	}{
		{MergeTTLDelete, "<nil>", "<nil>", nil, true, true, false},
		{MergeTTLColumnReset, "c", "0", nil, true, true, true},
		{MergeTTLColumnReset, "t", "<nil>", nil, true, true, true},       // stored absent: decided at the merge
		{MergeTTLColumnReset, "\xff\x00", "\xff", nil, true, true, true}, // both by their _b64 form
		{MergeTTLDelete, "<nil>", "<nil>", []int{4, 7}, false, true, false},
		{"a_reason_nobody_listed", "<nil>", "<nil>", nil, true, false, false},
		{MergeTTLColumnReset, "e", "", []int{}, false, true, true}, // an empty stored is not an absent one
	} {
		e := b.AtMerge[i]
		if e.Row != i || e.Reason != tc.reason || e.Reason.Known() != tc.known || str(e.Column) != tc.column || str(e.Stored) != tc.stored {
			t.Errorf("entry %d = {Row %d Reason %q Known %v Column %q Stored %q}, want {%d %q %v %q %q}",
				i, e.Row, e.Reason, e.Reason.Known(), str(e.Column), str(e.Stored), i, tc.reason, tc.known, tc.column, tc.stored)
		}
		if (e.Column != nil) != tc.columnPresent {
			t.Errorf("entry %d: Column present = %v, want %v", i, e.Column != nil, tc.columnPresent)
		}
		if (e.InputRows == nil) != tc.inputRowsNil || len(e.InputRows) != len(tc.inputRows) {
			t.Errorf("entry %d: InputRows = %#v, want %#v", i, e.InputRows, tc.inputRows)
		}
		for j := range tc.inputRows {
			if e.InputRows[j] != tc.inputRows[j] {
				t.Errorf("entry %d: InputRows = %v, want %v", i, e.InputRows, tc.inputRows)
			}
		}
	}
	if len(b.UnsupportedSettings) != 2 || b.UnsupportedSettings[0] != "session_timezone" || b.UnsupportedSettings[1] != "\xff" {
		t.Errorf("UnsupportedSettings = %q, want the call's own declined settings as bytes", b.UnsupportedSettings)
	}

	// An unknown member, in an entry and beside the list, is ignored (r2).
	b, err = decodeBatch([]byte(`{"outcome":"accepted","at_merge":[{"row":0,"reason":"ttl_delete","x_future":{"a":[1]},"x_future_b64":"/w=="}],"x_future_list":[1]}`), nil)
	if err != nil || len(b.AtMerge) != 1 || b.AtMerge[0].Reason != MergeTTLDelete || b.AtMerge[0].Column != nil {
		t.Errorf("at_merge with unknown members = %+v, %v", b.AtMerge, err)
	}

	// Absent is empty: nothing would happen at a merge, and no call setting was declined.
	for _, doc := range []string{`{"outcome":"accepted"}`, `{"outcome":"accepted","at_merge":null,"unsupported_settings":null}`} {
		b, err = decodeBatch([]byte(doc), nil)
		if err != nil || b.AtMerge != nil || b.UnsupportedSettings != nil {
			t.Errorf("%s: AtMerge = %#v, UnsupportedSettings = %#v, %v; want both nil", doc, b.AtMerge, b.UnsupportedSettings, err)
		}
	}
	b, err = decodeBatch([]byte(`{"outcome":"accepted","at_merge":[],"unsupported_settings":[]}`), nil)
	if err != nil || len(b.AtMerge) != 0 || len(b.UnsupportedSettings) != 0 {
		t.Errorf("empty lists = %+v, %v", b, err)
	}

	// A document that breaks its schema is an InternalError naming the key.
	for _, bad := range []string{
		`{"at_merge":[{"row":0,"reason":"ttl_column_reset","column":"c","column_b64":"Yw=="}]}`,
		`{"at_merge":[{"row":0,"reason":"ttl_column_reset","column":"c","stored_b64":"!!"}]}`,
		`{"at_merge":[{"row":0,"reason":"ttl_delete","input_rows":["x"]}]}`,
		`{"at_merge":[{"row":0,"reason":"ttl_delete","input_rows":[-1]}]}`,
		`{"at_merge":[{"row":0,"reason":7}]}`,
		`{"at_merge":{"row":0}}`,
		`{"unsupported_settings":["session_timezone"]}`,
	} {
		_, err := decodeBatch([]byte(bad), nil)
		var ie *InternalError
		if !errors.As(err, &ie) {
			t.Errorf("%s: err = %v, want an *InternalError", bad, err)
		}
	}
}

func TestDecodeBatchFramingKnown(t *testing.T) {
	doc := `{"outcome":"accepted","rows":[],"unconsumed":[],"framing":{"bom_skipped":false,"container":"array","header":{"consumed":true,"lines":1,"names":[{"name":"a"},{"name_b64":"/w=="}]}}}`
	b, err := decodeBatch([]byte(doc), nil)
	if err != nil {
		t.Fatal(err)
	}
	f := b.Framing
	if f == nil || f.BomSkipped == nil || *f.BomSkipped || f.Container != "array" || f.Header == nil {
		t.Fatalf("Framing = %+v", f)
	}
	if !f.Header.Consumed || f.Header.Lines != 1 || len(f.Header.Names) != 2 || f.Header.Names[1] != "\xff" {
		t.Errorf("Header = %+v", f.Header)
	}
	if b.Payload != nil {
		t.Errorf("an absent export buffer is nil, got %v", b.Payload)
	}
	b, err = decodeBatch([]byte(`{"outcome":"accepted"}`), []byte{})
	if err != nil || b.Payload == nil || len(b.Payload) != 0 {
		t.Errorf("an empty export buffer is non-nil and empty: %v %v", b.Payload, err)
	}
}

func TestDecodeBatchBigIntegerAsString(t *testing.T) {
	b, err := decodeBatch([]byte(`{"outcome":"accepted","rows_read":"18446744073709551615"}`), nil)
	if err != nil || b.RowsRead != 18446744073709551615 {
		t.Errorf("RowsRead = %d, %v", b.RowsRead, err)
	}
}

// ABI v2 rule r3: an unlisted outcome or verdict is kept as its unknown(n),
// never replaced by its fallback, and the fallback's fail-closed reading
// stays: an unknown outcome is never accepted, an unknown verdict never
// answered.
func TestUnknownOutcomesAreKeptAndFailClosed(t *testing.T) {
	b, err := decodeBatch([]byte(`{"outcome":"something_new"}`), nil)
	if err != nil || b.Outcome != "something_new" || b.Outcome.Known() || b.Outcome == Accepted {
		t.Errorf("an unknown batch outcome = %q, %v, want it kept as unknown(n)", b.Outcome, err)
	}
	f, err := decodeFilterResult([]byte(`{"outcome":"something_new","verdicts":"tfedx"}`))
	if err != nil || f.Outcome != "something_new" || f.Outcome.Known() || f.Outcome == FilterOK {
		t.Errorf("an unknown filter outcome = %q, %v", f.Outcome, err)
	}
	if len(f.Verdicts) != 5 || f.Verdicts[4] != "x" || f.Verdicts[4].Known() {
		t.Errorf("Verdicts = %q: an unknown verdict is kept as unknown(n)", f.Verdicts)
	}
	want := []bool{true, true, false, false, false}
	for i, v := range f.Verdicts {
		if v.Answered() != want[i] {
			t.Errorf("verdict %d (%q).Answered() = %v", i, v, v.Answered())
		}
	}
}

func TestDecodeNameLists(t *testing.T) {
	f, err := decodeFilterResult([]byte(`{"outcome":"ok","unsupported_settings":[{"name":"a"},{"name_b64":"/w=="}],"errors":[{"row":0,"code":1,"err_b64":"/w=="}]}`))
	if err != nil || len(f.UnsupportedSettings) != 2 || f.UnsupportedSettings[1] != "\xff" || f.Errors[0].Msg != "\xff" {
		t.Errorf("%+v, %v", f, err)
	}
	for _, bad := range []string{`{"unsupported_settings":[{"name":"a","name_b64":"YQ=="}]}`, `{"unsupported_settings":[{}]}`} {
		if _, err := decodeFilterResult([]byte(bad)); err == nil {
			t.Errorf("%s: both forms or neither is an InternalError", bad)
		}
	}
}

func TestDecodeFilterResult(t *testing.T) {
	f, err := decodeFilterResult([]byte(`{"outcome":"ok","rows_read":3,"verdicts":"tfe","errors":[{"row":2,"code":53,"err":"x"}],"unsupported_settings":[{"name":"s"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if f.Outcome != FilterOK || f.RowsRead != 3 || len(f.Verdicts) != 3 || len(f.Errors) != 1 || f.Errors[0].Code != 53 || f.Errors[0].Msg != "x" {
		t.Errorf("filter result = %+v", f)
	}
}

func TestDecodeSchemaDescriptionAndDiscovery(t *testing.T) {
	d, err := decodeSchemaDescription([]byte(`{"columns":[{"name":"a","type":"UInt8","default_kind":"DEFAULT","default_expression":"1"},{"name_b64":"/w==","type":"String","default_kind":""}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Columns) != 2 || d.Columns[0].DefaultKind != KindDefault || d.Columns[0].DefaultExpr != "1" ||
		d.Columns[1].Name != "\xff" || d.Columns[1].DefaultKind != KindNone {
		t.Errorf("description = %+v", d)
	}
	// ABI v2 rule r3: a default_kind the description does not list is its
	// unknown(n), kept for that column, and never fails the document.
	if u, err := decodeSchemaDescription([]byte(`{"columns":[{"name":"a","default_kind":"BOGUS"},{"name":"b","default_kind":"ALIAS"}]}`)); err != nil ||
		len(u.Columns) != 2 || u.Columns[0].DefaultKind != "BOGUS" || u.Columns[0].DefaultKind.Known() ||
		u.Columns[1].DefaultKind != KindAlias {
		t.Errorf("an unlisted default_kind = %+v, %v: want it kept as unknown(n), the next column decoded", u, err)
	}
	dc, err := decodeDiscovery([]byte(`{"columns":[{"name":"a","declaration":"` + "`a` UInt8" + `"}]}`))
	if err != nil || len(dc.Columns) != 1 || dc.Columns[0].Declaration != "`a` UInt8" {
		t.Errorf("discovery = %+v, %v", dc, err)
	}
}

func TestDecodeErrorCodesAndLiveHandles(t *testing.T) {
	tab, err := decodeErrorCodes([]byte(`[{"code":0,"name":"OK"},{"code":53,"name":"TYPE_MISMATCH"}]`))
	if err != nil {
		t.Fatal(err)
	}
	if n, ok := tab.Name(53); !ok || n != "TYPE_MISMATCH" {
		t.Errorf("Name(53) = %q, %v", n, ok)
	}
	if c, ok := tab.Code("OK"); !ok || c != 0 {
		t.Errorf("Code(OK) = %d, %v", c, ok)
	}
	if _, ok := tab.Name(999); ok {
		t.Error("an unknown code is absent, never synthesized")
	}
	if _, ok := tab.Code("ok"); ok {
		t.Error("names match exactly")
	}
	if len(tab.All()) != 2 {
		t.Errorf("All = %v", tab.All())
	}
	if _, err := decodeErrorCodes([]byte(`{"code":1}`)); err == nil {
		t.Error("an error-code table is an array")
	}
	h, err := decodeLiveHandles([]byte(`{"chs_schema":2,"chs_filter":0}`))
	if err != nil || h["chs_schema"] != 2 || h["chs_filter"] != 0 {
		t.Errorf("live handles = %v, %v", h, err)
	}
}

func TestDecodeBuildInfo(t *testing.T) {
	raw := `{"schema":1,"abi":1,"abi_fingerprint":"sha256:00","clickhouse_version":"26.8.1.2","channel":"lts","clickhouse_minor":"26.8","clickhouse_commit":"c","core_commit":"d","build":"b","inputs_sha256":"i","os":"linux","arch":"amd64","toolchain":{"cc":"clang"},"capabilities":{"input_formats":["CSV"],"export_formats":["CSV"],"doc_flags":["values"],"features":["default_generators"]}}`
	bi, err := decodeBuildInfo([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if bi.ClickHouseVersion != "26.8.1.2" || bi.ClickHouseMinor != "26.8" || bi.Channel != "lts" || bi.ABI != 1 ||
		bi.Toolchain["cc"] != "clang" || len(bi.Capabilities.Features) != 1 || string(bi.Raw) != raw {
		t.Errorf("build info = %+v", bi)
	}
}
