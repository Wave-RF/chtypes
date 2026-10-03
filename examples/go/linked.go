//go:build chtypes_linked

// linked.go is the one demonstration only the STATICALLY LINKED Go shape can
// give (bindings-v1.md section 2, "open the linked image"; the Registry is the
// product path). Built only with `-tags chtypes_linked`, which needs the
// artifact producer's build tree on CGO_LDFLAGS. Without the tag,
// linked_stub.go prints what was skipped.
package main

import (
	"fmt"

	"github.com/wave-rf/chtypes/go/chtypes"
)

// ---------------------------------------------------------------------------
// SECTION 14 — The linked image (Go only)
//
// WHAT: the second loader shape only Go has: ONE library linked at build
// time, no dlopen, opened by chtypes.OpenLinked.
// WHY: it is the fast path for a binary that serves exactly one ClickHouse
// version. It is the SAME Library type over the SAME code path as the
// registry: only the table filler differs, and every method behaves alike.
// LOOK FOR: the same verdict the registry gives, and no fetch record (the
// loader skips the signature steps for a linked image).
// C API: the same functions, resolved by the linker instead of dlsym.
// ---------------------------------------------------------------------------
func section14() {
	section(14, "The linked image (Go only)")

	lib, err := chtypes.OpenLinked()
	must(err)
	kv("OpenLinked", lib.Version+"  (the build tree this binary linked)")
	kv("  Resolved()", fmt.Sprintf("%v   <- nil: a linked image has no fetch record", lib.Resolved()))

	canon, err := lib.ValidateType("Decimal(18,4)")
	kv("ValidateType", "Decimal(18,4) -> "+okOr(err, canon))

	s, err := lib.CompileTable(tableOf("a UInt8, s String DEFAULT 'x'"))
	must(err)
	defer s.Close()
	r, err := s.Row(chtypes.JSONEachRow, []byte(`{"a":300}`))
	must(err)
	kv("  Row {\"a\":300}", string(r.Outcome))
	for _, t := range r.Transformed {
		raw(fmt.Sprintf("      ~ %s: %s -> %s (%s)", t.Column, t.Input, t.Stored, t.Reason))
	}
	note("the same verdicts as the Registry path: same C functions, same")
	note("vendored ClickHouse, different loader (300 wrapped to 44, reported)")
}
