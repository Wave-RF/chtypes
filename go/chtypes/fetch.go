package chtypes

// fetch.go — fetching, verifying and installing artifacts: the contract
// every SDK implements (docs/guides/fetch.md). Ensure is the function, `chtypes
// fetch` (go/cmd/chtypes) the command; both walk the same chain:
//
//	0. SHA256SUMS.sig verifies over the exact bytes of SHA256SUMS under a
//	   trusted ed25519 key — or nothing proceeds (CHTYPES_ARTIFACT_UNTRUSTED).
//	1. index.json names the asset for the line/platform and its sha256 — only
//	   ever a row at this package's own ABIRevision (docs/guides/fetch.md §2).
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
	"bytes"
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

	"github.com/wave-rf/chtypes/go/internal/testhook"
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

// ReleaseIndex is a release's index.json (docs/guides/artifacts.md §2), schema 1.
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
	// Build is the wrapper build for this ClickHouse version. A rebuild of the
	// same version is a NEW row beside the old one, never a swap, so Build is
	// what separates them. Absent from an old row, and from a file name with no
	// -b<N> suffix: both mean build 0.
	Build int `json:"build,omitempty"`
	// CoreCommit is the core commit the wrapper was built from; "" on an old row.
	CoreCommit string `json:"core_commit,omitempty"`
	// ABIRevision is the chs_* ABI revision the artifact was built from, or
	// nil when the row declares none — or declares something that is not an
	// integer, which is read as none rather than failing the whole listing.
	// The artifact producer writes the field from the revision that
	// introduced it onward, so a row without it is an older revision. Fetch
	// installs only a row whose ABIRevision is this package's own
	// (docs/guides/fetch.md §2): any other would be refused at load.
	ABIRevision *int `json:"abi_revision,omitempty"`
}

// UnmarshalJSON reads a row as encoding/json would, except that an
// abi_revision which is not a JSON integer is read as absent (nil) instead of
// failing the whole index.json: such a row can never be selected, which is
// the verdict every other binding and scripts/fetch.sh reach for it too.
func (a *ReleaseArtifact) UnmarshalJSON(b []byte) error {
	type plain ReleaseArtifact
	aux := struct {
		*plain
		ABIRevision json.RawMessage `json:"abi_revision"`
	}{plain: (*plain)(a)}
	if err := json.Unmarshal(b, &aux); err != nil {
		return err
	}
	a.ABIRevision = nil
	if n, err := strconv.Atoi(string(bytes.TrimSpace(aux.ABIRevision))); err == nil {
		a.ABIRevision = &n
	}
	return nil
}

// atRevision reports whether a row declares exactly ABI revision rev.
func (a ReleaseArtifact) atRevision(rev int) bool {
	return a.ABIRevision != nil && *a.ABIRevision == rev
}

// buildSuffix reads the -b<N> a current artifact file name ends with. A name
// without one is an old row, which is build 0 by definition.
var buildSuffix = regexp.MustCompile(`-b([0-9]+)\.tar\.gz$`)

// BuildNumber is the row's own Build when it has one, else the file name's
// -b<N>, else 0. Exported because it is the rule a consumer needs to answer
// "which of these two rows is the newer build", not just an internal detail.
func (a ReleaseArtifact) BuildNumber() int {
	if a.Build > 0 {
		return a.Build
	}
	if m := buildSuffix.FindStringSubmatch(a.File); m != nil {
		n, err := strconv.Atoi(m[1])
		if err == nil && n >= 0 {
			return n
		}
	}
	return 0
}

// newerRow reports whether a should be preferred over b: newest ClickHouse
// version first, and among rows of the SAME version the highest wrapper build.
// Without the build tie-break a rebuild's older sibling could win on nothing
// but its position in the index.
func newerRow(a, b ReleaseArtifact) bool {
	ka, kb := versionKey(a.ClickHouseVersion), versionKey(b.ClickHouseVersion)
	if lessVersionKey(ka, kb) {
		return false
	}
	if lessVersionKey(kb, ka) {
		return true
	}
	return a.BuildNumber() > b.BuildNumber()
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
// Code is one of the docs/guides/fetch.md §7 codes (errors.Is against the
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
	if f.opts.Frozen {
		return f.ensureFrozen(ctx, spelling, line, exact)
	}
	if err := f.loadRelease(ctx, spelling); err != nil {
		return nil, err
	}
	a, err := f.selectArtifact(spelling, line, exact)
	if err != nil {
		return nil, err
	}
	inst, err := f.installOne(ctx, spelling, exact, a)
	if err != nil {
		return nil, err
	}
	f.installGoldens(ctx)
	return inst, nil
}

// goldensAsset is the served golden set: a release-level file like index.json,
// and a row in the signed SHA256SUMS like a tarball, so it verifies through the
// same chain. It installs beside the artifacts as <registry>/sdk-goldens.json,
// where every binding's golden test reads it offline.
const goldensAsset = "sdk-goldens.json"

// readGoldensOnce is one look at the served golden set against sums — the
// release's own, already-verified SHA256SUMS map, or a fresh one a retry just
// re-read. Returns (blob, listed, err); err is an *ArtifactError (from
// f.fail, so isPublishWindow recognizes it) for either half of the window
// symptom: a hash mismatch, or sums listing the file while the source does
// not (yet) serve it. listed is true whenever sums names goldensAsset at all,
// independent of err, since a caller reports "not published" only when it is
// false AND there is no error.
func (f *fetcher) readGoldensOnce(ctx context.Context, sums map[string]string) (blob []byte, listed bool, err error) {
	want, listed := sums[goldensAsset]
	if !listed {
		return nil, false, nil
	}
	blob, err = f.src.readAll(ctx, goldensAsset, 8<<20)
	if err != nil {
		if errors.Is(err, errAssetNotFound) {
			return nil, true, f.fail(CodeArtifactCorrupt, "", err,
				"SHA256SUMS lists %s but %s does not serve it — the release disagrees with itself; not installing it", goldensAsset, f.src)
		}
		return nil, true, f.fail(CodeSourceUnreachable, "", err, "could not read %s from %s: %v", goldensAsset, f.src, err)
	}
	sum := sha256.Sum256(blob)
	if got := hex.EncodeToString(sum[:]); got != want {
		return nil, true, f.fail(CodeArtifactCorrupt, "", nil,
			"%s hashes to %s but the signed SHA256SUMS says %s — not installing it", goldensAsset, got, want)
	}
	return blob, true, nil
}

