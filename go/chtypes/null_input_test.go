package chtypes

// null_input_test.go — the null_input decode cells, through the public API,
// over the generated v2 stub (variant "null-input"). The stub answers
// scripts/abi-v1/emit/_stubshared.py's NULL_INPUT_DOCS: for a JSON null and a
// TSV \N into each of six non-Nullable types, the entry carries null_input
// true; two controls carry it false and absent. Each entry's expected value is
// read off its name (a "control." name expects false), and the cell count is
// _stubshared.NULL_INPUT_CELL_COUNT, so a document that lost its cells fails.
// The row goes through the generic reader and the batch through the fast one
// (decode_batch.go). Without CHTYPES_ABI2_STUBS this skips loudly by name.

import (
	"fmt"
	"strings"
	"testing"
)

const nullInputCellCount = 12 // _stubshared.NULL_INPUT_CELL_COUNT: 2 formats x 6 types

func checkNullInput(t *testing.T, where string, cols []Value) (cells int) {
	t.Helper()
	for _, c := range cols {
		want := !strings.HasPrefix(c.Column, "control.")
		if want {
			cells++
		}
		if c.NullInput != want {
			t.Errorf("%s: column %q: NullInput = %v, want %v", where, c.Column, c.NullInput, want)
		}
	}
	return cells
}

func TestStubNullInput(t *testing.T) {
	lib := openStub(t, "null-input")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	r, err := schema.Row(JSONEachRow, []byte(`{"x":null}`))
	if err != nil {
		t.Fatalf("row: %v", err)
	}
	if n := checkNullInput(t, "row", r.Columns); n != nullInputCellCount {
		t.Errorf("row: %d null_input cells, want %d", n, nullInputCellCount)
	}
	b, err := schema.Rows(TSV, []byte("\\N\n"))
	if err != nil {
		t.Fatalf("batch: %v", err)
	}
	n := 0
	for i, row := range b.Rows {
		n += checkNullInput(t, fmt.Sprintf("batch.rows[%d]", i), row.Columns)
	}
	if n != nullInputCellCount {
		t.Errorf("batch: %d null_input cells across %d rows, want %d", n, len(b.Rows), nullInputCellCount)
	}
}
