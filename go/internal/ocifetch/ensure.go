package ocifetch

// ensure.go — the seam (docs/guides/fetch-v1.md §9, the v1 fetch-layer
// plan's §1.3): the package's four entry points (Ensure, ResolveInstalled,
// ListInstalled, VerifyInstalled, FetchSigned) and the Request/Options/
// Resolved types every caller uses. Everything else in this package is
// implementation detail reached only from here.

import (
	"context"
	"crypto/ed25519"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Request is what a caller wants resolved: a version spelling (one to four
// dot-separated components) and a platform key. An empty Platform defaults
// to the running GOOS/GOARCH.
type Request struct {
	Spelling string
	Platform string
}

// Options configures one call. Every field's zero value falls back to the
// matching environment variable, then the constants_gen.go default — the
// same precedence v0 uses (docs/guides/fetch-v1.md §2, §4).
type Options struct {
	Bases                 []string // CHTYPES_ARTIFACTS_URL, comma-separated, else DefaultBases
	CacheDir              string   // CHTYPES_CACHE, else CacheRootTemplate expanded
	SystemDirs            []string // defaults to SystemCacheDirs
	TrustedKeys           []string // raw hex ed25519 public keys; CHTYPES_TRUSTED_KEYS, else ReleaseKeys
	Token                 string   // CHTYPES_DOWNLOAD_TOKEN
	AllowUnsigned         bool     // also set by CHTYPES_ALLOW_UNSIGNED=1
	Offline               bool
	Frozen                bool
	LockPath              string // defaults to LockDefaultFile
	LockWrite             bool
	Update                bool
	StrictCache           *bool               // nil means CHTYPES_CACHE_STRICT ("1" is on); see probeRoots
	Clock                 *Clock              // nil means DefaultClock()
	ConnectTimeout        time.Duration       // 0 means ConnectTimeoutSeconds
	IdleReadTimeout       time.Duration       // 0 means IdleReadTimeoutSeconds
	HookBeforeIndexRename func()              // test-only: simulates a concurrent cache writer
	OnRequest             func(*http.Request) // test-only: observes every outgoing request
}

// Resolved is what every successful resolution returns (§1.3).
type Resolved struct {
	ABIGeneration    int
	Platform         string
	Request          string
	Version          string
	Channel          string
	Build            string
	LibraryPath      string
	Dir              string
	Digests          Digests
	Predicate        map[string]any
	SignedBy         string
	Source           string
	AlreadyInstalled bool
	Warnings         []string
}

// SignedArtifact is fetch_signed's result: the verified content bytes, the
// predicate verbatim, and its digests.
type SignedArtifact struct {
	Bytes     []byte
	Statement map[string]any
	Digests   Digests
	SignedBy  string
	Warnings  []string
}

// VerifyResult is one installed entry's re-verification outcome.
type VerifyResult struct {
	Platform string
	Version  string
	Dir      string
	OK       bool
	Detail   string
}

type resolvedOptions struct {
	bases                 []string
	cacheDir              string
	systemDirs            []string
	trustedKeys           []ed25519.PublicKey
	token                 string
	tokenHosts            []string
	allowUnsigned         bool
	offline               bool
	frozen                bool
	lockPath              string
	lockWrite             bool
	update                bool
	strict                bool
	clock                 Clock
	connectTimeout        time.Duration
	idleReadTimeout       time.Duration
	hookBeforeIndexRename func()
	onRequest             func(*http.Request)
}

func resolveOptions(o *Options) (resolvedOptions, error) {
	if o == nil {
		o = &Options{}
	}
	var ro resolvedOptions

	ro.bases = o.Bases
	if len(ro.bases) == 0 {
		if env := strings.TrimSpace(os.Getenv(EnvBasesName)); env != "" {
			for _, b := range strings.Split(env, BaseSeparator) {
				if b = strings.TrimSpace(b); b != "" {
					ro.bases = append(ro.bases, b)
				}
			}
		} else {
			ro.bases = append([]string(nil), DefaultBases...)
		}
	}
	for _, b := range ro.bases {
		if _, err := parseBaseURL(b); err != nil {
			return ro, fmt.Errorf("chtypes: %w", err)
		}
	}

	ro.cacheDir = o.CacheDir
	if ro.cacheDir == "" {
		if env := os.Getenv(EnvCacheName); env != "" {
			ro.cacheDir = env
		} else {
			dir, err := expandCacheRoot()
			if err != nil {
				return ro, err
			}
			ro.cacheDir = dir
		}
	}

	ro.systemDirs = o.SystemDirs
	if ro.systemDirs == nil {
		ro.systemDirs = append([]string(nil), SystemCacheDirs...)
	}

	hexKeys := o.TrustedKeys
	if len(hexKeys) == 0 {
		if env := strings.TrimSpace(os.Getenv(EnvTrustedKeysName)); env != "" {
			for _, k := range strings.Split(env, ",") {
				if k = strings.TrimSpace(k); k != "" {
					hexKeys = append(hexKeys, k)
				}
			}
		}
	}
	if len(hexKeys) > 0 {
		keys, err := parseHexKeys(hexKeys)
		if err != nil {
			return ro, err
		}
		ro.trustedKeys = keys
	} else {
		for _, rk := range ReleaseKeys {
			pk, err := hexToPublicKey(rk.Ed25519Hex)
			if err != nil {
				return ro, err
			}
			ro.trustedKeys = append(ro.trustedKeys, pk)
		}
	}

	ro.token = o.Token
	if ro.token == "" {
		ro.token = os.Getenv(EnvTokenName)
	}
	if ro.token != "" {
		ro.tokenHosts = hostsOf(ro.bases)
	}

	ro.allowUnsigned = o.AllowUnsigned || os.Getenv(EnvAllowUnsignedName) == "1"
	ro.offline = o.Offline
	ro.frozen = o.Frozen
	ro.lockWrite = o.LockWrite
	ro.update = o.Update
	if o.StrictCache != nil {
		ro.strict = *o.StrictCache
	} else {
		ro.strict = os.Getenv(EnvCacheStrictName) == "1"
	}
	ro.lockPath = o.LockPath
	if ro.lockPath == "" {
		ro.lockPath = LockDefaultFile
	}

	if o.Clock != nil {
		ro.clock = *o.Clock
	} else {
		ro.clock = DefaultClock()
	}
	ro.connectTimeout = time.Duration(ConnectTimeoutSeconds * float64(time.Second))
	ro.idleReadTimeout = time.Duration(IdleReadTimeoutSeconds * float64(time.Second))
	if o.ConnectTimeout > 0 {
		ro.connectTimeout = o.ConnectTimeout
	}
	if o.IdleReadTimeout > 0 {
		ro.idleReadTimeout = o.IdleReadTimeout
	}
	ro.hookBeforeIndexRename = o.HookBeforeIndexRename
	ro.onRequest = o.OnRequest
	return ro, nil
}

func hostsOf(bases []string) []string {
	var hosts []string
	for _, b := range bases {
		u, err := parseBaseURL(b)
		if err == nil && u.authority != "" {
			hosts = append(hosts, u.authority)
		}
	}
	return hosts
}

func expandCacheRoot() (string, error) {
	base := os.Getenv("XDG_CACHE_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", fmt.Errorf("chtypes: resolving the default cache directory: %w", err)
		}
		base = filepath.Join(home, ".cache")
	}
	return filepath.Join(base, "chtypes", "v1"), nil
}

func hexToPublicKey(h string) (ed25519.PublicKey, error) {
	b, err := hex.DecodeString(h)
	if err != nil {
		return nil, fmt.Errorf("chtypes: %q is not hex: %w", h, err)
	}
	if len(b) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("chtypes: %q is %d bytes, an ed25519 public key is %d", h, len(b), ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(b), nil
}

func parseHexKeys(hexes []string) ([]ed25519.PublicKey, error) {
	out := make([]ed25519.PublicKey, 0, len(hexes))
	for _, h := range hexes {
		pk, err := hexToPublicKey(h)
		if err != nil {
			return nil, err
		}
		out = append(out, pk)
	}
	return out, nil
}

// hostPlatformKey maps runtime.GOOS/GOARCH onto a Platforms[] key.
func hostPlatformKey() (string, bool) {
	for _, p := range Platforms {
		if p.OS == runtime.GOOS && p.Architecture == runtime.GOARCH {
			return p.Key, true
		}
	}
	return "", false
}

// session is one call's shared state: the HTTP client and clock every
// network-touching helper in this package uses.
type session struct {
	client *client
	// hookBeforeIndexRename is test-only: it lets a conformance case
	// (index-race-reapply) simulate a concurrent cache writer completing
	// between this call's index.json read and its rename.
	hookBeforeIndexRename func()
}

func newSession(ro resolvedOptions) *session {
	return &session{
		client:                newClient(ro.clock, ro.connectTimeout, ro.idleReadTimeout, ro.token, ro.tokenHosts, ro.onRequest),
		hookBeforeIndexRename: ro.hookBeforeIndexRename,
	}
}

// Ensure resolves req against the registry (unless offline), verifies its
// signature, unpacks and installs it, and returns a Resolved (§1.3). It may
// use the network unless opts.Offline.
func Ensure(ctx context.Context, req Request, opts *Options) (*Resolved, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	res, err := ensure(ctx, ro, req)
	return res, cacheIOError(err, ro.cacheDir)
}

func ensure(ctx context.Context, ro resolvedOptions, req Request) (*Resolved, error) {
	platformKey := req.Platform
	if platformKey == "" {
		var ok bool
		platformKey, ok = hostPlatformKey()
		if !ok {
			return nil, fmt.Errorf("chtypes: this host's platform is not one of v1's: %v", Platforms)
		}
	}
	platform, ok := platformByKey(platformKey)
	if !ok {
		return nil, fmt.Errorf("chtypes: %q is not a known v1 platform", platformKey)
	}
	if err := validateSpelling(req.Spelling); err != nil {
		return nil, err
	}

	if ro.offline {
		return resolveOffline(ro, req, platform)
	}
	if ro.strict {
		// Strict: the cache is checked before anything is fetched into it.
		if _, err := probeRoots(ro); err != nil {
			return nil, err
		}
	}

	s := newSession(ro)
	l := newLayout(ro.cacheDir, false)
	if err := l.ensureSkeleton(); err != nil {
		return nil, err
	}

	if ro.frozen {
		return s.ensureFrozen(ctx, ro, l, req, platform)
	}
	resolved, err := s.ensureOnline(ctx, ro, l, req, platform)
	if err != nil {
		return nil, err
	}
	if ro.update {
		// `update` re-resolves every locked request and rewrites the lock
		// from scratch — "it never merges a stale entry with a fresh one"
		// (§6) — in addition to resolving the current request above.
		if err := s.updateLock(ctx, ro); err != nil {
			return nil, err
		}
	}
	return resolved, nil
}

// resolveOffline implements the offline branch of Ensure: --offline reads
// the cache only (§6), consulting unpacked/*/verified.json records first and
// falling back to verifying-and-unpacking a pre-seeded index.json entry that
// has none yet.
func resolveOffline(ro resolvedOptions, req Request, platform Platform) (*Resolved, error) {
	res, err := resolveInstalledInternal(ro, req, platform.Key)
	if err != nil {
		return nil, err
	}
	if res != nil {
		return res, nil
	}
	return nil, newError(CodeArtifactMissing, req.Spelling, platform.Key, "", nil,
		"%s", WithNotes(fmt.Sprintf("no installed artifact for %s/%s, and --offline forbids a network fetch", req.Spelling, platform.Key), missingNotes(ro)))
}

// MissingNotes is what a CHTYPES_ARTIFACT_MISSING answer from the cache opts
// names adds to its message, each a complete sentence: the default mode's
// warning for every unusable root or unreadable entry, then the 0.x hint when
// the cache is a 0.x registry directory (docs/guides/fetch-v1.md §1 and
// "Upgrading from 0.x"; public issue #486). The registry's own MISSING adds
// the same notes as the offline fetch's, and the CLI's list and verify print
// them.
func MissingNotes(opts *Options) []string {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil
	}
	return missingNotes(ro)
}

func missingNotes(ro resolvedOptions) []string {
	ro.strict = false
	notes, _ := probeRoots(ro)
	if hint := zeroXHint(ro.cacheDir); hint != "" {
		notes = append(notes, hint)
	}
	return notes
}

// WithNotes appends notes, each a complete sentence, to a message.
func WithNotes(msg string, notes []string) string {
	if len(notes) == 0 {
		return msg
	}
	return msg + ". " + strings.Join(notes, " ")
}

// ensureOnline is Ensure's normal (non-frozen, non-offline) path: resolve
// the index, select the platform manifest, verify trust, unpack, install.
func (s *session) ensureOnline(ctx context.Context, ro resolvedOptions, l *layout, req Request, platform Platform) (*Resolved, error) {
	resolved, idx, err := s.resolveAndInstall(ctx, ro, l, req, platform)
	if err != nil {
		return nil, err
	}
	// The lock is written on every successful resolution, including one that
	// found the build already installed: `fetch --lock` records what the
	// registry resolves the request to, not whether this call downloaded it.
	if ro.lockWrite {
		if err := s.writeLockForRequest(ctx, ro, l, idx, req, platform, resolved); err != nil {
			return nil, err
		}
	}
	return resolved, nil
}

func (s *session) resolveAndInstall(ctx context.Context, ro resolvedOptions, l *layout, req Request, platform Platform) (*Resolved, *ImageIndex, error) {
	idx, indexDigest, base, err := s.resolveIndex(ctx, ro.bases, req.Spelling)
	if err != nil {
		return nil, nil, err
	}
	desc, err := selectPlatformDescriptor(idx, platform.Key)
	if err != nil {
		return nil, nil, err
	}
	manifest, manifestBody, base2, err := s.fetchManifestByDigest(ctx, ro.bases, *desc)
	if err != nil {
		return nil, nil, err
	}
	if base2 != "" {
		base = base2
	}
	manifestDigest := digestOf(manifestBody)
	layerDesc := manifest.Layers[0]

	// Already installed: a fetch landed on this exact manifest digest
	// before. Content at a given digest never changes, so nothing further
	// needs checking — this also satisfies "zero layer requests" for the
	// existing-install-noop case, since nothing past this point runs. A
	// NEWER build within the request, installed beside it, is still the
	// answer (the monotonic check below, §9).
	if rec, dir, ok := readInstalledRecord(l, manifestDigest); ok {
		if err := l.writeBlob(desc.Digest, manifestBody); err != nil {
			return nil, nil, err
		}
		if err := l.addIndexEntry(*desc, s.hookBeforeIndexRename); err != nil {
			return nil, nil, err
		}
		if existing, found := newerAlreadyInstalled(l, platform.Key, req.Spelling, manifestDigest, rec.Version, rec.Build); found {
			warnings := []string{monotonicWarning(existing, rec.Version, rec.Build)}
			return recordToResolved(&existing.rec, existing.dir, platform.Key, req.Spelling, indexDigest, true, base, warnings), idx, nil
		}
		return recordToResolved(rec, dir, platform.Key, req.Spelling, indexDigest, true, base, nil), idx, nil
	}

	stmt, warnings, err := s.verifyManifestTrust(ctx, ro.bases, manifestDigest, layerDesc.Digest, platform, req.Spelling, ro.trustedKeys, ro.allowUnsigned)
	if err != nil {
		return nil, nil, err
	}

	// Monotonicity (§6, "monotonic-warning"): "a HIGHER build is already
	// installed than what the live registry now offers; the existing
	// (newer) install is kept, with a warning." Only an install WITHIN the
	// request counts (§9; public issue #481). Checked as soon as the
	// candidate's own (signed) version/build are known, and before the
	// layer is ever fetched — there is no point downloading and unpacking
	// bytes this call is about to discard. Only covers the signed path:
	// without a verified predicate there is no version/build to compare
	// without an extra, otherwise-unneeded config fetch, so an
	// allow-unsigned candidate falls through to the normal install below.
	if stmt != nil {
		candidateVersion := stringPredicate(stmt.Statement.Predicate, "clickhouse_version")
		candidateBuild := stringPredicate(stmt.Statement.Predicate, "build")
		if existing, found := newerAlreadyInstalled(l, platform.Key, req.Spelling, manifestDigest, candidateVersion, candidateBuild); found {
			warnings = append(warnings, monotonicWarning(existing, candidateVersion, candidateBuild))
			// The candidate manifest is discarded, never installed, so no
			// blob and no index entry is written for it — only the
			// already-installed (newer) entry's own index entry, which is
			// already there, stays.
			return recordToResolved(&existing.rec, existing.dir, platform.Key, req.Spelling, indexDigest, true, base, warnings), idx, nil
		}
	}

	layerResult, layerBase, err := s.fetchAcrossBases(ctx, ro.bases, "blobs/"+string(layerDesc.Digest), notFoundRetryOnLast, requestOptions{})
	if err != nil {
		return nil, nil, err
	}
	base = layerBase
	if verr := verifyDescriptor(layerResult.body, layerDesc); verr != nil {
		return nil, nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, layerResult.url, verr, "layer: %v", verr)
	}

	libraryRelPath, unsignedConfig, lerr := s.libraryPathFor(ctx, ro.bases, manifest, stmt)
	if lerr != nil {
		return nil, nil, lerr
	}
	resolved, err := s.installManifest(l, req, platform, manifestDigest, *desc, manifestBody, layerDesc, layerResult.body, stmt, libraryRelPath, unsignedConfig, indexDigest, base, warnings)
	if err != nil {
		return nil, nil, err
	}

	return resolved, idx, nil
}

