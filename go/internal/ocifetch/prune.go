package ocifetch

// prune.go — `chtypes prune` (docs/guides/fetch-v1.md §1, "Pruning"; public
// issue #494): remove the installed builds of a line that newer installed
// builds of the same line and platform supersede, keeping the newest N, and
// never one a live process holds (hold.go). It reads and writes the cache
// only, never a system directory, and never another fingerprint's records
// (§3: the dev channel's own-fingerprint rule).

import (
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

// lineRegex is a two-part line spelling, "26.8".
var lineRegex = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

// IsLine reports whether s is a two-part line spelling, such as "26.8".
func IsLine(s string) bool { return lineRegex.MatchString(s) }

// LineOf is the two-part line of a four-part version: "26.8" for
// "26.8.15.10", or "" for anything shorter.
func LineOf(version string) string {
	parts := strings.Split(version, ".")
	if len(parts) < 2 {
		return ""
	}
	return parts[0] + "." + parts[1]
}

// PruneOptions is what a prune removes.
type PruneOptions struct {
	Line   string // a two-part line ("26.8"); "" is every line
	Keep   int    // how many of each line's newest builds stay, per platform: at least 1
	DryRun bool   // decide and report exactly what would go, and remove nothing
}

// Superseded is one installed build a prune found superseded: removed (or,
// in a dry run, to be removed), or kept because it is in use.
type Superseded struct {
	Platform string
	Version  string
	Build    string
	Manifest Digest // the entry's own name, unpacked/sha256/<hex>
	Dir      string
	// InUse is set when the build was kept: a live process holds it (or its
	// record cannot be locked at all, so nothing can tell).
	InUse bool
}

// Prune removes, from the cache, every installed build that at least Keep
// newer installed builds of its own line and platform supersede, oldest
// first. Builds are ordered by (version, build), and the report by platform
// (the constants' order), then oldest first. A superseded build a live
// process holds is reported InUse and kept. For each build it removes the
// unpacked entry with its record, then its index.json entries, then its
// blobs (the manifest, its config and layer, and every referrer manifest of
// it the layout holds, with that referrer's layers), never a blob a
// remaining build names.
func Prune(opts *Options, p PruneOptions) ([]Superseded, error) {
	if p.Keep < 1 {
		return nil, fmt.Errorf("chtypes: --keep is at least 1 (the newest build of a line is never superseded), not %d", p.Keep)
	}
	if p.Line != "" && !IsLine(p.Line) {
		return nil, fmt.Errorf("chtypes: --line takes a two-part line such as 26.8, not %q", p.Line)
	}
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	out, err := prune(ro, p)
	return out, cacheIOError(err, ro.cacheDir)
}

// removal is one entry moved aside, to be deleted with its blobs.
type removal struct {
	entry    installedEntry
	manifest Digest
	aside    string
}

func prune(ro resolvedOptions, p PruneOptions) ([]Superseded, error) {
	// Only the cache is pruned: a system directory is read-only.
	cacheOnly := ro
	cacheOnly.systemDirs = nil
	if _, err := probeRoots(cacheOnly); err != nil {
		return nil, err
	}
	entries, err := listUnpacked(ro.cacheDir)
	if err != nil {
		// An unreadable cache holds nothing this call can prune; the default
		// mode's warning names it (MissingNotes).
		return nil, nil
	}
	type group struct{ platform, line string }
	groups := map[group][]installedEntry{}
	for _, e := range entries {
		if !ro.ch.visible(e.rec.Predicate) {
			continue // another fingerprint's build: its own SDK keeps or prunes it
		}
		line := LineOf(e.rec.Version)
		if p.Line != "" && line != p.Line {
			continue
		}
		g := group{e.rec.Platform, line}
		groups[g] = append(groups[g], e)
	}
	var superseded []installedEntry
	for _, members := range groups {
		sort.Slice(members, func(i, j int) bool { return entryOlder(members[j], members[i]) })
		if len(members) > p.Keep {
			superseded = append(superseded, members[p.Keep:]...)
		}
	}
	sort.Slice(superseded, func(i, j int) bool {
		if a, b := platformRank(superseded[i].rec.Platform), platformRank(superseded[j].rec.Platform); a != b {
			return a < b
		}
		return entryOlder(superseded[i], superseded[j])
	})

	unpackedRoot := filepath.Join(ro.cacheDir, UnpackedDirName())
	var out []Superseded
	var removed []removal
	for _, e := range superseded {
		manifest := entryManifest(e)
		s := Superseded{Platform: e.rec.Platform, Version: e.rec.Version, Build: e.rec.Build, Manifest: manifest, Dir: e.dir}
		state, release := claim(e.dir)
		switch state {
		case claimGone:
			continue // another prune removed it, or an install replaced it
		case claimInUse:
			s.InUse = true
			out = append(out, s)
			continue
		}
		if p.DryRun {
			release()
			out = append(out, s)
			continue
		}
		// Moved aside while the exclusive lock is held: once it drops, no
		// lookup lists the entry, and a hold taken on it finds it gone.
		aside, err := mkdirTemp(unpackedRoot, ".prune-")
		if err == nil {
			if err = os.Rename(e.dir, filepath.Join(aside, "entry")); err != nil {
				_ = os.Remove(aside)
			}
		}
		release()
		if err != nil {
			return out, err
		}
		removed = append(removed, removal{entry: e, manifest: manifest, aside: aside})
		out = append(out, s)
	}
	if len(removed) == 0 {
		return out, nil
	}

	l := newLayout(ro.cacheDir, false)
	doomed := map[Digest]bool{}
	for _, r := range removed {
		doomed[r.manifest] = true
	}
	blobs := l.blobsOf(doomed)
	for _, r := range removed {
		d := r.entry.rec.Digests
		for _, x := range []Digest{d.Layer, d.Bundle, d.BundleManifest} {
			if x.Valid() {
				blobs[x] = true
			}
		}
	}
	// Never a blob a remaining build names, whatever its fingerprint.
	if rest, err := listUnpacked(ro.cacheDir); err == nil {
		for _, e := range rest {
			d := e.rec.Digests
			for _, x := range append(l.ownBlobs(entryManifest(e)), d.Manifest, d.Layer, d.Bundle, d.BundleManifest) {
				delete(blobs, x)
			}
		}
	}
	if err := l.removeIndexEntries(blobs); err != nil {
		return out, err
	}
	for d := range blobs {
		if err := os.Remove(l.blobPath(d)); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return out, err
		}
	}
	for _, r := range removed {
		if err := os.RemoveAll(r.aside); err != nil {
			return out, err
		}
	}
	return out, nil
}

