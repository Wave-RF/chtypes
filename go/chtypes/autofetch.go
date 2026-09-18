package chtypes

// autofetch.go — the search-path miss and the opt-in lazy fetch
// (docs/guides/fetch.md §1, §6, §7).

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// ForContext is For with a context that bounds a lazy fetch.
func (r *Registry) ForContext(ctx context.Context, v Version) (*Library, error) {
	if v == "" {
		return nil, fmt.Errorf("chtypes: empty version")
	}
	if l := r.lookup(v); l != nil {
		return l, nil
	}
	minor := minorOf(string(v))
	if l, err := r.resolveWithoutFetch(v, minor); l != nil || err != nil {
		return l, err
	}
	if !r.autoFetch {
		return nil, missingArtifactError(string(v), HostPlatform(), r.search)
	}
	dir, err := autoFetchOnce(ctx, minor, r.fetchOptions())
	if err != nil {
		return nil, err
	}
	if err := r.loadArtifactDir(dir); err != nil {
		return nil, err
	}
	if l := r.lookup(v); l != nil {
		return l, nil
	}
	return nil, fmt.Errorf("chtypes: fetched %s but the library there does not answer for ClickHouse %s", dir, v)
}

// resolveWithoutFetch is the whole of resolution EXCEPT the fetch: what is
// already loaded, then the line's directory as the construction-time scan
// recorded it, then a fresh walk of the §1 search path for a line installed
// since. (nil, nil) means "no directory holds it", which is a fetch's cue on
// the For path and ErrArtifactMissing on the preload path — preload never
// fetches, and this is the one function that makes those two paths identical
// in everything else.
func (r *Registry) resolveWithoutFetch(v Version, minor string) (*Library, error) {
	if l := r.lookup(v); l != nil {
		return l, nil
	}
	// The scan already resolved every line it could see to the FIRST directory
	// holding it, and it knows which line a manifest claims even when the
	// directory is not named after it — which the <dir>/<minor> walk below
	// cannot see.
	if sub, ok := r.knownDir(minor); ok {
		if err := r.loadArtifactDir(sub); err != nil {
			return nil, err
		}
		if l := r.lookup(v); l != nil {
			return l, nil
		}
	}
	return r.loadFromSearchPath(v, minor)
}

// loadFromSearchPath walks the §1 directories for <minor>/manifest.json and
// loads the first one that names a library which, once loaded, answers for
// v. A directory whose library fails to load is an error, not a skip: a
// registry that silently passes over a broken install would answer from a
// neighbor, and the search order is the operator's to fix.
func (r *Registry) loadFromSearchPath(v Version, minor string) (*Library, error) {
	for _, dir := range r.search {
		sub := filepath.Join(dir, minor)
		if _, ok := readArtifactDir(sub); !ok {
			continue
		}
		if err := r.loadArtifactDir(sub); err != nil {
			return nil, err
		}
		if l := r.lookup(v); l != nil {
			return l, nil
		}
	}
	return nil, nil
}

// knownDir is the artifact directory the construction-time scan recorded for
// a line, if it saw one.
func (r *Registry) knownDir(minor string) (string, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	sub, ok := r.known[minor]
	return sub, ok
}

// loadArtifactDir loads the artifact directory sub (<registry>/<minor>).
func (r *Registry) loadArtifactDir(sub string) error {
	m, ok := readArtifactDir(sub)
	if !ok {
		return fmt.Errorf("chtypes: %s holds no usable manifest.json", sub)
	}
	if err := r.Load(filepath.Join(sub, m.Library)); err != nil {
		return fmt.Errorf("%s: %w", sub, err)
	}
	return nil
}

// discover records which line each directory on the §1 search path would
// serve — first directory wins — without dlopening anything. It runs for
// BOTH constructor shapes: an explicit directory is the head of the same
// search path, and skipping the scan for it left Versions() empty until
// something had been opened.
func (r *Registry) discover() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, dir := range r.search {
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			sub := filepath.Join(dir, e.Name())
			m, ok := readArtifactDir(sub)
			if !ok {
				continue
			}
			minor := m.Minor
			if minor == "" && m.Version != "" {
				minor = minorOf(m.Version)
			}
			if minor == "" {
				minor = e.Name()
			}
			if _, seen := r.known[minor]; !seen {
				r.known[minor] = sub
			}
		}
	}
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

// One fetch per process per (dest, line): concurrent opens of a missing
// line wait for the one in flight and share its result. A fetch that
// failed is forgotten so a later open may try again; a fetch that
// succeeded is remembered, though the installed directory would satisfy
// the search path anyway.
var (
	autoFetchMu    sync.Mutex
	autoFetchCalls = map[string]*autoFetchCall{}
)

type autoFetchCall struct {
	done chan struct{}
	dir  string
	err  error
}

func autoFetchOnce(ctx context.Context, line string, opts FetchOptions) (string, error) {
	key := opts.Dest + "\x00" + line
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

	inst, err := Ensure(ctx, line, opts)
	if err == nil {
		c.dir = inst.Dir
	}
	c.err = err
	close(c.done)
	if err != nil {
		autoFetchMu.Lock()
		delete(autoFetchCalls, key)
		autoFetchMu.Unlock()
	}
	return c.dir, c.err
}