// installManifest unpacks layerBody and installs it under l, returning the
// Resolved value (§1.3's guarantees).
func (s *session) installManifest(l *layout, req Request, platform Platform, manifestDigest Digest, desc Descriptor, manifestBody []byte, layerDesc Descriptor, layerBody []byte, stmt *verifiedStatement, libraryRelPath string, unsignedConfig map[string]any, indexDigest Digest, base string, warnings []string) (*Resolved, error) {
	if err := l.writeBlob(desc.Digest, manifestBody); err != nil {
		return nil, err
	}
	if err := l.writeBlob(layerDesc.Digest, layerBody); err != nil {
		return nil, err
	}
	if err := l.addIndexEntry(desc, s.hookBeforeIndexRename); err != nil {
		return nil, err
	}

	unpackParent := filepath.Join(l.dir, UnpackedDirName())
	if err := os.MkdirAll(unpackParent, dirMode); err != nil {
		return nil, err
	}
	up, err := unpackLibrary(layerBody, unpackParent, libraryRelPath)
	if err != nil {
		return nil, err
	}

	var predicate map[string]any
	var signedBy string
	var version, channel, build string
	switch {
	case stmt != nil:
		predicate = stmt.Statement.Predicate
		signedBy = stmt.KeyID
	case unsignedConfig != nil:
		// CHTYPES_ALLOW_UNSIGNED: no predicate was ever verified, so the
		// only source of these fields is the manifest's own config blob
		// (layout-v2 spec §4.1, value-equal to the predicate by
		// definition — there is nothing to cross-check it against here).
		predicate = unsignedConfig
	}
	if predicate != nil {
		version, _ = predicate["clickhouse_version"].(string)
		channel, _ = predicate["channel"].(string)
		build, _ = predicate["build"].(string)
	}
	if got, want := up.librarySHA256, stringPredicate(predicate, "library_sha256"); want != "" && got != want {
		_ = os.RemoveAll(up.dir)
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, base, nil,
			"installed library sha256 %s does not match the signed predicate's %s", got, want)
	}
	if want := intPredicate(predicate, "library_bytes"); want != 0 && up.librarySize != want {
		_ = os.RemoveAll(up.dir)
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, base, nil,
			"installed library size %d does not match the signed predicate's %d", up.librarySize, want)
	}

	rec := verifiedRecord{
		Schema:        1,
		Platform:      platform.Key,
		Version:       version,
		Channel:       channel,
		Build:         build,
		LibraryPath:   up.libraryPath,
		LibrarySHA256: up.librarySHA256,
		LibraryBytes:  up.librarySize,
		SignedBy:      signedBy,
		Predicate:     predicate,
	}
	var bundleDigest, bundleManifestDigest Digest
	if stmt != nil {
		bundleDigest = stmt.BundleDigest
		bundleManifestDigest = stmt.BundleManifestDigest
	}
	rec.Digests = Digests{Index: indexDigest, Manifest: desc.Digest, Layer: layerDesc.Digest, Bundle: bundleDigest, BundleManifest: bundleManifestDigest}

	dir, already, err := l.writeVerifiedRecord(manifestDigest, up.dir, rec)
	if err != nil {
		return nil, err
	}
	res := recordToResolved(&rec, dir, platform.Key, req.Spelling, indexDigest, already, base, warnings)
	return res, nil
}

