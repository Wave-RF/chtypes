package ocifetch

// revocation_test.go — the parts of build-level revocation the conformance
// cases cannot reach (docs/guides/fetch-v1.md, "Build-level revocation"): the
// 410 table every binding reads, the in-process memo, concurrent checks of one
// build, the 5 s deadline (shortened here), and the v1 contract doing nothing.

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type revoked410Row struct {
	Name     string  `json:"name"`
	BodyText *string `json:"body_text"`
	BodyHex  *string `json:"body_hex"`
	Code     string  `json:"code"`
	Revoked  *struct {
		Text       string `json:"text"`
		Advisories []struct {
			ID           string `json:"id"`
			URL          string `json:"url"`
			Class        string `json:"class"`
			Summary      string `json:"summary"`
			Build        string `json:"build"`
			SupersededBy string `json:"superseded_by"`
		} `json:"advisories"`
	} `json:"revoked"`
}

// TestRevoked410Table holds goneError to the one table all four bindings read
// (tests/fixtures/revoked-410/cases.json): the code, the message's text and
// the advisories the typed error carries. Under the v1 contract every 410 is
// a retired repository.
func TestRevoked410Table(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "tests", "fixtures", "revoked-410", "cases.json"))
	if err != nil {
		t.Skipf("SKIPPED: the shared revoked-410 table is not beside this checkout (%v)", err)
	}
	var doc struct {
		Cases []revoked410Row `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("the revoked-410 table has no rows")
	}
	const url = "https://registry.example/v2/chtypes/v2/manifests/26.13"
	revokedRows := 0
	for _, row := range doc.Cases {
		var body []byte
		switch {
		case row.BodyText != nil:
			body = []byte(*row.BodyText)
		case row.BodyHex != nil:
			body, err = hex.DecodeString(*row.BodyHex)
			if err != nil {
				t.Fatalf("%s: body_hex: %v", row.Name, err)
			}
		}
		fe := goneError(url, 410, body, true)
		if string(fe.Code) != row.Code {
			t.Errorf("%s: code %s, want %s (%s)", row.Name, fe.Code, row.Code, fe.Msg)
			continue
		}
		if v1 := goneError(url, 410, body, false); v1.Code != CodeSourceRetired {
			t.Errorf("%s: with revocation off, code %s, want CHTYPES_SOURCE_RETIRED", row.Name, v1.Code)
		}
		if row.Revoked == nil {
			if fe.Revocation != nil {
				t.Errorf("%s: a retired 410 carries a Revocation", row.Name)
			}
			continue
		}
		revokedRows++
		if want := "chtypes: " + url + " answered 410 Gone: " + row.Revoked.Text; !strings.HasPrefix(fe.Msg, want) {
			t.Errorf("%s: message\n got %q\nwant prefix %q", row.Name, fe.Msg, want)
		}
		if fe.Revocation == nil || fe.Revocation.Signed {
			t.Errorf("%s: the typed error carries %+v, want an unsigned Revocation", row.Name, fe.Revocation)
			continue
		}
		got := append([]Advisory{}, fe.Revocation.Advisories...)
		want := []Advisory{}
		for _, a := range row.Revoked.Advisories {
			want = append(want, Advisory{ID: a.ID, URL: a.URL, Class: a.Class, Summary: a.Summary, Build: a.Build, SupersededBy: a.SupersededBy})
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: advisories\n got %+v\nwant %+v", row.Name, got, want)
		}
	}
	if revokedRows == 0 {
		t.Fatal("the table has no revoked row: the comparison above checked nothing")
	}
}

// listingServer answers every request 404, as a tree with no discovery, and
// counts them; hold, when set, runs first (to stall, or to gate).
func listingServer(t *testing.T, hold func(*http.Request)) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var n atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		if hold != nil {
			hold(r)
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, &n
}

// installedBuild is a Resolved for a build installed in a fresh cache: what
// the installed-build check needs (its digests, predicate and entry).
func installedBuild(t *testing.T, cache, seed string) *Resolved {
	t.Helper()
	m := digestOf([]byte("manifest " + seed))
	dir := filepath.Join(cache, ProdV2CacheDir, UnpackedDirName(), m.Hex())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	return &Resolved{
		Platform: "linux-arm64", Request: "26.13", Version: "26.13.1.1", Build: "20261013.000001", Dir: dir,
		Digests:   Digests{Manifest: m, Layer: digestOf([]byte("layer " + seed))},
		Predicate: map[string]any{"abi": float64(2), "os": "linux", "arch": "arm64", "clickhouse_version": "26.13.1.1", "build": "20261013.000001"},
	}
}

// TestInstalledCheckOncePerBuild: the first check of a build makes its one
// listing (the API, then the fallback tag) and warns once; a second makes no
// request and prints nothing; a strict call never takes the kept "not
// checked", checks again and fails; a later call without strict mode is
// answered from what was kept.
func TestInstalledCheckOncePerBuild(t *testing.T) {
	defer UseProdV2ForTests()()
	ResetRevocationMemoForTests()
	defer ResetRevocationMemoForTests()
	srv, n := listingServer(t, nil)
	cache := t.TempDir()
	res := installedBuild(t, cache, "once")
	opts := &Options{Bases: []string{srv.URL + "/chtypes/v2"}, CacheDir: cache}

	w, err := CheckInstalled(context.Background(), res, opts)
	if err != nil || len(w) != 1 || !strings.Contains(w[0], "revocation status not checked for manifest "+string(res.Digests.Manifest)) {
		t.Fatalf("first check: %q, %v; want one not-checked warning", w, err)
	}
	if got := n.Load(); got != 2 {
		t.Fatalf("first check made %d requests, want 2 (the referrers API and the fallback tag)", got)
	}
	if w, err := CheckInstalled(context.Background(), res, opts); err != nil || len(w) != 0 || n.Load() != 2 {
		t.Fatalf("second check: %q, %v, %d requests in all; want nothing new", w, err, n.Load())
	}
	strict := *opts
	strict.RequireRevocationCheck = true
	_, err = CheckInstalled(context.Background(), res, &strict)
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeSourceUnreachable || !strings.Contains(fe.Msg, "revocation status not checked") {
		t.Fatalf("strict check: %v; want CHTYPES_SOURCE_UNREACHABLE naming the unchecked status", err)
	}
	if got := n.Load(); got != 4 {
		t.Fatalf("the strict check made %d requests in all, want 4: it checks for itself", got)
	}
	if w, err := CheckInstalled(context.Background(), res, opts); err != nil || len(w) != 0 || n.Load() != 4 {
		t.Fatalf("after the strict failure: %q, %v, %d requests; want the kept outcome", w, err, n.Load())
	}
}

// TestInstalledCheckConcurrentShareOne: concurrent checks of one build in one
// process share one check, and only one of them warns.
func TestInstalledCheckConcurrentShareOne(t *testing.T) {
	defer UseProdV2ForTests()()
	ResetRevocationMemoForTests()
	defer ResetRevocationMemoForTests()
	srv, n := listingServer(t, func(*http.Request) { time.Sleep(50 * time.Millisecond) })
	cache := t.TempDir()
	res := installedBuild(t, cache, "concurrent")
	opts := &Options{Bases: []string{srv.URL + "/chtypes/v2"}, CacheDir: cache}

	var wg sync.WaitGroup
	var warned atomic.Int64
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w, err := CheckInstalled(context.Background(), res, opts)
			if err != nil {
				t.Errorf("concurrent check: %v", err)
			}
			warned.Add(int64(len(w)))
		}()
	}
	wg.Wait()
	if n.Load() != 2 || warned.Load() != 1 {
		t.Fatalf("8 concurrent checks made %d requests and %d warnings, want 2 and 1", n.Load(), warned.Load())
	}
}

// TestInstalledCheckDeadline: a registry that never answers costs at most the
// check's one deadline, not the retry table's minutes, and the open goes on
// with the warning. The deadline is shortened here; the production one is
// RevocationCheckSeconds.
func TestInstalledCheckDeadline(t *testing.T) {
	defer UseProdV2ForTests()()
	ResetRevocationMemoForTests()
	defer ResetRevocationMemoForTests()
	if RevocationCheckSeconds != 5 {
		t.Fatalf("RevocationCheckSeconds = %v, the record says 5", RevocationCheckSeconds)
	}
	prev := revocationCheckDeadline
	revocationCheckDeadline = 300 * time.Millisecond
	defer func() { revocationCheckDeadline = prev }()
	release := make(chan struct{})
	defer close(release)
	srv, _ := listingServer(t, func(r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	cache := t.TempDir()
	res := installedBuild(t, cache, "deadline")
	var sleeps []time.Duration
	opts := &Options{Bases: []string{srv.URL + "/chtypes/v2", srv.URL + "/mirror/chtypes/v2"}, CacheDir: cache,
		Clock: &Clock{Now: time.Now, Sleep: func(_ context.Context, d time.Duration) { sleeps = append(sleeps, d) }}}
	start := time.Now()
	w, err := CheckInstalled(context.Background(), res, opts)
	elapsed := time.Since(start)
	if err != nil || len(w) != 1 || !strings.Contains(w[0], "passed its") {
		t.Fatalf("a stalled registry: %q, %v; want one warning naming the deadline", w, err)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("the check took %s, past its %s deadline", elapsed, revocationCheckDeadline)
	}
	if len(sleeps) != 0 {
		t.Fatalf("the check slept %v: it has no backoff", sleeps)
	}
}

// TestInstalledCheckV1ContractNone: the v1 contract has no revocation, so the
// check makes no request and says nothing, and a stored record is not read.
func TestInstalledCheckV1ContractNone(t *testing.T) {
	ResetRevocationMemoForTests()
	defer ResetRevocationMemoForTests()
	srv, n := listingServer(t, nil)
	cache := t.TempDir()
	res := installedBuild(t, cache, "v1")
	if err := os.WriteFile(filepath.Join(res.Dir, CacheRevocationRecord), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	w, err := CheckInstalled(context.Background(), res, &Options{Bases: []string{srv.URL + "/chtypes/v1"}, CacheDir: cache})
	if err != nil || len(w) != 0 || n.Load() != 0 {
		t.Fatalf("under the v1 contract: %q, %v, %d requests; want nothing", w, err, n.Load())
	}
}

// TestInstalledCheckOfflineMakesNoRequest: offline skips the network check.
func TestInstalledCheckOfflineMakesNoRequest(t *testing.T) {
	defer UseProdV2ForTests()()
	ResetRevocationMemoForTests()
	defer ResetRevocationMemoForTests()
	srv, n := listingServer(t, nil)
	cache := t.TempDir()
	res := installedBuild(t, cache, "offline")
	w, err := CheckInstalled(context.Background(), res, &Options{Bases: []string{srv.URL + "/chtypes/v2"}, CacheDir: cache, Offline: true})
	if err != nil || len(w) != 0 || n.Load() != 0 {
		t.Fatalf("offline: %q, %v, %d requests; want nothing", w, err, n.Load())
	}
}

// TestAllowRevokedNeverPinsALock: the override is dropped for a lock and an
// update, whatever the caller set.
func TestAllowRevokedNeverPinsALock(t *testing.T) {
	defer UseProdV2ForTests()()
	for _, o := range []Options{{AllowRevoked: true, LockWrite: true}, {AllowRevoked: true, Update: true}} {
		ro, err := resolveOptions(&o)
		if err != nil {
			t.Fatal(err)
		}
		if ro.allowRevoked {
			t.Errorf("%+v: AllowRevoked survived a lock or an update", o)
		}
	}
	ro, err := resolveOptions(&Options{AllowRevoked: true})
	if err != nil || !ro.allowRevoked {
		t.Fatalf("AllowRevoked alone: %v, %v", ro.allowRevoked, err)
	}
}
