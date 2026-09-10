package chtypes

// fetch.go — fetching, verifying and installing artifacts: the contract
// every SDK implements (docs/fetch.md). Ensure is the function, `chtypes
// fetch` (go/cmd/chtypes) the command; both walk the same chain:
//
//	0. SHA256SUMS.sig verifies over the exact bytes of SHA256SUMS under a
//	   trusted ed25519 key — or nothing proceeds (CHTYPES_ARTIFACT_UNTRUSTED).
//	1. index.json names the asset for the line/platform and its sha256.
//	2. SHA256SUMS — now known-authentic — must list the same file with the
//	   same sha256; a disagreement is a broken release, reported, not repaired.
//	3. The tarball is hashed BEFORE it is unpacked and must equal that sha256.
//	4. manifest.json inside names the library and its sha256; after the move
//	   into <registry>/<minor>/ the installed library is hashed again in place.
//
// Nothing is a verdict but the chain: no exit code, no Content-Length, no
// "download finished". The install is atomic (unpack into a temporary
// sibling, rename into place) and idempotent (an installed line that hashes
// what the release says is reported and nothing is downloaded).
//
// Stdlib only: crypto/ed25519 for the signature, crypto/sha256 for every
// hash, archive/tar + compress/gzip for the unpack, net/http for a remote
// source. No cgo in this file.

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// FetchOptions configures Ensure, FetchAll and ListRelease. The zero value
// is the default fetch: this host's platform, the §1 write directory, the
// public artifacts host's rolling release, the embedded release key, no
// progress output.
type FetchOptions struct {
	// Dest is the registry directory to install into. Empty: the first of
	// $CHTYPES_REGISTRY and the per-user cache for Platform (§1) — except
	// that a fetch for a platform other than this host's never writes into
	// $CHTYPES_REGISTRY, which is a directory this host dlopens from.
	Dest string
	// Platform is "<os>-<arch>". Empty: $CHTYPES_TARGET, else this host.
	Platform string
	// URL names any base — an http(s) mirror, a file:// path, a local
	// directory. Empty: $CHTYPES_ARTIFACTS_URL (default
	// https://artifacts.wavehouse.dev) plus "/" plus Tag.
	URL string
	// Tag is a release tag on the artifacts host; empty is the rolling
	// `artifacts` release. Exclusive with URL.
	Tag string
	// LockFile is a chtypes.lock to WRITE after an install (the asset file
	// and sha256 that were installed, per platform/line). With Frozen it is
	// the lock to ENFORCE instead (empty: "chtypes.lock").
	LockFile string
	// Frozen refuses any asset the lock file does not pin, with
	// CHTYPES_ARTIFACT_PINNED.
	Frozen bool
	// Force re-downloads a line that is already installed and verified.
	Force bool
	// Offline never reads the source. An installed line that hashes what
	// its own manifest says is reported as installed; anything else is
	// CHTYPES_SOURCE_UNREACHABLE.
	Offline bool
	// AllowUnsigned skips signature verification with one loud warning —
	// the same thing CHTYPES_ALLOW_UNSIGNED=1 does. Never the default.
	AllowUnsigned bool
	// TrustedKeys replaces the trust list (else $CHTYPES_TRUSTED_KEYS, else
	// the embedded release key).
	TrustedKeys []ed25519.PublicKey
	// Progress receives the fetch's narrative ("==> …" lines, notes); nil
	// is silent. The CLI passes stderr. The AllowUnsigned warning goes to
	// stderr even when this is nil — it is never silent.
	Progress io.Writer
	// HTTPClient serves remote sources; nil is a default client.
	HTTPClient *http.Client
}

// ReleaseIndex is a release's index.json (docs/artifacts.md §2), schema 1.
type ReleaseIndex struct {
	Schema      int               `json:"schema"`
	GeneratedAt string            `json:"generated_at"`
	ReleaseTag  string            `json:"release_tag"`
	License     string            `json:"license"`
	LicenseURL  string            `json:"license_url"`
	Artifacts   []ReleaseArtifact `json:"artifacts"`
	// SignedBy is the key id that verified SHA256SUMS.sig, or "" when the
	// fetch was told to allow an unsigned release. Set by ListRelease.
	SignedBy string `json:"-"`
	// Source is where the index was read from. Set by ListRelease.
	Source string `json:"-"`
}

// ReleaseArtifact is one row of index.json: one tarball for one
// (ClickHouse version, platform).
type ReleaseArtifact struct {
	OS                string `json:"os"`
	Arch              string `json:"arch"`
	File              string `json:"file"`
	SHA256            string `json:"sha256"`
	Bytes             int64  `json:"bytes"`
	ClickHouseVersion string `json:"clickhouse_version"`
	ClickHouseMinor   string `json:"clickhouse_minor"`
	Library           string `json:"library"`
	LibrarySHA256     string `json:"library_sha256"`
}

// Platform is the row's "<os>-<arch>" key.
func (a ReleaseArtifact) Platform() string { return a.OS + "-" + a.Arch }

