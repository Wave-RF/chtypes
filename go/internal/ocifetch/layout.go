package ocifetch

// layout.go — the cache (docs/guides/fetch-v1.md §1): a standard OCI image
// layout (oci-layout, index.json, blobs/sha256/<hex>) plus
// unpacked/sha256/<manifest-hex>/ beside blobs. Blobs and unpacked
// directories install by temp-then-rename; index.json is written by atomic
// rename, then re-read and re-applied, never in place, so two concurrent
// fetches racing the same cache both survive. The durable source of truth
// for --offline and resolve_installed is each unpacked directory's own
// verified.json record, never index.json (§6).

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const ociLayoutContent = `{"imageLayoutVersion":"1.0.0"}` + "\n"

// ociLayoutIndex is the OCI image layout's index.json: a plain list of
// manifest descriptors, deduplicated by digest.
type ociLayoutIndex struct {
	SchemaVersion int          `json:"schemaVersion"`
	Manifests     []Descriptor `json:"manifests"`
}

// verifiedRecord is the durable, immutable proof that one unpacked directory
// was verified once: under which key, against which digests, and the
// predicate that was checked. It is written exactly once, when the
// directory is created, and never edited afterward — a later fetch that
// lands on the same manifest digest finds the directory already there and
// reuses it (already_installed=true) rather than rewriting this file.
type verifiedRecord struct {
	Schema      int            `json:"schema"`
	Platform    string         `json:"platform"`
	Version     string         `json:"version"`
	Channel     string         `json:"channel,omitempty"`
	Build       string         `json:"build"`
	LibraryPath string         `json:"library_path"`
	Digests     Digests        `json:"digests"`
	Predicate   map[string]any `json:"predicate"`
	SignedBy    string         `json:"signed_by"`
}

// Digests carries every content digest a Resolved value names (§1.3).
type Digests struct {
	Index          Digest `json:"index,omitempty"`
	Manifest       Digest `json:"manifest"`
	Layer          Digest `json:"layer"`
	Bundle         Digest `json:"bundle,omitempty"`
	BundleManifest Digest `json:"bundle_manifest,omitempty"`
}

// layout wraps one cache directory (the user cache, or a read-only system
// directory) with the operations this package needs on it.
type layout struct {
	dir      string
	readOnly bool
}

func newLayout(dir string, readOnly bool) *layout {
	return &layout{dir: dir, readOnly: readOnly}
}

func (l *layout) blobPath(d Digest) string {
	return filepath.Join(l.dir, "blobs", "sha256", d.Hex())
}

func (l *layout) unpackedDir(manifestDigest Digest) string {
	return filepath.Join(l.dir, UnpackedDirName(), manifestDigest.Hex())
}

// UnpackedDirName is CacheUnpackedDir's leaf form, split out so layout.go
// never hand-parses the generated constant's "unpacked/sha256" path.
func UnpackedDirName() string { return CacheUnpackedDir }

// ensureSkeleton creates dir (if absent) and writes oci-layout and empty
// blobs/unpacked directories. A read-only layout never calls this.
func (l *layout) ensureSkeleton() error {
	if l.readOnly {
		return fmt.Errorf("chtypes: %s is a read-only cache directory", l.dir)
	}
	if err := os.MkdirAll(filepath.Join(l.dir, "blobs", "sha256"), 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(l.dir, UnpackedDirName()), 0o755); err != nil {
		return err
	}
	layoutFile := filepath.Join(l.dir, "oci-layout")
	if _, err := os.Stat(layoutFile); os.IsNotExist(err) {
		if werr := writeFileAtomic(l.dir, layoutFile, []byte(ociLayoutContent)); werr != nil {
			return werr
		}
	}
	return nil
}

// readBlob returns a blob's content if present in this layout.
func (l *layout) readBlob(d Digest) ([]byte, bool) {
	b, err := os.ReadFile(l.blobPath(d))
	if err != nil {
		return nil, false
	}
	return b, true
}

// writeBlob installs content at its content-addressed path by
// temp-then-rename. A blob already present is left untouched (content at a
// given digest never changes).
func (l *layout) writeBlob(d Digest, content []byte) error {
	dest := l.blobPath(d)
	if _, err := os.Stat(dest); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	return writeFileAtomic(filepath.Dir(dest), dest, content)
}

