package chtypes

// lazy_test.go — loading is lazy, and WithPreload is the one eager path.
//
// The sentence this file exists to hold is the same sentence in all four
// bindings: constructing a registry reads manifest.json files and dlopens
// nothing, and nothing in the package opens an artifact except a request for a
// specific version or an explicit preload.
//
// The proof needs no real artifact and inspects no code. Every "library" below
// is a text file, so any open at all fails at dlopen — a registry that opened
// one would return the error from whichever call opened it. What is counted is
// Registry.Libraries(), which is populated by the real load path and by
// nothing else.

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

// isolatedRegistry lays out a directory of N stand-in artifacts and takes
// $CHTYPES_REGISTRY and the per-user cache out of the search path, so
// Versions() answers about this directory rather than about whatever this
// machine happens to have fetched.
func isolatedRegistry(t *testing.T, lines ...string) string {
	t.Helper()
	t.Setenv(EnvRegistry, "")
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	dir := t.TempDir()
	for _, line := range lines {
		writeFakeArtifact(t, dir, line, map[string]any{
			"library":            "libchtypes.so",
			"clickhouse_version": line + ".1.1",
			"clickhouse_minor":   line,
		}, []byte("not a shared library"))
	}
	return dir
}

func TestConstructionAndListingOpenNothing(t *testing.T) {
	dir := isolatedRegistry(t, "25.8", "25.10", "26.7")

	r, err := NewRegistry(dir)
	if err != nil {
		t.Fatalf("construction must open nothing, and so must not fail on a library that cannot dlopen: %v", err)
	}
	if got := len(r.Libraries()); got != 0 {
		t.Fatalf("construction opened %d libraries; it must open none", got)
	}

	// Versions() answers from the manifest scan, not from what is open.
	want := "25.8,25.10,26.7"
	if got := strings.Join(r.Versions(), ","); got != want {
		t.Fatalf("Versions() = %v, want %v (loaded OR discovered, in release order)", got, want)
	}
	// And asking cost nothing: these are listings, not loads.
	if n := len(r.Libraries()); n != 0 {
		t.Fatalf("a listing opened %d libraries; it must open none", n)
	}

	// The first request for a line is what opens it — and here that open
	// reaches dlopen and dies there, which is the proof it reached dlopen at
	// all rather than being skipped.
	if _, err := r.For("25.8"); err == nil || !strings.Contains(err.Error(), "dlopen") {
		t.Fatalf("For must open the line it is asked for; got: %v", err)
	}
}

func TestPreloadOpensExactlyTheNamedLines(t *testing.T) {
	dir := isolatedRegistry(t, "25.8", "25.10", "26.7")

	// A preloaded line is opened before the constructor returns, so a text
	// file's dlopen failure arrives from NewRegistry rather than from For.
	_, err := NewRegistry(dir, WithPreload("25.10"))
	if err == nil || !strings.Contains(err.Error(), "dlopen") {
		t.Fatalf("WithPreload must open its lines at construction; got: %v", err)
	}
	if !strings.Contains(err.Error(), "25.10") {
		t.Fatalf("the preload failure must name the line it opened; got: %v", err)
	}
	// A different entry proves it is the LIST that decides which line opens,
	// not the directory listing.
	_, err = NewRegistry(dir, WithPreload("26.7"))
	if err == nil || !strings.Contains(err.Error(), "26.7") {
		t.Fatalf("the preload failure must name the line it opened; got: %v", err)
	}
	// An empty list is exactly the default, not a special case.
	r, err := NewRegistry(dir, WithPreload())
	if err != nil {
		t.Fatalf("an empty preload list must be the default: %v", err)
	}
	if n := len(r.Libraries()); n != 0 {
		t.Fatalf("an empty preload list opened %d libraries", n)
	}
}

func TestPreloadOfAnUnknownLineFailsAtConstruction(t *testing.T) {
	dir := isolatedRegistry(t, "25.8")

	_, err := NewRegistry(dir, WithPreload("26.7"))
	if !errors.Is(err, ErrArtifactMissing) {
		t.Fatalf("a preload entry no directory holds must be the §7 artifact-missing error, got: %v", err)
	}
	if !strings.Contains(err.Error(), "26.7") || !strings.Contains(err.Error(), dir) {
		t.Fatalf("the refusal must name the line and every directory looked in; got: %v", err)
	}
}

func TestPreloadNeverFetches(t *testing.T) {
	dir := isolatedRegistry(t, "25.8")

	// With autofetch on, a missing line asked for through For would begin a
	// download. A missing PRELOAD line must not: autofetch is a first-use
	// behavior, and a constructor is a worse place than a request to start a
	// 250 MB transfer. A fetch here would fail with a fetch verdict instead.
	_, err := NewRegistry(dir, WithAutoFetch(true), WithPreload("26.7"))
	if !errors.Is(err, ErrArtifactMissing) {
		t.Fatalf("preload must not fetch, even with autofetch on; got: %v", err)
	}
}

func TestAnUnreadableNamedDirectoryStillFailsAtConstruction(t *testing.T) {
	t.Setenv(EnvRegistry, "")
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	// The typo guard survives lazy loading and costs no dlopen: a directory the
	// caller NAMED and cannot be read is a configuration mistake, and the
	// message names the path.
	missing := filepath.Join(t.TempDir(), "chtyeps")
	_, err := NewRegistry(missing)
	if err == nil || !strings.Contains(err.Error(), missing) {
		t.Fatalf("a named directory that does not exist must fail at construction, naming it; got: %v", err)
	}
}

func TestAnEmptySearchPathStillFailsAtConstruction(t *testing.T) {
	t.Setenv(EnvRegistry, "")
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	dir := t.TempDir() // exists, readable, holds no <minor>/manifest.json

	_, err := NewRegistry(dir)
	if err == nil || !strings.Contains(err.Error(), "no version artifacts") {
		t.Fatalf("a search path holding no manifest at all must fail at construction; got: %v", err)
	}
	if !strings.Contains(err.Error(), dir) {
		t.Fatalf("the refusal must name every directory looked in; got: %v", err)
	}
	// Unless a fetch is going to populate it.
	if _, err := NewRegistry(dir, WithAutoFetch(true)); err != nil {
		t.Fatalf("with autofetch the empty path is the destination-to-be, not an error: %v", err)
	}
}

func TestAnExplicitDirectoryDiscoversItsLines(t *testing.T) {
	// discover() used to run only for the search-path shape, which left an
	// explicit-directory registry with an empty known set — and under lazy
	// loading that is a Versions() answering [] for a directory full of
	// artifacts.
	dir := isolatedRegistry(t, "24.8", "26.6")

	r, err := NewRegistry(dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(r.Versions(), ","); got != "24.8,26.6" {
		t.Fatalf("an explicit-directory registry must discover its own lines; Versions() = %v", got)
	}
}
