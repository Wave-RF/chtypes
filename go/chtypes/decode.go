package chtypes

// decode.go — the document decoders (bindings-v1.md section 5). Each document
// is parsed once by the strict JSON reader the loader already uses (stock
// encoding/json, refusing a duplicate key at any depth), then read field by
// field. Absent is the default and an unknown key is ignored; a value of the
// wrong JSON type, a duplicate key, or a name carried both ways or neither is
// an *InternalError naming the document and the key. Nothing is computed.

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"strconv"

	"github.com/wave-rf/chtypes/go/internal/abi1"
)

const b64Suffix = "_b64"

// reader reads one document. The first problem sticks: every accessor after it
// returns zero values, and finish reports it once.
type reader struct {
	doc string
	err error
}

func (r *reader) fail(path, format string, args ...any) {
	if r.err == nil {
		r.err = internalError("the %s document: %s: %s", r.doc, path, fmt.Sprintf(format, args...))
	}
}

func parseDocument(doc string, raw []byte) (any, error) {
	v, err := abi1.DecodeStrictJSON(raw)
	if err != nil {
		return nil, internalError("the %s document does not decode: %v", doc, err)
	}
	return v, nil
}

func (r *reader) object(v any, path string) map[string]any {
	if r.err != nil {
		return nil
	}
	m, ok := v.(map[string]any)
	if !ok {
		r.fail(path, "want a JSON object")
		return nil
	}
	return m
}

func (r *reader) array(v any, path string) []any {
	if r.err != nil || v == nil {
		return nil
	}
	a, ok := v.([]any)
	if !ok {
		r.fail(path, "want a JSON array")
		return nil
	}
	return a
}

// field returns the raw value of key, or nil when absent.
func (r *reader) field(m map[string]any, key string) any {
	if r.err != nil {
		return nil
	}
	return m[key]
}

// text reads a plain string field; absent is "".
func (r *reader) text(m map[string]any, key, path string) string {
	v := r.field(m, key)
	if v == nil {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		r.fail(path+"."+key, "want a JSON string")
	}
	return s
}

// bytes reads a data-derived string field: "<key>" holds it as UTF-8 text, or
// "<key>_b64" holds the raw bytes in standard base64. Absent is "". Both
// present is an InternalError.
func (r *reader) bytes(m map[string]any, key, path string) string {
	if r.err != nil {
		return ""
	}
	s, _, err := byteField(m, key, path)
	if err != nil {
		r.fail(path+"."+key, "%v", err)
	}
	return s
}

// name reads a column name: exactly one of "<key>" and "<key>_b64".
func (r *reader) name(m map[string]any, key, path string) string {
	if r.err != nil {
		return ""
	}
	s, present, err := byteField(m, key, path)
	if err != nil {
		r.fail(path+"."+key, "%v", err)
		return ""
	}
	if !present {
		r.fail(path+"."+key, "a name needs %q or %q, found neither", key, key+b64Suffix)
	}
	return s
}

// byteField is the one rule for a byte string in a document.
func byteField(m map[string]any, key, path string) (value string, present bool, err error) {
	plain, havePlain := m[key]
	b64, haveB64 := m[key+b64Suffix]
	switch {
	case havePlain && haveB64:
		return "", true, fmt.Errorf("both %q and %q are present", key, key+b64Suffix)
	case havePlain:
		if plain == nil {
			return "", false, nil
		}
		s, ok := plain.(string)
		if !ok {
			return "", true, fmt.Errorf("want a JSON string")
		}
		return s, true, nil
	case haveB64:
		s, ok := b64.(string)
		if !ok {
			return "", true, fmt.Errorf("%q: want a JSON string", key+b64Suffix)
		}
		raw, derr := base64.StdEncoding.DecodeString(s)
		if derr != nil {
			return "", true, fmt.Errorf("%q is not standard base64: %w", key+b64Suffix, derr)
		}
		return string(raw), true, nil
	}
	return "", false, nil
}

// nameList reads a list of names: each element a {"name"} or {"name_b64"}
// object, never a bare string.
func (r *reader) nameList(m map[string]any, key, path string) []string {
	arr := r.array(r.field(m, key), path+"."+key)
	if arr == nil {
		return nil
	}
	out := make([]string, 0, len(arr))
	for i, e := range arr {
		p := fmt.Sprintf("%s.%s[%d]", path, key, i)
		em := r.object(e, p)
		if em == nil {
			return nil
		}
		out = append(out, r.name(em, "name", p))
	}
	return out
}

