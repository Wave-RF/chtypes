package chtypes

// null_input_test.go — the null_input decode cells, through the public API,
// over the generated v2 stub (variant "null-input"). The stub answers
// scripts/abi-v1/emit/_stubshared.py's NULL_INPUT_DOCS: for a JSON null and a
// TSV \N into each of six non-Nullable types, the entry carries null_input
// true; two controls carry it false and absent. Each entry's expected value is
// read off its name (a "control." name expects false), and the cell count is
// _stubshared.NULL_INPUT_CELL_COUNT, so a document that lost its cells fails.
// The Enum8 cells also read Poison true (null-as-default stores 0, outside the
// enum), every other cell and control false.
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
		// A NULL into a non-Nullable Enum8 stores 0, outside the enum: poisoned.
		wantPoison := want && strings.HasSuffix(c.Column, ".enum8")
		if c.Poison != wantPoison {
			t.Errorf("%s: column %q: Poison = %v, want %v", where, c.Column, c.Poison, wantPoison)
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

// The poison and dup_dropped cells, variant "value-flags"
// (_stubshared.VALUE_FLAGS_DOCS): a JSON null or TSV \N into a non-Nullable
// Enum8 (poison, in JSONEachRow and TSV) and one canned entry that carries
// dup_dropped, plus four controls that hold each member false and absent. A
// "poison." entry expects Poison true and DupDropped false, a "dup." entry the
// reverse, a "control." entry both false,
// so a binding that swaps the members fails.
const (
	valueFlagsPoisonCells = 2 // _stubshared.VALUE_FLAGS_POISON_CELLS: 2 formats
	valueFlagsDupCells    = 1 // _stubshared.VALUE_FLAGS_DUP_CELLS
)

func checkValueFlags(t *testing.T, where string, cols []Value) (poison, dup int) {
	t.Helper()
	for _, c := range cols {
		wantP, wantD := false, false
		switch {
		case strings.HasPrefix(c.Column, "poison."):
			wantP = true
			poison++
		case strings.HasPrefix(c.Column, "dup."):
			wantD = true
			dup++
			// The producing input: a repeated top-level JSONEachRow key
			// after every column was read; the first value is stored.
			if c.Column != "dup.JSONEachRow.repeated_top_key" || c.Text != "1" {
				t.Errorf("%s: dup cell %q stores %q, want dup.JSONEachRow.repeated_top_key storing \"1\"", where, c.Column, c.Text)
			}
		case !strings.HasPrefix(c.Column, "control."):
			t.Errorf("%s: unexpected column %q", where, c.Column)
		}
		if c.Poison != wantP || c.DupDropped != wantD {
			t.Errorf("%s: column %q: Poison = %v, DupDropped = %v, want %v, %v",
				where, c.Column, c.Poison, c.DupDropped, wantP, wantD)
		}
	}
	return poison, dup
}

func TestStubValueFlags(t *testing.T) {
	lib := openStub(t, "value-flags")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	r, err := schema.Row(JSONEachRow, []byte(`{"a":1,"a":2}`))
	if err != nil {
		t.Fatalf("row: %v", err)
	}
	if p, d := checkValueFlags(t, "row", r.Columns); p != valueFlagsPoisonCells || d != valueFlagsDupCells {
		t.Errorf("row: %d poison and %d dup_dropped cells, want %d and %d", p, d, valueFlagsPoisonCells, valueFlagsDupCells)
	}
	b, err := schema.Rows(TSV, []byte("1\n"))
	if err != nil {
		t.Fatalf("batch: %v", err)
	}
	reasons := 0
	for _, tr := range r.Transformed {
		if tr.Reason == ReasonDuplicateKeyDropped && tr.Column == "dup.JSONEachRow.repeated_top_key" {
			reasons++
		}
	}
	if reasons != 1 {
		t.Errorf("row: %d duplicate_key_dropped transforms for the dup cell, want 1", reasons)
	}
	p, d := 0, 0
	for i, row := range b.Rows {
		rp, rd := checkValueFlags(t, fmt.Sprintf("batch.rows[%d]", i), row.Columns)
		p, d = p+rp, d+rd
	}
	if p != valueFlagsPoisonCells || d != valueFlagsDupCells {
		t.Errorf("batch: %d poison and %d dup_dropped cells across %d rows, want %d and %d", p, d, len(b.Rows), valueFlagsPoisonCells, valueFlagsDupCells)
	}
}
