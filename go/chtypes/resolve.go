//go:build chtypes_v0_retired

package chtypes

// resolve.go — exact-patch resolution, the flat/patches split and the
// same-line warned fallback (issue #284).
//
// LAYOUT (binding on every reader; supersedes docs/guides/fetch.md §4's
// nested-layout text, per the lead's layout-rule amendment on the issue):
//
//	<search-dir>/<minor>/                       the line's FLAT slot — one
//	                                             patch: the one a LINE fetch
//	                                             most recently selected.
//	<search-dir>/patches/<minor>/<exact>/        every OTHER installed patch
//	                                             of that line.
//
// A LINE request ("26.8") resolves within the FIRST search-path directory
// that holds any patch of the line (flat, patches/, or both), taking the
// newest patch found there, and PINS the line to it for the life of this
// Registry (R3). The pin never moves while the process runs.
//
// A PATCH request ("26.8.15.10-lts") is matched against EVERY search-path
// directory, not only the first that holds the line, taking the first exact
// hit under the channel-suffix matching rule (R2, fetch.md Decision 7).
// Failing that, with autofetch it is fetched; failing THAT (unpublished) or
// with autofetch off, it falls back to the newest installed/published patch
// of the SAME line, flagged Exact=false and warned once per (requested,
// actual) pair per process (§3). Never another line.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// patchesDirName is the sibling tree holding every installed patch that
// does not occupy its line's flat slot.
const patchesDirName = "patches"

// Resolution is what a resolve call hands back: the loaded Library, the
// caller's own spelling, what actually loaded, and whether it was exact.
// For/ForContext run the same resolution and hand back only the Library.
type Resolution struct {
	// Library is the loaded library Resolve chose.
	Library *Library
	// Requested is the caller's spelling, trimmed.
	Requested Version
	// Version is the loaded library's own version — what actually loaded,
	// never what was requested.
	Version Version
	// Exact is true for a line request. For a patch request it is true only
	// when the loaded library's version matches the requested patch under
	// the shared matching rule (R2) — false means the fallback within the
	// line was taken.
	Exact bool
}

// installedPatch names one patch found on the §1 search path, read from a
// manifest.json: the directory it lives in, its own reported version, and
// whether that directory is the line's flat slot.
type installedPatch struct {
	dir     string
	version string
	flat    bool
}

// patchesInDir lists every patch of minor that directory sp (one §1
// search-path entry, or a fetch destination) holds: the flat slot, plus
// every patches/<minor>/<exact> sibling. Order is unspecified; callers sort.
func patchesInDir(sp, minor string) []installedPatch {
	var out []installedPatch
	if m, ok := readArtifactDir(filepath.Join(sp, minor)); ok {
		v := m.Version
		if v == "" {
			v = minor
		}
		out = append(out, installedPatch{dir: filepath.Join(sp, minor), version: v, flat: true})
	}
	patchesRoot := filepath.Join(sp, patchesDirName, minor)
	entries, err := os.ReadDir(patchesRoot)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		sub := filepath.Join(patchesRoot, e.Name())
		if m, ok := readArtifactDir(sub); ok {
			v := m.Version
			if v == "" {
				v = e.Name()
			}
			out = append(out, installedPatch{dir: sub, version: v, flat: false})
		}
	}
	return out
}

// newestInDir is the newest patch of minor within sp (flat + patches/), or
// ok=false when sp holds none.
func newestInDir(sp, minor string) (installedPatch, bool) {
	ps := patchesInDir(sp, minor)
	if len(ps) == 0 {
		return installedPatch{}, false
	}
	sort.Slice(ps, func(i, j int) bool { return lessVersionKey(versionKey(ps[i].version), versionKey(ps[j].version)) })
	return ps[len(ps)-1], true
}

// locateLineAnywhere is R3 step 2: the first search-path directory that
// holds ANY patch of minor, and the newest patch found there.
func locateLineAnywhere(search []string, minor string) (installedPatch, bool) {
	for _, sp := range search {
		if p, ok := newestInDir(sp, minor); ok {
			return p, true
		}
	}
	return installedPatch{}, false
}

// locatePatchAnywhere is R4 step 2: every search-path directory, not only
// the first holding the line, for the first hit matching exact under R2.
func locatePatchAnywhere(search []string, minor, exact string) (installedPatch, bool) {
	for _, sp := range search {
		for _, p := range patchesInDir(sp, minor) {
			if exactMatches(p.version, exact) {
				return p, true
			}
		}
	}
	return installedPatch{}, false
}

// ---------------------------------------------------------- Registry state

// loadedPatch answers a PATCH request from what this registry already has
// open: an exact byVersion hit, matched under R2. It deliberately never
// consults the line pin — a registry holding a DIFFERENT patch of the same
// line must not silently answer a patch request with it.
func (r *Registry) loadedPatch(exact string) (*Library, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if lib, ok := r.byVersion[exact]; ok {
		return lib, true
	}
	if channelSuffix.MatchString(exact) {
		return nil, false // a channel-qualified spelling matches only itself (R2)
	}
	for ver, lib := range r.byVersion {
		if exactMatches(ver, exact) {
			return lib, true
		}
	}
	return nil, false
}

