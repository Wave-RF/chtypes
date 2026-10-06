package chtypes

import (
	"os"
	"path/filepath"
	"testing"
)

// benchDocs are the real 100-row batch documents (testdata/README.md says how
// they were produced) at doc_flags 0 and at 7.
var benchDocs = []struct{ name, file string }{
	{"flags0", "batch-100-flags0.json"},
	{"flags7", "batch-100-flags7.json"},
}

// BenchmarkDecodeBatch decodes one document per iteration. Run it with
// -benchmem: allocs/op and B/op are the figures the decoder is judged on, and
// "reference" is the decoder as it was before the rewrite (decode_reference_test.go).
//
//	go test ./chtypes -run '^$' -bench DecodeBatch -benchmem
func BenchmarkDecodeBatch(b *testing.B) {
	for _, tc := range benchDocs {
		raw, err := os.ReadFile(filepath.Join("testdata", tc.file))
		if err != nil {
			b.Fatal(err)
		}
		for _, dec := range []struct {
			name string
			fn   func([]byte, []byte) (BatchResult, error)
		}{{"reference", decodeBatchReference}, {"new", decodeBatch}} {
			b.Run(tc.name+"/"+dec.name, func(b *testing.B) {
				b.ReportAllocs()
				b.SetBytes(int64(len(raw)))
				for i := 0; i < b.N; i++ {
					res, err := dec.fn(raw, nil)
					if err != nil {
						b.Fatal(err)
					}
					if len(res.Rows) != 100 {
						b.Fatalf("decoded %d rows, want 100", len(res.Rows))
					}
				}
			})
		}
	}
}

// BenchmarkDecodeBatchParallel decodes on every goroutine at once, the shape
// of many Schema handles calling Rows together. Compare -cpu 1 with -cpu 8:
// ns/op falls with the goroutine count only while the decoder does not stall
// the others, which a decoder that allocates heavily does (the garbage
// collector runs often and every goroutine waits on it).
//
//	go test ./chtypes -run '^$' -bench DecodeBatchParallel -benchmem -cpu 1,8
func BenchmarkDecodeBatchParallel(b *testing.B) {
	for _, tc := range benchDocs {
		raw, err := os.ReadFile(filepath.Join("testdata", tc.file))
		if err != nil {
			b.Fatal(err)
		}
		for _, dec := range []struct {
			name string
			fn   func([]byte, []byte) (BatchResult, error)
		}{{"reference", decodeBatchReference}, {"new", decodeBatch}} {
			b.Run(tc.name+"/"+dec.name, func(b *testing.B) {
				b.ReportAllocs()
				b.RunParallel(func(pb *testing.PB) {
					for pb.Next() {
						if _, err := dec.fn(raw, nil); err != nil {
							b.Error(err)
							return
						}
					}
				})
			})
		}
	}
}
