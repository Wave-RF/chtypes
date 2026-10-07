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
	"time"

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
	Bases           []string // CHTYPES_ARTIFACTS_URL
	CacheDir        string   // CHTYPES_CACHE
	SystemDirs      []string
	TrustedKeys     []string // raw hex ed25519 public keys; CHTYPES_TRUSTED_KEYS
	Token           string   // CHTYPES_DOWNLOAD_TOKEN
	AllowUnsigned   bool     // also set by CHTYPES_ALLOW_UNSIGNED=1
	Offline         bool     // also set by CHTYPES_OFFLINE=1 (public issue #528); the cache only, no request
	Frozen          bool
	LockPath        string
	LockWrite       bool
	Update          bool
	ConnectTimeout  time.Duration
	IdleReadTimeout time.Duration
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
		ConnectTimeout: o.ConnectTimeout, IdleReadTimeout: o.IdleReadTimeout,
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
type Registry struct {
	fetch     FetchOptions
	autofetch bool

	mu   sync.Mutex
	memo map[string]*Library
	libs []*Library
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
	r := &Registry{fetch: c.fetch, memo: map[string]*Library{}}
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

// ForContext is For with a context for the fetch, when autofetch is on.
func (r *Registry) ForContext(ctx context.Context, request string) (*Library, error) {
	return r.open(ctx, request, r.autofetch)
}

func (r *Registry) open(ctx context.Context, request string, mayFetch bool) (_ *Library, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if l := r.memo[request]; l != nil {
		return l, nil
	}
	// An open that attempted a load and failed unlocks the setup record while
	// no image has completed load step 7, whatever failed: the resolve, the
	// fetch, the signature or any load step. A refused version spelling is the
	// caller's own misuse, refused before anything is attempted, and unlocks
	// nothing (bindings-v1.md section 6, rule 4).
	gen := setupGeneration()
	misuse := false
	defer func() {
		if err != nil && !misuse {
			failedOpen(gen)
		}
	}()
	opts := r.fetch.internal()
	req := ocifetch.Request{Spelling: request}
	res, err := ocifetch.ResolveInstalled(req, "", opts)
	if err != nil {
		var spelling *ocifetch.SpellingError
		misuse = errors.As(err, &spelling)
		return nil, fetchError(err)
	}
	if res == nil {
		if !mayFetch {
			return nil, missingError(request, ocifetch.MissingNotes(opts))
		}
		if res, err = ocifetch.Ensure(ctx, req, opts); err != nil {
			return nil, fetchError(err)
		}
	}
	l, err := openImage(imageKey("verified", res.LibraryPath), func(zone, defaults []byte) (*abi2.Table, error) {
		return abi2.Load(loadInput(res, zone, defaults))
	}, res.LibraryPath, res)
	if err != nil {
		return nil, err
	}
	if err := checkWithinRequest(l, request, res.Platform); err != nil {
		return nil, err
	}
	r.memo[request] = l
	r.libs = append(r.libs, l)
	return l, nil
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
		Code: CodeArtifactCorrupt, Request: request, Platform: platform,
		Reason: "build_info_mismatch:clickhouse_version", Path: l.Path, Want: request, Got: l.Version,
		Msg: fmt.Sprintf("chtypes: %s refused: build_info_mismatch:clickhouse_version (want a build within %q, got %q): "+
			"the library opened for ClickHouse %s reports another version [%s]",
			l.Path, request, l.Version, request, CodeArtifactCorrupt),
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
	return &ArtifactError{
		Code: CodeArtifactMissing, Request: request, Platform: platform,
		Msg: fmt.Sprintf("chtypes: %s [%s]", msg, CodeArtifactMissing),
	}
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
