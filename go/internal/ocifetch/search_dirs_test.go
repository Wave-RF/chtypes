package ocifetch

// search_dirs_test.go — public issue #530: SearchDirs is the order the
// lookups read, not a second copy of it.

import (
	"reflect"
	"testing"
)

// TestSearchDirsIsTheOrderALookupReads seeds one identical record into each
// of the cache and two system directories, taking the directories from
// SearchDirs alone. A tie goes to the earlier root (fetch-v1.md §1), so the
// answering root must be the first one SearchDirs names that holds a record.
func TestSearchDirsIsTheOrderALookupReads(t *testing.T) {
	cache, sysA, sysB := t.TempDir(), t.TempDir(), t.TempDir()
	opts := &Options{CacheDir: cache, SystemDirs: []string{sysA, sysB}}
	dirs, err := SearchDirs(opts)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{cache, sysA, sysB}; !reflect.DeepEqual(dirs, want) {
		t.Fatalf("SearchDirs = %v, want %v", dirs, want)
	}
	if root, err := CacheRoot(opts); err != nil || root != dirs[0] {
		t.Fatalf("CacheRoot = %q, %v; want the first search dir %q", root, err, dirs[0])
	}
	// Seed from the last directory to the first; the answer must walk forward.
	for i := len(dirs) - 1; i >= 1; i-- {
		entry := writeRecordRoot(t, dirs[i], "26.8.1.1", "20260801.000001")
		res, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts)
		if err != nil || res == nil {
			t.Fatalf("ResolveInstalled with %d seeded = %v, %v", len(dirs)-i, res, err)
		}
		if res.Dir != entry || res.Source != "system:"+dirs[i] {
			t.Errorf("answered %s from %q, want %s from system:%s", res.Dir, res.Source, entry, dirs[i])
		}
	}
}
