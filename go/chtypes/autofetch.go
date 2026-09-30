package chtypes

// autofetch.go — the search-path miss, the opt-in lazy fetch, and
// construction-time discovery (docs/guides/fetch.md §1, §6, §7). The
// resolution engine itself (what a line or patch request does, in what
// order, with or without a fetch) lives in resolve.go; this file is ForContext's
// thin wrapper over it, discover()'s manifest scan, and the process-wide
// autofetch memo both share.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// ForContext is For with a context that bounds a lazy fetch.
func (r *Registry) ForContext(ctx context.Context, v Version) (*Library, error) {
	res, err := r.ResolveContext(ctx, v)
	if err != nil {
		return nil, err
	}
	return res.Library, nil
}

// discover records which lines this registry can see SOMETHING for on the
// §1 search path — the flat slot, patches/, or both — without dlopening
// anything. It runs for BOTH constructor shapes: an explicit directory is
// the head of the same search path, and skipping the scan for it left
// Versions() empty until something had been opened. Its only role now is
// the construction-time emptiness check and Versions()'s unopened-line
// listing (resolve.go re-walks the search path itself on every call, so a
// patch installed after construction is found regardless of what this
// recorded).
func (r *Registry) discover() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, dir := range r.search {
		r.discoverDirLocked(dir)
	}
}

func (r *Registry) discoverDirLocked(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if name == patchesDirName {
			r.discoverPatchesTreeLocked(filepath.Join(dir, name))
			continue
		}
		sub := filepath.Join(dir, name)
		m, ok := readArtifactDir(sub)
		if !ok {
			continue
		}
		r.noteKnownLocked(lineOfManifest(m.Minor, m.Version, name))
	}
}

// discoverPatchesTreeLocked scans <dir>/patches/<minor>/<exact>/ for every
// line it names, so a registry holding ONLY nested patches (no flat
// install) for a line is not reported empty (P19).
func (r *Registry) discoverPatchesTreeLocked(patchesRoot string) {
	lineEntries, err := os.ReadDir(patchesRoot)
	if err != nil {
		return
	}
	for _, le := range lineEntries {
		if !le.IsDir() {
			continue
		}
		lineDir := filepath.Join(patchesRoot, le.Name())
		exactEntries, err := os.ReadDir(lineDir)
		if err != nil {
			continue
		}
		for _, ee := range exactEntries {
			if !ee.IsDir() || strings.HasPrefix(ee.Name(), ".") {
				continue
			}
			m, ok := readArtifactDir(filepath.Join(lineDir, ee.Name()))
			if !ok {
				continue
			}
			r.noteKnownLocked(lineOfManifest(m.Minor, m.Version, le.Name()))
		}
	}
}

func (r *Registry) noteKnownLocked(minor string) {
	if minor != "" {
		r.known[minor] = true
	}
}

// lineOfManifest is the minor line a manifest names: its own
// clickhouse_minor, else derived from clickhouse_version, else the
// directory name it was found under — the same fallback chain
// readArtifactDir's callers have always used.
func lineOfManifest(minor, version, dirName string) string {
	if minor != "" {
		return minor
	}
	if version != "" {
		return minorOf(version)
	}
	return dirName
}

// fetchOptions is what a lazy fetch runs with: the caller's options, the
// registry's own write directory (§1: the explicit path, else
// $CHTYPES_REGISTRY, else the cache) and always this host's platform.
func (r *Registry) fetchOptions() FetchOptions {
	o := r.fetch
	o.Platform = HostPlatform()
	if o.Dest == "" {
		o.Dest = FetchRegistryDir(r.explicit)
	}
	return o
}

// -------------------------------------------------------------- the memo
//
// One fetch per process per (dest, spelling): concurrent opens of a missing
// line OR patch wait for the one in flight and share its result. A success
// is remembered (the installed directory always still satisfies a later
// lookup anyway); CHTYPES_ARTIFACT_UNPUBLISHED is remembered explicitly —
// so a definite "not published" costs one network read, not one per call —
// and any other failure is forgotten, so a later call may retry it.

var (
	autoFetchMu          sync.Mutex
	autoFetchCalls       = map[string]*autoFetchCall{}
	autoFetchUnpublished = map[string]bool{}
)

type autoFetchCall struct {
	done chan struct{}
	dir  string
	err  error
}

func autoFetchOnce(ctx context.Context, spelling string, opts FetchOptions) (string, error) {
	key := opts.Dest + "\x00" + spelling
	autoFetchMu.Lock()
	if c, ok := autoFetchCalls[key]; ok {
		autoFetchMu.Unlock()
		select {
		case <-c.done:
			return c.dir, c.err
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	c := &autoFetchCall{done: make(chan struct{})}
	autoFetchCalls[key] = c
	autoFetchMu.Unlock()

	inst, err := Ensure(ctx, spelling, opts)
	if err == nil {
		c.dir = inst.Dir
	}
	c.err = err
	close(c.done)
	if err != nil {
		autoFetchMu.Lock()
		delete(autoFetchCalls, key)
		if isArtifactUnpublished(err) {
			autoFetchUnpublished[key] = true
		}
		autoFetchMu.Unlock()
	}
	return c.dir, c.err
}

// autoFetchRememberedUnpublished reports whether key (dest + "\x00" +
// spelling) already came back CHTYPES_ARTIFACT_UNPUBLISHED in this process.
func autoFetchRememberedUnpublished(key string) bool {
	autoFetchMu.Lock()
	defer autoFetchMu.Unlock()
	return autoFetchUnpublished[key]
}
