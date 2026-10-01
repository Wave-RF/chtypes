// timezone_test.go — the per-registry WithTimezone option and the race fix
// for the process-wide default (issue #300).
//
// Three things are proved here, matching the issue's two asks plus the
// lead's non-breaking ruling:
//
//   - A second open of the SAME artifact image under a DIFFERENT timezone is
//     refused loudly rather than silently reusing the first one's zone — the
//     same conflict rule Python and Rust already enforce
//     (docs/guides/multi-version.md), now mirrored in Go via WithTimezone.
//   - DefaultTimezone()/SetDefaultTimezone() are safe for concurrent use.
//     Before this fix, Timezone was a bare package-level `var` with no
//     accessor at all: a goroutine assigning it directly while another
//     goroutine read it (via openLibrary) was an unsynchronized read/write on
//     a plain string, a data race `go test -race` would catch.
//     TestDefaultTimezoneGetSetIsRaceFree drives exactly that access pattern
//     through the new accessors and is clean under -race.
//   - `chtypes.Timezone = "..."` still compiles and is exactly what
//     DefaultTimezone() reads back — the var was kept, not replaced, so a
//     downstream consumer that assigns it directly under its own mutex is
//     unaffected by this change.
package chtypes

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// TestTimezoneVarStillCompilesAndIsWhatDefaultTimezoneReads is the
// compile-level proof that this change is non-breaking: a downstream
// consumer's `chtypes.Timezone = tz` (the exact spelling the lead's ruling
// names) must still compile, and DefaultTimezone() must answer with exactly
// what was assigned — the var remains the single source of truth,
// DefaultTimezone is only a synchronized way to read it.
func TestTimezoneVarStillCompilesAndIsWhatDefaultTimezoneReads(t *testing.T) {
	original := Timezone
	defer func() { Timezone = original }()

	Timezone = "Asia/Kolkata"
	if got := DefaultTimezone(); got != "Asia/Kolkata" {
		t.Fatalf("DefaultTimezone() = %q after `Timezone = \"Asia/Kolkata\"`, want the same string", got)
	}

	SetDefaultTimezone("Pacific/Auckland")
	if Timezone != "Pacific/Auckland" {
		t.Fatalf("Timezone = %q after SetDefaultTimezone(\"Pacific/Auckland\"), want the same string", Timezone)
	}
}

// TestDefaultTimezoneGetSetIsRaceFree hammers SetDefaultTimezone and
// DefaultTimezone from many goroutines at once. Run with
// `go test -race -run TestDefaultTimezoneGetSetIsRaceFree`: a caller that
// goes through these two accessors exclusively cannot race openLibrary's own
// read of the default (issue #300) — both sides share timezoneMu and the
// race detector has nothing to report. Needs no artifact and no registry —
// it is purely about the package-level state's own synchronization.
func TestDefaultTimezoneGetSetIsRaceFree(t *testing.T) {
	original := DefaultTimezone()
	defer SetDefaultTimezone(original)

	zones := []string{"UTC", "America/New_York", "Europe/Berlin", "Asia/Tokyo", "Pacific/Kiritimati"}
	var wg sync.WaitGroup
	for i := 0; i < 200; i++ {
		wg.Add(2)
		go func(i int) {
			defer wg.Done()
			SetDefaultTimezone(zones[i%len(zones)])
		}(i)
		go func() {
			defer wg.Done()
			if got := DefaultTimezone(); got == "" {
				t.Error("DefaultTimezone() returned an empty string under contention")
			}
		}()
	}
	wg.Wait()
}

// TestOpenLibraryRefusesConflictingTimezone drives WithTimezone's conflict
// rule against a real artifact: the SAME path, opened by two registries that
// disagree on the timezone, must be refused — never silently answered with
// whichever zone got there first. A third registry asking for the SAME
// timezone as the first must succeed and reuse the already-live image.
func TestOpenLibraryRefusesConflictingTimezone(t *testing.T) {
	src, soext := testRegistryLibrary(t)
	dir := t.TempDir()
	// One directory, one artifact — openLibrary keys on the resolved PATH, so
	// every registry below opens the exact same image.
	blob, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "libchtypes"+soext)
	if err := os.WriteFile(path, blob, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"),
		[]byte(`{"library":"libchtypes`+soext+`","clickhouse_version":"x","clickhouse_minor":"x"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	r1, err := NewRegistry(dir, WithTimezone("UTC"))
	if err != nil {
		t.Fatal(err)
	}
	if err := r1.Load(path); err != nil {
		t.Fatalf("first open (UTC) must succeed: %v", err)
	}

	r2, err := NewRegistry(dir, WithTimezone("America/New_York"))
	if err != nil {
		t.Fatal(err)
	}
	err = r2.Load(path)
	if err == nil {
		t.Fatal("a second open of the same image under a DIFFERENT timezone must be refused, " +
			"not silently answered with the first one's zone")
	}
	for _, want := range []string{"UTC", "America/New_York", path} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("conflict error %q does not name %q", err.Error(), want)
		}
	}

	// Same timezone as the first open: this must succeed and must NOT be
	// treated as a conflict — the whole point is one image, one timezone,
	// reused freely by anyone asking for that same timezone.
	r3, err := NewRegistry(dir, WithTimezone("UTC"))
	if err != nil {
		t.Fatal(err)
	}
	if err := r3.Load(path); err != nil {
		t.Fatalf("a second open under the SAME timezone must reuse the live image, got: %v", err)
	}

	// A registry that names no WithTimezone falls back to the process-wide
	// default, which participates in the exact same conflict rule.
	original := DefaultTimezone()
	defer SetDefaultTimezone(original)
	SetDefaultTimezone("Europe/Berlin")
	r4, err := NewRegistry(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := r4.Load(path); err == nil {
		t.Fatal("the process-wide default must be refused too when it conflicts with the live image's timezone")
	}
	SetDefaultTimezone("UTC")
	r5, err := NewRegistry(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := r5.Load(path); err != nil {
		t.Fatalf("the process-wide default must succeed once it agrees with the live image's timezone, got: %v", err)
	}
}