// rawValue reads value_b64: the raw bytes of a scalar String or FixedString
// value, nil when absent.
func (r *reader) rawValue(m map[string]any, path string) *string {
	v := r.field(m, "value_b64")
	if v == nil {
		return nil
	}
	s, ok := v.(string)
	if !ok {
		r.fail(path+".value_b64", "want a JSON string")
		return nil
	}
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		r.fail(path+".value_b64", "not standard base64: %v", err)
		return nil
	}
	str := string(raw)
	return &str
}

// optBytes reads a data-derived string that may be absent: nil when absent.
func (r *reader) optBytes(m map[string]any, key, path string) *string {
	if r.err != nil {
		return nil
	}
	s, present, err := byteField(m, key, path)
	if err != nil {
		r.fail(path+"."+key, "%v", err)
		return nil
	}
	if !present {
		return nil
	}
	return &s
}

func (r *reader) number(v any, path string) (json.Number, bool) {
	switch n := v.(type) {
	case json.Number:
		return n, true
	case string: // an integer beyond 2^53 travels as a string
		return json.Number(n), true
	}
	r.fail(path, "want a JSON integer")
	return "", false
}

func (r *reader) u64(m map[string]any, key, path string) uint64 {
	v := r.field(m, key)
	if v == nil {
		return 0
	}
	return r.u64Of(v, path+"."+key)
}

func (r *reader) u64Of(v any, path string) uint64 {
	n, ok := r.number(v, path)
	if !ok {
		return 0
	}
	u, err := strconv.ParseUint(string(n), 10, 64)
	if err != nil {
		r.fail(path, "want an unsigned 64-bit integer, got %s", n)
	}
	return u
}

func (r *reader) i32(m map[string]any, key, path string) int32 {
	v := r.field(m, key)
	if v == nil {
		return 0
	}
	n, ok := r.number(v, path+"."+key)
	if !ok {
		return 0
	}
	i, err := strconv.ParseInt(string(n), 10, 32)
	if err != nil || i < math.MinInt32 || i > math.MaxInt32 {
		r.fail(path+"."+key, "want a 32-bit integer, got %s", n)
	}
	return int32(i)
}

func (r *reader) int(m map[string]any, key, path string) int {
	v := r.field(m, key)
	if v == nil {
		return 0
	}
	n, ok := r.number(v, path+"."+key)
	if !ok {
		return 0
	}
	i, err := strconv.ParseInt(string(n), 10, 64)
	if err != nil || i < 0 || i > math.MaxInt32 {
		r.fail(path+"."+key, "want a non-negative integer, got %s", n)
	}
	return int(i)
}

func (r *reader) boolean(m map[string]any, key, path string) bool {
	v := r.field(m, key)
	if v == nil {
		return false
	}
	b, ok := v.(bool)
	if !ok {
		r.fail(path+"."+key, "want a JSON boolean")
	}
	return b
}

// optBool reads a boolean that may be JSON null (unknown): nil for unknown,
// never false.
func (r *reader) optBool(m map[string]any, key, path string) *bool {
	v := r.field(m, key)
	if v == nil {
		return nil
	}
	b, ok := v.(bool)
	if !ok {
		r.fail(path+"."+key, "want a JSON boolean or null")
		return nil
	}
	return &b
}

func (r *reader) textList(m map[string]any, key, path string) []string {
	arr := r.array(r.field(m, key), path+"."+key)
	if arr == nil {
		return nil
	}
	out := make([]string, 0, len(arr))
	for i, e := range arr {
		s, ok := e.(string)
		if !ok {
			r.fail(fmt.Sprintf("%s.%s[%d]", path, key, i), "want a JSON string")
			return nil
		}
		out = append(out, s)
	}
	return out
}

func (r *reader) span(v any, path string) *Span {
	m := r.object(v, path)
	if m == nil {
		return nil
	}
	return &Span{Off: r.u64(m, "off", path), Len: r.u64(m, "len", path)}
}

func (r *reader) spans(m map[string]any, key, path string) []Span {
	arr := r.array(r.field(m, key), path+"."+key)
	if arr == nil {
		return nil
	}
	out := make([]Span, 0, len(arr))
	for i, e := range arr {
		if s := r.span(e, fmt.Sprintf("%s.%s[%d]", path, key, i)); s != nil {
			out = append(out, *s)
		}
	}
	return out
}

