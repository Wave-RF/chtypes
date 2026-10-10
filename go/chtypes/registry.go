package chtypes

// registry.go — from a request to a loaded library (bindings-v1.md section 6,
// "The sequence"): resolve_installed, then ensure when autofetch is on, then
// the Resolved to LoadInput adapter, then loader steps 1 to 7. The binding
// orders and matches no versions itself: which installed build a floating
// request means, and the spelling rules, are the fetch layer's.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"runtime"
	"sync"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// Resolved is the fetch layer's record of one installed build, re-exported
// unchanged.
type Resolved = ocifetch.Resolved

// FetchOptions carries everything docs/guides/fetch-v1.md configures. A zero
// field falls back to its environment variable, then to the documented default.
// It converts to the fetch layer's options without that layer's test hooks.
type FetchOptions struct {
	Bases         []string // CHTYPES_ARTIFACTS_URL
	CacheDir      string   // CHTYPES_CACHE
	SystemDirs    []string
	TrustedKeys   []string // raw hex ed25519 public keys; CHTYPES_TRUSTED_KEYS
	Token         string   // CHTYPES_DOWNLOAD_TOKEN
	AllowUnsigned bool     // also set by CHTYPES_ALLOW_UNSIGNED=1
	Offline       bool     // also set by CHTYPES_OFFLINE=1 (public issue #528); the cache only, no request
	Frozen        bool
	LockPath      string
	LockWrite     bool
	Update        bool
	// StrictCache makes every fault of the cache and of an existing system
	// dir a CodeCacheUnusable naming the path, never "not installed" and
	// never a fall-through to a system dir. nil means CHTYPES_CACHE_STRICT
	// ("1" is on), else off.
	StrictCache *bool
}

func (o FetchOptions) internal() *ocifetch.Options {
	return &ocifetch.Options{
		Bases: o.Bases, CacheDir: o.CacheDir, SystemDirs: o.SystemDirs, TrustedKeys: o.TrustedKeys,
		Token: o.Token, AllowUnsigned: o.AllowUnsigned, Offline: o.Offline, Frozen: o.Frozen,
		LockPath: o.LockPath, LockWrite: o.LockWrite, Update: o.Update, StrictCache: o.StrictCache,
	}
}

// CacheRoot is the cache root a fetch, list or `chtypes where` with o would
// use: o.CacheDir, else CHTYPES_CACHE, each through the dev channel's v2-dev
// subroot (spec/abi-v2/docs.md, rule r5), else
// `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`. It is the very resolution the
// fetch layer runs, and it creates nothing and reads no cache. It is the first
// entry of SearchDirs.
func CacheRoot(o FetchOptions) (string, error) { return ocifetch.CacheRoot(o.internal()) }

// SearchDirs is the ordered list of directories a lookup reads for installed
// builds: the cache root first, then each read-only system directory
// (o.SystemDirs, else the built-in list; an empty non-nil slice means none).
// The order is the fetch layer's own, and on a tie the earlier directory wins.
// It creates nothing and touches no file.
func SearchDirs(o FetchOptions) ([]string, error) { return ocifetch.SearchDirs(o.internal()) }

type registryConfig struct {
	fetch     FetchOptions
	autofetch *bool
	preload   []string
}

// RegistryOption configures NewRegistry.
type RegistryOption func(*registryConfig)

// WithFetchOptions sets the fetch layer's options.
func WithFetchOptions(o FetchOptions) RegistryOption {
	return func(c *registryConfig) { c.fetch = o }
}

// WithAutoFetch turns fetching on first use on or off. The default is the
// fetch constants' CHTYPES_AUTOFETCH environment variable, and off when that is
// unset.
func WithAutoFetch(on bool) RegistryOption {
	return func(c *registryConfig) { c.autofetch = &on }
}

// WithPreload opens each listed request at construction, in list order, and
// never fetches, even with autofetch on. A request no installed build answers
// is the ordinary ErrArtifactMissing, returned by NewRegistry.
func WithPreload(requests ...string) RegistryOption {
	return func(c *registryConfig) { c.preload = append(c.preload, requests...) }
}