func recordToResolved(rec *verifiedRecord, dir, platform, request string, indexDigest Digest, already bool, source string, warnings []string) *Resolved {
	digests := rec.Digests
	if indexDigest != "" {
		digests.Index = indexDigest
	}
	return &Resolved{
		ABIGeneration:    ABIGeneration,
		Platform:         platform,
		Request:          request,
		Version:          rec.Version,
		Channel:          rec.Channel,
		Build:            rec.Build,
		LibraryPath:      filepath.Join(dir, filepath.FromSlash(rec.LibraryPath)),
		Dir:              dir,
		Digests:          digests,
		Predicate:        rec.Predicate,
		SignedBy:         rec.SignedBy,
		Source:           source,
		AlreadyInstalled: already,
		Warnings:         warnings,
	}
}

func stringPredicate(p map[string]any, key string) string {
	v, _ := p[key].(string)
	return v
}

func intPredicate(p map[string]any, key string) int64 {
	v, ok := p[key].(float64)
	if !ok {
		return 0
	}
	return int64(v)
}

func predicateLibraryPath(stmt *verifiedStatement) (string, bool) {
	if stmt == nil {
		return "", false
	}
	v, ok := stmt.Statement.Predicate["library"].(string)
	return v, ok
}

// libraryPathFor finds the library's path within the tarball: the signed
// predicate's own "library" field when there is one, or — under
// CHTYPES_ALLOW_UNSIGNED, where no predicate was ever verified — the same
// field read from the manifest's own config blob instead (layout-v2 spec
// §4.1: "the tarball's own manifest.json, which is also the OCI config
// blob, carries the same fields, value-equal"). The fetch layer otherwise
// never fetches the config blob at all (§3: "without yet trusting
// either") — this is the one path that needs it, because there is no
// other source of truth once a fetch proceeds unsigned.
// libraryPathFor returns the library's path within the tarball, and — only
// when there was no signed predicate to begin with (unsignedConfig is nil
// otherwise) — the config blob's own parsed fields, so the caller can still
// report version/channel/build for an artifact that was never verified.
func (s *session) libraryPathFor(ctx context.Context, bases []string, manifest *ImageManifest, stmt *verifiedStatement) (path string, unsignedConfig map[string]any, err error) {
	if p, ok := predicateLibraryPath(stmt); ok {
		return p, nil, nil
	}
	result, _, err := s.fetchAcrossBases(ctx, bases, "blobs/"+string(manifest.Config.Digest), notFoundRetryOnLast, requestOptions{})
	if err != nil {
		return "", nil, err
	}
	if verr := verifyDescriptor(result.body, manifest.Config); verr != nil {
		return "", nil, newError(CodeArtifactCorrupt, "", "", result.url, verr, "config blob: %v", verr)
	}
	var cfg map[string]any
	if uerr := strictUnmarshal(result.body, &cfg); uerr != nil {
		return "", nil, newError(CodeArtifactCorrupt, "", "", result.url, uerr, "config blob is not valid JSON: %v", uerr)
	}
	p, _ := cfg["library"].(string)
	if p == "" {
		return "", nil, newError(CodeArtifactCorrupt, "", "", result.url, nil, "config blob names no library")
	}
	return p, cfg, nil
}