// pinnedLine answers a LINE request from this registry's pin, if it has
// resolved one already. The pin never moves while the process runs (R3).
func (r *Registry) pinnedLine(minor string) (*Library, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	lib, ok := r.linePin[minor]
	return lib, ok
}

// pinLine sets the line's pin, but only the FIRST time (R3, R8): a second
// call for a line that already resolved never re-points it.
func (r *Registry) pinLine(minor string, lib *Library) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.linePin[minor]; !ok {
		r.linePin[minor] = lib
	}
}

// load is Load's own body, returning the *Library it opened (or already had
// open) so the resolution engine can use it without a second lookup.
func (r *Registry) load(path string) (*Library, error) {
	if err := checkLibraryBytes(path); err != nil {
		return nil, err
	}
	if r.verify {
		if err := verifyArtifactLibrary(path); err != nil {
			return nil, err
		}
	}
	lib, err := openLibrary(path, r.effectiveTimezone())
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byVersion[string(lib.Version)] = lib
	if _, pinned := r.linePin[lib.Minor]; !pinned {
		r.linePin[lib.Minor] = lib
	}
	return lib, nil
}

// loadArtifactDirLib loads the artifact directory dir (one manifest.json +
// library pair, flat or under patches/) and returns the *Library.
func (r *Registry) loadArtifactDirLib(dir string) (*Library, error) {
	m, ok := readArtifactDir(dir)
	if !ok {
		return nil, fmt.Errorf("chtypes: %s holds no usable manifest.json", dir)
	}
	lib, err := r.load(filepath.Join(dir, m.Library))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", dir, err)
	}
	return lib, nil
}

// ------------------------------------------------------------- Resolve API

// Resolve is ResolveContext with context.Background().
func (r *Registry) Resolve(v Version) (Resolution, error) {
	return r.ResolveContext(context.Background(), v)
}

// ResolveContext resolves a minor line ("25.8") or an exact patch
// ("25.8.28.1-lts") to a Resolution: the loaded Library, the caller's own
// spelling, what actually loaded, and whether it was exact (§Version
// selection; issue #284). For/ForContext run the same resolution and hand
// back only the Library.
func (r *Registry) ResolveContext(ctx context.Context, v Version) (Resolution, error) {
	return r.resolveVersion(ctx, v, r.autoFetch)
}

// resolveVersion is ResolveContext with fetching independently controlled —
// WithPreload calls this with allowFetch=false even when AutoFetch is on
// (R7: preload never fetches).
func (r *Registry) resolveVersion(ctx context.Context, v Version, allowFetch bool) (Resolution, error) {
	if v == "" {
		return Resolution{}, fmt.Errorf("chtypes: empty version")
	}
	line, exact, err := parseSpelling(string(v))
	if err != nil {
		return Resolution{}, err
	}
	requested := Version(strings.TrimPrefix(strings.TrimSpace(string(v)), "v"))
	if exact == "" {
		return r.resolveLine(ctx, requested, line, allowFetch)
	}
	return r.resolvePatch(ctx, requested, line, exact, allowFetch)
}

// resolveLine is R3.
func (r *Registry) resolveLine(ctx context.Context, requested Version, minor string, allowFetch bool) (Resolution, error) {
	if lib, ok := r.pinnedLine(minor); ok {
		return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
	}
	if p, ok := locateLineAnywhere(r.search, minor); ok {
		lib, err := r.loadArtifactDirLib(p.dir)
		if err != nil {
			return Resolution{}, err
		}
		r.pinLine(minor, lib)
		return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
	}
	if !allowFetch {
		return Resolution{}, missingArtifactError(string(requested), HostPlatform(), r.search)
	}
	dir, err := autoFetchOnce(ctx, minor, r.fetchOptions())
	if err != nil {
		return Resolution{}, err
	}
	lib, err := r.loadArtifactDirLib(dir)
	if err != nil {
		return Resolution{}, err
	}
	r.pinLine(minor, lib)
	return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
}

// resolvePatch is R4.
func (r *Registry) resolvePatch(ctx context.Context, requested Version, minor, exact string, allowFetch bool) (Resolution, error) {
	// Step 1: already loaded, matched under R2.
	if lib, ok := r.loadedPatch(exact); ok {
		return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
	}
	// R-c: the disk scan that steps 2 and 4 need is throttled to once per
	// 60s per (registry, requested patch) once a fallback has been taken —
	// the in-memory check above still runs on every call, so a patch Load'd
	// by another goroutine in the meantime is picked up immediately.
	if cached, ok := r.fallbackHit(exact); ok {
		return cached, nil
	}
	// Step 2: anywhere on the search path, not only the first dir holding
	// the line.
	if p, ok := locatePatchAnywhere(r.search, minor, exact); ok {
		lib, err := r.loadArtifactDirLib(p.dir)
		if err != nil {
			return Resolution{}, err
		}
		return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
	}
	// Step 3: autofetch the exact patch, unless it already came back
	// unpublished in this process.
	if allowFetch && r.autoFetch {
		opts := r.fetchOptions()
		key := opts.Dest + "\x00" + exact
		if !autoFetchRememberedUnpublished(key) {
			dir, err := autoFetchOnce(ctx, exact, opts)
			switch {
			case err == nil:
				lib, lerr := r.loadArtifactDirLib(dir)
				if lerr != nil {
					return Resolution{}, lerr
				}
				return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: true}, nil
			case !isArtifactUnpublished(err):
				return Resolution{}, err
			}
			// CHTYPES_ARTIFACT_UNPUBLISHED: fall through to step 4.
		}
	}
	// Step 4: fall back within the line.
	res, err := r.fallbackWithinLine(ctx, requested, minor, exact, allowFetch)
	if err == nil {
		r.recordFallback(exact, res)
	}
	return res, err
}