// decodeTransforms reads a list of transform entries.
func (r *reader) transforms(m map[string]any, key, path string) []Transform {
	arr := r.array(r.field(m, key), path+"."+key)
	if arr == nil {
		return nil
	}
	out := make([]Transform, 0, len(arr))
	for i, e := range arr {
		p := fmt.Sprintf("%s.%s[%d]", path, key, i)
		em := r.object(e, p)
		if em == nil {
			return nil
		}
		reason := Reason(r.text(em, "reason", p))
		out = append(out, Transform{
			Column: r.name(em, "column", p),
			Input:  r.bytes(em, "input", p),
			Stored: r.bytes(em, "stored", p),
			Reason: reason,
			Lossy:  reason.Lossy(),
			Row:    r.int(em, "row", p),
		})
	}
	return out
}

// row reads one row document (a chs_preview_row document, or an entry of a
// batch's rows).
func (r *reader) row(m map[string]any, path string) RowResult {
	res := RowResult{
		Outcome:             parseRowOutcome(r.text(m, "outcome", path)),
		ErrCode:             r.i32(m, "code", path),
		ErrMsg:              r.bytes(m, "err", path),
		Transformed:         r.transforms(m, "transformed", path),
		UnsupportedSettings: r.nameList(m, "unsupported_settings", path),
		UnknownFields:       r.nameList(m, "unknown_fields", path),
		VerdictCode:         r.i32(m, "verdict_code", path),
		VerdictErr:          r.bytes(m, "verdict_err", path),
	}
	if arr := r.array(r.field(m, "cols"), path+".cols"); arr != nil {
		for i, e := range arr {
			p := fmt.Sprintf("%s.cols[%d]", path, i)
			cm := r.object(e, p)
			if cm == nil {
				break
			}
			src := Source(r.text(cm, "src", p))
			if r.err == nil && !src.known() {
				r.fail(p+".src", "%q is not a value_src value", string(src))
				break
			}
			v := Value{
				Column:   r.name(cm, "name", p),
				Text:     r.bytes(cm, "stored", p),
				Value:    r.rawValue(cm, p),
				Null:     r.boolean(cm, "null", p),
				Source:   src,
				IsStored: src.IsStored(),
			}
			res.Columns = append(res.Columns, v)
			if v.IsStored {
				res.Values = append(res.Values, v)
			}
		}
	}
	if arr := r.array(r.field(m, "computed"), path+".computed"); arr != nil {
		for i, e := range arr {
			p := fmt.Sprintf("%s.computed[%d]", path, i)
			cm := r.object(e, p)
			if cm == nil {
				break
			}
			res.Computed = append(res.Computed, Computed{
				Column: r.name(cm, "name", p),
				Kind:   r.text(cm, "kind", p),
				Text:   r.bytes(cm, "stored", p),
				Value:  r.rawValue(cm, p),
			})
		}
	}
	if v := r.field(m, "verdict"); v != nil {
		s, ok := v.(string)
		if !ok {
			r.fail(path+".verdict", "want a JSON string")
		} else {
			verdict := parseVerdict(s)
			res.Verdict = &verdict
		}
	}
	res.PartitionID = r.optBytes(m, "partition_id", path)
	if v := r.field(m, "input_span"); v != nil {
		res.InputSpan = r.span(v, path+".input_span")
	}
	return res
}

func decodeRow(raw []byte) (RowResult, error) {
	v, err := parseDocument("row", raw)
	if err != nil {
		return RowResult{}, err
	}
	r := &reader{doc: "row"}
	m := r.object(v, "$")
	res := r.row(m, "$")
	return res, r.err
}

func (r *reader) header(v any, path string) *Header {
	if v == nil {
		return nil
	}
	m := r.object(v, path)
	if m == nil {
		return nil
	}
	h := &Header{Consumed: r.boolean(m, "consumed", path), Lines: r.int(m, "lines", path)}
	if arr := r.array(r.field(m, "names"), path+".names"); arr != nil {
		h.Names = make([]string, 0, len(arr))
		for i, e := range arr {
			em := r.object(e, fmt.Sprintf("%s.names[%d]", path, i))
			if em == nil {
				break
			}
			h.Names = append(h.Names, r.name(em, "name", fmt.Sprintf("%s.names[%d]", path, i)))
		}
	}
	return h
}

