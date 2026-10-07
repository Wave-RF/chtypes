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
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
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

// verifiedRecord is the durable proof that one unpacked directory was
// verified once: under which key, against which digests, and the predicate
// that was checked. Its on-disk form is the one canonical record every
// binding reads and writes (spec/fetch-v1/schema/verified.schema.json,
// docs/guides/fetch-v1.md §1): see recordWire, encodeRecord and
// decodeRecord. It is written when the directory is created; a record that
// a reader cannot accept is replaced after a successful re-verify.
type verifiedRecord struct {
	Schema        int
	Platform      string
	Version       string
	Channel       string // "" is written as null
	Build         string
	LibraryPath   string // the library's file name, relative to the unpacked directory
	LibrarySHA256 string
	LibraryBytes  int64
	Digests       Digests // an empty digest is written as null
	Predicate     map[string]any
	SignedBy      string // "" is written as null (allow-unsigned only)
}

// recordWire is verified.json exactly: every member present, null where the
// schema allows it.
type recordWire struct {
	Schema        int            `json:"schema"`
	Platform      string         `json:"platform"`
	Version       string         `json:"version"`
	Channel       *string        `json:"channel"`
	Build         string         `json:"build"`
	Library       string         `json:"library"`
	LibrarySHA256 string         `json:"library_sha256"`
	LibraryBytes  int64          `json:"library_bytes"`
	Digests       digestsWire    `json:"digests"`
	SignedBy      *string        `json:"signed_by"`
	Predicate     map[string]any `json:"predicate"`
}

type digestsWire struct {
	Index          *string `json:"index"`
	Manifest       string  `json:"manifest"`
	Layer          string  `json:"layer"`
	Bundle         *string `json:"bundle"`
	BundleManifest *string `json:"bundle_manifest"`
}