// Registry opens libraries by request. It is safe for concurrent use and has no
// Close: it holds no handle, and no binding ever unloads an image.
//
// Opens of different requests never wait for each other's fetch: the registry
// holds no lock across network I/O, and an open of a request a build in the
// cache answers needs no network at all (public issue #491). Concurrent opens
// of one request share one attempt, and one fetch: each gets that attempt's
// Library or its error, and a failed attempt is never remembered, so the next
// open of the request starts a new one. A Fetch of the request shares that
// same fetch (public issue #492).
//
// Every build the registry opens or fetches is held by this process until it
// exits (ocifetch.Hold), so `chtypes prune` never removes it meanwhile
// (public issue #494).
type Registry struct {
	fetch     FetchOptions
	autofetch bool

	mu      sync.Mutex
	memo    map[string]*Library
	libs    []*Library
	flights map[string]*flight      // the attempts in progress, by request
	fetches map[string]*fetchFlight // the fetches in progress, by request
	// onWait, nil outside tests, is called each time an open starts waiting
	// on an attempt, its own or another open's, so a test can hold an
	// attempt until every open it starts is waiting on it.
	onWait func(request string)
	// onFetchWait, nil outside tests, is called each time an open or a
	// Fetch starts waiting on a fetch, its own or another's.
	onFetchWait func(request string)
}

// flight is one attempt to open one request: the installed lookup, the fetch
// when there is one, and the load. Every open of that request made while it
// is in progress waits on it and gets its answer.
type flight struct {
	done chan struct{} // closed when the attempt has landed or been abandoned

	// Written before done is closed, and read only after.
	lib       *Library
	err       error
	abandoned bool // the attempt panicked: every open waiting on it starts over

	// Under Registry.mu.
	gen     uint64             // the setup generation when the attempt began
	waiters int                // the opens waiting on it
	cancel  context.CancelFunc // ends its fetch; nil until it fetches
}

// fetchFlight is one fetch of one request (ocifetch.Ensure): every open that
// misses the cache and every Fetch of that request made while it is in
// progress waits on it and gets its answer, so they make one fetch.
type fetchFlight struct {
	done chan struct{} // closed when the fetch has an answer

	// Written before done is closed, and read only after.
	res *Resolved
	err error

	// Under Registry.mu.
	waiters int                // the callers waiting on it
	cancel  context.CancelFunc // ends the fetch
}

// NewRegistry builds a registry. Construction opens nothing; only a request
// for a version, or WithPreload, opens an artifact.
func NewRegistry(opts ...RegistryOption) (*Registry, error) {
	var c registryConfig
	for _, o := range opts {
		if o != nil {
			o(&c)
		}
	}
	r := &Registry{fetch: c.fetch, memo: map[string]*Library{}, flights: map[string]*flight{}, fetches: map[string]*fetchFlight{}}
	if c.autofetch != nil {
		r.autofetch = *c.autofetch
	} else {
		r.autofetch = os.Getenv(ocifetch.EnvAutofetchName) == "1"
	}
	for _, req := range c.preload {
		if _, err := r.open(context.Background(), req, false); err != nil {
			return nil, err
		}
	}
	return r, nil
}

// For opens the library a request names: two, three or four parts, no "v"
// prefix, no channel suffix. A request opened before returns the same Library
// for the registry's life, so a line request never moves mid-process; a newer
// patch installed later is picked up by a new registry.
func (r *Registry) For(request string) (*Library, error) {
	return r.ForContext(context.Background(), request)
}

// ForContext is For with a context for the fetch, when autofetch is on. When
// ctx is done before the open has an answer, ForContext returns ctx.Err(); a
// fetch other opens of the same request still wait on goes on for them, and
// one no open waits on any more is canceled.
func (r *Registry) ForContext(ctx context.Context, request string) (*Library, error) {
	return r.open(ctx, request, r.autofetch)
}