// newerAlreadyInstalled returns the newest installed entry in l, other than
// excludeManifest, for platformKey that lies WITHIN request and whose
// (version, build) is strictly newer than (version, build) — the
// monotonic-warning check (§6, §9): "a HIGHER build is already installed than
// what the live registry now offers; the existing (newer) install is kept,
// with a warning." The returned entry is the existing (newer) install itself,
// so the caller can return it in place of whatever the registry just
// resolved.
//
// The check is scoped to the request (public issue #481): an install of
// another line never answers a line request, and an exact request is
// answered only by that exact version, so for it only a newer BUILD of the
// same version counts. A request that is not a version spelling (an
// arbitrary tag) names no range, so nothing is kept for it.
func newerAlreadyInstalled(l *layout, platformKey, request string, excludeManifest Digest, version, build string) (*installedEntry, bool) {
	if !spellingRegex.MatchString(request) {
		return nil, false
	}
	entries, err := listUnpacked(l.dir)
	if err != nil {
		return nil, false
	}
	var best *installedEntry
	for i, e := range entries {
		if e.rec.Platform != platformKey || e.rec.Digests.Manifest == excludeManifest {
			continue
		}
		if !versionWithin(request, e.rec.Version) {
			continue
		}
		if !newerThan(e.rec.Version, e.rec.Build, version, build) {
			continue
		}
		if best == nil || newerThan(e.rec.Version, e.rec.Build, best.rec.Version, best.rec.Build) {
			best = &entries[i]
		}
	}
	return best, best != nil
}

// monotonicWarning is the warning a kept newer install carries.
func monotonicWarning(existing *installedEntry, offeredVersion, offeredBuild string) string {
	return fmt.Sprintf(
		"chtypes: a newer build is already installed locally (%s build %s, monotonic check) than the registry just resolved (%s build %s) — keeping the existing install",
		existing.rec.Version, existing.rec.Build, offeredVersion, offeredBuild)
}

// newerThan reports whether (version, build) a is strictly newer than b:
// a higher version, or the same version with a higher build (builds compare
// as the fixed-width strings they are).
func newerThan(aVersion, aBuild, bVersion, bBuild string) bool {
	if aVersion == bVersion {
		return aBuild > bBuild
	}
	return versionLess(bVersion, aVersion)
}

// SatisfiesRequest reports whether a library whose own clickhouse_version is
// version answers request: equal to an exact (four-part) request, or within
// a floating one. A request that is not a version spelling (an arbitrary tag)
// names no version, so it constrains nothing and this returns true. The
// loader's caller uses it to refuse a library outside its request, whatever
// the cache did (public issue #481).
func SatisfiesRequest(request, version string) bool {
	if !spellingRegex.MatchString(request) {
		return true
	}
	return versionWithin(request, version)
}

