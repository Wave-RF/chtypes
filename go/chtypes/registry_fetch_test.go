package chtypes

// registry_fetch_test.go — Registry.Fetch, the fetch-only call (public issue
// #492): it installs a build without opening it, and a concurrent Fetch and
// open of one request share one fetch.
//
// The build served here is a signed artifact whose "library" is not a library
// at all, so nothing could load it: a Fetch that tried to open it would fail.
// No stub is needed, and none of these tests skips. The verdicts come from
// what the fixture server logged and what each call returned; the waits are on
// events (the server's parked count, the registry's onFetchWait hook), never on
// a clock.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// fetchCases are the fixture server's case ids for these tests.
var fetchCases = []string{"fetch-installs", "fetch-shared", "fetch-unpublished"}

// notALibrary is the served build's library bytes: no loader accepts them.
var notALibrary = []byte("chtypes registry fetch test: these bytes are not a loadable library\n")

// newFetchFixture serves notALibrary as tag 26.8, signed with the test key
// for this SDK's own fingerprint, under every case in fetchCases.
func newFetchFixture(t *testing.T) *flightFixture {
	t.Helper()
	resetSetup(t)
	platformKey := runtime.GOOS + "-" + runtime.GOARCH
	known := false
	for _, p := range ocifetch.Platforms {
		known = known || p.Key == platformKey
	}
	if !known {
		t.Skipf("SKIPPED: this host (%s) is not a chtypes platform; the registry fetch tests did not run", platformKey)
	}
	// The test key and the fixture server live beside go/ in a checkout; the
	// bare standalone copy (scripts/check-standalone.sh) has neither.
	for _, need := range []string{
		filepath.Join(repoRootDir(t), "tests", "fixtures", "fetch-v1", "test-key", "private.pem"),
		filepath.Join(repoRootDir(t), "scripts", "fetch-v1", "server.py"),
	} {
		if _, err := os.Stat(need); err != nil {
			t.Skipf("SKIPPED: %s is not beside this checkout (%v); the registry fetch tests did not run", need, err)
		}
	}
	sum := sha256.Sum256(notALibrary)
	predicate := map[string]any{
		"abi": abi2.ChsAbiVersion, "abi_fingerprint": abi2.ChsAbiFingerprint,
		"clickhouse_version": "26.8.15.10", "clickhouse_minor": "26.8", "channel": "lts", "build": "20261001.183455",
		"os": runtime.GOOS, "arch": runtime.GOARCH,
		"library": hostLibName(), "library_sha256": hex.EncodeToString(sum[:]), "library_bytes": len(notALibrary),
	}
	fixtures := t.TempDir()
	layer := writeRouteTree(t, filepath.Join(fixtures, "trees", "fetch", "v2", "chtypes", "v1"), "26.8", hostLibName(), platformKey, notALibrary, predicate)
	cases := make([]map[string]string, 0, len(fetchCases))
	for _, id := range fetchCases {
		cases = append(cases, map[string]string{"id": id, "tree": "fetch"})
	}
	if err := os.WriteFile(filepath.Join(fixtures, "cases.json"), mustJSON(t, map[string]any{"schema": 1, "cases": cases}), 0o644); err != nil {
		t.Fatal(err)
	}
	_, trusted := loadTestKey(t)
	return &flightFixture{
		origin:    startFlightServer(t, fixtures),
		cache:     t.TempDir(),
		trusted:   trusted,
		layerPath: "/chtypes/v1/blobs/" + string(layer),
	}
}

