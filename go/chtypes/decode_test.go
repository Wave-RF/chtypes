package chtypes

import (
	"errors"
	"strings"
	"testing"
)

// The decoders are driven with documents written by hand in the shape of
// spec/abi-v1/docs: what they prove is the decoding rules of bindings-v1.md
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
    {"name": "a\u0000b", "src": "absent", "null": false, "stored_b64": "/wE="}
  ],
  "transformed": [
    {"column": "a", "input": "300", "stored": "44", "reason": "overflow_wrap", "row": 0},
    {"column_b64": "/w==", "input": "1.0", "stored": "1", "reason": "reformat", "row": 2},
    {"column": "z", "input": "q", "stored": "r", "reason": "a_reason_nobody_listed", "row": 3}
  ],
  "unknown_fields": ["u1", {"name_b64": "/v8="}],
  "unsupported_settings": ["s1"],
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
	if len(r.Columns) != 4 {
		t.Fatalf("Columns = %d, want 4", len(r.Columns))
	}
	// A name is bytes whichever way it arrives: base64 raw bytes, and NUL.
	if got := r.Columns[1].Column; got != "\xff\x00" {
		t.Errorf("name_b64 = %q, want the decoded raw bytes", got)
	}
	if got := r.Columns[3].Column; got != "a\x00b" {
		t.Errorf("name with NUL = %q", got)
	}
	if got := r.Columns[3].Text; got != "\xff\x01" {
		t.Errorf("stored_b64 = %q", got)
	}
	// Values is the subset whose is_stored is true, in order: ephemeral_input
	// is the one entry that is not.
	if len(r.Values) != 3 {
		t.Fatalf("Values = %d, want 3 (every entry but ephemeral_input)", len(r.Values))
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
		"duplicate key":          `{"outcome": "accepted", "outcome": "rejected"}`,
		"duplicate key, nested":  `{"cols": [{"name": "a", "name": "b", "src": "input"}]}`,
		"name both ways":         `{"cols": [{"name": "a", "name_b64": "YQ==", "src": "input"}]}`,
		"name neither way":       `{"cols": [{"src": "input"}]}`,
		"name, bad base64":       `{"cols": [{"name_b64": "!!", "src": "input"}]}`,
		"wrong type":             `{"code": "7x"}`,
		"cols not an array":      `{"cols": {"name": "a"}}`,
		"source not in the list": `{"cols": [{"name": "a", "src": "no_such_source"}]}`,
		"source absent":          `{"cols": [{"name": "a"}]}`,
		"not an object":          `[1]`,
		"not JSON":               `{`,
		"trailing data":          `{} {}`,
		"stored both ways":       `{"cols": [{"name": "a", "src": "input", "stored": "1", "stored_b64": "MQ=="}]}`,
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
	  "engine_rows": ["{\"a\":1}"],
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
	if len(b.EngineRows) != 1 || string(b.EngineRows[0]) != `{"a":1}` {
		t.Errorf("EngineRows = %q", b.EngineRows)
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

func TestUnknownOutcomesReadAsFallback(t *testing.T) {
	b, err := decodeBatch([]byte(`{"outcome":"something_new"}`), nil)
	if err != nil || b.Outcome != Unsupported {
		t.Errorf("an unknown batch outcome = %q, %v, want the description's fallback", b.Outcome, err)
	}
	f, err := decodeFilterResult([]byte(`{"outcome":"something_new","verdicts":"tfedx"}`))
	if err != nil || f.Outcome != FilterUnsupported {
		t.Errorf("an unknown filter outcome = %q, %v", f.Outcome, err)
	}
	if len(f.Verdicts) != 5 || f.Verdicts[4] != VerdictDecline {
		t.Errorf("Verdicts = %q: an unknown verdict reads as its fallback", f.Verdicts)
	}
	want := []bool{true, true, false, false, false}
	for i, v := range f.Verdicts {
		if v.Answered() != want[i] {
			t.Errorf("verdict %d (%q).Answered() = %v", i, v, v.Answered())
		}
	}
}

func TestDecodeFilterResult(t *testing.T) {
	f, err := decodeFilterResult([]byte(`{"outcome":"ok","rows_read":3,"verdicts":"tfe","errors":[{"row":2,"code":53,"err":"x"}],"unsupported_settings":["s"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if f.Outcome != FilterOK || f.RowsRead != 3 || len(f.Verdicts) != 3 || len(f.Errors) != 1 || f.Errors[0].Code != 53 || f.Errors[0].Msg != "x" {
		t.Errorf("filter result = %+v", f)
	}
}

func TestDecodeSchemaDescriptionAndDiscovery(t *testing.T) {
	d, err := decodeSchemaDescription([]byte(`{"columns":[{"name":"a","type":"UInt8","default_kind":"DEFAULT","default_expr":"1"},{"name_b64":"/w==","type":"String","default_kind":""}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(d.Columns) != 2 || d.Columns[0].DefaultKind != KindDefault || d.Columns[0].DefaultExpr != "1" ||
		d.Columns[1].Name != "\xff" || d.Columns[1].DefaultKind != KindNone {
		t.Errorf("description = %+v", d)
	}
	if _, err := decodeSchemaDescription([]byte(`{"columns":[{"name":"a","default_kind":"BOGUS"}]}`)); err == nil {
		t.Error("a default_kind the description does not list must be refused: it has no fallback")
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
