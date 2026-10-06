package ocifetch

// cache_roots_test.go — public issue #486, the behavior fixes: modes that
// follow the umask, one root order, read-only lookups that create nothing, and
// the 0.x hint. Every fault here is made by a real write or a real install,
// never by a stubbed reader.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

// TestInstallModesFollowTheUmask: every directory and file an install creates
// is 0777 and 0666 less the umask, so at umask 022 another uid can read the
// cache (public issue #486). The 022 pass is the one that discriminates: the
// temp APIs' private modes are 0700 and 0600, which only 077 also gives.
func TestInstallModesFollowTheUmask(t *testing.T) {
	reg, _ := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()

	for _, umask := range []int{0o022, 0o077} {
		old := syscall.Umask(umask)
		opts := testOptions(t, srv.URL)
		opts.SystemDirs = []string{}
		res, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
		syscall.Umask(old)
		if err != nil {
			t.Fatalf("umask %03o: Ensure: %v", umask, err)
		}
		wantDir, wantFile := fs.FileMode(0o777&^umask), fs.FileMode(0o666&^umask)
		var seen int
		err = filepath.WalkDir(opts.CacheDir, func(p string, d fs.DirEntry, err error) error {
			if err != nil || p == opts.CacheDir {
				return err // the root itself is the test's own t.TempDir
			}
			fi, err := os.Lstat(p)
			if err != nil {
				return err
			}
			want := wantFile
			if fi.IsDir() {
				want = wantDir
			}
			if got := fi.Mode().Perm(); got != want {
				t.Errorf("umask %03o: %s is %03o, want %03o", umask, p, got, want)
			}
			seen++
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		// The walk covered the entry, its record and its library.
		for _, p := range []string{res.Dir, filepath.Join(res.Dir, CacheVerifiedRecord), res.LibraryPath} {
			if _, err := os.Lstat(p); err != nil {
				t.Fatalf("umask %03o: %v", umask, err)
			}
		}
		if seen < 6 {
			t.Fatalf("umask %03o: the walk saw only %d paths", umask, seen)
		}
	}
}

// writeRecordRoot installs one record by hand under root, as any binding
// would have left it, and returns the entry directory.
func writeRecordRoot(t *testing.T, root, version, build string) string {
	t.Helper()
	lib := []byte("library " + version + " " + build)
	sum := sha256.Sum256(lib)
	manifest := sha256.Sum256([]byte(root + version + build))
	rec := verifiedRecord{
		Schema: 1, Platform: "linux-arm64", Version: version, Build: build,
		LibraryPath: "lib.so", LibrarySHA256: hex.EncodeToString(sum[:]), LibraryBytes: int64(len(lib)),
		Digests:   Digests{Manifest: Digest("sha256:" + hex.EncodeToString(manifest[:])), Layer: Digest("sha256:" + hex64('c'))},
		Predicate: map[string]any{"clickhouse_version": version, "build": build, "library_sha256": hex.EncodeToString(sum[:])},
	}
	b, err := encodeRecord(rec)
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, UnpackedDirName(), hex.EncodeToString(manifest[:]))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "lib.so"), lib, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, CacheVerifiedRecord), b, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

// TestResolveInstalledNewestAcrossRoots: the cache and the system dirs are
// one search, the newest (version, build) wins, and a tie goes to the earlier
// root (public issue #486). The first case is the one first-root-wins got
// wrong: the cache's older build answered.
func TestResolveInstalledNewestAcrossRoots(t *testing.T) {
	for _, c := range []struct {
		name                     string
		cacheVersion, sysVersion string
		cacheBuild, sysBuild     string
		wantSource               string // "cache" or "system"
	}{
		{"the system dir holds the newer version", "26.8.1.1", "26.8.2.1", "20260801.000001", "20260802.000001", "system"},
		{"the system dir holds a newer build of the same version", "26.8.1.1", "26.8.1.1", "20260801.000001", "20260801.000002", "system"},
		{"the cache holds the newer version", "26.8.2.1", "26.8.1.1", "20260802.000001", "20260801.000001", "cache"},
		{"a tie goes to the cache", "26.8.1.1", "26.8.1.1", "20260801.000001", "20260801.000001", "cache"},
	} {
		t.Run(c.name, func(t *testing.T) {
			cache, sys := t.TempDir(), t.TempDir()
			cacheEntry := writeRecordRoot(t, cache, c.cacheVersion, c.cacheBuild)
			sysEntry := writeRecordRoot(t, sys, c.sysVersion, c.sysBuild)
			opts := &Options{CacheDir: cache, SystemDirs: []string{sys}}
			res, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", opts)
			if err != nil || res == nil {
				t.Fatalf("ResolveInstalled = %v, %v", res, err)
			}
			wantDir, wantSource := cacheEntry, "cache"
			if c.wantSource == "system" {
				wantDir, wantSource = sysEntry, "system:"+sys
			}
			if res.Dir != wantDir || res.Source != wantSource {
				t.Errorf("answered %s from %q, want %s from %q", res.Dir, res.Source, wantDir, wantSource)
			}
			// The offline fetch answers the same way.
			opts.Offline = true
			got, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
			if err != nil || got.Dir != wantDir {
				t.Errorf("Ensure(offline) = %v, %v; want %s", got, err, wantDir)
			}
		})
	}
}