// entryManifest is an installed entry's own manifest digest: its directory's
// name.
func entryManifest(e installedEntry) Digest { return Digest("sha256:" + filepath.Base(e.dir)) }

// entryOlder orders installed builds by age: (version, build), then the
// manifest digest, so the order is total.
func entryOlder(a, b installedEntry) bool {
	if a.rec.Version != b.rec.Version {
		return versionLess(a.rec.Version, b.rec.Version)
	}
	if a.rec.Build != b.rec.Build {
		return a.rec.Build < b.rec.Build
	}
	return entryManifest(a) < entryManifest(b)
}

// platformRank is a platform key's place in the constants' platform list.
func platformRank(key string) int {
	for i, p := range Platforms {
		if p.Key == key {
			return i
		}
	}
	return len(Platforms)
}

// ownBlobs are the blobs a manifest names itself: the manifest, its config
// and its layers (only the manifest when its blob is absent or unreadable).
func (l *layout) ownBlobs(manifest Digest) []Digest {
	out := []Digest{manifest}
	b, ok := l.readBlob(manifest)
	if !ok {
		return out
	}
	var m ImageManifest
	if json.Unmarshal(b, &m) != nil {
		return out
	}
	if m.Config.Digest.Valid() {
		out = append(out, m.Config.Digest)
	}
	for _, layer := range m.Layers {
		if layer.Digest.Valid() {
			out = append(out, layer.Digest)
		}
	}
	return out
}

// blobsOf are the blobs a prune removes with the given manifests: each
// manifest's own (ownBlobs), and every referrer manifest the layout holds
// whose subject is one of them (a signature, a goldens document), with that
// referrer's layers. A referrer's config, the empty `{}` every referrer
// shares, is never among them.
func (l *layout) blobsOf(manifests map[Digest]bool) map[Digest]bool {
	out := map[Digest]bool{}
	for d := range manifests {
		for _, x := range l.ownBlobs(d) {
			out[x] = true
		}
	}
	dir := filepath.Join(l.dir, "blobs", "sha256")
	names, err := os.ReadDir(dir)
	if err != nil {
		return out
	}
	for _, e := range names {
		if e.IsDir() || !recordHexPattern.MatchString(e.Name()) {
			continue
		}
		info, err := e.Info()
		if err != nil || info.Size() > ManifestMaxBytes {
			continue
		}
		b, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			continue
		}
		var m ImageManifest
		if json.Unmarshal(b, &m) != nil || m.Subject == nil || !manifests[m.Subject.Digest] {
			continue
		}
		out[Digest("sha256:"+e.Name())] = true
		for _, layer := range m.Layers {
			if layer.Digest.Valid() {
				out[layer.Digest] = true
			}
		}
	}
	return out
}

// removeIndexEntries drops every index.json entry whose digest is in drop,
// by the same read-check-rename loop addIndexEntry uses, so an entry another
// process adds meanwhile survives. An absent or unparsable index.json is
// left as it is.
func (l *layout) removeIndexEntries(drop map[Digest]bool) error {
	indexPath := filepath.Join(l.dir, "index.json")
	for {
		before, err := os.ReadFile(indexPath)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return err
		}
		var idx ociLayoutIndex
		if json.Unmarshal(before, &idx) != nil {
			return nil
		}
		kept := []Descriptor{}
		for _, d := range idx.Manifests {
			if !drop[d.Digest] {
				kept = append(kept, d)
			}
		}
		if len(kept) == len(idx.Manifests) {
			return nil
		}
		idx.Manifests = kept
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
		if current, _ := os.ReadFile(indexPath); string(current) != string(before) {
			_ = os.Remove(tmp.Name())
			continue // another process wrote it meanwhile: drop from theirs
		}
		if err := os.Rename(tmp.Name(), indexPath); err != nil {
			_ = os.Remove(tmp.Name())
			return err
		}
		return nil
	}
}