// Installed describes one artifact directory in a registry: what Ensure
// installed (or found installed), what ListInstalled enumerates, what
// VerifyInstalled re-hashes.
type Installed struct {
	Line          string // the minor line, e.g. "25.8" — the directory name
	Version       string // the exact patch, e.g. "25.8.28.1-lts"
	Platform      string // "<os>-<arch>", from the manifest
	Dir           string // <registry>/<minor>
	Library       string // the shared-library file name, from the manifest
	LibrarySHA256 string // the manifest's claim, which the install verified
	// File and SHA256 name the release asset this came from; empty when
	// the directory was found rather than fetched.
	File   string
	SHA256 string
	// AlreadyInstalled is true when Ensure downloaded nothing because the
	// installed library already hashed what the release says.
	AlreadyInstalled bool
	// SignedBy is the key id that verified the release, "" when unsigned
	// (allowed) or not fetched.
	SignedBy string
}

// VerifyResult is one line of VerifyInstalled: the installed directory,
// the hash it has now, and whether that is what its manifest claims.
type VerifyResult struct {
	Installed
	Got string // the library's sha256 now
	OK  bool
	Err error // the library could not be read
}

// Ensure makes one ClickHouse line available in a registry directory and
// returns where. A minor line ("25.8") takes the one patch the release
// publishes for it; an exact patch ("25.8.28.1-lts") is a hard requirement.
// Idempotent: installed-and-verified is a no-op; otherwise the fetch walks
// the verification chain above. Every failure is an *ArtifactError whose
// Code is one of the docs/fetch.md §7 codes (errors.Is against the
// sentinels works), except a bad option, which is a plain error.
func Ensure(ctx context.Context, spelling string, opts FetchOptions) (*Installed, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	line, exact, err := parseSpelling(spelling)
	if err != nil {
		return nil, err
	}
	f, err := newFetcher(opts)
	if err != nil {
		return nil, err
	}
	exactNote := ""
	if exact != "" {
		exactNote = " (exact " + exact + ")"
	}
	f.say("ClickHouse %s -> line %s%s, %s", spelling, line, exactNote, f.platform)
	f.crossPlatformNote()
	if f.opts.Offline {
		return f.ensureOffline(spelling, line, exact)
	}
	if err := f.loadRelease(ctx, spelling); err != nil {
		return nil, err
	}
	a, err := f.selectArtifact(spelling, line, exact)
	if err != nil {
		return nil, err
	}
	return f.installOne(ctx, spelling, a)
}

// FetchAll installs every line the release publishes for the platform —
// which lines exist is a question index.json answers, so a caller never
// restates the list. Lines are installed in numeric order; the first
// failure stops the run and is returned with what was installed so far.
func FetchAll(ctx context.Context, opts FetchOptions) ([]*Installed, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	f, err := newFetcher(opts)
	if err != nil {
		return nil, err
	}
	f.say("every published ClickHouse line, %s -> %s", f.platform, f.dest)
	f.crossPlatformNote()
	if f.opts.Offline {
		return nil, artifactErrorf(CodeSourceUnreachable, "--all", f.platform, f.src.String(), nil,
			"offline: --all needs the release's index.json, and --offline forbids reading %s", f.src)
	}
	if err := f.loadRelease(ctx, "--all"); err != nil {
		return nil, err
	}
	rows, err := f.selectAll()
	if err != nil {
		return nil, err
	}
	var out []*Installed
	for i := range rows {
		inst, err := f.installOne(ctx, rows[i].ClickHouseMinor, &rows[i])
		if err != nil {
			return out, err
		}
		out = append(out, inst)
	}
	f.say("%d version(s) installed into %s", len(out), f.dest)
	return out, nil
}

// ListRelease reads and verifies a release's listing without installing
// anything: the same steps 0–1 a fetch runs, so an unsigned or mis-signed
// release is CHTYPES_ARTIFACT_UNTRUSTED here too.
func ListRelease(ctx context.Context, opts FetchOptions) (*ReleaseIndex, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	f, err := newFetcher(opts)
	if err != nil {
		return nil, err
	}
	if f.opts.Offline {
		return nil, artifactErrorf(CodeSourceUnreachable, "", f.platform, f.src.String(), nil,
			"offline: --offline forbids reading %s", f.src)
	}
	if err := f.loadRelease(ctx, ""); err != nil {
		return nil, err
	}
	f.index.SignedBy = f.signedBy
	f.index.Source = f.src.String()
	return f.index, nil
}

// ListInstalled enumerates the artifact directories under one registry —
// every <minor>/manifest.json — in numeric line order, without hashing
// anything (VerifyInstalled does that). A registry that does not exist is
// simply empty.
func ListInstalled(dir string) ([]Installed, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var out []Installed
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		sub := filepath.Join(dir, e.Name())
		m, err := readManifest(filepath.Join(sub, "manifest.json"))
		if err != nil {
			continue
		}
		out = append(out, Installed{
			Line:          e.Name(),
			Version:       m.ClickHouseVersion,
			Platform:      m.platform(),
			Dir:           sub,
			Library:       m.Library,
			LibrarySHA256: m.LibrarySHA256,
		})
	}
	sort.Slice(out, func(i, j int) bool { return lessMinor(out[i].Line, out[j].Line) })
	return out, nil
}