var (
	recordHexPattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	recordVersionPattern  = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$`)
	recordRequiredMembers = []string{"schema", "platform", "version", "channel", "build", "library", "library_sha256", "library_bytes", "digests", "signed_by", "predicate"}
	recordRequiredDigests = []string{"index", "manifest", "layer", "bundle", "bundle_manifest"}
)

func nullable[T ~string](v T) *string {
	if v == "" {
		return nil
	}
	s := string(v)
	return &s
}

func unnull(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// encodeRecord renders rec as the canonical verified.json.
func encodeRecord(rec verifiedRecord) ([]byte, error) {
	pred := rec.Predicate
	if pred == nil {
		pred = map[string]any{}
	}
	return json.Marshal(recordWire{
		Schema:        active().recordSchema,
		Platform:      rec.Platform,
		Version:       rec.Version,
		Channel:       nullable(rec.Channel),
		Build:         rec.Build,
		Library:       rec.LibraryPath,
		LibrarySHA256: rec.LibrarySHA256,
		LibraryBytes:  rec.LibraryBytes,
		Digests: digestsWire{
			Index:          nullable(rec.Digests.Index),
			Manifest:       string(rec.Digests.Manifest),
			Layer:          string(rec.Digests.Layer),
			Bundle:         nullable(rec.Digests.Bundle),
			BundleManifest: nullable(rec.Digests.BundleManifest),
		},
		SignedBy:  nullable(rec.SignedBy),
		Predicate: pred,
	})
}

// decodeRecord accepts exactly the canonical record of the active contract's
// schema (1 for v1; 2 for the dev channel, rule r5, so neither reads the other's). Anything else
// (unparsable, another schema, a missing member, a member of the wrong
// type, a rule broken) is an error, which every caller treats as an ABSENT
// record, never as a failure by itself.
func decodeRecord(b []byte) (*verifiedRecord, error) {
	if err := checkNoDuplicateKeys(b); err != nil {
		return nil, err
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return nil, err
	}
	for _, k := range recordRequiredMembers {
		if _, ok := raw[k]; !ok {
			return nil, fmt.Errorf("verified.json: missing member %q", k)
		}
	}
	var rawDigests map[string]json.RawMessage
	if err := json.Unmarshal(raw["digests"], &rawDigests); err != nil {
		return nil, fmt.Errorf("verified.json: digests: %w", err)
	}
	for _, k := range recordRequiredDigests {
		if _, ok := rawDigests[k]; !ok {
			return nil, fmt.Errorf("verified.json: missing digests member %q", k)
		}
	}
	var w recordWire
	if err := json.Unmarshal(b, &w); err != nil {
		return nil, err
	}
	if want := active().recordSchema; w.Schema != want {
		return nil, fmt.Errorf("verified.json: schema %d is not %d", w.Schema, want)
	}
	if _, ok := platformByKey(w.Platform); !ok {
		return nil, fmt.Errorf("verified.json: platform %q is not one of v1's", w.Platform)
	}
	if !recordVersionPattern.MatchString(w.Version) {
		return nil, fmt.Errorf("verified.json: version %q is not four-part", w.Version)
	}
	if w.Build == "" {
		return nil, fmt.Errorf("verified.json: empty build")
	}
	if w.Library == "" || !filepath.IsLocal(w.Library) || strings.ContainsAny(w.Library, `/\`) {
		return nil, fmt.Errorf("verified.json: library %q is not a plain relative file name", w.Library)
	}
	if !recordHexPattern.MatchString(w.LibrarySHA256) {
		return nil, fmt.Errorf("verified.json: library_sha256 is not 64 lowercase hex")
	}
	if w.LibraryBytes < 0 {
		return nil, fmt.Errorf("verified.json: negative library_bytes")
	}
	if w.Predicate == nil {
		return nil, fmt.Errorf("verified.json: predicate is not an object")
	}
	for name, d := range map[string]*string{"index": w.Digests.Index, "bundle": w.Digests.Bundle, "bundle_manifest": w.Digests.BundleManifest} {
		if d != nil && !Digest(*d).Valid() {
			return nil, fmt.Errorf("verified.json: digests.%s %q is not sha256:<hex>", name, *d)
		}
	}
	if !Digest(w.Digests.Manifest).Valid() || !Digest(w.Digests.Layer).Valid() {
		return nil, fmt.Errorf("verified.json: digests.manifest and digests.layer must be sha256:<hex>")
	}
	return &verifiedRecord{
		Schema:        w.Schema,
		Platform:      w.Platform,
		Version:       w.Version,
		Channel:       unnull(w.Channel),
		Build:         w.Build,
		LibraryPath:   w.Library,
		LibrarySHA256: w.LibrarySHA256,
		LibraryBytes:  w.LibraryBytes,
		Digests: Digests{
			Index:          Digest(unnull(w.Digests.Index)),
			Manifest:       Digest(w.Digests.Manifest),
			Layer:          Digest(w.Digests.Layer),
			Bundle:         Digest(unnull(w.Digests.Bundle)),
			BundleManifest: Digest(unnull(w.Digests.BundleManifest)),
		},
		Predicate: w.Predicate,
		SignedBy:  unnull(w.SignedBy),
	}, nil
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
	if err := os.MkdirAll(filepath.Join(l.dir, "blobs", "sha256"), dirMode); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(l.dir, UnpackedDirName()), dirMode); err != nil {
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
	if err := os.MkdirAll(filepath.Dir(dest), dirMode); err != nil {
		return err
	}
	return writeFileAtomic(filepath.Dir(dest), dest, content)
}

// writeFileAtomic writes content to a temp file under dir, then renames it
// onto dest, so a reader never observes a partially written file.
func writeFileAtomic(dir, dest string, content []byte) error {
	tmp, err := createTempFile(dir, ".tmp-")
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
		tmp, err := createTempFile(l.dir, ".tmp-index-")
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

// zeroXMinorPattern matches a 0.x registry directory's per-line entry, `26.8`.
var zeroXMinorPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+$`)

// zeroXShape names the `<minor>/manifest.json` a 0.x registry directory holds,
// when dir has that shape: no oci-layout, no unpacked/, and at least one
// `<minor>/manifest.json` (the first by name). A missing oci-layout alone is
// not the shape (a Python-written 1.x cache has none), and a directory that
// has unpacked/ is a 1.x cache whoever wrote it. It is "" for anything else,
// including a directory that is missing or cannot be read.
func zeroXShape(dir string) string {
	for _, name := range []string{"oci-layout", "unpacked"} {
		if _, err := os.Lstat(filepath.Join(dir, name)); !errors.Is(err, fs.ErrNotExist) {
			return ""
		}
	}
	entries, err := os.ReadDir(dir) // sorted by name
	if err != nil {
		return ""
	}
	for _, e := range entries {
		if !e.IsDir() || !zeroXMinorPattern.MatchString(e.Name()) {
			continue
		}
		if fi, err := os.Stat(filepath.Join(dir, e.Name(), "manifest.json")); err == nil && fi.Mode().IsRegular() {
			return e.Name() + "/manifest.json"
		}
	}
	return ""
}

