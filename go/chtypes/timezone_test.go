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
	"errors"
	"io"
	"io/fs"
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
	// One directory, one artifact — openLibrary keys on the file's identity,
	// so every registry below opens the exact same image.
	blob, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "libchtypes"+soext)
	if err := os.WriteFile(path, blob, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"),
		[]byte(`{"library":"libchtypes`+soext+`","clickhouse_version":"x","clickhouse_minor":"x","unsafe_families":""}`), 0o644); err != nil {
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
	if !errors.Is(err, ErrInitConflict) {
		t.Errorf("conflict error %q is not errors.Is(err, ErrInitConflict)", err)
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

// stageArtifact copies the test registry's library into dir as a FRESH file
// (its own inode, so no other test has initialized this image) with a minimal
// manifest beside it, and returns the library's path.
func stageArtifact(t *testing.T, src, soext, dir string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "libchtypes"+soext)
	copyFileForTest(t, src, path)
	writeStubManifest(t, dir, soext)
	return path
}

func writeStubManifest(t *testing.T, dir, soext string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"),
		[]byte(`{"library":"libchtypes`+soext+`","clickhouse_version":"x","clickhouse_minor":"x","unsafe_families":""}`), 0o644); err != nil {
		t.Fatal(err)
	}
}

func copyFileForTest(t *testing.T, src, dst string) {
	t.Helper()
	in, err := os.Open(src)
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
}

// loadUnder opens path through a fresh registry over its directory with the
// given timezone — the public path a caller takes — and returns the Library
// on success.
func loadUnder(t *testing.T, path, tz string) (*Library, error) {
	t.Helper()
	r, err := NewRegistry(filepath.Dir(path), WithTimezone(tz))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Load(path); err != nil {
		return nil, err
	}
	libs := r.Libraries()
	if len(libs) != 1 {
		t.Fatalf("one Load must register exactly one library, got %d", len(libs))
	}
	return libs[0], nil
}

// renderEpoch is what the live image says about a zone-sensitive value: a
// bare DateTime column given the epoch, rendered by the library under
// whatever zone its chs_init last set.
func renderEpoch(t *testing.T, lib *Library) string {
	t.Helper()
	cs, err := lib.CompileDDL("x DateTime")
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.Rows(JSONEachRow, []byte(`{"x":0}`), nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Accepted || len(res.Rows) != 1 || len(res.Rows[0].Values) != 1 {
		t.Fatalf("epoch row: %+v", res)
	}
	return res.Rows[0].Values[0].Text
}

// TestHardLinkedArtifactIsTheSameImage is issue #355: a hardlink to a loaded
// artifact is a different PATH to the same FILE, dlopen hands it the image
// already mapped, and a guard keyed on the resolved path let it re-run
// chs_init and move the first opener's zone. Opening the link under another
// zone must be refused as ErrInitConflict and leave the first opener's
// answers unchanged; opening it under the SAME zone is allowed and is the
// same Library; the same path twice under the same zone stays allowed.
func TestHardLinkedArtifactIsTheSameImage(t *testing.T) {
	src, soext := testRegistryLibrary(t)
	root := t.TempDir()
	orig := stageArtifact(t, src, soext, filepath.Join(root, "orig"))
	linkDir := filepath.Join(root, "link")
	if err := os.MkdirAll(linkDir, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(linkDir, "libchtypes"+soext)
	if err := os.Link(orig, link); err != nil {
		t.Fatalf("hardlink %s -> %s: %v", link, orig, err)
	}
	writeStubManifest(t, linkDir, soext)

	libA, err := loadUnder(t, orig, "UTC")
	if err != nil {
		t.Fatalf("first open (UTC) must succeed: %v", err)
	}
	before := renderEpoch(t, libA)

	_, err = loadUnder(t, link, "Asia/Tokyo")
	if err == nil {
		t.Fatal("a hardlink to an image initialized under UTC was opened under Asia/Tokyo: " +
			"chs_init re-ran on the live image (issue #355)")
	}
	if !errors.Is(err, ErrInitConflict) {
		t.Fatalf("hardlink conflict %q is not errors.Is(err, ErrInitConflict)", err)
	}
	for _, want := range []string{`"UTC"`, `"Asia/Tokyo"`, link} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("conflict error %q does not name %s", err.Error(), want)
		}
	}
	if after := renderEpoch(t, libA); after != before {
		t.Fatalf("the refused open moved the live image's zone: epoch rendered %s before, %s after", before, after)
	}

	again, err := loadUnder(t, orig, "UTC")
	if err != nil {
		t.Fatalf("the same path twice under the same zone must be allowed: %v", err)
	}
	if again != libA {
		t.Error("the same path twice must be the same Library")
	}
	viaLink, err := loadUnder(t, link, "UTC")
	if err != nil {
		t.Fatalf("the hardlink under the SAME zone is the same image and zone, and must be allowed: %v", err)
	}
	if viaLink != libA {
		t.Error("the hardlink under the same zone must be the Library already open, not a second init")
	}
}