// VerifyInstalled re-hashes every installed line under dir against its
// own manifest — the same predicate the install path ends with, applied
// to what is on disk now.
func VerifyInstalled(dir string) ([]VerifyResult, error) {
	installed, err := ListInstalled(dir)
	if err != nil {
		return nil, err
	}
	out := make([]VerifyResult, 0, len(installed))
	for _, inst := range installed {
		r := VerifyResult{Installed: inst}
		r.Got, r.Err = fileSHA256(filepath.Join(inst.Dir, inst.Library))
		r.OK = r.Err == nil && r.Got == strings.ToLower(inst.LibrarySHA256)
		out = append(out, r)
	}
	return out, nil
}

// ---------------------------------------------------------------- spelling

var channelSuffix = regexp.MustCompile(`-(lts|stable|prestable|testing)$`)

// parseSpelling turns what a caller typed into (line, exact): "25.8" and
// "v25.8" are the line; "25.8.28.1" and "v25.8.28.1-lts" are that line
// plus an exact patch, which is a hard requirement (docs/fetch.md §2). A
// three-part spelling is taken as its line.
func parseSpelling(s string) (line, exact string, err error) {
	s = strings.TrimSpace(s)
	s = strings.TrimPrefix(s, "v")
	bare := channelSuffix.ReplaceAllString(s, "")
	parts := strings.Split(bare, ".")
	if len(parts) < 2 {
		return "", "", fmt.Errorf("chtypes: cannot make a ClickHouse version out of %q", s)
	}
	for _, p := range parts {
		if p == "" {
			return "", "", fmt.Errorf("chtypes: cannot make a ClickHouse version out of %q", s)
		}
		for _, ch := range p {
			if ch < '0' || ch > '9' {
				return "", "", fmt.Errorf("chtypes: cannot make a ClickHouse version out of %q", s)
			}
		}
	}
	line = parts[0] + "." + parts[1]
	if len(parts) >= 4 {
		exact = s
	}
	return line, exact, nil
}

// exactMatches says whether a published version satisfies an exact-patch
// spelling. A spelling that names its channel ("25.8.28.1-lts") matches only
// itself; one that omits it ("25.8.28.1") matches that patch on any channel —
// the same rule in all four SDKs (docs/fetch.md, Decisions).
func exactMatches(version, exact string) bool {
	if version == exact {
		return true
	}
	if channelSuffix.MatchString(exact) {
		return false
	}
	return channelSuffix.ReplaceAllString(version, "") == exact
}

// versionKey orders exact versions numerically ("25.8.28.1-lts" → 25,8,28,1).
func versionKey(v string) []int {
	v = strings.SplitN(v, "-", 2)[0]
	parts := strings.Split(v, ".")
	out := make([]int, len(parts))
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil {
			n = -1
		}
		out[i] = n
	}
	return out
}

func lessVersionKey(a, b []int) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

func lessMinor(a, b string) bool { return lessVersionKey(versionKey(a), versionKey(b)) }

// ---------------------------------------------------------------- manifest

// artifactManifest is the artifact's own record of itself (spec/artifact.md).
type artifactManifest struct {
	OS                string `json:"os"`
	Arch              string `json:"arch"`
	ClickHouseVersion string `json:"clickhouse_version"`
	ClickHouseMinor   string `json:"clickhouse_minor"`
	Library           string `json:"library"`
	LibraryBytes      int64  `json:"library_bytes"`
	LibrarySHA256     string `json:"library_sha256"`
}

func readManifest(path string) (*artifactManifest, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m artifactManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if m.Library == "" || m.LibrarySHA256 == "" {
		return nil, fmt.Errorf("%s: manifest.json is missing library/library_sha256", path)
	}
	return &m, nil
}

// minor derives the line when the manifest predates the field — what
// minorOf does for the loader, so the two agree by construction.
func (m *artifactManifest) minor() string {
	if m.ClickHouseMinor != "" {
		return m.ClickHouseMinor
	}
	return minorOf(m.ClickHouseVersion)
}

func (m *artifactManifest) platform() string {
	arch := m.Arch
	switch arch {
	case "x86_64":
		arch = "amd64"
	case "aarch64":
		arch = "arm64"
	}
	if m.OS == "" || arch == "" {
		return ""
	}
	return m.OS + "-" + arch
}

// ---------------------------------------------------------------- fetcher

type fetcher struct {
	opts          FetchOptions
	platform      string
	dest          string
	src           *source
	keys          []ed25519.PublicKey
	keysFrom      string
	allowUnsigned bool

	loaded   bool
	index    *ReleaseIndex
	sums     map[string]string // asset file -> sha256, from the verified SHA256SUMS
	signedBy string
}