// fallbackWithinLine is R4 step 4: the newest installed patch of the line
// (autofetch off, or this is a preload/no-fetch resolution), or the
// directory Ensure(line) installs (autofetch on) — at most once per process
// per (destination, line), the same call a line request would have made.
func (r *Registry) fallbackWithinLine(ctx context.Context, requested Version, minor, exact string, allowFetch bool) (Resolution, error) {
	if !allowFetch || !r.autoFetch {
		p, ok := locateLineAnywhere(r.search, minor)
		if !ok {
			return Resolution{}, missingArtifactError(string(requested), HostPlatform(), r.search)
		}
		lib, err := r.loadArtifactDirLib(p.dir)
		if err != nil {
			return Resolution{}, err
		}
		r.warnFallback(requested, lib.Version, minor, false)
		return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: false}, nil
	}
	dir, err := autoFetchOnce(ctx, minor, r.fetchOptions())
	if err != nil {
		return Resolution{}, err
	}
	lib, err := r.loadArtifactDirLib(dir)
	if err != nil {
		return Resolution{}, err
	}
	r.warnFallback(requested, lib.Version, minor, true)
	return Resolution{Library: lib, Requested: requested, Version: lib.Version, Exact: false}, nil
}

// ---------------------------------------------------------------- R-c: the
// 60s fallback re-check throttle (the lead's amendment to R4's "not
// memoized" rule). Per-Registry, keyed by the requested exact patch.

type fallbackCacheEntry struct {
	at  time.Time
	res Resolution
}

// fallbackRecheckInterval is amendment R-c's bound. A var, not a const, so
// a test can shrink it to prove the throttle actually gates the re-scan
// rather than merely existing in prose.
var fallbackRecheckInterval = 60 * time.Second

func (r *Registry) fallbackHit(exact string) (Resolution, bool) {
	r.fbMu.Lock()
	defer r.fbMu.Unlock()
	c, ok := r.fallback[exact]
	if !ok || time.Since(c.at) >= fallbackRecheckInterval {
		return Resolution{}, false
	}
	return c.res, true
}

func (r *Registry) recordFallback(exact string, res Resolution) {
	r.fbMu.Lock()
	defer r.fbMu.Unlock()
	if r.fallback == nil {
		r.fallback = map[string]fallbackCacheEntry{}
	}
	r.fallback[exact] = fallbackCacheEntry{at: time.Now(), res: res}
}

// --------------------------------------------------------------- the warning
//
// §3: one `chtypes: WARNING:` line per (requested, actual) pair per process,
// written to the registry's FetchOptions.Progress, else os.Stderr — the
// same mechanism fetcher.warn already uses. Process-wide (not per-Registry),
// so two registries in one process warn once between them. The pair is
// recorded BEFORE it is emitted, so a caller's own warning handling cannot
// cause a retry to warn again.

var (
	warnedMu    sync.Mutex
	warnedPairs = map[string]bool{}
)

// recordWarned reports whether (requested, actual) was already warned, and
// marks it warned.
func recordWarned(requested, actual Version) (already bool) {
	warnedMu.Lock()
	defer warnedMu.Unlock()
	k := string(requested) + "\x00" + string(actual)
	if warnedPairs[k] {
		return true
	}
	warnedPairs[k] = true
	return false
}

func (r *Registry) warnFallback(requested, actual Version, minor string, viaAutofetch bool) {
	if recordWarned(requested, actual) {
		return
	}
	var body string
	if viaAutofetch {
		body = fmt.Sprintf(
			"ClickHouse %s is not published for %s at ABI revision %d; using %s, the newest published patch of %s. Behavior can differ between patches.",
			requested, HostPlatform(), fetchABIRevision(), actual, minor)
	} else {
		body = fmt.Sprintf(
			"ClickHouse %s is not installed for %s; using %s, the newest installed patch of %s. Behavior can differ between patches. If %s is published, install it with: %s %s",
			requested, HostPlatform(), actual, minor, requested, GoFetchCommand, requested)
	}
	w := r.fetch.Progress
	if w == nil {
		w = os.Stderr
	}
	fmt.Fprintf(w, "chtypes: WARNING: %s\n", body)
}

// isArtifactUnpublished reports whether err is CHTYPES_ARTIFACT_UNPUBLISHED.
func isArtifactUnpublished(err error) bool {
	var ae *ArtifactError
	return errors.As(err, &ae) && ae.Code == CodeArtifactUnpublished
}
