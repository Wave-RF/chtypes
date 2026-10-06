//go:build chtypes_linked

package chtypes

import "github.com/wave-rf/chtypes/go/internal/abi1"

// OpenLinked opens the statically linked library: the same Library, over the
// table the linked filler fills from the symbols the linker resolved. It runs
// loader steps 3, 4, 6 and 7 (step 6 is satisfied by the linker) and skips 1,
// 2 and 5; one code path serves both modes, and only the table filler
// differs. It needs a core build tree via CGO_LDFLAGS, and -tags chtypes_linked.
func OpenLinked() (l *Library, err error) {
	gen := setupGeneration()
	defer func() {
		if err != nil {
			failedOpen(gen)
		}
	}()
	return openImage("linked", func(zone, defaults []byte) (*abi1.Table, error) {
		return abi1.LoadLinked(abi1.OpenLinked(), zone, defaults)
	}, "<linked>", nil)
}
