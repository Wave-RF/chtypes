package chtypes

import (
	"os"
	"path/filepath"
	"testing"
)

// BenchmarkDecodeBatch decodes a real 100-row batch document (testdata/README.md
// says how it was produced) at doc_flags 0 and at 7. Run it with -benchmem; the
// allocs/op column is the figure the decoder is judged on.
func BenchmarkDecodeBatch(b *testing.B) {
	for _, tc := range []struct{ name, file string }{
		{"flags0", "batch-100-flags0.json"},
		{"flags7", "batch-100-flags7.json"},
	} {
		raw, err := os.ReadFile(filepath.Join("testdata", tc.file))
		if err != nil {
			b.Fatal(err)
		}
		b.Run(tc.name, func(b *testing.B) {
			b.ReportAllocs()
			b.SetBytes(int64(len(raw)))
			for i := 0; i < b.N; i++ {
				res, err := decodeBatch(raw, nil)
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
