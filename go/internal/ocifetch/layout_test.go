package ocifetch

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLayoutBlobRoundTrip(t *testing.T) {
	dir := t.TempDir()
	l := newLayout(dir, false)
	if err := l.ensureSkeleton(); err != nil {
		t.Fatalf("ensureSkeleton: %v", err)
	}
	if _, ok := os.Stat(filepath.Join(dir, "oci-layout")); ok != nil {
		t.Fatalf("oci-layout file was not created")
	}
	content := []byte(`{"schemaVersion":2}`)
	d := digestOf(content)
	if err := l.writeBlob(d, content); err != nil {
		t.Fatalf("writeBlob: %v", err)
	}
	got, ok := l.readBlob(d)
	if !ok {
		t.Fatalf("readBlob: not found")
	}
	if string(got) != string(content) {
		t.Fatalf("readBlob content mismatch")
	}
	if _, ok := l.readBlob(Digest("sha256:" + hex64('0'))); ok {
		t.Fatalf("readBlob found content for a digest that was never written")
	}
}

func TestAddIndexEntryMerges(t *testing.T) {
	dir := t.TempDir()
	l := newLayout(dir, false)
	if err := l.ensureSkeleton(); err != nil {
		t.Fatalf("ensureSkeleton: %v", err)
	}
	d1 := Descriptor{Digest: Digest("sha256:" + hex64('a')), Size: 1}
	d2 := Descriptor{Digest: Digest("sha256:" + hex64('b')), Size: 2}
	if err := l.addIndexEntry(d1, nil); err != nil {
		t.Fatalf("addIndexEntry(d1): %v", err)
	}
	if err := l.addIndexEntry(d2, nil); err != nil {
		t.Fatalf("addIndexEntry(d2): %v", err)
	}
	// Re-adding d1 must not duplicate it.
	if err := l.addIndexEntry(d1, nil); err != nil {
		t.Fatalf("re-adding d1: %v", err)
	}
	entries := l.readIndexEntries()
	if len(entries) != 2 {
		t.Fatalf("index.json has %d entries, want 2 (deduplicated): %+v", len(entries), entries)
	}
}

func TestAddIndexEntrySurvivesConcurrentWriterRace(t *testing.T) {
	// index-race-reapply (plan §3.2): a hook writes a competing index.json
	// between our temp-write and our rename, and both entries must survive.
	dir := t.TempDir()
	l := newLayout(dir, false)
	if err := l.ensureSkeleton(); err != nil {
		t.Fatalf("ensureSkeleton: %v", err)
	}
	ours := Descriptor{Digest: Digest("sha256:" + hex64('a')), Size: 1}
	theirs := Descriptor{Digest: Digest("sha256:" + hex64('b')), Size: 2}

	fired := false
	hook := func() {
		if fired {
			return
		}
		fired = true
		// Simulate a fully completed concurrent writer: read-modify-write
		// its own entry via the same primitive, before our rename lands.
		other := newLayout(dir, false)
		if err := other.addIndexEntry(theirs, nil); err != nil {
			t.Fatalf("simulated concurrent writer failed: %v", err)
		}
	}
	if err := l.addIndexEntry(ours, hook); err != nil {
		t.Fatalf("addIndexEntry with a racing writer: %v", err)
	}

	entries := l.readIndexEntries()
	foundOurs, foundTheirs := false, false
	for _, e := range entries {
		if e.Digest == ours.Digest {
			foundOurs = true
		}
		if e.Digest == theirs.Digest {
			foundTheirs = true
		}
	}
	if !foundOurs || !foundTheirs {
		t.Fatalf("both entries must survive the race; got %+v", entries)
	}
}