func newFetcher(opts FetchOptions) (*fetcher, error) {
	f := &fetcher{opts: opts}
	f.platform = opts.Platform
	if f.platform == "" {
		f.platform = os.Getenv(envTarget)
	}
	if f.platform == "" {
		f.platform = HostPlatform()
	}
	if !ValidPlatform(f.platform) {
		return nil, fmt.Errorf("chtypes: not a known platform key: %s ((linux|darwin)-(arm64|amd64))", f.platform)
	}
	f.dest = opts.Dest
	if f.dest == "" {
		if f.platform == HostPlatform() {
			f.dest = fetchRegistryDirFor("", f.platform)
		} else {
			f.dest = DefaultRegistryDirFor(f.platform)
		}
	}
	if f.dest == "" {
		return nil, fmt.Errorf("chtypes: cannot determine a registry directory (no home directory); pass Dest or set CHTYPES_REGISTRY")
	}
	var err error
	if f.src, err = newSource(opts.URL, opts.Tag, opts.HTTPClient); err != nil {
		return nil, err
	}
	if f.keys, f.keysFrom, err = trustedKeys(opts.TrustedKeys); err != nil {
		return nil, err
	}
	f.allowUnsigned = opts.AllowUnsigned || os.Getenv(envAllowUnsign) == "1"
	return f, nil
}

func (f *fetcher) say(format string, args ...any) {
	if f.opts.Progress != nil {
		fmt.Fprintf(f.opts.Progress, "==> "+format+"\n", args...)
	}
}

func (f *fetcher) note(format string, args ...any) {
	if f.opts.Progress != nil {
		fmt.Fprintf(f.opts.Progress, "chtypes: "+format+"\n", args...)
	}
}

// warn is never silent: the one loud warning docs/fetch.md §4 requires.
func (f *fetcher) warn(format string, args ...any) {
	w := f.opts.Progress
	if w == nil {
		w = os.Stderr
	}
	fmt.Fprintf(w, "chtypes: WARNING: "+format+"\n", args...)
}

func (f *fetcher) crossPlatformNote() {
	if f.platform != HostPlatform() {
		f.note("note — fetching %s artifacts on a %s host.", f.platform, HostPlatform())
		f.note("        They are for a %s process (a container, usually), not this one.", f.platform)
		f.note("        Pass --platform %s for a library this host can dlopen.", HostPlatform())
	}
}

func (f *fetcher) fail(code ErrorCode, line string, cause error, format string, args ...any) *ArtifactError {
	return artifactErrorf(code, line, f.platform, f.src.String(), cause, format, args...)
}

// ensureOffline is the no-network path: installed and hashing what its
// own manifest says is an answer; anything else is unreachable.
func (f *fetcher) ensureOffline(spelling, line, exact string) (*Installed, error) {
	if f.opts.Force {
		return nil, f.fail(CodeSourceUnreachable, spelling, nil,
			"offline: --force needs a download, and --offline forbids reading %s", f.src)
	}
	dir := filepath.Join(f.dest, line)
	m, err := readManifest(filepath.Join(dir, "manifest.json"))
	if err == nil && (exact == "" || exactMatches(m.ClickHouseVersion, exact)) {
		got, herr := fileSHA256(filepath.Join(dir, m.Library))
		if herr == nil && got == strings.ToLower(m.LibrarySHA256) {
			f.say("already installed and verified against its manifest (offline): %s", filepath.Join(dir, m.Library))
			if f.opts.Frozen {
				f.note("offline: the lock file was not re-checked against a release; the installed manifest verified")
			}
			return &Installed{
				Line: line, Version: m.ClickHouseVersion, Platform: m.platform(), Dir: dir,
				Library: m.Library, LibrarySHA256: got, AlreadyInstalled: true,
			}, nil
		}
		if herr == nil {
			return nil, f.fail(CodeArtifactCorrupt, spelling, nil,
				"offline: installed %s hashes %s, its manifest says %s, and --offline forbids re-fetching it",
				filepath.Join(dir, m.Library), got, m.LibrarySHA256)
		}
	}
	return nil, f.fail(CodeSourceUnreachable, spelling, nil,
		"offline: ClickHouse %s is not installed in %s, and --offline forbids reading %s", spelling, f.dest, f.src)
}

