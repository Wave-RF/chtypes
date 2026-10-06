package chtypes

// rows_pool_test.go — Rows reads its document into a pooled buffer and decodes
// it with pooled row decoders; none of that may show in a result. These tests
// hold the result to that: concurrent calls over the ABI v1 stub (which echoes
// a Rows body back as the document, so the real batch documents in testdata
// travel the whole path: call, TakeInto, the pooled buffer, decodeBatch), and a
// decode from a buffer that is then overwritten.

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"

	"github.com/wave-rf/chtypes/go/internal/abi1"
)

func readBatchDoc(t testing.TB, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// TestDecodeBatchOwnsItsMemory decodes from a buffer, overwrites the buffer,
// and compares the result with one decoded from an untouched copy: nothing the
// result holds may point into the document it was decoded from, because Rows
// reuses that buffer for the next call.
func TestDecodeBatchOwnsItsMemory(t *testing.T) {
	for _, name := range []string{"batch-100-flags0.json", "batch-100-flags7.json"} {
		raw := readBatchDoc(t, name)
		want, err := decodeBatch(bytes.Clone(raw), nil)
		if err != nil {
			t.Fatal(err)
		}
		scratch := bytes.Clone(raw)
		got, err := decodeBatch(scratch, nil)
		if err != nil {
			t.Fatal(err)
		}
		for i := range scratch {
			scratch[i] = 'X'
		}
		// A later decode through the pooled decoders must not reach it either.
		if _, err := decodeBatch(bytes.Clone(raw), nil); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: the result changed when its document's buffer was overwritten", name)
		}
	}
}

// TestDecodeBatchPointersAreSeparate checks the shared chunks the per-row
// pointers come from: a write through one row's pointer changes only that row.
func TestDecodeBatchPointersAreSeparate(t *testing.T) {
	raw := readBatchDoc(t, "batch-100-flags0.json")
	a, err := decodeBatch(raw, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, err := decodeBatch(raw, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(a.Rows) != 100 {
		t.Fatalf("decoded %d rows", len(a.Rows))
	}
	seen := 0
	for i := range a.Rows {
		if a.Rows[i].Verdict != nil {
			*a.Rows[i].Verdict = "zz"
			seen++
		}
		if a.Rows[i].InputSpan != nil {
			a.Rows[i].InputSpan.Off = 1 << 40
			seen++
		}
		if a.Rows[i].PartitionID != nil {
			*a.Rows[i].PartitionID = "zz"
			seen++
		}
	}
	// b is an independent decode, and within a every row kept its own value.
	for i := range b.Rows {
		if v := b.Rows[i].Verdict; v != nil && *v == "zz" {
			t.Fatalf("row %d of a second decode shares memory with the first", i)
		}
	}
	if seen == 0 {
		t.Skip("the document carries no per-row pointer to test")
	}
	if a.Rows[0].InputSpan != nil && a.Rows[1].InputSpan != nil && a.Rows[0].InputSpan == a.Rows[1].InputSpan {
		t.Error("two rows share one InputSpan")
	}
}

// TestStubTakeIntoMatchesTake reads the same deterministic echo through Take
// and through TakeInto, into a destination that is nil, too small, and large
// and full of leftovers: every read gives the same bytes, the large one reuses
// the destination, and a nil buffer gives nil as Take does.
func TestStubTakeIntoMatchesTake(t *testing.T) {
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	defer schema.Close()
	call := func() *abi1.Buf {
		doc, exp, cerr := lib.tbl.PreviewBatch(schema.h, int32(JSONEachRow), []byte(`{"x":1}`), nil, nil, nil, int32(ExportNone), 0)
		if cerr != nil {
			t.Fatal(cerr)
		}
		lib.tbl.Take(exp)
		return doc
	}
	want := lib.tbl.Take(call())
	if len(want) == 0 {
		t.Fatal("the stub's echo document is empty")
	}
	junk := bytes.Repeat([]byte{'J'}, len(want)*2)
	for name, dst := range map[string][]byte{"nil": nil, "small": make([]byte, 0, 3), "large": junk[:0]} {
		got := lib.tbl.TakeInto(call(), dst)
		if !bytes.Equal(got, want) {
			t.Errorf("%s: TakeInto = %q, want %q", name, got, want)
		}
		if name == "large" && &got[0] != &junk[0] {
			t.Errorf("a destination with room was not reused")
		}
	}
	if got := lib.tbl.TakeInto(nil, make([]byte, 0, 8)); got != nil {
		t.Errorf("a nil buffer gave %q, want nil", got)
	}
}

// TestStubRowsConcurrentPooled drives Rows from many goroutines at once over
// documents of different sizes, so the pooled buffers and row decoders are
// taken, grown and handed on while others are in use. It proves the plumbing
// (no call fails, none panics) and, under -race, that the pools are used
// without a data race; that no result points into a pooled buffer is
// TestDecodeBatchOwnsItsMemory's, because the stub's echo is not a batch
// document.
func TestStubRowsConcurrentPooled(t *testing.T) {
	lib := openStub(t, "ok")
	schema, err := lib.CompileTable("CREATE TABLE t (x Int32)")
	if err != nil {
		t.Fatal(err)
	}
	defer schema.Close()
	f, err := schema.CompileFilter("x > 1")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				body := bytes.Repeat([]byte{'a' + byte(g)}, 1+(g*i)%700)
				res, err := schema.Rows(JSONEachRow, body, WithRowFilter(f), WithExport(JSONCompactEachRow), WithDocFlags(0))
				if err != nil {
					t.Errorf("Rows: %v", err)
					return
				}
				if len(res.Payload) == 0 {
					t.Errorf("goroutine %d call %d: the export payload is empty", g, i)
					return
				}
			}
		}(g)
	}
	wg.Wait()
}
