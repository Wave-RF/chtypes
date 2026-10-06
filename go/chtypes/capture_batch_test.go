package chtypes

// capture_batch_test.go — records the real batch documents under testdata/.
// It runs only when CHTYPES_CAPTURE_BATCH_DIR is set; testdata/README.md says
// how (CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 and CHTYPES_CAPTURE_LIBRARY name a
// local library file).

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const captureDDL = "CREATE TABLE t (id UInt64, name String, ts DateTime, score Float64, tags Array(String), note Nullable(String), small Int8, label LowCardinality(String) DEFAULT 'none', total Float64 MATERIALIZED score * 2) ENGINE = MergeTree ORDER BY id"

func captureBody() []byte {
	var b strings.Builder
	for i := 0; i < 100; i++ {
		note := `null`
		if i%3 != 0 {
			note = fmt.Sprintf(`"note %d with \"quotes\" and é"`, i)
		}
		small := fmt.Sprintf("%d", i%100)
		if i%17 == 5 {
			small = "300" // out of range for Int8
		}
		label := ""
		if i%4 != 0 {
			label = fmt.Sprintf(`,"label":"group-%d"`, i%7)
		}
		fmt.Fprintf(&b, `{"id":%d,"name":"user-%04d","ts":"2026-10-%02d 12:%02d:00","score":%d.25,"tags":["a%d","b%d","c"],"note":%s,"small":%s%s}`+"\n",
			i, i, 1+i%28, i%60, i, i%5, i%9, note, small, label)
	}
	return []byte(b.String())
}

func TestCaptureBatchDocuments(t *testing.T) {
	dir := os.Getenv("CHTYPES_CAPTURE_BATCH_DIR")
	if dir == "" {
		t.Skip("SKIPPED: CHTYPES_CAPTURE_BATCH_DIR is not set; no batch document was captured")
	}
	lib, err := OpenUnverified(os.Getenv("CHTYPES_CAPTURE_LIBRARY"), true)
	if err != nil {
		t.Fatal(err)
	}
	sch, err := lib.CompileTable(captureDDL)
	if err != nil {
		t.Fatal(err)
	}
	defer sch.Close()
	body := captureBody()
	for _, flags := range []DocFlags{0, DocAll} {
		docBuf, exportBuf, cerr := sch.lib.tbl.PreviewBatch(sch.h, int32(JSONEachRow), body, nil, nil, nil, int32(ExportNone), uint32(flags))
		if cerr != nil {
			t.Fatal(callError(cerr))
		}
		doc := sch.lib.tbl.Take(docBuf)
		sch.lib.tbl.Take(exportBuf)
		name := filepath.Join(dir, fmt.Sprintf("batch-100-flags%d.json", flags))
		if err := os.WriteFile(name, doc, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s (%d bytes)", name, len(doc))
	}
	// The third document is the shape a filtering caller gets: a filter
	// attached, so the document also carries rows_passed, rows_cut and one
	// row_spans entry per row, and an export asked for.
	flt, cerr := sch.lib.tbl.FilterCreate(sch.h, []byte("small < {m:Int8}"), []byte(`{"m":"50"}`), nil)
	if cerr != nil {
		t.Fatal(callError(cerr))
	}
	defer flt.Close()
	docBuf, exportBuf, cerr := sch.lib.tbl.PreviewBatch(sch.h, int32(JSONEachRow), body, nil, nil, flt, int32(JSONCompactEachRow), 0)
	if cerr != nil {
		t.Fatal(callError(cerr))
	}
	doc := sch.lib.tbl.Take(docBuf)
	sch.lib.tbl.Take(exportBuf)
	name := filepath.Join(dir, "batch-100-filter.json")
	if err := os.WriteFile(name, doc, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Logf("wrote %s (%d bytes)", name, len(doc))
}