// loadReleaseOnce reads the release's three small files once and runs steps 0
// and 1: the signature over SHA256SUMS, then the index.
func (f *fetcher) loadReleaseOnce(ctx context.Context, line string) error {
	if f.loaded {
		return nil
	}
	f.say("source %s", f.src)
	// Reachability first: a source with no index.json is not a release at
	// all, and saying "unsigned" about an empty directory would mislead.
	indexBytes, err := f.src.readAll(ctx, "index.json", 8<<20)
	if err != nil {
		return f.fail(CodeSourceUnreachable, line, err, "no index.json at %s: %v", f.src, err)
	}
	sums, err := f.src.readAll(ctx, "SHA256SUMS", 1<<20)
	if err != nil {
		if errors.Is(err, errAssetNotFound) {
			return f.fail(CodeArtifactUntrusted, line, err,
				"the release at %s has no SHA256SUMS — nothing a signature could cover; refusing it", f.src)
		}
		return f.fail(CodeSourceUnreachable, line, err, "could not read SHA256SUMS from %s: %v", f.src, err)
	}
	// Step 0: the signature — or the one loud warning.
	if f.allowUnsigned {
		f.warn("%s=1 — NOT verifying the signature of the release at %s; whatever it serves will be installed", envAllowUnsign, f.src)
	} else {
		sig, err := f.src.readAll(ctx, "SHA256SUMS.sig", 64<<10)
		if err != nil {
			if errors.Is(err, errAssetNotFound) {
				return f.fail(CodeArtifactUntrusted, line, err,
					"the release at %s is unsigned (no SHA256SUMS.sig); refusing it. %s=1 installs it anyway, loudly", f.src, envAllowUnsign)
			}
			return f.fail(CodeSourceUnreachable, line, err, "could not read SHA256SUMS.sig from %s: %v", f.src, err)
		}
		key, err := VerifySignature(sums, sig, f.keys)
		if err != nil {
			return f.fail(CodeArtifactUntrusted, line, err,
				"SHA256SUMS.sig at %s: %v; trusted keys from %s. Refusing the release", f.src, err, f.keysFrom)
		}
		f.signedBy = KeyID(key)
		f.say("signature verified: SHA256SUMS signed by ed25519 key %s", f.signedBy)
	}
	// Step 1: the listing.
	var index ReleaseIndex
	if err := json.Unmarshal(indexBytes, &index); err != nil {
		return f.fail(CodeArtifactCorrupt, line, err, "index.json at %s is not readable: %v", f.src, err)
	}
	if index.Schema != 1 {
		return f.fail(CodeArtifactCorrupt, line, nil, "index.json at %s has schema %d, not 1 — this SDK cannot read it", f.src, index.Schema)
	}
	if index.License != "" {
		f.note("artifacts are licensed under %s %s — LICENSE and NOTICE ship beside them", index.License, index.LicenseURL)
	}
	f.sums = parseSums(sums)
	f.index = &index
	f.loaded = true
	// Step 2 for the whole release, not just the asset being installed: every
	// row SHA256SUMS also names must agree with the index. installOne still
	// checks its own asset — this one exists so a disagreement is seen while
	// loadRelease can still fix it by reading all three files again.
	for i := range f.index.Artifacts {
		a := &f.index.Artifacts[i]
		sumsSHA, listed := f.sums[a.File]
		if listed && sumsSHA != strings.ToLower(a.SHA256) {
			f.loaded = false
			return f.fail(CodeArtifactCorrupt, line, nil,
				"index.json says %s is %s but SHA256SUMS says %s — the release disagrees with itself; not installing it", a.File, a.SHA256, sumsSHA)
		}
	}
	return nil
}

// How many times loadRelease reads a remote release before giving up, and the
// wait between reads: three attempts span about ten seconds. A var, not a
// const, so the retry test can exercise a real window without a real wait.
var (
	releaseLoadAttempts = 3
	releaseRetryDelay   = 4 * time.Second
)

// loadRelease is loadReleaseOnce, retried through a publish window.
//
// A publish into the rolling release is three objects — SHA256SUMS,
// SHA256SUMS.sig, index.json — and object storage cannot swap them atomically.
// They go up in that order, so an old index read against new sums still
// cross-checks; the unsafe window is between the sums and the signature that
// covers them, one small object wide and seconds long.
//
// The two symptoms of reading inside it — a signature that verifies under no
// trusted key, and an index that disagrees with the sums — are retried. Nothing
// else is, and neither are these once the attempts run out: the same error
// surfaces with the same code and exit status as before. A tarball whose hash is
// wrong is never retried; that is the release lying about a byte, not a
// half-finished upload.
//
// Only a remote source can be mid-publish, so a directory or file:// source is
// read exactly once and refuses on the first look.
func (f *fetcher) loadRelease(ctx context.Context, line string) error {
	attempts := 1
	if f.src.remote {
		attempts = releaseLoadAttempts
	}
	for attempt := 1; ; attempt++ {
		err := f.loadReleaseOnce(ctx, line)
		if err == nil || attempt >= attempts || !isPublishWindow(err) {
			return err
		}
		f.say("%v (attempt %d/%d) — this is what a release being published looks like from outside; retrying in %s",
			err, attempt, attempts, releaseRetryDelay)
		select {
		case <-ctx.Done():
			return err
		case <-time.After(releaseRetryDelay):
		}
	}
}

// isPublishWindow reports whether err is one of the two symptoms of reading a
// release mid-publish, and so worth reading again.
func isPublishWindow(err error) bool {
	var ae *ArtifactError
	if !errors.As(err, &ae) {
		return false
	}
	return ae.Code == CodeArtifactUntrusted || ae.Code == CodeArtifactCorrupt
}