// TestReplacedArtifactAtAnOpenPathIsTheOpenImage pins the other half of what
// the loader deduplicates on. A path already open names the image it was
// opened as, even after a NEW file (a fresh inode) is renamed over it: the
// loader matches the path before it looks at the file, so keying on the
// inode alone would call it new and re-run chs_init on the live image.
func TestReplacedArtifactAtAnOpenPathIsTheOpenImage(t *testing.T) {
	src, soext := testRegistryLibrary(t)
	path := stageArtifact(t, src, soext, filepath.Join(t.TempDir(), "a"))

	libA, err := loadUnder(t, path, "UTC")
	if err != nil {
		t.Fatalf("first open (UTC) must succeed: %v", err)
	}
	before := renderEpoch(t, libA)
	oldInfo, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	copyFileForTest(t, src, path+".new")
	if err := os.Rename(path+".new", path); err != nil {
		t.Fatal(err)
	}
	newInfo, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if os.SameFile(oldInfo, newInfo) {
		t.Fatal("precondition: the rename must put a different file at the path")
	}

	_, err = loadUnder(t, path, "Asia/Tokyo")
	if !errors.Is(err, ErrInitConflict) {
		t.Fatalf("reopening a replaced path under another zone must be ErrInitConflict, got %v", err)
	}
	if after := renderEpoch(t, libA); after != before {
		t.Fatalf("the refused open moved the live image's zone: epoch rendered %s before, %s after", before, after)
	}
	same, err := loadUnder(t, path, "UTC")
	if err != nil {
		t.Fatalf("reopening the replaced path under the same zone must be allowed: %v", err)
	}
	if same != libA {
		t.Error("the replaced path must still answer with the Library opened there")
	}
}

// TestUnstattableArtifactPathIsRefused: the identity comes from a stat that
// follows symlinks, and a path it cannot stat is refused with the path named
// — never keyed on its spelling instead. A dangling symlink is the case that
// tells stat from lstat. Needs no artifact: nothing is dlopen'd.
func TestUnstattableArtifactPathIsRefused(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "libchtypes.so")
	if err := os.Symlink(filepath.Join(dir, "missing.so"), path); err != nil {
		t.Fatal(err)
	}
	// openLibrary directly — the function that holds the guard, and what
	// Registry.Load calls — because a Registry needs a discoverable line on
	// the search path to be constructed at all, and this case must run on a
	// machine with no artifact anywhere.
	_, err := openLibrary(path, "UTC")
	if err == nil {
		t.Fatal("a path that cannot be stat'ed must be refused")
	}
	if !errors.Is(err, fs.ErrNotExist) {
		t.Errorf("refusal %q does not carry the stat failure (fs.ErrNotExist)", err)
	}
	if errors.Is(err, ErrInitConflict) {
		t.Errorf("a stat failure is not an init conflict: %q", err)
	}
	if !strings.Contains(err.Error(), path) || !strings.Contains(err.Error(), "cannot identify") {
		t.Errorf("refusal %q does not name the path it could not identify", err)
	}
}
