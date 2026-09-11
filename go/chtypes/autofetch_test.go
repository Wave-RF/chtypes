package chtypes

// autofetch_test.go — the §1 fallthrough and the opt-in lazy fetch, with a
// REAL artifact: a release is built from the smallest artifact installed
// in the test registry, signed with an ephemeral key, served over
// loopback, and opened through a registry that has nothing — so the whole
// path runs: miss → fetch → verify → install → dlopen → CompileDDL. Skips
// loudly without a registry, like every registry test here.

import (
	"archive/tar"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// smallestInstalled picks the installed line with the smallest library.
func smallestInstalled(t *testing.T) Installed {
	t.Helper()
	root := testRegistryDir(t)
	installed, err := ListInstalled(root)
	if err != nil || len(installed) == 0 {
		detail := "nothing installed"
		if err != nil {
			detail += ": " + err.Error()
		}
		skipNoArtifacts(t, root, detail)
	}
	best, bestSize := installed[0], int64(-1)
	for _, inst := range installed {
		fi, err := os.Stat(filepath.Join(inst.Dir, inst.Library))
		if err != nil {
			continue
		}
		if bestSize < 0 || fi.Size() < bestSize {
			best, bestSize = inst, fi.Size()
		}
	}
	return best
}

// writeRealRelease packs one real artifact directory into a signed
// release under dir, streaming (the library is hundreds of MB).
func writeRealRelease(t *testing.T, dir string, inst Installed, priv ed25519.PrivateKey) (file string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	m, err := readManifest(filepath.Join(inst.Dir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	file = fmt.Sprintf("chtypes-%s-%s.tar.gz", m.ClickHouseVersion, m.platform())
	out, err := os.Create(filepath.Join(dir, file))
	if err != nil {
		t.Fatal(err)
	}
	h := sha256.New()
	gz, _ := gzip.NewWriterLevel(io.MultiWriter(out, h), gzip.BestSpeed)
	tw := tar.NewWriter(gz)
	entries, _ := os.ReadDir(inst.Dir)
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		fi, _ := e.Info()
		hdr, _ := tar.FileInfoHeader(fi, "")
		hdr.Name = e.Name()
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		fh, err := os.Open(filepath.Join(inst.Dir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := io.Copy(tw, fh); err != nil {
			t.Fatal(err)
		}
		fh.Close()
	}
	tw.Close()
	gz.Close()
	out.Close()
	fi, _ := os.Stat(filepath.Join(dir, file))
	sum := hex.EncodeToString(h.Sum(nil))
	index, _ := json.Marshal(map[string]any{"schema": 1, "release_tag": "test", "artifacts": []map[string]any{{
		"os": m.OS, "arch": m.Arch, "file": file, "sha256": sum, "bytes": fi.Size(),
		"clickhouse_version": m.ClickHouseVersion, "clickhouse_minor": m.minor(),
		"library": m.Library, "library_sha256": m.LibrarySHA256,
	}}})
	os.WriteFile(filepath.Join(dir, "index.json"), index, 0o644)
	sums := sum + "  " + file + "\n"
	os.WriteFile(filepath.Join(dir, "SHA256SUMS"), []byte(sums), 0o644)
	sig := ed25519.Sign(priv, []byte(sums))
	os.WriteFile(filepath.Join(dir, "SHA256SUMS.sig"), []byte("untrusted comment: test\n"+base64.StdEncoding.EncodeToString(sig)+"\n"), 0o644)
	return file
}

func TestAutoFetchOpensOnceAndDispatches(t *testing.T) {
	inst := smallestInstalled(t)
	cache := isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	rel := filepath.Join(t.TempDir(), "release")
	file := writeRealRelease(t, rel, inst, priv)
	srv := serveRelease(t, rel)

	var progress strings.Builder
	var mu sync.Mutex
	reg, err := NewRegistry("", WithAutoFetch(true), WithFetchOptions(FetchOptions{URL: srv.URL, Progress: lockedWriter{&mu, &progress}}))
	if err != nil {
		t.Fatal(err)
	}
	if v := reg.Versions(); len(v) != 0 {
		t.Fatalf("Versions before any fetch = %v", v)
	}
	// Eight concurrent opens of the missing line: one fetch, one library.
	const n = 8
	libs := make([]*Library, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			libs[i], errs[i] = reg.For(Version(inst.Line))
		}(i)
	}
	wg.Wait()
	for i := range libs {
		if errs[i] != nil {
			t.Fatalf("open %d: %v\n%s", i, errs[i], progress.String())
		}
		if libs[i] != libs[0] {
			t.Fatalf("open %d got a different *Library", i)
		}
	}
	if got := srv.count(file); got != 1 {
		t.Fatalf("the tarball was downloaded %d times, want 1\n%s", got, progress.String())
	}
	want := filepath.Join(cache, "chtypes", "artifacts", HostPlatform(), inst.Line)
	if libs[0].Path != filepath.Join(want, inst.Library) || libs[0].Minor != inst.Line {
		t.Fatalf("library %s (%s), want under %s", libs[0].Path, libs[0].Minor, want)
	}
	if v := reg.Versions(); len(v) != 1 || v[0] != inst.Line {
		t.Fatalf("Versions after = %v", v)
	}
	// It answers.
	cs, err := libs[0].CompileDDL("x UInt8")
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.Rows(JSONEachRow, []byte(`{"x":256}`), nil)
	if err != nil || res.Outcome != Accepted || len(res.Rows) != 1 || res.Rows[0].Values[0].Text != "0" {
		t.Fatalf("rows: %+v %v", res, err)
	}
	// A second registry on the same search path finds it installed: no fetch.
	before := srv.all.Load()
	reg2, err := NewRegistry("")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reg2.For(Version(inst.Line)); err != nil {
		t.Fatal(err)
	}
	if srv.all.Load() != before {
		t.Fatal("a second registry fetched again")
	}
}

func TestRegistryFallsThroughTheSearchPath(t *testing.T) {
	// An explicit directory holding one line; another line lives only in
	// $CHTYPES_REGISTRY. The explicit registry answers for both (§1),
	// loading the second lazily, and Versions() grows as it does.
	root := testRegistryDir(t)
	installed, err := ListInstalled(root)
	if err != nil || len(installed) < 2 {
		t.Skipf("need two installed lines under %s, have %d", root, len(installed))
	}
	isolateEnv(t)
	t.Setenv(envRegistry, root)
	small := smallestInstalled(t)
	explicit := t.TempDir()
	// Copy the smallest line into the explicit directory (a second image of
	// the same library, as TestRegistryLoadsAndDispatches does).
	sub := filepath.Join(explicit, small.Line)
	os.MkdirAll(sub, 0o755)
	for _, name := range []string{"manifest.json", small.Library, "unsafe_families.txt"} {
		b, err := os.ReadFile(filepath.Join(small.Dir, name))
		if err != nil && name != "unsafe_families.txt" {
			t.Fatal(err)
		}
		os.WriteFile(filepath.Join(sub, name), b, 0o755)
	}
	reg, err := NewRegistry(explicit)
	if err != nil {
		t.Fatal(err)
	}
	if v := reg.Versions(); len(v) != 1 || v[0] != small.Line {
		t.Fatalf("Versions = %v", v)
	}
	var other Installed
	for _, inst := range installed {
		if inst.Line != small.Line {
			other = inst
			break
		}
	}
	lib, err := reg.For(Version(other.Line))
	if err != nil {
		t.Fatalf("fallthrough to %s: %v", envRegistry, err)
	}
	if lib.Minor != other.Line || !strings.HasPrefix(lib.Path, root) {
		t.Fatalf("got %s from %s", lib.Minor, lib.Path)
	}
	if v := reg.Versions(); len(v) != 2 {
		t.Fatalf("Versions after fallthrough = %v", v)
	}
	// The explicit line was served from the explicit directory, not the env.
	first, _ := reg.For(Version(small.Line))
	if !strings.HasPrefix(first.Path, explicit) {
		t.Fatalf("explicit line served from %s", first.Path)
	}
}

// lockedWriter serializes progress output from concurrent fetch waiters.
type lockedWriter struct {
	mu *sync.Mutex
	w  *strings.Builder
}

func (l lockedWriter) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.w.Write(p)
}