// writeFileAtomic writes content to a temp file under dir, then renames it
// onto dest, so a reader never observes a partially written file.
func writeFileAtomic(dir, dest string, content []byte) error {
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(content); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, dest); err != nil {
		return err
	}
	ok = true
	return nil
}

// addIndexEntry merges desc into index.json by a read-check-rename loop:
// read the current file, merge, write a temp file, optionally let a test
// hook simulate a concurrent writer, then rename only if the file has not
// changed since the read — otherwise re-read the now-current file and retry
// the merge against it. This is the "written by atomic rename, then re-read
// and re-applied" rule (§1): two fetches racing the same cache both survive,
// because a losing attempt re-merges onto the winner's result rather than
// clobbering it.
func (l *layout) addIndexEntry(desc Descriptor, beforeRename func()) error {
	indexPath := filepath.Join(l.dir, "index.json")
	for {
		before, _ := os.ReadFile(indexPath) // absent is treated as an empty index
		idx := parseIndexOrEmpty(before)
		mergeDescriptor(&idx, desc)
		out, err := json.Marshal(idx)
		if err != nil {
			return err
		}
		out = append(out, '\n')
		tmp, err := os.CreateTemp(l.dir, ".tmp-index-*")
		if err != nil {
			return err
		}
		if _, err := tmp.Write(out); err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
			return err
		}
		if err := tmp.Close(); err != nil {
			_ = os.Remove(tmp.Name())
			return err
		}
		if beforeRename != nil {
			beforeRename()
		}
		current, _ := os.ReadFile(indexPath)
		if !bytes.Equal(current, before) {
			_ = os.Remove(tmp.Name())
			continue // someone else wrote in the meantime; re-merge onto their result
		}
		if err := os.Rename(tmp.Name(), indexPath); err != nil {
			_ = os.Remove(tmp.Name())
			return err
		}
		return nil
	}
}

func parseIndexOrEmpty(b []byte) ociLayoutIndex {
	var idx ociLayoutIndex
	if len(b) == 0 {
		idx.SchemaVersion = 2
		return idx
	}
	if err := json.Unmarshal(b, &idx); err != nil {
		idx = ociLayoutIndex{SchemaVersion: 2}
	}
	return idx
}

func mergeDescriptor(idx *ociLayoutIndex, desc Descriptor) {
	for _, existing := range idx.Manifests {
		if existing.Digest == desc.Digest {
			return
		}
	}
	idx.Manifests = append(idx.Manifests, desc)
}

// readIndexEntries lists every descriptor currently in index.json, for the
// pre-seed case: an entry with no corresponding unpacked/ directory yet is
// verified and unpacked on first use (§1).
func (l *layout) readIndexEntries() []Descriptor {
	b, err := os.ReadFile(filepath.Join(l.dir, "index.json"))
	if err != nil {
		return nil
	}
	return parseIndexOrEmpty(b).Manifests
}

// listAllBlobDigests lists every blob l's own blobs/sha256/ directory
// holds, as "sha256:<hex>" digests — not only the ones index.json's
// top-level "manifests" array names. A referrer manifest (one with its own
// "subject" field) is a real blob in a plain OCI layout, but a layout
// generated directly from fixture content, rather than by `oras copy -r`
// against a live registry, has no obligation to also list it in index.json
// — discovering it locally means scanning every blob (layout.go's
// findLocalBundle), the same way layer/config blobs are already found by
// digest rather than by a directory listing.
func (l *layout) listAllBlobDigests() []Digest {
	entries, err := os.ReadDir(filepath.Join(l.dir, "blobs", "sha256"))
	if err != nil {
		return nil
	}
	out := make([]Digest, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		out = append(out, Digest("sha256:"+e.Name()))
	}
	return out
}