// decodeBatchGeneric is the strict generic reader of a batch document: the
// whole document as a tree, read field by field. decodeBatch (decode_batch.go)
// answers first and falls back here for any document it will not judge.
func decodeBatchGeneric(raw []byte, payload []byte) (BatchResult, error) {
	v, err := parseDocument("batch", raw)
	if err != nil {
		return BatchResult{}, err
	}
	r := &reader{doc: "batch"}
	m := r.object(v, "$")
	res := BatchResult{
		Outcome:        parseBatchOutcome(r.text(m, "outcome", "$")),
		ErrCode:        r.i32(m, "code", "$"),
		ErrMsg:         r.bytes(m, "err", "$"),
		RowsRead:       r.u64(m, "rows_read", "$"),
		RowsSkipped:    r.u64(m, "rows_skipped", "$"),
		Transformed:    r.transforms(m, "transformed", "$"),
		Payload:        payload,
		Spans:          r.spans(m, "row_spans", "$"),
		ExportDeclined: r.bytes(m, "export_declined", "$"),
		RowsPassed:     r.u64(m, "rows_passed", "$"),
		RowsCut:        r.u64(m, "rows_cut", "$"),
		Unconsumed:     r.spans(m, "unconsumed", "$"),
	}
	if arr := r.array(r.field(m, "rows"), "$.rows"); arr != nil {
		res.Rows = make([]RowResult, 0, len(arr))
		for i, e := range arr {
			p := fmt.Sprintf("$.rows[%d]", i)
			rm := r.object(e, p)
			if rm == nil {
				break
			}
			res.Rows = append(res.Rows, r.row(rm, p))
		}
	}
	if arr := r.array(r.field(m, "engine_rows"), "$.engine_rows"); arr != nil {
		res.EngineRows = make([][]EngineCell, 0, len(arr))
		for i, row := range arr {
			rp := fmt.Sprintf("$.engine_rows[%d]", i)
			cells := r.array(row, rp)
			if cells == nil && r.err == nil {
				cells = []any{}
			}
			out := make([]EngineCell, 0, len(cells))
			for j, c := range cells {
				p := fmt.Sprintf("%s[%d]", rp, j)
				cm := r.object(c, p)
				if cm == nil {
					break
				}
				out = append(out, EngineCell{
					Column: r.name(cm, "name", p),
					Text:   r.bytes(cm, "stored", p),
					Null:   r.boolean(cm, "null", p),
					Value:  r.rawValue(cm, p),
				})
			}
			res.EngineRows = append(res.EngineRows, out)
		}
	}
	if v := r.field(m, "partition_count"); v != nil {
		n := r.u64Of(v, "$.partition_count")
		res.PartitionCount = &n
	}
	if v := r.field(m, "framing"); v != nil {
		fm := r.object(v, "$.framing")
		if fm != nil {
			res.Framing = &Framing{
				BomSkipped: r.optBool(fm, "bom_skipped", "$.framing"),
				Header:     r.header(r.field(fm, "header"), "$.framing.header"),
			}
			if c := r.field(fm, "container"); c != nil {
				s, ok := c.(string)
				if !ok {
					r.fail("$.framing.container", "want a JSON string or null")
				}
				res.Framing.Container = s
			}
		}
	}
	return res, r.err
}

func decodeFilterResult(raw []byte) (FilterResult, error) {
	v, err := parseDocument("filter_result", raw)
	if err != nil {
		return FilterResult{}, err
	}
	r := &reader{doc: "filter_result"}
	m := r.object(v, "$")
	res := FilterResult{
		Outcome:             parseFilterOutcome(r.text(m, "outcome", "$")),
		ErrCode:             r.i32(m, "code", "$"),
		ErrMsg:              r.bytes(m, "err", "$"),
		RowsRead:            r.u64(m, "rows_read", "$"),
		UnsupportedSettings: r.nameList(m, "unsupported_settings", "$"),
	}
	for _, c := range r.text(m, "verdicts", "$") {
		res.Verdicts = append(res.Verdicts, parseVerdict(string(c)))
	}
	if arr := r.array(r.field(m, "errors"), "$.errors"); arr != nil {
		for i, e := range arr {
			p := fmt.Sprintf("$.errors[%d]", i)
			em := r.object(e, p)
			if em == nil {
				break
			}
			res.Errors = append(res.Errors, FilterRowError{
				Row:  r.int(em, "row", p),
				Code: r.i32(em, "code", p),
				Msg:  r.bytes(em, "err", p),
			})
		}
	}
	return res, r.err
}

