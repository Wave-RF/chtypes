package ocifetch

// preseed_scan_test.go — public issue #690: an offline miss reads a bounded
// number of blobs, whatever the number of builds the cache holds. The
// verdicts come from the blobs the lookup itself read (blobReadObserver),
// never from a timing.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const preseedFixtureLayout = "../../../tests/fixtures/fetch-v1/layouts/offline-hit"

// The offline-hit layout's one build: 26.1.1.1 on linux-arm64, its manifest,
// its layer and its signature bundle.
const (
	preseedBuildManifest = "sha256:b461315c30649fec6d9cc988330ed3081ffd6a2a45aa21acda6ff212413f8637"
	preseedBuildLayer    = "sha256:df72d6f06f8ed738ecdd8e080dbd71244b66024daee20c85f53d3f822a49463b"
	preseedBundleRef     = "sha256:887b88511be68e82d653c734f57fc0e2c9c2e64bda4f23f25fe0ae1648f6244b"
)

func sha256Digest(b []byte) (Digest, string) {
	s := sha256.Sum256(b)
	h := hex.EncodeToString(s[:])
	return Digest("sha256:" + h), h
}

// seedClones copies the offline-hit layout to a fresh directory and adds
// n-1 more pre-seeded entries of the same signed build: each its own
// manifest (the original plus one annotation), its own signature referrer
// manifest naming it, and its own copy of the bundle (the original bytes
// plus trailing whitespace, which the signature does not cover). The layer
// stays the one signed layer. It returns the layout directory, the trusted
// key, and every entry's manifest digest in index order.
func seedClones(t *testing.T, n int) (string, string, []Digest) {
	t.Helper()
	if _, err := os.Stat(preseedFixtureLayout); err != nil {
		t.Skipf("fetch fixtures not available: %v", err)
	}
	keyHex, err := os.ReadFile(filepath.Join(testKeyFixtureDir, "public.hex"))
	if err != nil {
		t.Skipf("test-key fixture not available: %v", err)
	}
	dir := filepath.Join(t.TempDir(), "cache")
	if err := copyDir(preseedFixtureLayout, dir); err != nil {
		t.Fatal(err)
	}
	blob := func(d Digest) []byte {
		b, err := os.ReadFile(filepath.Join(dir, "blobs", "sha256", d.Hex()))
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	put := func(b []byte) (Digest, int64) {
		d, h := sha256Digest(b)
		if err := os.WriteFile(filepath.Join(dir, "blobs", "sha256", h), b, 0o644); err != nil {
			t.Fatal(err)
		}
		return d, int64(len(b))
	}
	var idx map[string]any
	if err := json.Unmarshal(blob0(t, filepath.Join(dir, "index.json")), &idx); err != nil {
		t.Fatal(err)
	}
	entries := idx["manifests"].([]any)
	first := entries[0].(map[string]any)
	digests := []Digest{Digest(first["digest"].(string))}

	var manifest, ref map[string]any
	if err := json.Unmarshal(blob(preseedBuildManifest), &manifest); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(blob(preseedBundleRef), &ref); err != nil {
		t.Fatal(err)
	}
	bundleDesc := ref["layers"].([]any)[0].(map[string]any)
	bundle := blob(Digest(bundleDesc["digest"].(string)))
	for i := 1; i < n; i++ {
		manifest["annotations"] = map[string]any{"test.clone": fmt.Sprint(i)}
		mb, _ := json.Marshal(manifest)
		md, msize := put(mb)
		bd, bsize := put(append(append([]byte{}, bundle...), []byte(strings.Repeat(" ", i))...))
		bundleDesc["digest"], bundleDesc["size"] = string(bd), bsize
		ref["subject"] = map[string]any{"mediaType": MediaTypeManifest, "digest": string(md), "size": msize}
		rb, _ := json.Marshal(ref)
		put(rb)
		e := map[string]any{}
		for k, v := range first {
			e[k] = v
		}
		e["digest"], e["size"] = string(md), msize
		delete(e, "annotations")
		entries = append(entries, e)
		digests = append(digests, md)
	}
	idx["manifests"] = entries
	ib, _ := json.Marshal(idx)
	if err := os.WriteFile(filepath.Join(dir, "index.json"), ib, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir, strings.TrimSpace(string(keyHex)), digests
}

func blob0(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// blobReads counts, per digest, the blobs read under dir while fn runs.
func blobReads(t *testing.T, dir string, fn func()) map[Digest]int {
	t.Helper()
	reads := map[Digest]int{}
	blobReadObserver = func(layoutDir string, d Digest, _ int) {
		if layoutDir == dir {
			reads[d]++
		}
	}
	defer func() { blobReadObserver = nil }()
	fn()
	return reads
}

func totalReads(reads map[Digest]int) (total, most int) {
	for _, n := range reads {
		total += n
		most = max(most, n)
	}
	return total, most
}

// TestOfflineMissReadsNoLayerWhateverTheCacheHolds: an offline miss over N
// pre-seeded entries that the lookup does not install (a build outside the
// request, or a signature that verifies under no trusted key) reads no
// layer at all, no blob more than twice (once to rule a candidate in, once
// in the one referrer scan), and a number of blobs linear in N, at N=2 and
// N=20 alike. The old scan read every entry's layer, and every blob of the
// layout once per entry.
func TestOfflineMissReadsNoLayerWhateverTheCacheHolds(t *testing.T) {
	otherKey, err := os.ReadFile("../../../tests/fixtures/fetch-v1/other-key/public.hex")
	if err != nil {
		t.Skipf("other-key fixture not available: %v", err)
	}
	for _, tc := range []struct {
		name, request, key string
	}{
		{"another line", "26.3", ""},
		{"untrusted", "26.1", strings.TrimSpace(string(otherKey))},
	} {
		for _, n := range []int{2, 20} {
			dir, key, _ := seedClones(t, n)
			if tc.key != "" {
				key = tc.key
			}
			opts := &Options{CacheDir: dir, SystemDirs: []string{}, TrustedKeys: []string{key}}
			var res *Resolved
			var err error
			reads := blobReads(t, dir, func() {
				res, err = ResolveInstalled(Request{Spelling: tc.request}, "linux-arm64", opts)
			})
			if err != nil || res != nil {
				t.Fatalf("%s, N=%d: ResolveInstalled(%s) = %v, %v; want a miss", tc.name, n, tc.request, res, err)
			}
			total, most := totalReads(reads)
			t.Logf("%s, N=%d: %d blob reads, %d distinct blobs, the most of any one blob %d, the layer %d", tc.name, n, total, len(reads), most, reads[preseedBuildLayer])
			if reads[preseedBuildLayer] != 0 {
				t.Errorf("%s, N=%d: the miss read the layer %d time(s); want 0", tc.name, n, reads[preseedBuildLayer])
			}
			if most > 2 {
				t.Errorf("%s, N=%d: one blob was read %d times in one miss; want at most 2", tc.name, n, most)
			}
			// Per entry: its manifest twice, its referrer manifest once and
			// its bundle twice; plus the two shared config blobs, once each.
			if bound := 5*n + 2; total > bound {
				t.Errorf("%s, N=%d: %d blob reads in one miss; want at most %d (5 per entry, plus 2)", tc.name, n, total, bound)
			}
		}
	}
}

// TestOfflineMissOverInstalledBuildsReadsNoBlob: the consumer's case. Every
// entry is already installed (it has an unpacked/ record), so it is not
// pre-seeded (fetch-v1.md §6) and an offline miss reads no blob at all, at
// N=2 and at N=20.
func TestOfflineMissOverInstalledBuildsReadsNoBlob(t *testing.T) {
	for _, n := range []int{2, 20} {
		dir, key, digests := seedClones(t, n)
		keys, err := parseHexKeys([]string{key})
		if err != nil {
			t.Fatal(err)
		}
		l := newLayout(dir, false)
		for _, d := range digests {
			if err := installPreseededByDigest(l, d, keys); err != nil {
				t.Fatalf("installing %s: %v", d, err)
			}
		}
		opts := &Options{CacheDir: dir, SystemDirs: []string{}, TrustedKeys: []string{key}}
		var res *Resolved
		reads := blobReads(t, dir, func() {
			res, err = ResolveInstalled(Request{Spelling: "26.3"}, "linux-arm64", opts)
		})
		if err != nil || res != nil {
			t.Fatalf("N=%d: ResolveInstalled(26.3) = %v, %v; want a miss", n, res, err)
		}
		total, _ := totalReads(reads)
		t.Logf("N=%d installed: %d blob reads", n, total)
		if total != 0 {
			t.Errorf("N=%d installed: the miss read %d blob(s); want 0", n, total)
		}
	}
}

// TestPreseededHitStillVerifiesTheLayer: the candidate that is installed is
// verified as strictly as before. Among 20 pre-seeded entries the first in
// index order is installed and its layer is read once; with the layer's
// bytes altered (same digest name, other content) nothing is installed.
func TestPreseededHitStillVerifiesTheLayer(t *testing.T) {
	dir, key, digests := seedClones(t, 20)
	opts := &Options{CacheDir: dir, SystemDirs: []string{}, TrustedKeys: []string{key}}
	var res *Resolved
	var err error
	reads := blobReads(t, dir, func() {
		res, err = ResolveInstalled(Request{Spelling: "26.1"}, "linux-arm64", opts)
	})
	if err != nil || res == nil {
		t.Fatalf("ResolveInstalled(26.1) = %v, %v; want the pre-seeded build", res, err)
	}
	if res.Digests.Manifest != digests[0] || res.Version != "26.1.1.1" {
		t.Errorf("installed %s (%s); want %s (26.1.1.1), the first entry", res.Digests.Manifest, res.Version, digests[0])
	}
	if reads[preseedBuildLayer] != 1 {
		t.Errorf("the hit read the layer %d times; want 1", reads[preseedBuildLayer])
	}

	dir, key, _ = seedClones(t, 20)
	layer := filepath.Join(dir, "blobs", "sha256", Digest(preseedBuildLayer).Hex())
	b := blob0(t, layer)
	b[len(b)-1] ^= 0xff
	if err := os.WriteFile(layer, b, 0o644); err != nil {
		t.Fatal(err)
	}
	opts = &Options{CacheDir: dir, SystemDirs: []string{}, TrustedKeys: []string{key}}
	res, err = ResolveInstalled(Request{Spelling: "26.1"}, "linux-arm64", opts)
	if err != nil || res != nil {
		t.Fatalf("with the layer altered, ResolveInstalled(26.1) = %v, %v; want nothing installed", res, err)
	}
	if _, serr := os.Stat(filepath.Join(dir, UnpackedDirName(), Digest(digests[0]).Hex())); serr == nil {
		t.Errorf("with the layer altered, an unpacked/ entry was written")
	}
}