// parseSums reads the `<sha256>  <file>` (or `<sha256> *<file>`) lines.
func parseSums(b []byte) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(string(b), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		out[strings.TrimPrefix(fields[1], "*")] = strings.ToLower(fields[0])
	}
	return out
}

func (f *fetcher) rowsForPlatform() []ReleaseArtifact {
	var rows []ReleaseArtifact
	for _, a := range f.index.Artifacts {
		if a.Platform() == f.platform {
			rows = append(rows, a)
		}
	}
	return rows
}

func (f *fetcher) platformsOffered() string {
	seen := map[string]bool{}
	var out []string
	for _, a := range f.index.Artifacts {
		if p := a.Platform(); !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	sort.Strings(out)
	if len(out) == 0 {
		return "nothing"
	}
	return strings.Join(out, ", ")
}

func versionsOf(rows []ReleaseArtifact) string {
	var out []string
	for _, a := range rows {
		out = append(out, a.ClickHouseVersion)
	}
	return strings.Join(out, ", ")
}

func checkRow(a *ReleaseArtifact) error {
	switch {
	case a.File == "":
		return errors.New("file")
	case a.SHA256 == "":
		return errors.New("sha256")
	case a.Bytes <= 0:
		return errors.New("bytes")
	case a.ClickHouseVersion == "":
		return errors.New("clickhouse_version")
	case a.ClickHouseMinor == "":
		return errors.New("clickhouse_minor")
	case a.Library == "":
		return errors.New("library")
	case a.LibrarySHA256 == "":
		return errors.New("library_sha256")
	}
	return nil
}

// selectArtifact picks the one row for (platform, line|exact).
func (f *fetcher) selectArtifact(spelling, line, exact string) (*ReleaseArtifact, error) {
	rows := f.rowsForPlatform()
	if len(rows) == 0 {
		return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
			"the release at %s has nothing for %s (it has: %s)", f.src, f.platform, f.platformsOffered())
	}
	var hits []ReleaseArtifact
	if exact != "" {
		for _, a := range rows {
			if exactMatches(a.ClickHouseVersion, exact) {
				hits = append(hits, a)
			}
		}
		if len(hits) == 0 {
			return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
				"you asked for exactly ClickHouse %s on %s and the release at %s does not publish it (it has: %s). Ask for the line (%s) to take what was published",
				exact, f.platform, f.src, versionsOf(rows), line)
		}
	} else {
		for _, a := range rows {
			if a.ClickHouseMinor == line {
				hits = append(hits, a)
			}
		}
		if len(hits) == 0 {
			return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
				"no artifact for ClickHouse line %s on %s at %s (it has: %s)", line, f.platform, f.src, versionsOf(rows))
		}
	}
	// More than one patch on a line can only happen if a release shipped
	// two; take the newest by version number rather than by list order.
	sort.SliceStable(hits, func(i, j int) bool {
		return lessVersionKey(versionKey(hits[i].ClickHouseVersion), versionKey(hits[j].ClickHouseVersion))
	})
	a := hits[len(hits)-1]
	if err := checkRow(&a); err != nil {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil, "index.json entry for %s is missing %v", a.File, err)
	}
	return &a, nil
}

// selectAll picks one row per minor line for the platform, newest patch
// per line, in numeric line order.
func (f *fetcher) selectAll() ([]ReleaseArtifact, error) {
	rows := f.rowsForPlatform()
	if len(rows) == 0 {
		return nil, f.fail(CodeArtifactUnpublished, "--all", nil,
			"the release at %s has nothing for %s (it has: %s)", f.src, f.platform, f.platformsOffered())
	}
	best := map[string]ReleaseArtifact{}
	for _, a := range rows {
		cur, ok := best[a.ClickHouseMinor]
		if !ok || lessVersionKey(versionKey(cur.ClickHouseVersion), versionKey(a.ClickHouseVersion)) {
			best[a.ClickHouseMinor] = a
		}
	}
	out := make([]ReleaseArtifact, 0, len(best))
	for _, a := range best {
		if err := checkRow(&a); err != nil {
			return nil, f.fail(CodeArtifactCorrupt, "--all", nil, "index.json entry for %s is missing %v", a.File, err)
		}
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return lessMinor(out[i].ClickHouseMinor, out[j].ClickHouseMinor) })
	return out, nil
}

// checkPin enforces the §5 lock under --frozen.
func (f *fetcher) checkPin(spelling string, a *ReleaseArtifact) error {
	path := f.opts.LockFile
	if path == "" {
		path = DefaultLockFile
	}
	l, err := ReadLockFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return f.fail(CodeArtifactPinned, spelling, err, "--frozen, but there is no lock file at %s", path)
		}
		return f.fail(CodeArtifactPinned, spelling, err, "--frozen: %v", err)
	}
	key := LockKey(f.platform, a.ClickHouseMinor)
	entry, ok := l.Artifacts[key]
	if !ok {
		return f.fail(CodeArtifactPinned, spelling, nil,
			"%s does not pin %s; the release offers %s (%s). Refusing an unpinned artifact under --frozen", path, key, a.File, a.SHA256)
	}
	if entry.File != a.File || !strings.EqualFold(entry.SHA256, a.SHA256) {
		return f.fail(CodeArtifactPinned, spelling, nil,
			"%s pins %s to %s (%s) but the release offers %s (%s). Refusing it under --frozen", path, key, entry.File, entry.SHA256, a.File, a.SHA256)
	}
	f.say("pinned: %s matches %s", key, path)
	return nil
}