func decodeSchemaDescription(raw []byte) (SchemaDescription, error) {
	v, err := parseDocument("schema_description", raw)
	if err != nil {
		return SchemaDescription{}, err
	}
	r := &reader{doc: "schema_description"}
	m := r.object(v, "$")
	var res SchemaDescription
	if arr := r.array(r.field(m, "columns"), "$.columns"); arr != nil {
		for i, e := range arr {
			p := fmt.Sprintf("$.columns[%d]", i)
			cm := r.object(e, p)
			if cm == nil {
				break
			}
			kind := DefaultKind(r.text(cm, "default_kind", p))
			if r.err == nil && !kind.known() {
				r.fail(p+".default_kind", "%q is not a default_kind value", string(kind))
				break
			}
			res.Columns = append(res.Columns, Column{
				Name:        r.name(cm, "name", p),
				Type:        r.bytes(cm, "type", p),
				DefaultKind: kind,
				DefaultExpr: r.bytes(cm, "default_expression", p),
			})
		}
	}
	return res, r.err
}

func decodeDiscovery(raw []byte) (Discovery, error) {
	v, err := parseDocument("discovery", raw)
	if err != nil {
		return Discovery{}, err
	}
	r := &reader{doc: "discovery"}
	m := r.object(v, "$")
	res := Discovery{ColumnsSQL: r.bytes(m, "columns_sql", "$")}
	if arr := r.array(r.field(m, "columns"), "$.columns"); arr != nil {
		for i, e := range arr {
			p := fmt.Sprintf("$.columns[%d]", i)
			cm := r.object(e, p)
			if cm == nil {
				break
			}
			res.Columns = append(res.Columns, DiscoveredColumn{
				Name:        r.name(cm, "name", p),
				Declaration: r.bytes(cm, "declaration", p),
			})
		}
	}
	return res, r.err
}

func decodeErrorCodes(raw []byte) (*ErrorCodeTable, error) {
	v, err := parseDocument("error_code_table", raw)
	if err != nil {
		return nil, err
	}
	r := &reader{doc: "error_code_table"}
	arr := r.array(v, "$")
	if arr == nil && r.err == nil && v == nil {
		r.fail("$", "want a JSON array")
	}
	t := &ErrorCodeTable{byCode: map[int32]string{}, byName: map[string]int32{}}
	for i, e := range arr {
		p := fmt.Sprintf("$[%d]", i)
		em := r.object(e, p)
		if em == nil {
			break
		}
		code, name := r.i32(em, "code", p), r.text(em, "name", p)
		t.entries = append(t.entries, ErrorCodeEntry{Code: code, Name: name})
		t.byCode[code] = name
		t.byName[name] = code
	}
	return t, r.err
}

func decodeLiveHandles(raw []byte) (map[string]uint64, error) {
	v, err := parseDocument("live_handles", raw)
	if err != nil {
		return nil, err
	}
	r := &reader{doc: "live_handles"}
	m := r.object(v, "$")
	out := make(map[string]uint64, len(m))
	for k, e := range m {
		out[k] = r.u64Of(e, "$."+k)
	}
	return out, r.err
}

func decodeBuildInfo(raw []byte) (BuildInfo, error) {
	v, err := parseDocument("build_info", raw)
	if err != nil {
		return BuildInfo{}, err
	}
	r := &reader{doc: "build_info"}
	m := r.object(v, "$")
	bi := BuildInfo{
		Schema:            r.int(m, "schema", "$"),
		ABI:               r.int(m, "abi", "$"),
		ABIFingerprint:    r.text(m, "abi_fingerprint", "$"),
		ClickHouseVersion: r.text(m, "clickhouse_version", "$"),
		Channel:           r.text(m, "channel", "$"),
		ClickHouseMinor:   r.text(m, "clickhouse_minor", "$"),
		ClickHouseCommit:  r.text(m, "clickhouse_commit", "$"),
		CoreCommit:        r.text(m, "core_commit", "$"),
		Build:             r.text(m, "build", "$"),
		InputsSHA256:      r.text(m, "inputs_sha256", "$"),
		OS:                r.text(m, "os", "$"),
		Arch:              r.text(m, "arch", "$"),
		Raw:               append([]byte(nil), raw...),
	}
	if tc := r.field(m, "toolchain"); tc != nil {
		if tm := r.object(tc, "$.toolchain"); tm != nil {
			bi.Toolchain = tm
		}
	}
	if cv := r.field(m, "capabilities"); cv != nil {
		if cm := r.object(cv, "$.capabilities"); cm != nil {
			bi.Capabilities = Capabilities{
				InputFormats:  r.textList(cm, "input_formats", "$.capabilities"),
				ExportFormats: r.textList(cm, "export_formats", "$.capabilities"),
				DocFlags:      r.textList(cm, "doc_flags", "$.capabilities"),
				Features:      r.textList(cm, "features", "$.capabilities"),
			}
		}
	}
	return bi, r.err
}