// readInstalledRecord reads the verified.json record for manifestDigest from
// l, if present.
func readInstalledRecord(l *layout, manifestDigest Digest) (*verifiedRecord, string, bool) {
	dir := l.unpackedDir(manifestDigest)
	rec, err := readVerifiedRecord(dir)
	if err != nil {
		return nil, "", false
	}
	return rec, dir, true
}

// rootSource is Resolved.Source for a record read from the i-th search root:
// the user cache first, then each read-only system directory.
func rootSource(i int, dir string) string {
	if i == 0 {
		return "cache"
	}
	return "system:" + dir
}

// resolveInstalledInternal implements both ResolveInstalled and the offline
// branch of Ensure, never touching the network. It reads the records of the
// user cache, then of each read-only system directory in order, and answers
// with the newest (version, build) among every record that satisfies the
// request; a tie goes to the earlier root (docs/guides/fetch-v1.md §1, one
// root order; public issue #486). Only when no root holds such a record does
// it install a pre-seeded index.json entry from local blobs, trying the roots
// in the same order. Nothing is created unless such an install begins.
func resolveInstalledInternal(ro resolvedOptions, req Request, platformKey string) (*Resolved, error) {
	warnings, err := probeRoots(ro)
	if err != nil {
		return nil, err
	}
	cacheLayout := newLayout(ro.cacheDir, false)
	dirs := append([]string{ro.cacheDir}, ro.systemDirs...)
	var best *installedEntry
	var bestSource string
	for i, dir := range dirs {
		entries, err := listUnpacked(dir)
		if err != nil {
			continue
		}
		cand, ok := newestMatching(entries, platformKey, req.Spelling)
		if !ok {
			continue
		}
		if best == nil || newerThan(cand.rec.Version, cand.rec.Build, best.rec.Version, best.rec.Build) {
			best, bestSource = cand, rootSource(i, dir)
		}
	}
	if best != nil {
		return recordToResolved(&best.rec, best.dir, platformKey, req.Spelling, "", true, bestSource, warnings), nil
	}
	for i, dir := range dirs {
		// A pre-seeded index.json entry with no verified.json yet: verify
		// and unpack it now, entirely from local blobs (§1). A read-only
		// system directory is only ever read here — the result lands in
		// the user's own (writable) cache, never back into the system dir.
		readOnly := i > 0
		l := newLayout(dir, readOnly)
		dst := l
		source := "cache (pre-seeded)"
		if readOnly {
			dst = cacheLayout
			source = "system:" + dir + " (pre-seeded, installed into the cache)"
		}
		if res, err := verifyPreseededEntry(l, dst, req, platformKey, ro.trustedKeys, source); err == nil && res != nil {
			res.Warnings = append(warnings, res.Warnings...)
			return res, nil
		}
	}
	return nil, nil
}

// ResolveInstalled implements the seam's resolve_installed: never touches
// the network.
func ResolveInstalled(req Request, platform string, opts *Options) (*Resolved, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	if platform == "" {
		var ok bool
		platform, ok = hostPlatformKey()
		if !ok {
			return nil, fmt.Errorf("chtypes: this host's platform is not one of v1's: %v", Platforms)
		}
	}
	if err := validateSpelling(req.Spelling); err != nil {
		return nil, err
	}
	return resolveInstalledInternal(ro, req, platform)
}

// ListInstalled implements the seam's list_installed.
func ListInstalled(opts *Options) ([]Resolved, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	warnings, err := probeRoots(ro)
	if err != nil {
		return nil, err
	}
	dirs := append([]string{ro.cacheDir}, ro.systemDirs...)
	var out []Resolved
	for i, dir := range dirs {
		readOnly := i > 0
		entries, err := listUnpacked(dir)
		if err != nil {
			continue
		}
		source := "cache"
		if readOnly {
			source = "system:" + dir
		}
		for _, e := range entries {
			out = append(out, *recordToResolved(&e.rec, e.dir, e.rec.Platform, e.rec.Version, "", true, source, warnings))
		}
	}
	return out, nil
}

// VerifyInstalled implements the seam's verify_installed: re-hash every
// installed library against its own recorded digests.
func VerifyInstalled(opts *Options) ([]VerifyResult, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	if _, err := probeRoots(ro); err != nil {
		return nil, err
	}
	dirs := append([]string{ro.cacheDir}, ro.systemDirs...)
	var out []VerifyResult
	for _, dir := range dirs {
		entries, err := listUnpacked(dir)
		if err != nil {
			continue
		}
		for _, e := range entries {
			out = append(out, verifyOne(e))
		}
	}
	return out, nil
}

func verifyOne(e installedEntry) VerifyResult {
	libPath := filepath.Join(e.dir, filepath.FromSlash(e.rec.LibraryPath))
	b, err := os.ReadFile(libPath)
	if err != nil {
		return VerifyResult{Platform: e.rec.Platform, Version: e.rec.Version, Dir: e.dir, OK: false, Detail: err.Error()}
	}
	got := digestOf(b)
	want := stringPredicate(e.rec.Predicate, "library_sha256")
	if want != "" && got.Hex() != want {
		return VerifyResult{Platform: e.rec.Platform, Version: e.rec.Version, Dir: e.dir, OK: false,
			Detail: fmt.Sprintf("library sha256 %s does not match the recorded %s", got.Hex(), want)}
	}
	return VerifyResult{Platform: e.rec.Platform, Version: e.rec.Version, Dir: e.dir, OK: true}
}