// installGoldens installs the served golden set, if this release publishes
// one, and never fails the fetch that called it: a release-level file here is
// best-effort.
//
// The first look reuses f.sums — the sums loadRelease already verified —
// which is cheap and right on the overwhelmingly common case that nothing is
// mid-publish. Only a disagreement is retried (the same publish-window
// symptom as the signature and index.json: docs/guides/fetch.md §3a), and a retry
// re-reads the WHOLE consistent set fresh through loadReleaseOnce — SHA256SUMS,
// its signature, index.json AND the golden set together — never the golden
// set alone checked against this call's by-then possibly-stale f.sums.
//
// A release with no such row simply predates the served set, and a mismatch
// that never heals — or a golden set that cannot be written — leaves the
// golden tests skipping loudly, which is their job when there is nothing
// trustworthy to read.
func (f *fetcher) installGoldens(ctx context.Context) {
	attempts := 1
	if f.src.remote {
		attempts = releaseLoadAttempts
	}
	delay := releaseRetryDelay
	var elapsed time.Duration
	var blob []byte
	var listed bool
	for attempt := 1; ; attempt++ {
		var err error
		if attempt == 1 {
			blob, listed, err = f.readGoldensOnce(ctx, f.sums)
		} else {
			// A stale first look: re-verify everything from scratch, not just
			// the golden set against sums that may themselves have moved on.
			f.wantGoldens = true
			f.loaded = false
			err = f.loadReleaseOnce(ctx, "")
			blob, listed = f.goldensBlob, f.goldensListed
		}
		if err == nil {
			break
		}
		retry, retryAfter, hasRetryAfter := isRetryable(err)
		if attempt >= attempts || !retry {
			f.say("%v — the golden tests will skip", err)
			return
		}
		wait := delay
		if hasRetryAfter {
			wait = retryAfter
		}
		if elapsed+wait > retryBudget() {
			f.say("%v — the source asked to wait %s before retrying, which would exceed the %s retry budget; the golden tests will skip",
				err, wait, retryBudget())
			return
		}
		f.say("%v (attempt %d/%d) — this is what a release being published (or briefly unreachable) looks like from outside; retrying in %s",
			err, attempt, attempts, wait)
		select {
		case <-ctx.Done():
			f.say("%v — the golden tests will skip", err)
			return
		case <-time.After(wait):
		}
		elapsed += wait
		delay *= 2
	}
	if !listed {
		f.say("this release does not publish %s (the SDKs' golden tests will skip until it does)", goldensAsset)
		return
	}
	if err := os.MkdirAll(f.dest, 0o755); err != nil {
		f.say("could not create %s: %v — the golden tests will skip", f.dest, err)
		return
	}
	if err := os.WriteFile(filepath.Join(f.dest, goldensAsset), blob, 0o644); err != nil {
		f.say("could not write %s: %v — the golden tests will skip", goldensAsset, err)
		return
	}
	f.say("golden set verified and installed: %s", filepath.Join(f.dest, goldensAsset))
}

// FetchAll installs every line the release publishes for the platform at
// this package's ABI revision — which lines exist is a question index.json
// answers, so a caller never restates the list. A line the release has only
// at another revision is not installed and is named in one loud warning
// line (docs/guides/fetch.md §2); the rest go on. Lines are installed in
// numeric order; the first failure stops the run and is returned with what
// was installed so far.
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
	if f.opts.Frozen {
		return f.fetchAllFrozen(ctx)
	}
	if err := f.loadRelease(ctx, "--all"); err != nil {
		return nil, err
	}
	rows, err := f.selectAll()
	if err != nil {
		return nil, err
	}
	// A line the release has only at another ABI revision is not installed —
	// and never silently: one loud line per such line, then the rest go on.
	for _, msg := range f.skippedLines(f.rowsForPlatform()) {
		f.warn("%s", msg)
	}
	var out []*Installed
	for i := range rows {
		inst, err := f.installOne(ctx, rows[i].ClickHouseMinor, "", &rows[i])
		if err != nil {
			return out, err
		}
		out = append(out, inst)
	}
	f.installGoldens(ctx)
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

// ListInstalled enumerates every installed PATCH under one registry — the
// flat line slot (<minor>/manifest.json) and every other patch
// (patches/<minor>/<exact>/manifest.json, docs/guides/fetch.md §4) — in
// numeric line, then patch, order, without hashing anything (VerifyInstalled
// does that). A registry that does not exist is simply empty.
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
		if e.Name() == patchesDirName {
			patched, err := listInstalledPatches(filepath.Join(dir, e.Name()))
			if err != nil {
				return nil, err
			}
			out = append(out, patched...)
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
	sort.Slice(out, func(i, j int) bool {
		if out[i].Line != out[j].Line {
			return lessMinor(out[i].Line, out[j].Line)
		}
		return lessVersionKey(versionKey(out[i].Version), versionKey(out[j].Version))
	})
	return out, nil
}