// recordLock writes the §5 entry when a lock file was asked for (never
// under --frozen, which is read-only).
func (f *fetcher) recordLock(a *ReleaseArtifact) error {
	if f.opts.LockFile == "" || f.opts.Frozen {
		return nil
	}
	l, err := readOrNewLockFile(f.opts.LockFile)
	if err != nil {
		return err
	}
	key := LockKey(f.platform, a.ClickHouseMinor)
	l.Artifacts[key] = LockEntry{File: a.File, SHA256: strings.ToLower(a.SHA256)}
	if err := l.Write(f.opts.LockFile); err != nil {
		return fmt.Errorf("chtypes: writing %s: %w", f.opts.LockFile, err)
	}
	f.say("pinned %s -> %s in %s", key, a.File, f.opts.LockFile)
	return nil
}

// installOne walks steps 2–4 for one selected row.
func (f *fetcher) installOne(ctx context.Context, spelling string, a *ReleaseArtifact) (*Installed, error) {
	f.say("%s  (%d bytes, ClickHouse %s, library %s)", a.File, a.Bytes, a.ClickHouseVersion, a.Library)
	wantSHA := strings.ToLower(a.SHA256)
	wantLib := strings.ToLower(a.LibrarySHA256)

	// Step 2: the signed SHA256SUMS must agree with index.json.
	sumsSHA, ok := f.sums[a.File]
	if !ok {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil, "SHA256SUMS at %s has no line for %s", f.src, a.File)
	}
	if sumsSHA != wantSHA {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil,
			"index.json says %s is %s but SHA256SUMS says %s — the release disagrees with itself; not installing it", a.File, wantSHA, sumsSHA)
	}
	if f.opts.Frozen {
		if err := f.checkPin(spelling, a); err != nil {
			return nil, err
		}
	}

	installDir := filepath.Join(f.dest, a.ClickHouseMinor)
	result := &Installed{
		Line: a.ClickHouseMinor, Version: a.ClickHouseVersion, Platform: f.platform, Dir: installDir,
		Library: a.Library, LibrarySHA256: wantLib, File: a.File, SHA256: wantSHA, SignedBy: f.signedBy,
	}

	// Already installed and intact? The test is the one the install path
	// ends with, so "already there" is a verified claim.
	if !f.opts.Force {
		if _, err := os.Stat(filepath.Join(installDir, "manifest.json")); err == nil {
			if got, err := fileSHA256(filepath.Join(installDir, a.Library)); err == nil {
				if got == wantLib {
					f.say("already installed and verified: %s", filepath.Join(installDir, a.Library))
					result.AlreadyInstalled = true
					if err := f.recordLock(a); err != nil {
						return nil, err
					}
					return result, nil
				}
				f.note("%s is present but hashes %s (want %s) — replacing", filepath.Join(installDir, a.Library), got, wantLib)
			}
		}
	}

	// Step 3: the tarball, hashed before it is unpacked.
	if err := os.MkdirAll(f.dest, 0o755); err != nil {
		return nil, fmt.Errorf("chtypes: %w", err)
	}
	pid := strconv.Itoa(os.Getpid())
	partial := filepath.Join(f.dest, "."+a.File+".partial."+pid)
	defer os.Remove(partial)
	f.say("downloading %s", a.File)
	rc, err := f.src.open(ctx, a.File)
	if err != nil {
		return nil, f.fail(CodeSourceUnreachable, spelling, err, "could not download %s from %s: %v", a.File, f.src, err)
	}
	n, gotSHA, err := hashAndWrite(rc, partial)
	rc.Close()
	if err != nil {
		return nil, f.fail(CodeSourceUnreachable, spelling, err, "download of %s from %s failed: %v", a.File, f.src, err)
	}
	if n != a.Bytes {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil, "%s is %d bytes, index.json says %d — NOT unpacking it", a.File, n, a.Bytes)
	}
	if gotSHA != wantSHA {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil, "%s FAILED its sha256: got %s, want %s — NOT unpacking it", a.File, gotSHA, wantSHA)
	}
	f.say("sha256 verified before unpacking: %s", gotSHA)

	// Step 4: unpack into a temporary sibling, check the artifact's own
	// claim about itself, rename into place, re-hash in place.
	incoming, err := os.MkdirTemp(f.dest, "."+a.ClickHouseMinor+".incoming.")
	if err != nil {
		return nil, fmt.Errorf("chtypes: %w", err)
	}
	defer os.RemoveAll(incoming)
	if err := untarGz(partial, incoming); err != nil {
		return nil, f.fail(CodeArtifactCorrupt, spelling, err, "%s does not unpack cleanly: %v", a.File, err)
	}
	m, err := readManifest(filepath.Join(incoming, "manifest.json"))
	if err != nil {
		return nil, f.fail(CodeArtifactCorrupt, spelling, err, "%s carries no usable manifest.json at its root: %v", a.File, err)
	}
	if m.Library != a.Library || m.ClickHouseVersion != a.ClickHouseVersion || m.minor() != a.ClickHouseMinor || !strings.EqualFold(m.LibrarySHA256, wantLib) {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil,
			"manifest.json inside %s disagrees with index.json (%s/%s/%s/%s vs %s/%s/%s/%s)", a.File,
			m.Library, m.ClickHouseVersion, m.minor(), m.LibrarySHA256, a.Library, a.ClickHouseVersion, a.ClickHouseMinor, wantLib)
	}
	if _, err := os.Stat(filepath.Join(incoming, m.Library)); err != nil {
		return nil, f.fail(CodeArtifactCorrupt, spelling, err, "%s names library %s but does not contain it", a.File, m.Library)
	}
	os.Remove(partial)

	replaced := filepath.Join(f.dest, "."+a.ClickHouseMinor+".replaced."+pid)
	os.RemoveAll(replaced)
	hadOld := false
	if _, err := os.Lstat(installDir); err == nil {
		if err := os.Rename(installDir, replaced); err != nil {
			return nil, fmt.Errorf("chtypes: moving the previous %s aside: %w", installDir, err)
		}
		hadOld = true
	}
	if err := os.Rename(incoming, installDir); err != nil {
		if hadOld {
			os.Rename(replaced, installDir)
		}
		return nil, fmt.Errorf("chtypes: installing %s: %w", installDir, err)
	}
	if hadOld {
		os.RemoveAll(replaced)
	}

	final, err := fileSHA256(filepath.Join(installDir, m.Library))
	if err != nil || final != strings.ToLower(m.LibrarySHA256) {
		os.RemoveAll(installDir)
		if err != nil {
			return nil, f.fail(CodeArtifactCorrupt, spelling, err, "installed %s could not be re-hashed: %v — the install was removed", filepath.Join(installDir, m.Library), err)
		}
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil,
			"installed %s hashes %s, manifest says %s — the install is bad and was removed", filepath.Join(installDir, m.Library), final, m.LibrarySHA256)
	}
	f.say("installed and verified")
	f.note("    %s/", installDir)
	f.note("    %s sha256 %s", m.Library, final)
	f.note("    ClickHouse %s — chtypes.NewRegistry(%q) will now serve %s", m.ClickHouseVersion, f.dest, a.ClickHouseMinor)
	result.Platform = m.platform()
	if result.Platform == "" {
		result.Platform = f.platform
	}
	if err := f.recordLock(a); err != nil {
		return nil, err
	}
	return result, nil
}