// open answers a request from the memo, else waits on the request's attempt,
// starting it when none is in progress. r.mu guards the memo and the attempts
// only: it is never held across the installed lookup, a fetch or a load.
func (r *Registry) open(ctx context.Context, request string, mayFetch bool) (*Library, error) {
	for {
		r.mu.Lock()
		if l := r.memo[request]; l != nil {
			r.mu.Unlock()
			return l, nil
		}
		f := r.flights[request]
		lead := f == nil
		if lead {
			f = &flight{done: make(chan struct{}), gen: setupGeneration()}
			r.flights[request] = f
		}
		f.waiters++
		onWait := r.onWait
		r.mu.Unlock()
		if onWait != nil {
			onWait(request)
		}
		if lead {
			r.lead(ctx, f, request, mayFetch)
		}
		select {
		case <-f.done:
		default:
			select {
			case <-f.done:
			case <-ctx.Done():
				r.leave(f, request)
				return nil, ctx.Err()
			}
		}
		if !f.abandoned {
			return f.lib, f.err
		}
	}
}

// lead runs an attempt. The installed lookup, and the load of a build the cache
// answers, run inline: neither touches the network. A fetch runs on its own
// goroutine, under a context of its own that keeps ctx's values, so the
// leading open waits for it like every other open, each on its own context.
func (r *Registry) lead(ctx context.Context, f *flight, request string, mayFetch bool) {
	landed := false
	defer func() {
		if !landed {
			r.abandon(f, request) // a panic: release the waiters, then let it unwind
		}
	}()
	opts := r.fetch.internal()
	req := ocifetch.Request{Spelling: request}
	res, err := lookupHeld(req, opts)
	switch {
	case err != nil:
		// A refused version spelling is the caller's own misuse, refused
		// before anything is attempted, and unlocks nothing (bindings-v1.md
		// section 6, rule 4).
		var spelling *ocifetch.SpellingError
		r.land(f, request, nil, fetchError(err), !errors.As(err, &spelling))
	case res != nil:
		l, err := r.load(request, res)
		r.land(f, request, l, err, true)
	case !mayFetch:
		r.land(f, request, nil, missingError(request, ocifetch.MissingNotes(opts)), true)
	default:
		fetchCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
		r.mu.Lock()
		f.cancel = cancel
		r.mu.Unlock()
		go func() {
			defer cancel()
			res, err := r.ensureHeld(fetchCtx, request)
			if err != nil {
				r.land(f, request, nil, fetchError(err), true)
				return
			}
			l, err := r.load(request, res)
			r.land(f, request, l, err, true)
		}()
	}
	landed = true
}

// lookupHeld is the installed lookup, then this process's hold on the build
// it found (ocifetch.Hold), taken before anything reads the library. A build a
// concurrent prune removed between the two is looked up again, once; removed
// twice, it is a miss.
func lookupHeld(req ocifetch.Request, opts *ocifetch.Options) (*Resolved, error) {
	for range 2 {
		res, err := ocifetch.ResolveInstalled(req, "", opts)
		if err != nil || res == nil {
			return res, err
		}
		if ocifetch.Hold(res.Dir) != ocifetch.HoldVanished {
			return res, nil
		}
	}
	return nil, nil
}

// ensureHeld is the request's one fetch (ensure), then this process's hold on
// the build it installed. A build a concurrent prune removed between the two
// is fetched again, once; removed twice, it is ErrArtifactMissing.
func (r *Registry) ensureHeld(ctx context.Context, request string) (*Resolved, error) {
	for attempt := 0; ; attempt++ {
		res, err := r.ensure(ctx, request)
		if err != nil {
			return nil, err
		}
		if ocifetch.Hold(res.Dir) != ocifetch.HoldVanished {
			return res, nil
		}
		if attempt == 1 {
			return nil, ocifetch.RemovedWhileHeld(request, res.Dir)
		}
	}
}