// listInstalledPatches reads every patches/<minor>/<exact>/manifest.json
// under patchesRoot.
func listInstalledPatches(patchesRoot string) ([]Installed, error) {
	lineEntries, err := os.ReadDir(patchesRoot)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var out []Installed
	for _, le := range lineEntries {
		if !le.IsDir() || strings.HasPrefix(le.Name(), ".") {
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
			sub := filepath.Join(lineDir, ee.Name())
			m, err := readManifest(filepath.Join(sub, "manifest.json"))
			if err != nil {
				continue
			}
			out = append(out, Installed{
				Line:          le.Name(),
				Version:       m.ClickHouseVersion,
				Platform:      m.platform(),
				Dir:           sub,
				Library:       m.Library,
				LibrarySHA256: m.LibrarySHA256,
			})
		}
	}
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
// plus an exact patch, which is a hard requirement (docs/guides/fetch.md §2). A
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
// the same rule in all four SDKs (docs/guides/fetch.md, Decisions).
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

// artifactManifest is the artifact's own record of itself (docs/reference/artifact.md).
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
	// abiRevision is the one ABI revision whose rows may be installed:
	// ABIRevision, or the test-only override (go/internal/testhook).
	abiRevision int

	loaded   bool
	index    *ReleaseIndex
	sums     map[string]string // asset file -> sha256, from the verified SHA256SUMS
	signedBy string

	// wantGoldens asks loadReleaseOnce to read and verify the served golden
	// set (goldensAsset) as part of the SAME fresh read as the signature and
	// index.json. installGoldens sets it only when its OWN cheap first look
	// (against the already-verified f.sums) disagreed and it needs a full
	// reread on retry — never on the initial Ensure/FetchAll load, and never
	// for ListRelease, so the common case pays for exactly one read of the
	// release, not two.
	wantGoldens   bool
	goldensListed bool   // true iff the verified SHA256SUMS lists goldensAsset
	goldensBlob   []byte // the verified bytes, once loadRelease has succeeded
}

func newFetcher(opts FetchOptions) (*fetcher, error) {
	f := &fetcher{opts: opts, abiRevision: fetchABIRevision()}
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

// warn is never silent: the one loud warning docs/guides/fetch.md §4 requires.
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

// ensureOffline is the no-network path: installed and hashing what its own
// manifest says is an answer; anything else is unreachable. It never reads
// the lock, under --frozen or not (docs/guides/fetch.md §6): offline's
// verdict depends only on what is on disk.
func (f *fetcher) ensureOffline(spelling, line, exact string) (*Installed, error) {
	if f.opts.Force {
		return nil, f.fail(CodeSourceUnreachable, spelling, nil,
			"offline: --force needs a download, and --offline forbids reading %s", f.src)
	}
	for _, dir := range f.offlineCandidates(line, exact) {
		m, err := readManifest(filepath.Join(dir, "manifest.json"))
		if err != nil {
			continue
		}
		if exact != "" && !exactMatches(m.ClickHouseVersion, exact) {
			continue
		}
		got, herr := fileSHA256(filepath.Join(dir, m.Library))
		if herr != nil {
			continue
		}
		if got == strings.ToLower(m.LibrarySHA256) {
			f.say("already installed and verified against its manifest (offline): %s", filepath.Join(dir, m.Library))
			if f.opts.Frozen {
				f.note("offline: the lock file was not re-checked against a release; the installed manifest verified")
			}
			return &Installed{
				Line: line, Version: m.ClickHouseVersion, Platform: m.platform(), Dir: dir,
				Library: m.Library, LibrarySHA256: got, AlreadyInstalled: true,
			}, nil
		}
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil,
			"offline: installed %s hashes %s, its manifest says %s, and --offline forbids re-fetching it",
			filepath.Join(dir, m.Library), got, m.LibrarySHA256)
	}
	return nil, f.fail(CodeSourceUnreachable, spelling, nil,
		"offline: ClickHouse %s is not installed in %s, and --offline forbids reading %s", spelling, f.dest, f.src)
}

// offlineCandidates is where an offline Ensure looks, in order. Ensure is a
// hard requirement (R5): a patch spelling is satisfied only by that exact
// patch (R2) — the flat slot when it happens to hold it, else its own
// patches/ slot — never by a same-line fallback, which only the Registry's
// resolution takes. A line spelling is satisfied by the newest patch
// installed anywhere in f.dest (flat or patches/), since a line request has
// no "exact" to miss.
func (f *fetcher) offlineCandidates(line, exact string) []string {
	all := patchesInDir(f.dest, line)
	if exact != "" {
		var dirs []string
		for _, p := range all {
			if exactMatches(p.version, exact) {
				dirs = append(dirs, p.dir)
			}
		}
		return dirs
	}
	if len(all) == 0 {
		return []string{filepath.Join(f.dest, line)}
	}
	sort.Slice(all, func(i, j int) bool { return lessVersionKey(versionKey(all[i].version), versionKey(all[j].version)) })
	dirs := make([]string, len(all))
	for i, p := range all {
		dirs[len(all)-1-i] = p.dir
	}
	return dirs
}

// loadReleaseOnce reads the release's three small files once and runs steps 0
// and 1: the signature over SHA256SUMS, then the index.
func (f *fetcher) loadReleaseOnce(ctx context.Context, line string) error {
	if f.loaded {
		return nil
	}
	f.goldensListed = false
	f.goldensBlob = nil
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
	// The served golden set, read as part of the SAME set as the three
	// objects above — only when installGoldens asked for a fresh reread
	// (f.wantGoldens), which it does on a retry, never on its own first,
	// cheap look at the already-verified f.sums. It is a row in SHA256SUMS
	// exactly like a tarball, so a stale pairing of it against the sums (or
	// against nothing, if the sums row landed before the file itself is
	// visible) is the same publish window here too, and readGoldensOnce
	// raises the same *ArtifactError codes isPublishWindow already knows.
	if f.wantGoldens {
		blob, listed, err := f.readGoldensOnce(ctx, f.sums)
		f.goldensListed = listed
		f.goldensBlob = blob
		if err != nil {
			f.loaded = false
			return err
		}
	}
	return nil
}

// How many times loadRelease reads a remote release before giving up, and the
// base delay before the first retry: each subsequent retry doubles it, so the
// default 5 attempts sleep 4+8+16+32 = 60s (~70s wall with network time) —
// enough to outlast the artifacts host's edge cache (observed max-age=60 on
// the mutable release objects, so a stale pairing can persist up to 60s),
// while a genuine few-second mid-publish window still clears on the second
// attempt. Vars, not consts, so the retry tests can exercise a real window
// without a real wait.
var (
	releaseLoadAttempts = 5
	releaseRetryDelay   = 4 * time.Second
)

