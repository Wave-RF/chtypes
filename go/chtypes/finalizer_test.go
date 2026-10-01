// finalizer_test.go — the GC backstop for a never-Closed LoadedSchema
// (issue #302): newLoadedSchema's runtime.SetFinalizer(s, func(x)
// { x.Close() }), added alongside the existing LoadedFilter/LoadedBlock ones.
package chtypes

import (
	"runtime"
	"testing"
	"time"
)

// TestLoadedSchemaFinalizerRunsForAnUnclosedSchema proves the finalizer
// actually engages for a schema nobody ever called Close() on.
//
// A test cannot hold a reference to the schema AND observe its finalizer
// running — holding one keeps it reachable and the finalizer never fires. So
// the schema is built and abandoned entirely inside an inner function, and
// what the test waits on is a SEPARATE runtime.AddCleanup registered on the
// SAME object at construction time. AddCleanup (Go 1.24+) is deliberately
// used instead of a second runtime.SetFinalizer: Go allows only ONE
// finalizer per object, so a second SetFinalizer call here would silently
// REPLACE newLoadedSchema's own — which calls Close() — with this test's,
// proving nothing about the production path. AddCleanup coexists with an
// existing SetFinalizer by design, and the runtime only runs it once the
// object is reachable from nowhere else, which is the same point
// SetFinalizer's own callback runs.
func TestLoadedSchemaFinalizerRunsForAnUnclosedSchema(t *testing.T) {
	r := testRegistry(t)
	libs := r.Libraries()
	if len(libs) == 0 {
		t.Fatal("testRegistry opened no libraries")
	}
	lib := libs[0]

	done := make(chan struct{})
	func() {
		s, err := lib.CompileDDL("x UInt8")
		if err != nil {
			t.Fatal(err)
		}
		runtime.AddCleanup(s, func(_ int) { close(done) }, 0)
		// s is deliberately never Close()'d and never referenced again.
	}()

	deadline := time.NewTimer(10 * time.Second)
	defer deadline.Stop()
	for {
		select {
		case <-done:
			return
		case <-deadline.C:
			t.Fatal("the LoadedSchema finalizer did not run within 10s of repeated runtime.GC")
		default:
			runtime.GC()
			time.Sleep(20 * time.Millisecond)
		}
	}
}