// ensure joins the fetch in progress for request, or starts it. The fetch
// runs on its own goroutine under a context of its own that keeps ctx's
// values. When ctx is done before the fetch has an answer, ensure returns
// ctx.Err(); the fetch goes on for every other caller waiting on it, and is
// canceled once none does.
func (r *Registry) ensure(ctx context.Context, request string) (*Resolved, error) {
	if err := ctx.Err(); err != nil {
		return nil, err // a caller that has already given up starts no fetch
	}
	r.mu.Lock()
	ff := r.fetches[request]
	if ff == nil {
		fetchCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
		ff = &fetchFlight{done: make(chan struct{}), cancel: cancel}
		r.fetches[request] = ff
		opts := r.fetch.internal()
		go func() {
			defer cancel()
			res, err := ocifetch.Ensure(fetchCtx, ocifetch.Request{Spelling: request}, opts)
			r.mu.Lock()
			if r.fetches[request] == ff {
				delete(r.fetches, request)
			}
			ff.res, ff.err = res, err
			r.mu.Unlock()
			close(ff.done)
		}()
	}
	ff.waiters++
	onFetchWait := r.onFetchWait
	r.mu.Unlock()
	if onFetchWait != nil {
		onFetchWait(request)
	}
	select {
	case <-ff.done:
		return ff.res, ff.err
	default:
	}
	select {
	case <-ff.done:
		return ff.res, ff.err
	case <-ctx.Done():
		r.mu.Lock()
		ff.waiters--
		last := ff.waiters == 0 && r.fetches[request] == ff
		if last {
			delete(r.fetches, request)
		}
		r.mu.Unlock()
		if last {
			ff.cancel()
		}
		return nil, ctx.Err()
	}
}

// Fetch resolves, fetches, verifies and installs the build request names
// into the cache, as `chtypes fetch` does, and opens nothing: no library is
// loaded and the process setup is untouched, so it needs no Setup (public
// issue #492). It honors the registry's fetch options (offline, the cache,
// and under the production channel the bases, the trust and the lock), and
// it shares one fetch with a concurrent open or Fetch of the same request.
// The Resolved names the installed version, build, digests and library path.
// The build is held by this process until it exits, so no `chtypes prune`
// removes it meanwhile. When ctx is done before the fetch has an answer,
// Fetch returns ctx.Err(), and the fetch goes on for any other caller waiting
// on it. Errors are the fetch codes, as an *ArtifactError.
func (r *Registry) Fetch(ctx context.Context, request string) (Resolved, error) {
	res, err := r.ensureHeld(ctx, request)
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil && errors.Is(err, ctxErr) {
			return Resolved{}, err
		}
		return Resolved{}, fetchError(err)
	}
	return *res, nil
}

// load opens the image a resolved build names (loader steps 1 to 7) and makes
// the load-time assertion against the request.
func (r *Registry) load(request string, res *Resolved) (*Library, error) {
	l, err := openImage(imageKey("verified", res.LibraryPath), func(zone, defaults []byte) (*abi2.Table, error) {
		return abi2.Load(loadInput(res, zone, defaults))
	}, res.LibraryPath, res)
	if err != nil {
		return nil, err
	}
	if err := checkWithinRequest(l, request, res.Platform); err != nil {
		return nil, err
	}
	return l, nil
}

// land ends an attempt and releases every open waiting on it. A failed
// attempt unlocks the setup record it began under while no image has completed
// load step 7, whatever failed: the resolve, the fetch, the signature or any
// load step (settles is false only for the caller's own misuse); it is never
// remembered. A Library is remembered for the request, unless every open gave
// up on the attempt first (leave), which has made it no longer the request's.
func (r *Registry) land(f *flight, request string, l *Library, err error, settles bool) {
	if err != nil && settles {
		failedOpen(f.gen)
	}
	r.mu.Lock()
	if r.flights[request] == f {
		delete(r.flights, request)
		if err == nil {
			r.memo[request] = l
			r.libs = append(r.libs, l)
		}
	}
	f.lib, f.err = l, err
	r.mu.Unlock()
	close(f.done)
}

