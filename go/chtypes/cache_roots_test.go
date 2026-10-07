package chtypes_test

// cache_roots_test.go — public issue #530: CacheRoot and SearchDirs report the
// resolution the fetch layer runs, and create nothing.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func clearCacheEnv(t *testing.T) {
	t.Helper()
	t.Setenv("CHTYPES_CACHE", "")
	t.Setenv("XDG_CACHE_HOME", "")
}

func TestCacheRootPrecedence(t *testing.T) {
	home := t.TempDir()
	xdg := filepath.Join(t.TempDir(), "xdg")
	explicit := filepath.Join(t.TempDir(), "explicit")
	env := filepath.Join(t.TempDir(), "env")

	clearCacheEnv(t)
	t.Setenv("HOME", home)
	if got, err := chtypes.CacheRoot(chtypes.FetchOptions{}); err != nil || got != filepath.Join(home, ".cache", "chtypes", "v1") {
		t.Errorf("~/.cache: %q, %v", got, err)
	}
	t.Setenv("XDG_CACHE_HOME", xdg)
	if got, err := chtypes.CacheRoot(chtypes.FetchOptions{}); err != nil || got != filepath.Join(xdg, "chtypes", "v1") {
		t.Errorf("XDG_CACHE_HOME: %q, %v", got, err)
	}
	t.Setenv("CHTYPES_CACHE", env)
	if got, err := chtypes.CacheRoot(chtypes.FetchOptions{}); err != nil || got != env {
		t.Errorf("CHTYPES_CACHE: %q, %v", got, err)
	}
	if got, err := chtypes.CacheRoot(chtypes.FetchOptions{CacheDir: explicit}); err != nil || got != explicit {
		t.Errorf("explicit: %q, %v", got, err)
	}
}

func TestSearchDirsOrderAndSystemDirs(t *testing.T) {
	clearCacheEnv(t)
	cache := filepath.Join(t.TempDir(), "cache")
	defaults, err := chtypes.SearchDirs(chtypes.FetchOptions{CacheDir: cache})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{cache, "/usr/local/share/chtypes/v1", "/opt/chtypes/v1"}; !reflect.DeepEqual(defaults, want) {
		t.Errorf("default system dirs: %v, want %v", defaults, want)
	}
	custom, err := chtypes.SearchDirs(chtypes.FetchOptions{CacheDir: cache, SystemDirs: []string{"/b", "/a"}})
	if err != nil || !reflect.DeepEqual(custom, []string{cache, "/b", "/a"}) {
		t.Errorf("custom system dirs: %v, %v", custom, err)
	}
	none, err := chtypes.SearchDirs(chtypes.FetchOptions{CacheDir: cache, SystemDirs: []string{}})
	if err != nil || !reflect.DeepEqual(none, []string{cache}) {
		t.Errorf("an empty list searches none: %v, %v", none, err)
	}
	root, _ := chtypes.CacheRoot(chtypes.FetchOptions{CacheDir: cache})
	if custom[0] != root {
		t.Errorf("SearchDirs[0] = %q, CacheRoot = %q", custom[0], root)
	}
}

func TestCacheRootAndSearchDirsCreateNothing(t *testing.T) {
	clearCacheEnv(t)
	parent := t.TempDir()
	cache := filepath.Join(parent, "cache")
	sys := filepath.Join(parent, "sys")
	t.Setenv("XDG_CACHE_HOME", filepath.Join(parent, "xdg"))
	for _, o := range []chtypes.FetchOptions{
		{CacheDir: cache, SystemDirs: []string{sys}},
		{SystemDirs: []string{sys}},
	} {
		if _, err := chtypes.CacheRoot(o); err != nil {
			t.Fatal(err)
		}
		if _, err := chtypes.SearchDirs(o); err != nil {
			t.Fatal(err)
		}
	}
	ents, err := os.ReadDir(parent)
	if err != nil || len(ents) != 0 {
		t.Errorf("the parent holds %v (err %v), want it empty", ents, err)
	}
}