// ---------------------------------------------------------------- bytes

// hashAndWrite streams r into path, hashing as it goes: the sha256 is of
// exactly the bytes that landed.
func hashAndWrite(r io.Reader, path string) (n int64, sha string, err error) {
	out, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return 0, "", err
	}
	h := sha256.New()
	n, err = io.Copy(io.MultiWriter(out, h), r)
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return n, "", err
	}
	return n, hex.EncodeToString(h.Sum(nil)), nil
}

func fileSHA256(path string) (string, error) {
	fh, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer fh.Close()
	h := sha256.New()
	if _, err := io.Copy(h, fh); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// untarGz unpacks a release tarball — regular files and directories at the
// tar root, nothing else — into dir, refusing anything that would land
// outside it.
func untarGz(archive, dir string) error {
	fh, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer fh.Close()
	gz, err := gzip.NewReader(fh)
	if err != nil {
		return err
	}
	defer gz.Close()
	tr := tar.NewReader(gz)
	root := filepath.Clean(dir) + string(filepath.Separator)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		name := filepath.Clean(hdr.Name)
		if name == "." {
			continue
		}
		if filepath.IsAbs(name) || name == ".." || strings.HasPrefix(name, ".."+string(filepath.Separator)) {
			return fmt.Errorf("entry %q escapes the artifact directory", hdr.Name)
		}
		target := filepath.Join(dir, name)
		if !strings.HasPrefix(target, root) {
			return fmt.Errorf("entry %q escapes the artifact directory", hdr.Name)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			mode := os.FileMode(hdr.Mode) & 0o777
			mode |= 0o600
			out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, tr); err != nil {
				out.Close()
				return err
			}
			if err := out.Close(); err != nil {
				return err
			}
		case tar.TypeXGlobalHeader, tar.TypeXHeader:
			continue
		default:
			return fmt.Errorf("entry %q has type %q — a release tarball holds only files and directories", hdr.Name, hdr.Typeflag)
		}
	}
}