// verifyPreseededEntry is best-effort local verification of a pre-seeded
// index.json entry (one with no unpacked/ directory yet): it has no
// registry referrers API to call, so it scans the layout's own index.json
// for a bundle-artifactType entry and treats that as the manifest's
// referrer, exactly what `oras copy -r --to-oci-layout` is expected to have
// pulled alongside the subject manifest.
// verifyPreseededEntry scans srcLayout's index.json for a platform-matching
// entry with no verified.json yet, and verifies and unpacks it entirely from
// srcLayout's own local blobs. The result (the unpacked library and its
// verified.json record) is written into dstLayout, which must be writable —
// srcLayout itself is only ever read from, so this is safe to call with a
// read-only system directory as the source and the user cache as the
// destination (§1 of the guide: "read-only system directories … never
// written to"). For the user cache itself, src and dst are the same layout.
func verifyPreseededEntry(srcLayout, dstLayout *layout, req Request, platformKey string, trustedKeys []ed25519.PublicKey, source string) (*Resolved, error) {
	platform, ok := platformByKey(platformKey)
	if !ok {
		return nil, nil
	}
	for _, desc := range srcLayout.readIndexEntries() {
		if desc.Platform == nil || desc.Platform.OS != platform.OS || desc.Platform.Architecture != platform.Architecture {
			continue
		}
		manifestBody, ok := srcLayout.readBlob(desc.Digest)
		if !ok {
			continue
		}
		if verr := verifyDescriptor(manifestBody, desc); verr != nil {
			continue
		}
		var m ImageManifest
		if uerr := strictUnmarshal(manifestBody, &m); uerr != nil || len(m.Layers) != 1 {
			continue
		}
		layerDesc := m.Layers[0]
		layerBody, ok := srcLayout.readBlob(layerDesc.Digest)
		if !ok {
			continue
		}
		if verr := verifyDescriptor(layerBody, layerDesc); verr != nil {
			continue
		}
		manifestDigest := desc.Digest
		stmt := findLocalBundle(srcLayout, trustedKeys, manifestDigest, layerDesc.Digest, &platform, req.Spelling)
		if stmt == nil {
			continue
		}
		libraryRelPath, _ := predicateLibraryPath(stmt)
		// The install begins here, and only here is anything created: a
		// lookup that finds nothing to install leaves the cache untouched
		// (docs/guides/fetch-v1.md §6; public issue #486).
		if dstLayout != srcLayout {
			if err := dstLayout.ensureSkeleton(); err != nil {
				continue
			}
		}
		if err := os.MkdirAll(filepath.Join(dstLayout.dir, UnpackedDirName()), dirMode); err != nil {
			continue
		}
		up, err := unpackLibrary(layerBody, filepath.Join(dstLayout.dir, UnpackedDirName()), libraryRelPath)
		if err != nil {
			continue
		}
		rec := verifiedRecord{
			Schema:        1,
			Platform:      platform.Key,
			Version:       stringPredicate(stmt.Statement.Predicate, "clickhouse_version"),
			Channel:       stringPredicate(stmt.Statement.Predicate, "channel"),
			Build:         stringPredicate(stmt.Statement.Predicate, "build"),
			LibraryPath:   up.libraryPath,
			LibrarySHA256: up.librarySHA256,
			LibraryBytes:  up.librarySize,
			SignedBy:      stmt.KeyID,
			Predicate:     stmt.Statement.Predicate,
			Digests: Digests{
				Manifest:       desc.Digest,
				Layer:          layerDesc.Digest,
				Bundle:         stmt.BundleDigest,
				BundleManifest: stmt.BundleManifestDigest,
			},
		}
		dir, already, werr := dstLayout.writeVerifiedRecord(manifestDigest, up.dir, rec)
		if werr != nil {
			continue
		}
		return recordToResolved(&rec, dir, platform.Key, req.Spelling, "", already, source, nil), nil
	}
	return nil, nil
}

// installPreseededByDigest verifies and unpacks ONE pre-seeded index.json
// entry identified by its own manifest digest, entirely from l's local
// blobs, regardless of platform or any particular request. This is not part
// of the seam; it exists for test setup — a conformance cache fixture names
// manifest digests, in its own installed.json, that must already be
// installed (not merely present) before a case's clock starts
// (docs/guides/fetch-v1.md §10, "Cache fixtures and installed.json").
func installPreseededByDigest(l *layout, manifestDigest Digest, trustedKeys []ed25519.PublicKey) error {
	entries := l.readIndexEntries()
	var target *Descriptor
	for i := range entries {
		if entries[i].Digest == manifestDigest {
			target = &entries[i]
			break
		}
	}
	if target == nil {
		return fmt.Errorf("chtypes: installed.json names %s, which is not in index.json", manifestDigest)
	}
	manifestBody, ok := l.readBlob(target.Digest)
	if !ok {
		return fmt.Errorf("chtypes: manifest blob %s is missing", target.Digest)
	}
	if verr := verifyDescriptor(manifestBody, *target); verr != nil {
		return verr
	}
	var m ImageManifest
	if uerr := strictUnmarshal(manifestBody, &m); uerr != nil {
		return uerr
	}
	if len(m.Layers) != 1 {
		return fmt.Errorf("chtypes: manifest %s carries %d layers, expected 1", target.Digest, len(m.Layers))
	}
	layerDesc := m.Layers[0]
	layerBody, ok := l.readBlob(layerDesc.Digest)
	if !ok {
		return fmt.Errorf("chtypes: layer blob %s is missing", layerDesc.Digest)
	}
	if verr := verifyDescriptor(layerBody, layerDesc); verr != nil {
		return verr
	}
	var platformKey string
	if target.Platform != nil {
		platformKey = target.Platform.OS + "-" + target.Platform.Architecture
	}
	stmt := findLocalBundle(l, trustedKeys, target.Digest, layerDesc.Digest, nil, "")
	if stmt == nil {
		return fmt.Errorf("chtypes: no local referrer verifies manifest %s", target.Digest)
	}
	libraryRelPath, _ := predicateLibraryPath(stmt)
	if err := os.MkdirAll(filepath.Join(l.dir, UnpackedDirName()), dirMode); err != nil {
		return err
	}
	up, err := unpackLibrary(layerBody, filepath.Join(l.dir, UnpackedDirName()), libraryRelPath)
	if err != nil {
		return err
	}
	rec := verifiedRecord{
		Schema:        1,
		Platform:      platformKey,
		Version:       stringPredicate(stmt.Statement.Predicate, "clickhouse_version"),
		Channel:       stringPredicate(stmt.Statement.Predicate, "channel"),
		Build:         stringPredicate(stmt.Statement.Predicate, "build"),
		LibraryPath:   up.libraryPath,
		LibrarySHA256: up.librarySHA256,
		LibraryBytes:  up.librarySize,
		SignedBy:      stmt.KeyID,
		Predicate:     stmt.Statement.Predicate,
		Digests: Digests{
			Manifest:       target.Digest,
			Layer:          layerDesc.Digest,
			Bundle:         stmt.BundleDigest,
			BundleManifest: stmt.BundleManifestDigest,
		},
	}
	_, _, werr := l.writeVerifiedRecord(manifestDigest, up.dir, rec)
	return werr
}

// findLocalBundle scans l's own index.json for a bundle-artifactType entry
// that verifies manifestLayerDigest's statement, entirely from local blobs.
// platform nil skips the platform/version check (§4's validateStatement),
// for installing a pre-seeded entry by its own digest rather than against a
// live request.
func findLocalBundle(l *layout, trustedKeys []ed25519.PublicKey, manifestDigest, layerDigest Digest, platform *Platform, request string) *verifiedStatement {
	for _, blobDigest := range l.listAllBlobDigests() {
		refBody, ok := l.readBlob(blobDigest)
		if !ok {
			continue
		}
		var refManifest ImageManifest
		if uerr := strictUnmarshal(refBody, &refManifest); uerr != nil {
			continue
		}
		if refManifest.ArtifactType != MediaTypeBundle || refManifest.Subject == nil || refManifest.Subject.Digest != manifestDigest {
			continue
		}
		if len(refManifest.Layers) != 1 {
			continue
		}
		bundleBody, ok := l.readBlob(refManifest.Layers[0].Digest)
		if !ok {
			continue
		}
		vr, verr := verifyBundle(bundleBody, trustedKeys)
		if verr != nil || !vr.verified {
			continue
		}
		if cerr := validateStatement(vr.statement, PredicateTypeArtifact, layerDigest, platform, request); cerr != nil {
			continue
		}
		return &verifiedStatement{KeyID: vr.keyID, Statement: *vr.statement, BundleDigest: refManifest.Layers[0].Digest, BundleManifestDigest: blobDigest}
	}
	return nil
}