// loadRelease is loadReleaseOnce, retried through a publish window. It is the
// only caller during Ensure/FetchAll's initial load (wantGoldens unset —
// that is the cheap, common case); installGoldens runs the SAME loop shape
// itself, directly around loadReleaseOnce, only when its own first look
// disagreed and wantGoldens needs to be set for the reread.
//
// A publish into the rolling release is three objects — SHA256SUMS,
// SHA256SUMS.sig, index.json — plus, on a goldens reread, a fourth
// release-level file (the golden set) — and object storage cannot swap them
// atomically. The edge cache in front of the artifacts host widens the unsafe
// window from "between two uploads" to "as long as any one object can still
// be served stale from cache", which measures the same as its Cache-Control
// max-age.
//
// Three symptoms of reading inside that window are retried: a signature that
// verifies under no trusted key, an index that disagrees with the sums, and a
// release-level file whose hash disagrees with its own SHA256SUMS row (or that
// the sums list but the source does not yet serve — a new row can land before
// the file it describes is visible). On every retry the WHOLE set is read
// again from scratch — never one freshly re-fetched object checked against
// another attempt's stale one. Nothing else is retried, and neither are these
// three once the attempts run out: the same error surfaces with the same code
// and exit status as before. A tarball whose hash is wrong is never retried;
// that is the release lying about a byte, not a half-finished upload.
//
// Only a remote source can be mid-publish, so a directory or file:// source is
// read exactly once and refuses on the first look.
func (f *fetcher) loadRelease(ctx context.Context, line string) error {
	attempts := 1
	if f.src.remote {
		attempts = releaseLoadAttempts
	}
	delay := releaseRetryDelay
	var elapsed time.Duration
	for attempt := 1; ; attempt++ {
		err := f.loadReleaseOnce(ctx, line)
		if err == nil {
			return nil
		}
		retry, retryAfter, hasRetryAfter := isRetryable(err)
		if !retry {
			return err
		}
		if attempt >= attempts {
			return withRetryNote(err, fmt.Sprintf(" — giving up after %s", attemptWord(attempt)))
		}
		wait := delay
		if hasRetryAfter {
			wait = retryAfter
		}
		if elapsed+wait > retryBudget() {
			return withRetryNote(err, fmt.Sprintf(
				" — the source asked to wait %s before retrying, which would exceed the %s retry budget; giving up after %s",
				wait, retryBudget(), attemptWord(attempt)))
		}
		f.say("%v (attempt %d/%d) — this is what a release being published (or briefly unreachable) looks like from outside; retrying in %s",
			err, attempt, attempts, wait)
		select {
		case <-ctx.Done():
			return err
		case <-time.After(wait):
		}
		elapsed += wait
		delay *= 2
	}
}

// isRetryable reports whether err is worth retrying through the §3a budget —
// either a publish-window symptom (the signature or an index/sums/release-
// file disagreement, as before) or, since chtypes#365, a retryable
// source-level failure: an HTTP 5xx/408/429 or a connection-level failure.
// retryAfter/hasRetryAfter carry the server's own requested wait, when the
// failure came with one (only 503 and 429 ever do); they are always
// zero/false for a publish-window symptom, which never comes with one.
//
// A tarball hash or size mismatch is never routed through this: it is
// CodeArtifactCorrupt too, but installOne raises it directly, never wrapping
// a *retryableSourceError, and nothing here is called on it — "a retry buys
// time; it never converts a refusal into an install" (§3a) still holds.
func isRetryable(err error) (ok bool, retryAfter time.Duration, hasRetryAfter bool) {
	var ae *ArtifactError
	if errors.As(err, &ae) && (ae.Code == CodeArtifactUntrusted || ae.Code == CodeArtifactCorrupt) {
		return true, 0, false
	}
	var rs *retryableSourceError
	if errors.As(err, &rs) {
		return true, rs.retryAfter, rs.hasRetryAfter
	}
	return false, 0, false
}

// retryBudget is the total sleep time the default doubling schedule spends
// across every attempt but the last — 4+8+16+32 = 60s for the default 5
// attempts (docs/guides/fetch.md §3a) — and the cap chtypes#365 puts on a
// server's own Retry-After: honored only as long as honoring it still fits
// inside this, so a 503 that asks for far longer than the publish-window
// retry was ever sized for fails fast instead of blocking for it.
func retryBudget() time.Duration {
	if releaseLoadAttempts <= 1 {
		return 0
	}
	return releaseRetryDelay * time.Duration((int64(1)<<(releaseLoadAttempts-1))-1)
}

// attemptWord is "1 attempt" or "<n> attempts", for a message that names how
// many were made.
func attemptWord(n int) string {
	if n == 1 {
		return "1 attempt"
	}
	return fmt.Sprintf("%d attempts", n)
}

