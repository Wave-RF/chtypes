package main

import "testing"

// The planted case: a build that ADVERTISES the insert call but whose call is
// declined must fail the tour, never skip; one that does not advertise it skips.
func TestInsertGate(t *testing.T) {
	advertised := missingFrom([]string{featPreviewInsert, featMemoryLimit}, []string{exportNative},
		[]string{featPreviewInsert, featMemoryLimit}, []string{exportNative})
	if len(advertised) != 0 {
		t.Fatalf("advertised set read as missing %v", advertised)
	}
	if skip, failure := insertGate(advertised, true); skip || failure == "" {
		t.Fatalf("advertised but declined: skip=%v failure=%q, want a failure", skip, failure)
	}
	if skip, failure := insertGate(advertised, false); skip || failure != "" {
		t.Fatalf("advertised and implemented: skip=%v failure=%q, want a run", skip, failure)
	}
	absent := missingFrom(nil, nil, []string{featPreviewInsert}, []string{exportNative})
	if len(absent) != 2 {
		t.Fatalf("absent set read as missing %v", absent)
	}
	if skip, failure := insertGate(absent, true); !skip || failure != "" {
		t.Fatalf("not advertised: skip=%v failure=%q, want a skip", skip, failure)
	}
}