// FetchSigned implements the seam's fetch_signed: resolve ref (a tag or a
// digest) in the repository named by appending repositorySuffix to every
// configured base, find that manifest's own single layer and its own
// referrer signature, verify, and return the layer bytes
// (docs/guides/fetch-v1.md §9; used for goldens, repositorySuffix="", and
// fixtures, repositorySuffix=FixturesRepoSuffix). For predicateType
// PredicateTypeGoldens, ref is instead a PLATFORM manifest digest and the
// result is its highest-revision verified goldens referrer (goldens.go).
func FetchSigned(ctx context.Context, repositorySuffix, ref, predicateType string, opts *Options) (*SignedArtifact, error) {
	ro, err := resolveOptions(opts)
	if err != nil {
		return nil, err
	}
	bases := ro.bases
	if repositorySuffix != "" {
		bases = make([]string, len(ro.bases))
		for i, b := range ro.bases {
			bases[i] = strings.TrimRight(b, "/") + repositorySuffix
		}
	}
	s := newSession(ro)

	if predicateType == PredicateTypeGoldens {
		// Goldens are an OCI referrer of a platform manifest (D7) and the
		// registry is append-only, so ref names the PLATFORM manifest and the
		// highest-revision verified goldens referrer is returned (goldens.go).
		return s.fetchGoldens(ctx, ro, bases, ref)
	}

	policy := notFoundRetryOnLast
	if !Digest(ref).Valid() {
		policy = notFoundUnpublished
	}
	result, base, err := s.fetchAcrossBases(ctx, bases, "manifests/"+ref, policy, requestOptions{maxBytes: ManifestMaxBytes, accept: manifestAccept})
	if err != nil {
		return nil, err
	}
	if Digest(ref).Valid() {
		if verr := verifyDescriptor(result.body, Descriptor{Digest: Digest(ref), Size: int64(len(result.body))}); verr != nil {
			return nil, newError(CodeArtifactCorrupt, ref, "", result.url, verr, "manifest at %s does not match the pinned digest %s", result.url, ref)
		}
	}
	var m ImageManifest
	if uerr := strictUnmarshal(result.body, &m); uerr != nil {
		return nil, newError(CodeArtifactCorrupt, ref, "", result.url, uerr, "manifest is not valid JSON: %v", uerr)
	}
	if m.MediaType != MediaTypeManifest {
		return nil, newError(CodeSourceIncompatible, ref, "", result.url, nil, "unrecognized mediaType %q", m.MediaType)
	}
	if len(m.Layers) != 1 {
		return nil, newError(CodeArtifactCorrupt, ref, "", result.url, nil, "manifest carries %d layers, expected exactly 1", len(m.Layers))
	}
	layerDesc := m.Layers[0]
	manifestDigest := digestOf(result.body)

	layerResult, _, err := s.fetchAcrossBases(ctx, bases, "blobs/"+string(layerDesc.Digest), notFoundRetryOnLast, requestOptions{})
	if err != nil {
		return nil, err
	}
	if verr := verifyDescriptor(layerResult.body, layerDesc); verr != nil {
		return nil, newError(CodeArtifactCorrupt, ref, "", layerResult.url, verr, "layer: %v", verr)
	}

	candidates := s.findReferrers(ctx, bases, manifestDigest, MediaTypeBundle)
	for _, cand := range candidates {
		body, bundleDigest, bundleManifestDigest, ferr := s.fetchReferrerContent(ctx, bases, cand)
		if ferr != nil {
			continue
		}
		vr, verr := verifyBundle(body, ro.trustedKeys)
		if verr != nil {
			return nil, newError(CodeArtifactCorrupt, ref, "", base, verr, "signed statement is corrupt: %v", verr)
		}
		if !vr.verified {
			continue
		}
		if cerr := validateStatement(vr.statement, predicateType, layerDesc.Digest, nil, ""); cerr != nil {
			return nil, newError(CodeArtifactCorrupt, ref, "", base, cerr, "%v", cerr)
		}
		return &SignedArtifact{
			Bytes:     layerResult.body,
			Statement: vr.statement.Predicate,
			Digests:   Digests{Manifest: manifestDigest, Layer: layerDesc.Digest, Bundle: bundleDigest, BundleManifest: bundleManifestDigest},
			SignedBy:  vr.keyID,
		}, nil
	}
	if ro.allowUnsigned {
		return &SignedArtifact{
			Bytes:    layerResult.body,
			Digests:  Digests{Manifest: manifestDigest, Layer: layerDesc.Digest},
			Warnings: []string{"chtypes: no signature found; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned"},
		}, nil
	}
	return nil, newError(CodeArtifactUntrusted, ref, "", base, nil, "no referrer signature verifies under a trusted key")
}

