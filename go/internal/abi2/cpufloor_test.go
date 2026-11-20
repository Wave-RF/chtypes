package abi2

// cpufloor_test.go — step 2b on the linked path: LoadLinked reads the same three
// answers through a table that is already filled, with no resolve. A linked
// build's table comes from the linker; ResolveAll over a dlopen'd stub fills the
// same table, so this runs on every leg with no linked binary.

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadLinkedRefusesBelowTheCPUFloor(t *testing.T) {
	dir := os.Getenv("CHTYPES_ABI2_STUBS")
	if dir == "" {
		t.Skip("SKIPPED: CHTYPES_ABI2_STUBS is not set (scripts/abi-v1/build-stubs.sh --out DIR)")
	}
	tbl, errmsg := OpenLibrary(filepath.Join(dir, "cpu-floor-refused.so"))
	if tbl == nil {
		t.Fatalf("opening the stub: %s", errmsg)
	}
	if missing := tbl.ResolveAll(); missing != "" {
		t.Fatalf("the stub lacks %s", missing)
	}
	_, err := LoadLinked(tbl, nil, nil)
	var le *LoadError
	if !errors.As(err, &le) {
		t.Fatalf("got %T (%v); want a *LoadError", err, err)
	}
	if le.Reason != "cpu_floor" || le.Path != "<linked>" || le.Want != "x86-64-v3+pclmul" || le.Got != "avx2,bmi2" {
		t.Errorf("%+v", le)
	}
	want := "this CPU cannot run this library: missing avx2,bmi2 (the 26.8 linux-amd64 build requires x86-64-v3+pclmul). Use a CPU at or above that floor."
	if le.Message() != want || le.Code() != "CHTYPES_CPU_UNSUPPORTED" {
		t.Errorf("message %q, code %q", le.Message(), le.Code())
	}
}
