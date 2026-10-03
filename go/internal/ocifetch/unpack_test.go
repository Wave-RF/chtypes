package ocifetch

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"

	"github.com/klauspost/compress/zstd"
)

// tarEntry is one entry to build into a test tarball.
type tarEntry struct {
	name     string
	typeflag byte
	body     []byte
	linkname string
}

// buildZstdTar builds a zstd-compressed tar stream from entries, for tests
// that need a realistic layer blob without a network fixture.
func buildZstdTar(t *testing.T, entries []tarEntry) []byte {
	t.Helper()
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)
	for _, e := range entries {
		typeflag := e.typeflag
		if typeflag == 0 {
			typeflag = tar.TypeReg
		}
		hdr := &tar.Header{Name: e.name, Typeflag: typeflag, Size: int64(len(e.body)), Mode: 0o644, Linkname: e.linkname}
		if typeflag == tar.TypeDir {
			hdr.Size = 0
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatalf("tar WriteHeader(%q): %v", e.name, err)
		}
		if len(e.body) > 0 {
			if _, err := tw.Write(e.body); err != nil {
				t.Fatalf("tar Write(%q): %v", e.name, err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tar Close: %v", err)
	}

	var zBuf bytes.Buffer
	zw, err := zstd.NewWriter(&zBuf)
	if err != nil {
		t.Fatalf("zstd.NewWriter: %v", err)
	}
	if _, err := zw.Write(tarBuf.Bytes()); err != nil {
		t.Fatalf("zstd write: %v", err)
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("zstd close: %v", err)
	}
	return zBuf.Bytes()
}

func TestUnpackLibraryHappyPath(t *testing.T) {
	libBody := []byte("pretend this is a shared library, a few hundred bytes")
	layer := buildZstdTar(t, []tarEntry{
		{name: "manifest.json", body: []byte(`{"library":"libchtypes.so"}`)},
		{name: "libchtypes.so", body: libBody},
	})
	parent := t.TempDir()
	res, err := unpackLibrary(layer, parent, "libchtypes.so")
	if err != nil {
		t.Fatalf("unpackLibrary: %v", err)
	}
	sum := sha256.Sum256(libBody)
	wantSHA := hex.EncodeToString(sum[:])
	if res.librarySHA256 != wantSHA {
		t.Errorf("librarySHA256 = %s, want %s", res.librarySHA256, wantSHA)
	}
	if res.librarySize != int64(len(libBody)) {
		t.Errorf("librarySize = %d, want %d", res.librarySize, len(libBody))
	}
	installed, err := os.ReadFile(res.dir + "/" + res.libraryPath)
	if err != nil {
		t.Fatalf("reading unpacked library: %v", err)
	}
	if !bytes.Equal(installed, libBody) {
		t.Errorf("unpacked library content does not match")
	}
}

func TestUnpackLibraryMissingLibraryFile(t *testing.T) {
	layer := buildZstdTar(t, []tarEntry{{name: "manifest.json", body: []byte(`{}`)}})
	parent := t.TempDir()
	_, err := unpackLibrary(layer, parent, "libchtypes.so")
	if err == nil {
		t.Fatalf("unpackLibrary should fail when the named library is absent")
	}
	if entries, _ := os.ReadDir(parent); len(entries) != 0 {
		t.Fatalf("a failed unpack left %d entries behind in %s, want none", len(entries), parent)
	}
}

func refusalCase(t *testing.T, name string, entries []tarEntry) {
	t.Helper()
	layer := buildZstdTar(t, entries)
	parent := t.TempDir()
	_, err := unpackLibrary(layer, parent, "libchtypes.so")
	if err == nil {
		t.Fatalf("%s: unpackLibrary should refuse, got nil error", name)
	}
	remaining, _ := os.ReadDir(parent)
	if len(remaining) != 0 {
		t.Fatalf("%s: a refused unpack left %d entries behind, want the temp dir removed", name, len(remaining))
	}
}

func TestUnpackLibraryRefusesSymlink(t *testing.T) {
	refusalCase(t, "symlink", []tarEntry{
		{name: "libchtypes.so", typeflag: tar.TypeSymlink, linkname: "/etc/passwd"},
	})
}

func TestUnpackLibraryRefusesHardlink(t *testing.T) {
	refusalCase(t, "hardlink", []tarEntry{
		{name: "real", body: []byte("x")},
		{name: "libchtypes.so", typeflag: tar.TypeLink, linkname: "real"},
	})
}

func TestUnpackLibraryRefusesDevice(t *testing.T) {
	refusalCase(t, "device", []tarEntry{
		{name: "libchtypes.so", typeflag: tar.TypeChar},
	})
}

func TestUnpackLibraryRefusesAbsolutePath(t *testing.T) {
	refusalCase(t, "abs-path", []tarEntry{
		{name: "/etc/libchtypes.so", body: []byte("x")},
	})
}

func TestUnpackLibraryRefusesDotDot(t *testing.T) {
	refusalCase(t, "dotdot", []tarEntry{
		{name: "../../libchtypes.so", body: []byte("x")},
	})
}

func TestUnpackLibraryRefusesDuplicateEntry(t *testing.T) {
	refusalCase(t, "duplicate-entry", []tarEntry{
		{name: "libchtypes.so", body: []byte("first")},
		{name: "libchtypes.so", body: []byte("second")},
	})
}

func TestWriteCappedRejectsMidStreamOnceCapExceeded(t *testing.T) {
	dir := t.TempDir()
	var total int64
	data := bytes.Repeat([]byte("x"), 10)
	_, _, err := writeCapped(dir+"/f", bytes.NewReader(data), &total, 5)
	if err == nil {
		t.Fatalf("writeCapped should refuse once the running total exceeds the cap")
	}
}

func TestWriteCappedAccumulatesAcrossCalls(t *testing.T) {
	// The cap is enforced against the running total across the WHOLE tar
	// stream, not per file: two files individually under the cap but
	// together over it must still be refused on the second write.
	dir := t.TempDir()
	var total int64
	if _, _, err := writeCapped(dir+"/a", bytes.NewReader(bytes.Repeat([]byte("x"), 4)), &total, 5); err != nil {
		t.Fatalf("first write under the cap should succeed: %v", err)
	}
	if _, _, err := writeCapped(dir+"/b", bytes.NewReader(bytes.Repeat([]byte("x"), 4)), &total, 5); err == nil {
		t.Fatalf("second write should fail: the running total (8) now exceeds the cap (5)")
	}
}