func TestWriteVerifiedRecordAndReuse(t *testing.T) {
	dir := t.TempDir()
	l := newLayout(dir, false)
	if err := l.ensureSkeleton(); err != nil {
		t.Fatalf("ensureSkeleton: %v", err)
	}
	manifestDigest := Digest("sha256:" + hex64('a'))
	unpackDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(unpackDir, "lib.so"), []byte("x"), 0o644); err != nil {
		t.Fatalf("seed unpack dir: %v", err)
	}
	rec := verifiedRecord{Schema: 1, Platform: "linux-arm64", Version: "26.8.15.10", LibraryPath: "lib.so"}

	gotDir, already, err := l.writeVerifiedRecord(manifestDigest, unpackDir, rec)
	if err != nil {
		t.Fatalf("writeVerifiedRecord: %v", err)
	}
	if already {
		t.Fatalf("first install reported already=true")
	}
	read, err := readVerifiedRecord(gotDir)
	if err != nil {
		t.Fatalf("readVerifiedRecord: %v", err)
	}
	if read.Version != "26.8.15.10" {
		t.Fatalf("read back version = %q", read.Version)
	}

	// A second "install" at the same manifest digest must reuse the
	// existing directory and discard the new one, never re-verify.
	unpackDir2 := t.TempDir()
	if err := os.WriteFile(filepath.Join(unpackDir2, "lib.so"), []byte("y"), 0o644); err != nil {
		t.Fatalf("seed second unpack dir: %v", err)
	}
	gotDir2, already2, err := l.writeVerifiedRecord(manifestDigest, unpackDir2, rec)
	if err != nil {
		t.Fatalf("writeVerifiedRecord (second): %v", err)
	}
	if !already2 {
		t.Fatalf("second install at the same manifest digest should report already=true")
	}
	if gotDir2 != gotDir {
		t.Fatalf("second install dir = %q, want the same as the first %q", gotDir2, gotDir)
	}
	if _, err := os.Stat(unpackDir2); !os.IsNotExist(err) {
		t.Fatalf("the redundant second unpack dir should have been removed")
	}
}

func TestNewestMatchingPicksHighestVersionThenBuild(t *testing.T) {
	entries := []installedEntry{
		{dir: "a", rec: verifiedRecord{Platform: "linux-arm64", Version: "26.8.15.9", Build: "20260101.000000"}},
		{dir: "b", rec: verifiedRecord{Platform: "linux-arm64", Version: "26.8.15.10", Build: "20260101.000000"}},
		{dir: "c", rec: verifiedRecord{Platform: "linux-arm64", Version: "26.8.15.10", Build: "20270101.000000"}},
		{dir: "d", rec: verifiedRecord{Platform: "darwin-arm64", Version: "99.99.99.99", Build: "20280101.000000"}},
	}
	best, ok := newestMatching(entries, "linux-arm64", "26.8")
	if !ok {
		t.Fatalf("newestMatching found nothing")
	}
	if best.dir != "c" {
		t.Fatalf("newestMatching picked %q, want c (highest version, then highest build)", best.dir)
	}
}

func TestNewestMatchingFiltersByRequestAndPlatform(t *testing.T) {
	entries := []installedEntry{
		{dir: "a", rec: verifiedRecord{Platform: "linux-arm64", Version: "25.3.1.1", Build: "1"}},
		{dir: "b", rec: verifiedRecord{Platform: "linux-arm64", Version: "26.8.15.10", Build: "1"}},
	}
	best, ok := newestMatching(entries, "linux-arm64", "26.8")
	if !ok || best.dir != "b" {
		t.Fatalf("newestMatching should only match the 26.8 family: got %+v, ok=%v", best, ok)
	}
	if _, ok := newestMatching(entries, "darwin-arm64", "26.8"); ok {
		t.Fatalf("newestMatching matched the wrong platform")
	}
}

func TestVersionLess(t *testing.T) {
	cases := []struct{ a, b string }{
		{"26.8.15.9", "26.8.15.10"},
		{"26.7.1.1", "26.8.0.0"},
		{"9.9.9.9", "10.0.0.0"},
	}
	for _, c := range cases {
		if !versionLess(c.a, c.b) {
			t.Errorf("versionLess(%q, %q) = false, want true", c.a, c.b)
		}
		if versionLess(c.b, c.a) {
			t.Errorf("versionLess(%q, %q) = true, want false", c.b, c.a)
		}
	}
}