// leave is an open giving up on its own context. The attempt goes on for the
// other opens waiting on it. When the last one leaves, its fetch is canceled
// and the attempt abandoned: it is no longer the request's, so the next open
// starts a new one, and as a failed attempt it unlocks the setup record now.
func (r *Registry) leave(f *flight, request string) {
	r.mu.Lock()
	f.waiters--
	last := f.waiters == 0 && r.flights[request] == f
	if last {
		delete(r.flights, request)
	}
	cancel := f.cancel
	r.mu.Unlock()
	if last {
		if cancel != nil {
			cancel()
		}
		failedOpen(f.gen)
	}
}

// abandon releases the opens waiting on an attempt that panicked: each starts
// over, and the panic unwinds the leading open alone.
func (r *Registry) abandon(f *flight, request string) {
	r.mu.Lock()
	if r.flights[request] == f {
		delete(r.flights, request)
	}
	r.mu.Unlock()
	f.abandoned = true
	close(f.done)
}

// loadInput is the adapter between the fetch layer's record and the loader's
// input. It passes the predicate VERBATIM: never re-encoded, because a
// re-encoding would be a second derivation of the statement the signature
// covered.
func loadInput(res *Resolved, zone, defaults []byte) abi2.LoadInput {
	return abi2.LoadInput{
		LibraryPath: res.LibraryPath,
		Predicate:   res.Predicate,
		Platform:    res.Platform,
		Timezone:    zone,
		Defaults:    defaults,
	}
}

// checkWithinRequest is the load-time assertion (fetch-v1.md section 9; public
// issue #481): the library just opened for request must report, in its own
// build_info, a clickhouse_version within that request — equal to an exact
// request, within a line one. Otherwise the open fails as
// CHTYPES_ARTIFACT_CORRUPT with reason build_info_mismatch:clickhouse_version,
// the code fetch-v1.md section 4 gives a signed version outside the request,
// whatever the cache answered. The image stays loaded for the requests it
// does answer.
func checkWithinRequest(l *Library, request, platform string) error {
	if ocifetch.SatisfiesRequest(request, l.Version) {
		return nil
	}
	return &ArtifactError{
		Code:   CodeArtifactCorrupt,
		Reason: "build_info_mismatch:clickhouse_version", Path: l.Path, Want: request, Got: l.Version,
		msg: fmt.Sprintf("chtypes: %s refused: build_info_mismatch:clickhouse_version (want a build within %q, got %q): "+
			"the library opened for ClickHouse %s (%s) reports another version [%s]",
			l.Path, request, l.Version, request, platform, CodeArtifactCorrupt),
	}
}

// missingError is the miss of an open with autofetch off. notes are the fetch
// layer's own sentences about the cache (the 0.x hint), the same ones its
// offline fetch adds.
func missingError(request string, notes []string) error {
	platform := ""
	for _, p := range ocifetch.Platforms {
		if p.OS == runtime.GOOS && p.Architecture == runtime.GOARCH {
			platform = p.Key
		}
	}
	msg := ocifetch.WithNotes(fmt.Sprintf("no installed artifact for ClickHouse %s (%s), and autofetch is off. "+
		"Fetch it first, or enable autofetch with WithAutoFetch or %s=1", request, platform, ocifetch.EnvAutofetchName), notes)
	return &ArtifactError{Code: CodeArtifactMissing, msg: fmt.Sprintf("chtypes: %s [%s]", msg, CodeArtifactMissing)}
}

// Installed lists what the fetch layer holds: never the network.
func (r *Registry) Installed() ([]Resolved, error) {
	out, err := ocifetch.ListInstalled(r.fetch.internal())
	if err != nil {
		return nil, fetchError(err)
	}
	return out, nil
}

// Libraries lists the libraries this registry has opened, in opening order.
func (r *Registry) Libraries() []*Library {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]*Library(nil), r.libs...)
}
