package chtypes

// decode_reference_test.go — the batch decoder as it was before the
// allocation-light rewrite (a copy of decodeBatchGeneric as of the change that
// introduced decode_batch.go), kept only as the reference decode_equivalence
// compares against. It reads through the shared reader helpers of decode.go; a
// change to one of those changes both sides, which is the point of comparing
// the two decoders on the same documents.

import "fmt"

func decodeBatchReference(raw []byte, payload []byte) (BatchResult, error) {
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