// treeOf lists every path under root with its size, or nil when root is absent.
func treeOf(t *testing.T, root string) map[string]int64 {
	t.Helper()
	if _, err := os.Lstat(root); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	out := map[string]int64{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		fi, err := d.Info()
		if err != nil {
			return err
		}
		out[p] = fi.Size()
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// writeZeroXRegistry lays out what a 0.x install left behind: a registry
// directory of <minor>/manifest.json entries, no oci-layout, no index.json.
func writeZeroXRegistry(t *testing.T, root string) {
	t.Helper()
	for rel, body := range map[string]string{
		"26.1/manifest.json":                  `{"clickhouse_version":"26.1.3.4"}`,
		"26.1/libchtypes.so":                  "0.x library",
		"patches/26.1.3.4/manifest.json":      `{}`,
		"patches/26.1.3.4/libchtypes.so":      "0.x patch library",
		"unrelated-notes/manifest.json.saved": "not a minor",
	} {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// TestReadOnlyLookupsCreateNothing: resolve_installed, list_installed,
// verify_installed and an offline ensure never create a directory or a file
// (public issue #486), whether the cache is missing or holds a 0.x registry,
// and with a system dir configured that holds nothing either.
func TestReadOnlyLookupsCreateNothing(t *testing.T) {
	tmp := t.TempDir()
	missing := filepath.Join(tmp, "no-such-cache")
	zeroX := filepath.Join(tmp, "zero-x")
	writeZeroXRegistry(t, zeroX)
	for _, cache := range []string{missing, zeroX} {
		before := treeOf(t, cache)
		opts := &Options{CacheDir: cache, SystemDirs: []string{filepath.Join(tmp, "no-such-system-dir")}}
		if res, err := ResolveInstalled(Request{Spelling: "26.1"}, "linux-arm64", opts); err != nil || res != nil {
			t.Errorf("%s: ResolveInstalled = %v, %v", cache, res, err)
		}
		if got, err := ListInstalled(opts); err != nil || len(got) != 0 {
			t.Errorf("%s: ListInstalled = %v, %v", cache, got, err)
		}
		if got, err := VerifyInstalled(opts); err != nil || len(got) != 0 {
			t.Errorf("%s: VerifyInstalled = %v, %v", cache, got, err)
		}
		off := *opts
		off.Offline = true
		if _, err := Ensure(context.Background(), Request{Spelling: "26.1", Platform: "linux-arm64"}, &off); !errors.Is(err, ErrArtifactMissing) {
			t.Errorf("%s: Ensure(offline) = %v, want CHTYPES_ARTIFACT_MISSING", cache, err)
		}
		after := treeOf(t, cache)
		if len(before) != len(after) {
			t.Errorf("%s: a read-only lookup changed the tree: %d paths before, %d after (%v)", cache, len(before), len(after), after)
		}
		for p, size := range before {
			if after[p] != size {
				t.Errorf("%s: %s changed", cache, p)
			}
		}
		if _, err := os.Lstat(filepath.Join(tmp, "no-such-system-dir")); !errors.Is(err, fs.ErrNotExist) {
			t.Errorf("the system dir was created: %v", err)
		}
	}
}

// TestMissingCarriesTheZeroXHint: a MISSING answer from a cache that holds a
// 0.x registry says so and names the v1 root (public issue #486). A 1.x cache
// without an oci-layout (as Python writes it) and an empty directory carry no
// hint.
func TestMissingCarriesTheZeroXHint(t *testing.T) {
	tmp := t.TempDir()
	zeroX := filepath.Join(tmp, "zero-x")
	writeZeroXRegistry(t, zeroX)
	opts := &Options{CacheDir: zeroX, SystemDirs: []string{}, Offline: true}
	_, err := Ensure(context.Background(), Request{Spelling: "26.1", Platform: "linux-arm64"}, opts)
	want := zeroX + " holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at ${XDG_CACHE_HOME:-~/.cache}/chtypes/v1"
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeArtifactMissing || !strings.Contains(fe.Msg, want) {
		t.Fatalf("Ensure(offline) on a 0.x registry = %v, want MISSING carrying %q", err, want)
	}
	if notes := MissingNotes(opts); len(notes) != 1 || !strings.HasPrefix(notes[0], want) {
		t.Errorf("MissingNotes = %q", notes)
	}

	pyCache := filepath.Join(tmp, "python-written")
	writeRecordRoot(t, pyCache, "26.8.1.1", "20260801.000001")
	empty := t.TempDir()
	for _, dir := range []string{pyCache, empty, filepath.Join(tmp, "absent")} {
		if notes := MissingNotes(&Options{CacheDir: dir}); len(notes) != 0 {
			t.Errorf("%s: MissingNotes = %q, want none", dir, notes)
		}
	}
}