// writeVerifiedRecord creates a fresh unpacked directory for manifestDigest
// by renaming unpackDir (already populated by unpackLibrary) into place,
// then writes verified.json inside it. If the directory already exists
// (another fetch landed on the same manifest digest first), unpackDir is
// discarded and the existing directory is left untouched — content at a
// given manifest digest never changes, so there is nothing to reconcile.
func (l *layout) writeVerifiedRecord(manifestDigest Digest, unpackDir string, rec verifiedRecord) (dest string, alreadyInstalled bool, err error) {
	dest = l.unpackedDir(manifestDigest)
	if _, statErr := os.Stat(dest); statErr == nil {
		_ = os.RemoveAll(unpackDir)
		return dest, true, nil
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", false, err
	}
	out, err := json.Marshal(rec)
	if err != nil {
		return "", false, err
	}
	if err := writeFileAtomic(unpackDir, filepath.Join(unpackDir, CacheVerifiedRecord), out); err != nil {
		return "", false, err
	}
	if err := os.Rename(unpackDir, dest); err != nil {
		// Another fetch won the race between our Stat and our Rename: its
		// directory is now in place, and ours is redundant.
		if _, statErr := os.Stat(dest); statErr == nil {
			_ = os.RemoveAll(unpackDir)
			return dest, true, nil
		}
		return "", false, err
	}
	return dest, false, nil
}

// readVerifiedRecord reads one unpacked directory's verified.json.
func readVerifiedRecord(dir string) (*verifiedRecord, error) {
	b, err := os.ReadFile(filepath.Join(dir, CacheVerifiedRecord))
	if err != nil {
		return nil, err
	}
	var rec verifiedRecord
	if err := strictUnmarshal(b, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

// installedEntry pairs one unpacked directory with its verified.json record.
type installedEntry struct {
	dir string
	rec verifiedRecord
}

// listUnpacked walks dir's unpacked/sha256/* directories and returns every
// one that carries a verified.json record (an unfinished or pre-seeded
// directory with none is skipped here — resolving it is resolve_installed's
// job, which unpacks it on demand per §1/§6).
func listUnpacked(dir string) ([]installedEntry, error) {
	root := filepath.Join(dir, UnpackedDirName())
	entries, err := os.ReadDir(root)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []installedEntry
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		sub := filepath.Join(root, e.Name())
		rec, err := readVerifiedRecord(sub)
		if err != nil {
			continue // a pre-seeded or partially written directory: not yet a verified install
		}
		out = append(out, installedEntry{dir: sub, rec: *rec})
	}
	return out, nil
}

// newestMatching picks, among entries whose platform equals platform and
// whose version satisfies versionWithin(request, version), the newest by
// (version, build) — build compared as the fixed-width string it is
// (docs/guides/fetch-v1.md §6's offline-newest-build case).
func newestMatching(entries []installedEntry, platform, request string) (*installedEntry, bool) {
	var candidates []installedEntry
	for _, e := range entries {
		if e.rec.Platform != platform {
			continue
		}
		if !versionWithin(request, e.rec.Version) {
			continue
		}
		candidates = append(candidates, e)
	}
	if len(candidates) == 0 {
		return nil, false
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].rec.Version != candidates[j].rec.Version {
			return versionLess(candidates[i].rec.Version, candidates[j].rec.Version)
		}
		return candidates[i].rec.Build < candidates[j].rec.Build
	})
	best := candidates[len(candidates)-1]
	return &best, true
}

// sortPlatformKeys orders keys by their position in the canonical Platforms
// list (constants_gen.go), not alphabetically — the fixtures lane's own
// expected-lock fixtures use this order (linux-amd64, linux-arm64,
// darwin-arm64), and a lock file is for a reader to compare, not a sorted
// index. A key not in Platforms (should not happen) sorts after every
// known one, stably.
func sortPlatformKeys(keys []string) {
	rank := func(key string) int {
		for i, p := range Platforms {
			if p.Key == key {
				return i
			}
		}
		return len(Platforms)
	}
	sort.SliceStable(keys, func(i, j int) bool { return rank(keys[i]) < rank(keys[j]) })
}

// versionLess compares two four-part version strings numerically, component
// by component.
func versionLess(a, b string) bool {
	as := strings.Split(a, ".")
	bs := strings.Split(b, ".")
	for i := 0; i < len(as) && i < len(bs); i++ {
		if as[i] != bs[i] {
			an, aok := parseUint(as[i])
			bn, bok := parseUint(bs[i])
			if aok && bok {
				return an < bn
			}
			return as[i] < bs[i]
		}
	}
	return len(as) < len(bs)
}

func parseUint(s string) (uint64, bool) {
	var n uint64
	if s == "" {
		return 0, false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0, false
		}
		n = n*10 + uint64(r-'0')
	}
	return n, true
}