// zeroXHint is the sentence a CHTYPES_ARTIFACT_MISSING answer from a cache with
// the 0.x shape carries (docs/guides/fetch-v1.md, "Upgrading from 0.x"), or ""
// when dir does not have it.
func zeroXHint(dir string) string {
	shape := zeroXShape(dir)
	if shape == "" {
		return ""
	}
	return fmt.Sprintf("%s holds a 0.x registry (%s); chtypes 1.x uses an OCI layout at "+
		"${XDG_CACHE_HOME:-~/.cache}/chtypes/v1 — point CHTYPES_CACHE at an empty or 1.x directory.", dir, shape)
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

// writeVerifiedRecord writes verified.json into unpackDir (already populated
// by unpackLibrary) and moves the directory into place as manifestDigest's
// unpacked directory (installDir). If the destination already holds a record
// a reader accepts (another fetch landed on the same manifest digest first),
// unpackDir is discarded and the existing directory is left untouched. If it
// holds an entry WITHOUT an acceptable record (a foreign, torn or
// older-format one), the caller has just re-verified from the cache's own
// blobs, so that entry is moved aside and replaced. unpackDir is consumed
// either way: it is never left behind, even on an error.
func (l *layout) writeVerifiedRecord(manifestDigest Digest, unpackDir string, rec verifiedRecord) (dest string, alreadyInstalled bool, err error) {
	dest = l.unpackedDir(manifestDigest)
	if _, rerr := readVerifiedRecord(dest); rerr == nil {
		_ = os.RemoveAll(unpackDir)
		return dest, true, nil
	}
	out, err := encodeRecord(rec)
	if err == nil {
		err = os.MkdirAll(filepath.Dir(dest), dirMode)
	}
	if err == nil {
		err = writeFileAtomic(unpackDir, filepath.Join(unpackDir, CacheVerifiedRecord), out)
	}
	if err != nil {
		_ = os.RemoveAll(unpackDir)
		return "", false, err
	}
	return installDir(unpackDir, dest)
}

// installDirAttempts bounds installDir's retries: each one follows another
// process changing the destination under it, so a handful is plenty.
const installDirAttempts = 8

// installDir moves src, a complete directory carrying its verified.json, to
// dest by rename, and never removes an entry another process may be using
// (public issue #482). Several processes may install the same build into one
// cache at once, and each must end up with a usable dest:
//
//   - The rename comes first. It fails while dest exists, so the first
//     installer wins and every later one finds dest in place.
//   - A dest that holds an acceptable record is kept, and src is discarded:
//     the same content is already installed.
//   - Only a dest WITHOUT an acceptable record (foreign, torn or older-format)
//     is moved aside, and only into a fresh ".stale-*" directory beside it.
//     If what was moved turns out to carry an acceptable record (another
//     installer's rename landed between the read and the move), it is put
//     back.
//
// It returns alreadyInstalled true when another process's install is the one
// in place. src is consumed either way.
func installDir(src, dest string) (string, bool, error) {
	var lastErr error
	for attempt := 0; attempt < installDirAttempts; attempt++ {
		err := os.Rename(src, dest)
		if err == nil {
			return dest, false, nil
		}
		lastErr = err
		if _, rerr := readVerifiedRecord(dest); rerr == nil {
			_ = os.RemoveAll(src)
			return dest, true, nil
		}
		if _, serr := os.Lstat(dest); serr != nil {
			continue // whatever stood there went away: try the rename again
		}
		stale, terr := os.MkdirTemp(filepath.Dir(dest), ".stale-*")
		if terr != nil {
			lastErr = terr
			break
		}
		aside := filepath.Join(stale, "old")
		if merr := os.Rename(dest, aside); merr != nil {
			_ = os.RemoveAll(stale)
			continue // another process moved or replaced it: look again
		}
		if _, rerr := readVerifiedRecord(aside); rerr == nil && os.Rename(aside, dest) == nil {
			_ = os.RemoveAll(stale)
			_ = os.RemoveAll(src)
			return dest, true, nil
		}
		_ = os.RemoveAll(stale)
	}
	_ = os.RemoveAll(src)
	return "", false, lastErr
}

// readVerifiedRecord reads one unpacked directory's verified.json. Any
// error means "no acceptable record here".
func readVerifiedRecord(dir string) (*verifiedRecord, error) {
	b, err := os.ReadFile(filepath.Join(dir, CacheVerifiedRecord))
	if err != nil {
		return nil, err
	}
	return decodeRecord(b)
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
		// Only <manifest-hex> names are entries: an installer's temporary
		// directory beside them (unpack-*, .staging-*, .tmp-*, .stale-*) may
		// already carry a record, and is renamed away a moment later.
		if !e.IsDir() || !recordHexPattern.MatchString(e.Name()) {
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
func newestMatching(entries []installedEntry, platform, request string, ch *channel) (*installedEntry, bool) {
	var candidates []installedEntry
	for _, e := range entries {
		if e.rec.Platform != platform {
			continue
		}
		if !ch.visible(e.rec.Predicate) {
			continue // another fingerprint's build (channel.go): never the answer
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