// ensureFrozen implements --frozen: skip resolution entirely, fetch the
// platform manifest, bundle and layer by digest from the lock, verify them
// exactly as a normal fetch would, and never touch the index (§6, §7.4).
func (s *session) ensureFrozen(ctx context.Context, ro resolvedOptions, l *layout, req Request, platform Platform) (*Resolved, error) {
	lock, err := readLock(ro.lockPath)
	if err != nil {
		return nil, err
	}
	pin, ok := lock.Pin(req.Spelling, platform.Key)
	if !ok {
		return nil, newError(CodeArtifactPinned, req.Spelling, platform.Key, "", nil,
			"lock file %s names no entry for %s/%s; re-lock with `fetch --lock`", ro.lockPath, req.Spelling, platform.Key)
	}

	manifestResult, base, err := s.fetchAcrossBases(ctx, ro.bases, "manifests/"+string(pin.Manifest), notFoundRetryOnLast, requestOptions{maxBytes: ManifestMaxBytes, accept: manifestAccept})
	if err != nil {
		return nil, err
	}
	if digestOf(manifestResult.body) != pin.Manifest {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, manifestResult.url, nil, "manifest does not match the locked digest %s", pin.Manifest)
	}
	var m ImageManifest
	if uerr := strictUnmarshal(manifestResult.body, &m); uerr != nil {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, manifestResult.url, uerr, "manifest is not valid JSON: %v", uerr)
	}
	if len(m.Layers) != 1 || m.Layers[0].Digest != pin.Layer {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, manifestResult.url, nil, "manifest's layer does not match the locked digest %s", pin.Layer)
	}
	desc := Descriptor{MediaType: MediaTypeManifest, Digest: pin.Manifest, Size: int64(len(manifestResult.body))}

	layerResult, _, err := s.fetchAcrossBases(ctx, ro.bases, "blobs/"+string(pin.Layer), notFoundRetryOnLast, requestOptions{})
	if err != nil {
		return nil, err
	}
	if digestOf(layerResult.body) != pin.Layer {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, layerResult.url, nil, "layer does not match the locked digest %s", pin.Layer)
	}

	bundleResult, _, err := s.fetchAcrossBases(ctx, ro.bases, "blobs/"+string(pin.Bundle), notFoundRetryOnLast, requestOptions{maxBytes: BundleMaxBytes})
	if err != nil {
		return nil, err
	}
	if digestOf(bundleResult.body) != pin.Bundle {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, bundleResult.url, nil, "bundle does not match the locked digest %s", pin.Bundle)
	}
	vr, verr := verifyBundle(bundleResult.body, ro.trustedKeys)
	if verr != nil {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, bundleResult.url, verr, "signed statement is corrupt: %v", verr)
	}
	if !vr.verified {
		if !ro.allowUnsigned {
			return nil, newError(CodeArtifactUntrusted, req.Spelling, platform.Key, bundleResult.url, nil, "the locked bundle does not verify under a trusted key")
		}
	} else if cerr := validateStatement(vr.statement, PredicateTypeArtifact, pin.Layer, &platform, req.Spelling); cerr != nil {
		return nil, newError(CodeArtifactCorrupt, req.Spelling, platform.Key, bundleResult.url, cerr, "%v", cerr)
	}

	var stmt *verifiedStatement
	var warnings []string
	if vr.verified {
		stmt = &verifiedStatement{KeyID: vr.keyID, Statement: *vr.statement, BundleDigest: pin.Bundle}
	} else {
		warnings = []string{"chtypes: the locked bundle is unsigned or untrusted; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned"}
	}
	libraryRelPath, unsignedConfig, lerr := s.libraryPathFor(ctx, ro.bases, &m, stmt)
	if lerr != nil {
		return nil, lerr
	}
	return s.installManifest(l, req, platform, pin.Manifest, desc, manifestResult.body, m.Layers[0], layerResult.body, stmt, libraryRelPath, unsignedConfig, pin.Index, base, warnings)
}

// writeLockForRequest builds pins for every platform idx offers (downloading
// each platform's manifest and verifying its trust, but fetching the LAYER
// only for the host's own platform — "lock-write-all-platforms" asserts zero
// layer GETs for the others) and merges them into the lock at ro.lockPath.
func (s *session) writeLockForRequest(ctx context.Context, ro resolvedOptions, l *layout, idx *ImageIndex, req Request, hostPlatform Platform, hostResolved *Resolved) error {
	// The index digest is never recorded, even for the host platform: it is
	// informational only (§6 of the guide — the index is computed by the
	// host, not stored, so a by-digest GET of it is not guaranteed to be
	// served), and the fixtures lane's own expected-lock fixtures never
	// carry one.
	pins := map[string]LockPin{
		hostPlatform.Key: {
			Version:  hostResolved.Version,
			Build:    hostResolved.Build,
			Manifest: hostResolved.Digests.Manifest,
			Layer:    hostResolved.Digests.Layer,
			Bundle:   hostResolved.Digests.Bundle,
		},
	}
	for i := range idx.Manifests {
		d := idx.Manifests[i]
		if d.Platform == nil {
			continue
		}
		key := d.Platform.OS + "-" + d.Platform.Architecture
		if key == hostPlatform.Key {
			continue
		}
		plat, ok := platformByKey(key)
		if !ok {
			continue
		}
		manifest, manifestBody, _, err := s.fetchManifestByDigest(ctx, ro.bases, d)
		if err != nil {
			return err
		}
		manifestDigest := digestOf(manifestBody)
		layerDesc := manifest.Layers[0]
		stmt, _, err := s.verifyManifestTrust(ctx, ro.bases, manifestDigest, layerDesc.Digest, plat, req.Spelling, ro.trustedKeys, ro.allowUnsigned)
		if err != nil {
			return err
		}
		if stmt == nil {
			continue
		}
		pins[key] = LockPin{
			Version:  stringPredicate(stmt.Statement.Predicate, "clickhouse_version"),
			Build:    stringPredicate(stmt.Statement.Predicate, "build"),
			Manifest: manifestDigest,
			Layer:    layerDesc.Digest,
			Bundle:   stmt.BundleDigest,
		}
	}
	existing, _ := readLock(ro.lockPath)
	merged := mergeLockRequest(existing, req.Spelling, pins)
	return writeLock(ro.lockPath, merged)
}

// updateLock implements `update`: re-resolve every (spelling, platform) pair
// the existing lock already names against the current index, and rewrite
// the lock from that fresh set alone (§6: "never merges a stale entry with
// a fresh one"). A spelling whose re-resolution verifies no platform at all
// is dropped rather than carried over stale.
func (s *session) updateLock(ctx context.Context, ro resolvedOptions) error {
	existing, err := readLock(ro.lockPath)
	if err != nil {
		return err
	}
	fresh := make(map[string]map[string]LockPin, len(existing.Requests))
	for spelling, byPlatform := range existing.Requests {
		idx, _, _, err := s.resolveIndex(ctx, ro.bases, spelling)
		if err != nil {
			return err
		}
		pins := make(map[string]LockPin, len(byPlatform))
		for platformKey := range byPlatform {
			plat, ok := platformByKey(platformKey)
			if !ok {
				continue
			}
			desc, err := selectPlatformDescriptor(idx, plat.Key)
			if err != nil {
				return err
			}
			manifest, manifestBody, _, err := s.fetchManifestByDigest(ctx, ro.bases, *desc)
			if err != nil {
				return err
			}
			manifestDigest := digestOf(manifestBody)
			layerDesc := manifest.Layers[0]
			stmt, _, err := s.verifyManifestTrust(ctx, ro.bases, manifestDigest, layerDesc.Digest, plat, spelling, ro.trustedKeys, ro.allowUnsigned)
			if err != nil {
				return err
			}
			if stmt == nil {
				continue
			}
			pins[platformKey] = LockPin{
				Version:  stringPredicate(stmt.Statement.Predicate, "clickhouse_version"),
				Build:    stringPredicate(stmt.Statement.Predicate, "build"),
				Manifest: manifestDigest,
				Layer:    layerDesc.Digest,
				Bundle:   stmt.BundleDigest,
			}
		}
		if len(pins) > 0 {
			fresh[spelling] = pins
		}
	}
	return writeLock(ro.lockPath, rewriteLockForUpdate(fresh))
}