// withRetryNote appends a sentence to a retryable *ArtifactError's message,
// before its trailing " [CODE]", without disturbing its Code, Line, Platform,
// Source or wrapped cause — errors.Is/As and ExitCode all still see the same
// error they always would. err that is not an *ArtifactError (should not
// happen for anything isRetryable ever returns true for) is returned as-is.
func withRetryNote(err error, note string) error {
	var ae *ArtifactError
	if !errors.As(err, &ae) {
		return err
	}
	suffix := " [" + string(ae.Code) + "]"
	base := strings.TrimSuffix(ae.Msg, suffix)
	return &ArtifactError{Code: ae.Code, Line: ae.Line, Platform: ae.Platform, Source: ae.Source, Err: ae.Err, Msg: base + note + suffix}
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

// fetchABIRevision is the revision fetch selects rows at: this package's own
// ABIRevision, unless a fetch-fixture suite set the test-only override.
func fetchABIRevision() int {
	if n := testhook.FetchABIRevision; n != 0 {
		return n
	}
	return ABIRevision
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

// atRevision keeps only the rows at the fetcher's ABI revision. This is the
// FIRST rule of selection (docs/guides/fetch.md §2): a row of any other
// revision, or one that declares none, is never installed and never a
// fallback — the loader would refuse it.
func (f *fetcher) atRevision(rows []ReleaseArtifact) []ReleaseArtifact {
	var out []ReleaseArtifact
	for _, a := range rows {
		if a.atRevision(f.abiRevision) {
			out = append(out, a)
		}
	}
	return out
}

// noRecordedRevision names rows that carry no abi_revision, and why: the
// artifact producer records the field from the revision that introduced it
// onward. Every unpublished message, --all warning and list note says this
// in these words rather than that the release serves "none".
const noRecordedRevision = "rows that record no ABI revision (built before revisions were recorded)"

// servedRevisions says, for an unpublished message, what the release DOES
// have for noun: the ABI revision(s) its rows carry, or nothing at all.
func servedRevisions(rows []ReleaseArtifact, noun string) string {
	if len(rows) == 0 {
		return "the release does not have " + noun + " at any ABI revision"
	}
	seen := map[int]bool{}
	var revs []int
	undeclared := false
	for _, a := range rows {
		if a.ABIRevision == nil {
			undeclared = true
			continue
		}
		if !seen[*a.ABIRevision] {
			seen[*a.ABIRevision] = true
			revs = append(revs, *a.ABIRevision)
		}
	}
	if len(revs) == 0 {
		return "the release has " + noun + " only in " + noRecordedRevision
	}
	sort.Ints(revs)
	said := "ABI revision " + strconv.Itoa(revs[0])
	if len(revs) > 1 {
		parts := make([]string, len(revs))
		for i, r := range revs {
			parts[i] = strconv.Itoa(r)
		}
		said = "ABI revisions " + strings.Join(parts, ", ")
	}
	if undeclared {
		said += " and in " + noRecordedRevision
	}
	return "the release has " + noun + " only at " + said
}

// skippedLines names every line the release has for the platform only at
// another ABI revision, or only in rows that record none — the lines --all
// installs nothing for — one message each, in numeric line order.
func (f *fetcher) skippedLines(all []ReleaseArtifact) []string {
	byLine := map[string][]ReleaseArtifact{}
	for _, a := range all {
		byLine[a.ClickHouseMinor] = append(byLine[a.ClickHouseMinor], a)
	}
	var lines []string
	for line, rows := range byLine {
		if len(f.atRevision(rows)) == 0 {
			lines = append(lines, line)
		}
	}
	sort.Slice(lines, func(i, j int) bool { return lessMinor(lines[i], lines[j]) })
	out := make([]string, 0, len(lines))
	for _, line := range lines {
		out = append(out, fmt.Sprintf("ClickHouse line %s on %s is not installed: %s, and this SDK speaks ABI revision %d",
			line, f.platform, servedRevisions(byLine[line], "that line for "+f.platform), f.abiRevision))
	}
	return out
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
	if len(out) == 0 {
		return "nothing"
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

// selectArtifact picks the one row for (platform, line|exact): first the rows
// at the fetcher's ABI revision, then among those the newest version and the
// highest build (docs/guides/fetch.md §2).
func (f *fetcher) selectArtifact(spelling, line, exact string) (*ReleaseArtifact, error) {
	all := f.rowsForPlatform()
	if len(all) == 0 {
		return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
			"the release at %s has nothing for %s (it has: %s)", f.src, f.platform, f.platformsOffered())
	}
	rows := f.atRevision(all)
	var hits, anyRevision []ReleaseArtifact
	if exact != "" {
		for _, a := range all {
			if exactMatches(a.ClickHouseVersion, exact) {
				anyRevision = append(anyRevision, a)
				if a.atRevision(f.abiRevision) {
					hits = append(hits, a)
				}
			}
		}
		if len(hits) == 0 {
			return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
				"you asked for exactly ClickHouse %s on %s at ABI revision %d (this SDK's) and the release at %s does not publish it at that revision: %s; at ABI revision %d the release has: %s. Ask for the line (%s) to take what was published.",
				exact, f.platform, f.abiRevision, f.src, servedRevisions(anyRevision, "that patch for "+f.platform),
				f.abiRevision, versionsOf(rows), line)
		}
	} else {
		for _, a := range all {
			if a.ClickHouseMinor == line {
				anyRevision = append(anyRevision, a)
				if a.atRevision(f.abiRevision) {
					hits = append(hits, a)
				}
			}
		}
		if len(hits) == 0 {
			return nil, f.fail(CodeArtifactUnpublished, spelling, nil,
				"no artifact for ClickHouse line %s on %s at ABI revision %d (this SDK's) at %s: %s; at ABI revision %d the release has: %s.",
				line, f.platform, f.abiRevision, f.src, servedRevisions(anyRevision, "that line for "+f.platform),
				f.abiRevision, versionsOf(rows))
		}
	}
	// A line can carry more than one row: two patches, or the same patch built
	// twice (the release lists every build it publishes). Take the newest by
	// version and then by build, never by list order.
	sort.SliceStable(hits, func(i, j int) bool {
		return newerRow(hits[j], hits[i]) // ascending: the last is the one to take
	})
	a := hits[len(hits)-1]
	if err := checkRow(&a); err != nil {
		return nil, f.fail(CodeArtifactCorrupt, spelling, nil, "index.json entry for %s is missing %v", a.File, err)
	}
	return &a, nil
}

// selectAll picks one row per minor line for the platform, among the rows at
// the fetcher's ABI revision: newest patch, then highest build, per line, in
// numeric line order.
func (f *fetcher) selectAll() ([]ReleaseArtifact, error) {
	all := f.rowsForPlatform()
	if len(all) == 0 {
		return nil, f.fail(CodeArtifactUnpublished, "--all", nil,
			"the release at %s has nothing for %s (it has: %s)", f.src, f.platform, f.platformsOffered())
	}
	rows := f.atRevision(all)
	if len(rows) == 0 {
		return nil, f.fail(CodeArtifactUnpublished, "--all", nil,
			"the release at %s has nothing for %s at ABI revision %d (this SDK's): %s",
			f.src, f.platform, f.abiRevision, servedRevisions(all, "rows for "+f.platform))
	}
	best := map[string]ReleaseArtifact{}
	for _, a := range rows {
		cur, ok := best[a.ClickHouseMinor]
		if !ok || newerRow(a, cur) {
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

// lockPath is the file --frozen enforces or a plain fetch records into:
// LockFile, or DefaultLockFile when none was given.
func (f *fetcher) lockPath() string {
	if f.opts.LockFile != "" {
		return f.opts.LockFile
	}
	return DefaultLockFile
}

// recordLock writes the §5 entry when a lock file was asked for (never
// under --frozen, which is read-only), keyed by the EXACT patch that
// installed (schema 2, #284), including the ABI revision the selected row
// carries — this binding's own, since selection never picks any other
// (docs/guides/fetch.md §2). It adds or replaces one entry and removes
// nothing else already in the file.
func (f *fetcher) recordLock(a *ReleaseArtifact) error {
	if f.opts.LockFile == "" || f.opts.Frozen {
		return nil
	}
	l, err := readOrNewLockFile(f.opts.LockFile)
	if err != nil {
		return err
	}
	key := LockKey(f.platform, a.ClickHouseVersion)
	rev := f.abiRevision
	l.Artifacts[key] = LockEntry{File: a.File, SHA256: strings.ToLower(a.SHA256), ABIRevision: &rev}
	if err := l.Write(f.opts.LockFile); err != nil {
		return fmt.Errorf("chtypes: writing %s: %w", f.opts.LockFile, err)
	}
	f.say("pinned %s -> %s (ABI revision %d) in %s", key, a.File, rev, f.opts.LockFile)
	return nil
}

// ------------------------------------------------------------- §6 F1–F7:
// --frozen selects from the LOCK, never from the release's unpinned rows.

// frozenCandidate is one lock entry that could satisfy a frozen request:
// its full key, the exact patch it pins, and the pin itself.
type frozenCandidate struct {
	key     string
	version string
	entry   LockEntry
}

// revisionNote282 is the one sentence appended to a PINNED or UNPUBLISHED
// message when the candidate's own lock entry names no ABI revision at all
// — written by an SDK before #253. Never silently accepted as a pass: it is
// either a real drift or a stale revision wearing an older lock's shape,
// and re-locking is the remedy either way. "" when the entry does carry one.
func (f *fetcher) revisionNote282(lockPath, version string, e LockEntry) string {
	if e.ABIRevision != nil {
		return ""
	}
	return fmt.Sprintf(" %s records no ABI revision (written by an older SDK); this SDK speaks ABI revision %d — re-lock with: %s %s --lock %s",
		lockPath, f.abiRevision, GoFetchCommand, version, lockPath)
}

// checkCandidateRevision is F2: every candidate's own ABI revision is
// checked before the release is ever read, so a lock made for a revision
// this binding no longer speaks is named as that — never as a drifted pin
// or an unpublished patch once selection runs.
func (f *fetcher) checkCandidateRevision(lockPath string, c frozenCandidate) error {
	if c.entry.ABIRevision == nil || *c.entry.ABIRevision == f.abiRevision {
		return nil
	}
	return f.fail(CodeArtifactPinned, c.version, nil,
		"%s pins %s at ABI revision %d; this SDK speaks ABI revision %d — re-lock with: %s %s --lock %s",
		lockPath, c.key, *c.entry.ABIRevision, f.abiRevision, GoFetchCommand, c.version, lockPath)
}

// newestFrozenCandidate picks the highest-version entry of cands — used
// both for a single (platform, line) group (the newest patch the lock
// pins for that line) and, trivially, for an exact-patch match (there is
// only ever one candidate under R2).
func newestFrozenCandidate(cands []frozenCandidate) frozenCandidate {
	best := cands[0]
	for _, c := range cands[1:] {
		if lessVersionKey(versionKey(best.version), versionKey(c.version)) {
			best = c
		}
	}
	return best
}

// frozenCandidates is F1: the lock's own entries for this platform that
// could satisfy spelling — every entry whose key's exact patch matches
// under R2 for a patch spelling, or whose line equals line for a line
// spelling.
func (f *fetcher) frozenCandidates(l *LockFile, line, exact string) []frozenCandidate {
	prefix := f.platform + "/"
	var out []frozenCandidate
	for key, entry := range l.Artifacts {
		if !strings.HasPrefix(key, prefix) {
			continue
		}
		version := strings.TrimPrefix(key, prefix)
		if exact != "" {
			if !exactMatches(version, exact) {
				continue
			}
		} else if minorOf(version) != line {
			continue
		}
		out = append(out, frozenCandidate{key: key, version: version, entry: entry})
	}
	return out
}

// verifyFrozenCandidate is F5: the candidate's own file, looked up in BOTH
// the platform's index.json rows and the verified SHA256SUMS. The release
// must already have been loaded (f.loadRelease) before this runs.
func (f *fetcher) verifyFrozenCandidate(lockPath string, c frozenCandidate) (*ReleaseArtifact, error) {
	var row *ReleaseArtifact
	for i := range f.index.Artifacts {
		a := &f.index.Artifacts[i]
		if a.Platform() == f.platform && a.File == c.entry.File {
			row = a
			break
		}
	}
	sumsSHA, listed := f.sums[c.entry.File]
	if row == nil || !listed {
		return nil, f.fail(CodeArtifactUnpublished, c.version, nil,
			"the release at %s does not list %s, which %s pins for %s.%s",
			f.src, c.entry.File, lockPath, c.key, f.revisionNote282(lockPath, c.version, c.entry))
	}
	if !row.atRevision(f.abiRevision) {
		return nil, f.fail(CodeArtifactPinned, c.version, nil,
			"%s pins %s (%s), and the release's row for it is not at ABI revision %d (this SDK's).%s",
			lockPath, c.key, c.entry.File, f.abiRevision, f.revisionNote282(lockPath, c.version, c.entry))
	}
	if sumsSHA != strings.ToLower(row.SHA256) {
		return nil, f.fail(CodeArtifactCorrupt, c.version, nil,
			"index.json says %s is %s but SHA256SUMS says %s — the release disagrees with itself; not installing it",
			c.entry.File, row.SHA256, sumsSHA)
	}
	if !strings.EqualFold(c.entry.SHA256, row.SHA256) {
		return nil, f.fail(CodeArtifactPinned, c.version, nil,
			"%s pins %s to %s (%s) but the release's row for it is %s. Refusing it under --frozen.%s",
			lockPath, c.key, c.entry.File, c.entry.SHA256, row.SHA256, f.revisionNote282(lockPath, c.version, c.entry))
	}
	return row, nil
}

// readFrozenLock is F3's lock-not-found half: a missing lock file is
// CHTYPES_ARTIFACT_PINNED, before the release is ever read.
func (f *fetcher) readFrozenLock(spelling string) (*LockFile, error) {
	path := f.lockPath()
	l, err := ReadLockFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, f.fail(CodeArtifactPinned, spelling, err, "--frozen, but there is no lock file at %s", path)
		}
		return nil, f.fail(CodeArtifactPinned, spelling, err, "--frozen: %v", err)
	}
	return l, nil
}

// ensureFrozen is Ensure's --frozen path (F1–F5, F7): the lock is the
// candidate set in place of the release's rows, its revision is checked
// before the release is read, and the chosen row installs exactly as
// pinned — never refused merely because the release now offers something
// newer.
func (f *fetcher) ensureFrozen(ctx context.Context, spelling, line, exact string) (*Installed, error) {
	lockPath := f.lockPath()
	l, err := f.readFrozenLock(spelling)
	if err != nil {
		return nil, err
	}
	candidates := f.frozenCandidates(l, line, exact)
	if len(candidates) == 0 {
		return nil, f.fail(CodeArtifactPinned, spelling, nil, "%s pins nothing for %s/%s", lockPath, f.platform, spelling)
	}
	for _, c := range candidates {
		if err := f.checkCandidateRevision(lockPath, c); err != nil {
			return nil, err
		}
	}
	if err := f.loadRelease(ctx, spelling); err != nil {
		return nil, err
	}
	chosen := newestFrozenCandidate(candidates)
	a, err := f.verifyFrozenCandidate(lockPath, chosen)
	if err != nil {
		return nil, err
	}
	inst, err := f.installOne(ctx, spelling, exact, a)
	if err != nil {
		return nil, err
	}
	f.installGoldens(ctx)
	return inst, nil
}

// fetchAllFrozen is FetchAll's --frozen path (F6): the newest pinned patch
// of every line the lock pins for the platform. A line the release has but
// the lock does not pin is not installed, and is named in one progress
// note — a new line is not drift in anything that was pinned.
func (f *fetcher) fetchAllFrozen(ctx context.Context) ([]*Installed, error) {
	lockPath := f.lockPath()
	l, err := f.readFrozenLock("--all")
	if err != nil {
		return nil, err
	}
	prefix := f.platform + "/"
	byLine := map[string][]frozenCandidate{}
	for key, entry := range l.Artifacts {
		if !strings.HasPrefix(key, prefix) {
			continue
		}
		version := strings.TrimPrefix(key, prefix)
		line := minorOf(version)
		byLine[line] = append(byLine[line], frozenCandidate{key: key, version: version, entry: entry})
	}
	if len(byLine) == 0 {
		return nil, f.fail(CodeArtifactPinned, "--all", nil, "%s pins nothing for %s", lockPath, f.platform)
	}
	var lines []string
	for line := range byLine {
		lines = append(lines, line)
	}
	sort.Slice(lines, func(i, j int) bool { return lessMinor(lines[i], lines[j]) })
	for _, line := range lines {
		for _, c := range byLine[line] {
			if err := f.checkCandidateRevision(lockPath, c); err != nil {
				return nil, err
			}
		}
	}
	if err := f.loadRelease(ctx, "--all"); err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	for _, a := range f.rowsForPlatform() {
		seen[a.ClickHouseMinor] = true
	}
	var notPinned []string
	for line := range seen {
		if _, ok := byLine[line]; !ok {
			notPinned = append(notPinned, line)
		}
	}
	sort.Slice(notPinned, func(i, j int) bool { return lessMinor(notPinned[i], notPinned[j]) })
	for _, line := range notPinned {
		f.say("%s: the release has it, but %s does not pin it for %s — --all --frozen does not install it", line, lockPath, f.platform)
	}
	var out []*Installed
	for _, line := range lines {
		chosen := newestFrozenCandidate(byLine[line])
		a, err := f.verifyFrozenCandidate(lockPath, chosen)
		if err != nil {
			return out, err
		}
		inst, err := f.installOne(ctx, line, "", a)
		if err != nil {
			return out, err
		}
		out = append(out, inst)
	}
	f.installGoldens(ctx)
	f.say("%d version(s) installed into %s", len(out), f.dest)
	return out, nil
}

// alreadyInstalledAt reports whether dir already holds wantLib, verified
// against its own manifest — the test the install path ends with, so
// "already there" is a verified claim, not an assumption from a file name.
func alreadyInstalledAt(dir, wantLib string) bool {
	m, err := readManifest(filepath.Join(dir, "manifest.json"))
	if err != nil {
		return false
	}
	got, err := fileSHA256(filepath.Join(dir, m.Library))
	return err == nil && got == wantLib
}

// openTarball opens one tarball, retrying a 5xx/408/429 or a connection-level
// failure through the same docs/guides/fetch.md §3a budget and schedule
// loadRelease uses (chtypes#365) — a transient blip reaching the host mid
// download is exactly the symptom that budget exists for. It is a fresh
// open() each attempt, never a resume: the previous attempt's partial bytes
// are simply overwritten. A 404/410 (errAssetNotFound) and anything else not
// isRetryable fail at once, as they always have.
func (f *fetcher) openTarball(ctx context.Context, spelling, file string) (io.ReadCloser, error) {
	attempts := 1
	if f.src.remote {
		attempts = releaseLoadAttempts
	}
	delay := releaseRetryDelay
	var elapsed time.Duration
	for attempt := 1; ; attempt++ {
		rc, err := f.src.open(ctx, file)
		if err == nil {
			return rc, nil
		}
		wrapped := f.fail(CodeSourceUnreachable, spelling, err, "could not download %s from %s: %v", file, f.src, err)
		retry, retryAfter, hasRetryAfter := isRetryable(err)
		if !retry {
			return nil, wrapped
		}
		if attempt >= attempts {
			return nil, withRetryNote(wrapped, fmt.Sprintf(" — giving up after %s", attemptWord(attempt)))
		}
		wait := delay
		if hasRetryAfter {
			wait = retryAfter
		}
		if elapsed+wait > retryBudget() {
			return nil, withRetryNote(wrapped, fmt.Sprintf(
				" — the source asked to wait %s before retrying, which would exceed the %s retry budget; giving up after %s",
				wait, retryBudget(), attemptWord(attempt)))
		}
		f.say("%v (attempt %d/%d) — retrying the download of %s in %s", err, attempt, attempts, file, wait)
		select {
		case <-ctx.Done():
			return nil, wrapped
		case <-time.After(wait):
		}
		elapsed += wait
		delay *= 2
	}
}

// installOne walks steps 2–4 for one selected row, installing it at the
// layout the lead's amendment fixes (docs/guides/fetch.md §4, superseded):
// the patch a LINE spelling selects (exact=="") installs FLAT at
// <dest>/<minor>/; any OTHER exact patch (exact!="") installs at
// <dest>/patches/<minor>/<its clickhouse_version>/, and never touches the
// flat slot. When a line fetch replaces a DIFFERENT patch that was sitting
// in the flat slot, the outgoing install is DEMOTED — renamed into
// patches/, never deleted — before the incoming one takes the slot.
func (f *fetcher) installOne(ctx context.Context, spelling, exact string, a *ReleaseArtifact) (*Installed, error) {
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

	flatDir := filepath.Join(f.dest, a.ClickHouseMinor)
	installDir := flatDir
	if exact != "" {
		installDir = filepath.Join(f.dest, patchesDirName, a.ClickHouseMinor, a.ClickHouseVersion)
	}
	result := &Installed{
		Line: a.ClickHouseMinor, Version: a.ClickHouseVersion, Platform: f.platform, Dir: installDir,
		Library: a.Library, LibrarySHA256: wantLib, File: a.File, SHA256: wantSHA, SignedBy: f.signedBy,
	}

	if !f.opts.Force {
		// An exact request already sitting in the flat slot needs no
		// separate patches/ copy — no re-fetch, no duplicated bytes.
		if exact != "" && alreadyInstalledAt(flatDir, wantLib) {
			result.Dir = flatDir
			result.AlreadyInstalled = true
			f.say("already installed and verified (in the flat line slot): %s", filepath.Join(flatDir, a.Library))
			if err := f.recordLock(a); err != nil {
				return nil, err
			}
			return result, nil
		}
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

	// A line install that is about to replace a DIFFERENT patch in the flat
	// slot demotes it FIRST (before the download even starts is fine — the
	// demotion only has to land before the incoming patch takes the slot,
	// and computing it early keeps the rest of this function one straight
	// line). "" when there is nothing to demote (nothing installed, or the
	// same version already handled above).
	var demoteOldVersion string
	if exact == "" {
		if m, err := readManifest(filepath.Join(flatDir, "manifest.json")); err == nil && m.ClickHouseVersion != a.ClickHouseVersion {
			demoteOldVersion = m.ClickHouseVersion
		}
	}

	// Step 3: the tarball, hashed before it is unpacked.
	if err := os.MkdirAll(filepath.Dir(installDir), 0o755); err != nil {
		return nil, fmt.Errorf("chtypes: %w", err)
	}
	pid := strconv.Itoa(os.Getpid())
	partial := filepath.Join(f.dest, "."+a.File+".partial."+pid)
	defer os.Remove(partial)
	f.say("downloading %s", a.File)
	rc, err := f.openTarball(ctx, spelling, a.File)
	if err != nil {
		return nil, err
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
	incoming, err := os.MkdirTemp(filepath.Dir(installDir), ".incoming.")
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

	if demoteOldVersion != "" {
		if err := f.demoteFlatInstall(flatDir, a.ClickHouseMinor, demoteOldVersion); err != nil {
			return nil, err
		}
	}

	replaced := filepath.Join(filepath.Dir(installDir), ".replaced."+pid)
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
	if exact == "" {
		f.note("    ClickHouse %s — chtypes.NewRegistry(%q) will now serve %s", m.ClickHouseVersion, f.dest, a.ClickHouseMinor)
	} else {
		f.note("    ClickHouse %s — resolves exactly from chtypes.NewRegistry(%q)", m.ClickHouseVersion, f.dest)
	}
	result.Platform = m.platform()
	if result.Platform == "" {
		result.Platform = f.platform
	}
	if err := f.recordLock(a); err != nil {
		return nil, err
	}
	return result, nil
}

// demoteFlatInstall renames the flat slot's CURRENT install — reported to
// be oldVersion by its own manifest — into
// patches/<minor>/<oldVersion>/, atomically and on the same filesystem,
// before the incoming patch takes the flat slot (the lead's amendment:
// "demote, never delete"). If that destination already holds a patch
// (installed earlier, directly, as an exact fetch, or by a previous
// demotion), the two are byte-identical by construction, so the now
// redundant flat copy is simply removed instead of failing the whole
// install over a directory that already exists.
func (f *fetcher) demoteFlatInstall(flatDir, minor, oldVersion string) error {
	demoteTo := filepath.Join(f.dest, patchesDirName, minor, oldVersion)
	if _, err := os.Stat(demoteTo); err == nil {
		return os.RemoveAll(flatDir)
	}
	if err := os.MkdirAll(filepath.Dir(demoteTo), 0o755); err != nil {
		return fmt.Errorf("chtypes: %w", err)
	}
	if err := os.Rename(flatDir, demoteTo); err != nil {
		return fmt.Errorf("chtypes: demoting the outgoing ClickHouse %s (%s) to %s: %w", oldVersion, flatDir, demoteTo, err)
	}
	return nil
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