// TestRegistryFetchInstallsWithoutOpening: Fetch installs the build and
// returns its record, and opens nothing: the library it installed cannot be
// loaded, no Library exists, and the process setup is never latched.
func TestRegistryFetchInstallsWithoutOpening(t *testing.T) {
	fx := newFetchFixture(t)
	reg := fx.registry(t, "fetch-installs")
	res, err := reg.Fetch(t.Context(), "26.8")
	if err != nil {
		t.Fatalf("Fetch(26.8) = %v", err)
	}
	if res.Version != "26.8.15.10" || res.Build != "20261001.183455" || res.Request != "26.8" {
		t.Errorf("Fetch(26.8) = version %q build %q request %q", res.Version, res.Build, res.Request)
	}
	if !res.Digests.Manifest.Valid() || filepath.Base(res.Dir) != res.Digests.Manifest.Hex() {
		t.Errorf("Fetch(26.8): manifest %q, dir %q", res.Digests.Manifest, res.Dir)
	}
	got, err := os.ReadFile(res.LibraryPath)
	if err != nil || string(got) != string(notALibrary) {
		t.Fatalf("the installed library at %s = %q, %v", res.LibraryPath, got, err)
	}
	if libs := reg.Libraries(); len(libs) != 0 {
		t.Errorf("Fetch opened %d Libraries, want none", len(libs))
	}
	setup.mu.Lock()
	latched := setup.latched
	setup.mu.Unlock()
	if latched {
		t.Error("Fetch latched the process setup; it must touch no image")
	}
	// The installed lookup an open makes now answers with no request.
	found, err := ocifetch.ResolveInstalled(ocifetch.Request{Spelling: "26.8"}, "", reg.fetch.internal())
	if err != nil || found == nil || found.Digests.Manifest != res.Digests.Manifest {
		t.Fatalf("after Fetch, ResolveInstalled(26.8) = %v, %v", found, err)
	}
	// The build is held by this process: a prune in it reports it in use.
	pruned, err := ocifetch.Prune(reg.fetch.internal(), ocifetch.PruneOptions{Keep: 1})
	if err != nil || len(pruned) != 0 {
		t.Fatalf("prune with one build of the line = %+v, %v, want nothing superseded", pruned, err)
	}
	if got := ocifetch.Hold(res.Dir); got != ocifetch.HoldHeld {
		t.Errorf("Hold of the fetched build = %v, want HoldHeld", got)
	}
	// A second Fetch is answered by the registry again, and keeps the build.
	again, err := reg.Fetch(t.Context(), "26.8")
	if err != nil || again.Digests.Manifest != res.Digests.Manifest || !again.AlreadyInstalled {
		t.Fatalf("the second Fetch(26.8) = %+v, %v, want the same build, already installed", again, err)
	}
	if n := fx.requests(t, "fetch-installs")["GET "+fx.layerPath]; n != 1 {
		t.Errorf("the layer was fetched %d times over two Fetches, want once", n)
	}
}

// TestRegistryFetchSharesOneFetchWithAnOpen: with the fetch held at the gate,
// a Fetch and an open of the same request both wait on ONE fetch; when it
// lands, the Fetch has the build and the open goes on to its load (which fails
// here: the bytes are no library), and the registry saw one tag request and one
// layer request.
func TestRegistryFetchSharesOneFetchWithAnOpen(t *testing.T) {
	fx := newFetchFixture(t)
	reg := fx.registry(t, "fetch-shared")
	waiting := make(chan string, 4)
	reg.onFetchWait = func(request string) { waiting <- request }
	fx.closeGate(t, "fetch-shared")

	type fetchResult struct {
		res Resolved
		err error
	}
	fetched := make(chan fetchResult, 1)
	go func() {
		res, err := reg.Fetch(t.Context(), "26.8")
		fetched <- fetchResult{res, err}
	}()
	receive(t, waiting, "the Fetch waiting on the fetch")
	opened := make(chan openResult, 1)
	go func() {
		l, err := reg.For("26.8")
		opened <- openResult{l, err}
	}()
	receive(t, waiting, "the open waiting on the same fetch")
	if parked := fx.waitParked(t, "fetch-shared", 1); parked != 1 {
		t.Fatalf("%d requests parked with a Fetch and an open waiting, want one fetch's one", parked)
	}
	fx.openGate(t, "fetch-shared")

	f := receive(t, fetched, "the Fetch")
	if f.err != nil || f.res.Version != "26.8.15.10" {
		t.Fatalf("the Fetch sharing the fetch = %+v, %v", f.res, f.err)
	}
	o := receive(t, opened, "the open")
	var ae *ArtifactError
	if o.lib != nil || !errors.As(o.err, &ae) {
		t.Fatalf("the open of bytes that are no library = %v, %v, want a load refusal (an *ArtifactError)", o.lib, o.err)
	}
	requests := fx.requests(t, "fetch-shared")
	t.Logf("the fixture server's log for one Fetch and one open: %v", requests)
	if n := requests["GET /chtypes/v1/manifests/26.8"]; n != 1 {
		t.Errorf("the tag was fetched %d times, want once", n)
	}
	if n := requests["GET "+fx.layerPath]; n != 1 {
		t.Errorf("the layer was fetched %d times, want once", n)
	}
}

// TestRegistryFetchErrorsAreTheFetchCodes: a request nothing serves is the
// fetch layer's own code, as an *ArtifactError, and a canceled context is the
// context's error.
func TestRegistryFetchErrorsAreTheFetchCodes(t *testing.T) {
	fx := newFetchFixture(t)
	reg := fx.registry(t, "fetch-unpublished")
	_, err := reg.Fetch(t.Context(), "26.3")
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != CodeArtifactUnpublished {
		t.Fatalf("Fetch(26.3), which nothing serves = %v, want CHTYPES_ARTIFACT_UNPUBLISHED", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := reg.Fetch(ctx, "26.8"); !errors.Is(err, context.Canceled) {
		t.Fatalf("Fetch with a canceled context = %v, want context.Canceled", err)
	}
	offline, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: t.TempDir(), SystemDirs: []string{}, Offline: true}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := offline.Fetch(t.Context(), "26.8"); !errors.As(err, &ae) || ae.Code != CodeArtifactMissing {
		t.Fatalf("an offline Fetch with nothing installed = %v, want CHTYPES_ARTIFACT_MISSING", err)
	}
}
