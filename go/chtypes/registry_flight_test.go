package chtypes

// registry_flight_test.go — an open never waits for another request's fetch,
// and concurrent opens of one request share one fetch (public issue #491).
//
// Each test serves the stub, signed with the fetch fixtures' TEST key, from the
// fetch fixture server (scripts/fetch-v1/server.py) over HTTP, and holds a
// fetch in flight with the server's test-only gate: the request is logged and
// parked until the test opens the gate. Every wait is on an event (the
// server's parked count, the registry's onWait hook, a result channel), never
// on a clock; a bound only turns a hang into a failure. The verdicts come from
// what the server logged and what each open returned.

import (
	"archive/tar"
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/klauspost/compress/zstd"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// flightBound turns a hang into a failure; a working registry answers at once.
const flightBound = 30 * time.Second

// startBound bounds the fixture server's start, which took about 35 s on the
// darwin-arm64 runner before server.py stopped looking its address up.
const startBound = 120 * time.Second

// flightCases are the fixture server's case ids, one per test, each served
// from the one stub tree.
var flightCases = []string{"flight-install", "flight-held", "flight-one-fetch", "flight-fails", "flight-cancel", "flight-abandon"}

type flightFixture struct {
	origin    string
	cache     string
	trusted   string
	layerPath string // the layer blob's path under a case's repository
}

type openResult struct {
	lib *Library
	err error
}

// newFlightFixture serves the stub as tag 26.8 of every flight case. It skips
// loudly without the stubs, as every stub-driven test here does.
func newFlightFixture(t *testing.T) *flightFixture {
	t.Helper()
	resetSetup(t)
	stubDir(t)
	lib, err := os.ReadFile(stubFile(t, "ok"))
	if err != nil {
		t.Fatal(err)
	}
	pred, platformKey := stubPredicate(t, lib, hostLibName())
	if platformKey != runtime.GOOS+"-"+runtime.GOARCH {
		t.Skipf("SKIPPED: this host (%s-%s) is not the stub's platform; the registry flight tests did not run", runtime.GOOS, runtime.GOARCH)
	}
	fixtures := t.TempDir()
	layer := writeRouteTree(t, filepath.Join(fixtures, "trees", "stub", "v2", "chtypes", "v1"), "26.8", hostLibName(), platformKey, lib, pred)
	cases := make([]map[string]string, 0, len(flightCases))
	for _, id := range flightCases {
		cases = append(cases, map[string]string{"id": id, "tree": "stub"})
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

// writeRouteTree writes libBytes as tag's one-platform signed artifact in the
// route-tree shape the fixture server serves (manifests/<tag or digest>,
// blobs/<digest>, referrers/<digest>), and returns the layer's digest.
func writeRouteTree(t *testing.T, repo, tag, libName, platformKey string, libBytes []byte, predicate map[string]any) ocifetch.Digest {
	t.Helper()
	priv, _ := loadTestKey(t)
	for _, sub := range []string{"manifests", "blobs", "referrers"} {
		if err := os.MkdirAll(filepath.Join(repo, sub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	put := func(sub, name string, content []byte) {
		if err := os.WriteFile(filepath.Join(repo, sub, name), content, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	blob := func(mediaType string, content []byte) ocifetch.Descriptor {
		d := sha256Digest(content)
		put("blobs", string(d), content)
		return ocifetch.Descriptor{MediaType: mediaType, Digest: d, Size: int64(len(content))}
	}
	manifestDoc := func(mediaType string, content []byte) ocifetch.Descriptor {
		d := sha256Digest(content)
		put("manifests", string(d), content)
		return ocifetch.Descriptor{MediaType: mediaType, Digest: d, Size: int64(len(content))}
	}

	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)
	if err := tw.WriteHeader(&tar.Header{Name: libName, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(libBytes))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(libBytes); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	var zbuf bytes.Buffer
	enc, err := zstd.NewWriter(&zbuf, zstd.WithEncoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := enc.Write(tarBuf.Bytes()); err != nil {
		t.Fatal(err)
	}
	if err := enc.Close(); err != nil {
		t.Fatal(err)
	}
	layer := blob(ocifetch.MediaTypeLayer, zbuf.Bytes())
	config := blob(ocifetch.MediaTypeConfig, mustJSON(t, predicate))
	mdesc := manifestDoc(ocifetch.MediaTypeManifest, mustJSON(t, ocifetch.ImageManifest{
		SchemaVersion: 2, MediaType: ocifetch.MediaTypeManifest, ArtifactType: ocifetch.ArtifactType,
		Config: config, Layers: []ocifetch.Descriptor{layer},
	}))

	statement := mustJSON(t, map[string]any{
		"_type":         ocifetch.StatementType,
		"subject":       []any{map[string]any{"name": libName + "-" + platformKey + ".tar.zst", "digest": map[string]string{"sha256": layer.Digest.Hex()}}},
		"predicateType": ocifetch.PredicateTypeArtifact,
		"predicate":     predicate,
	})
	statement = append(statement, '\n')
	pae := []byte("DSSEv1 " + strconv.Itoa(len(ocifetch.DSSEPayloadType)) + " " + ocifetch.DSSEPayloadType + " " + strconv.Itoa(len(statement)) + " ")
	pae = append(pae, statement...)
	keyID := sha256.Sum256(priv.Public().(ed25519.PublicKey))
	bundle := blob(ocifetch.MediaTypeBundle, mustJSON(t, map[string]any{
		"mediaType":            ocifetch.MediaTypeBundle,
		"verificationMaterial": map[string]any{"publicKey": map[string]string{"hint": hex.EncodeToString(keyID[:8])}},
		"dsseEnvelope": map[string]any{
			"payload": base64.StdEncoding.EncodeToString(statement), "payloadType": ocifetch.DSSEPayloadType,
			"signatures": []any{map[string]string{"sig": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, pae))}},
		},
	}))
	empty := blob(ocifetch.MediaTypeEmptyConfig, []byte("{}"))
	referrer := manifestDoc(ocifetch.MediaTypeManifest, mustJSON(t, ocifetch.ImageManifest{
		SchemaVersion: 2, MediaType: ocifetch.MediaTypeManifest, ArtifactType: ocifetch.MediaTypeBundle,
		Config: empty, Layers: []ocifetch.Descriptor{bundle},
		Subject: &ocifetch.Descriptor{MediaType: mdesc.MediaType, Digest: mdesc.Digest, Size: mdesc.Size},
	}))
	referrer.ArtifactType = ocifetch.MediaTypeBundle
	referrers := mustJSON(t, ocifetch.ImageIndex{SchemaVersion: 2, MediaType: ocifetch.MediaTypeIndex, Manifests: []ocifetch.Descriptor{referrer}})
	put("referrers", string(mdesc.Digest), referrers)
	put("manifests", "sha256-"+mdesc.Digest.Hex(), referrers)

	var platform ocifetch.Platform
	for _, p := range ocifetch.Platforms {
		if p.Key == platformKey {
			platform = p
		}
	}
	mdesc.ArtifactType = ocifetch.ArtifactType
	mdesc.Platform = &ocifetch.PlatformDescriptor{OS: platform.OS, Architecture: platform.Architecture}
	put("manifests", tag, mustJSON(t, ocifetch.ImageIndex{SchemaVersion: 2, MediaType: ocifetch.MediaTypeIndex, Manifests: []ocifetch.Descriptor{mdesc}}))
	return layer.Digest
}

// startFlightServer starts scripts/fetch-v1/server.py over fixtures and
// returns its primary origin; the server is killed when the test ends.
func startFlightServer(t *testing.T, fixtures string) string {
	t.Helper()
	script := filepath.Join(repoRootDir(t), "scripts", "fetch-v1", "server.py")
	cmd := exec.CommandContext(t.Context(), "python3", script, "--fixtures", fixtures, "--port", "0")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting %s: %v", script, err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	line := make(chan string, 1)
	go func() {
		s := bufio.NewScanner(stdout)
		if s.Scan() {
			line <- s.Text()
		}
		close(line)
		_, _ = io.Copy(io.Discard, stdout)
	}()
	var got string
	select {
	case got = <-line:
	case <-time.After(startBound):
		t.Fatalf("%s printed no LISTENING line in %s", script, startBound)
	}
	fields := strings.Fields(got)
	if len(fields) != 3 || fields[0] != "LISTENING" {
		t.Fatalf("%s printed %q, want LISTENING <port> <port2>", script, got)
	}
	return "http://127.0.0.1:" + fields[1]
}

// registry is a registry with autofetch on whose one base is caseID's
// repository on the fixture server, over the fixture's cache.
func (fx *flightFixture) registry(t *testing.T, caseID string) *Registry {
	t.Helper()
	reg, err := NewRegistry(WithFetchOptions(FetchOptions{
		Bases: []string{fx.origin + "/s-" + caseID + "/chtypes/v1"}, CacheDir: fx.cache,
		SystemDirs: []string{}, TrustedKeys: []string{fx.trusted},
	}), WithAutoFetch(true))
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

// get GETs one of the server's own endpoints.
func (fx *flightFixture) get(ctx context.Context, path string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fx.origin+path, http.NoBody)
	if err != nil {
		return nil, err
	}
	return http.DefaultClient.Do(req)
}

// control GETs one of the server's own endpoints and decodes its JSON answer.
func (fx *flightFixture) control(t *testing.T, path string, into any) {
	t.Helper()
	resp, err := fx.get(t.Context(), path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s: %s", path, resp.Status)
	}
	if err := json.NewDecoder(resp.Body).Decode(into); err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
}

// closeGate holds every later request of caseID at the server; the gate opens
// again when the test ends, whatever happened.
func (fx *flightFixture) closeGate(t *testing.T, caseID string) {
	t.Helper()
	var closed map[string]bool
	fx.control(t, "/_gate/close/s-"+caseID, &closed)
	t.Cleanup(func() {
		// t.Context() is already done when cleanups run.
		if resp, err := fx.get(context.WithoutCancel(t.Context()), "/_gate/open/s-"+caseID); err == nil {
			resp.Body.Close()
		}
	})
}

func (fx *flightFixture) openGate(t *testing.T, caseID string) {
	t.Helper()
	var released map[string]int
	fx.control(t, "/_gate/open/s-"+caseID, &released)
}

// waitParked returns once n requests of caseID are parked at its gate, and
// how many are.
func (fx *flightFixture) waitParked(t *testing.T, caseID string, n int) int {
	t.Helper()
	var parked map[string]int
	fx.control(t, fmt.Sprintf("/_gate/parked/s-%s?n=%d", caseID, n), &parked)
	return parked["parked"]
}

// requests counts caseID's logged requests by "METHOD path", the path relative
// to the case's own segment (/chtypes/v1/...).
func (fx *flightFixture) requests(t *testing.T, caseID string) map[string]int {
	t.Helper()
	var log []struct {
		Method string `json:"method"`
		Path   string `json:"path"`
	}
	fx.control(t, "/_log/s-"+caseID, &log)
	out := map[string]int{}
	for _, e := range log {
		out[e.Method+" "+strings.TrimPrefix(e.Path, "/v2/s-"+caseID)]++
	}
	return out
}

func receive[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(flightBound):
		t.Fatalf("%s: no answer in %s", what, flightBound)
	}
	var zero T
	return zero
}

// TestRegistryAnInstalledLineNeverWaitsForAnotherLinesFetch is the issue's
// regression: with 26.3's fetch held at the gate, an open of 26.8, which the
// cache answers, returns while 26.3's request is still parked.
func TestRegistryAnInstalledLineNeverWaitsForAnotherLinesFetch(t *testing.T) {
	fx := newFlightFixture(t)
	if _, err := fx.registry(t, "flight-install").For("26.8"); err != nil {
		t.Fatalf("installing 26.8: %v", err)
	}
	reg := fx.registry(t, "flight-held")
	fx.closeGate(t, "flight-held")
	held := make(chan error, 1)
	go func() {
		_, err := reg.For("26.3")
		held <- err
	}()
	fx.waitParked(t, "flight-held", 1)

	installed := make(chan openResult, 1)
	go func() {
		l, err := reg.For("26.8")
		installed <- openResult{l, err}
	}()
	select {
	case got := <-installed:
		if got.err != nil || got.lib == nil || got.lib.Version != "26.8.15.10" {
			t.Fatalf("For(26.8), installed, while 26.3 fetches = %v, %v", got.lib, got.err)
		}
	case <-time.After(flightBound):
		t.Fatalf("For(26.8), installed, waited %s for 26.3's fetch, held at the gate", flightBound)
	}
	select {
	case err := <-held:
		t.Fatalf("For(26.3) returned while its fetch was held at the gate: %v", err)
	default:
	}
	if parked := fx.waitParked(t, "flight-held", 1); parked != 1 {
		t.Fatalf("%d requests parked at the gate, want 26.3's one", parked)
	}

	fx.openGate(t, "flight-held")
	var ae *ArtifactError
	if err := receive(t, held, "For(26.3) after the gate opened"); !errors.As(err, &ae) {
		t.Fatalf("For(26.3), which nothing serves = %v, want an *ArtifactError", err)
	}
	for req := range fx.requests(t, "flight-held") {
		if strings.Contains(req, "/manifests/26.8") {
			t.Errorf("the open of installed 26.8 made a request: %s", req)
		}
	}
}

// TestRegistryConcurrentOpensOfOneRequestShareOneFetch: N opens of one
// uninstalled request, all waiting while its fetch is held, make one fetch and
// all get its Library.
func TestRegistryConcurrentOpensOfOneRequestShareOneFetch(t *testing.T) {
	const n = 8
	fx := newFlightFixture(t)
	reg := fx.registry(t, "flight-one-fetch")
	waiting := make(chan string, n)
	reg.onWait = func(request string) { waiting <- request }
	fx.closeGate(t, "flight-one-fetch")
	results := make(chan openResult, n)
	for range n {
		go func() {
			l, err := reg.For("26.8")
			results <- openResult{l, err}
		}()
	}
	for i := range n {
		receive(t, waiting, fmt.Sprintf("open %d of %d waiting", i+1, n))
	}
	if parked := fx.waitParked(t, "flight-one-fetch", 1); parked != 1 {
		t.Fatalf("%d requests parked at the gate with %d opens waiting, want one fetch's one", parked, n)
	}
	fx.openGate(t, "flight-one-fetch")

	var first *Library
	for i := range n {
		got := receive(t, results, fmt.Sprintf("open %d of %d", i+1, n))
		if got.err != nil {
			t.Fatalf("an open sharing the fetch: %v", got.err)
		}
		if first == nil {
			first = got.lib
		} else if got.lib != first {
			t.Fatalf("two opens of 26.8 got two Libraries, %p and %p", first, got.lib)
		}
	}
	requests := fx.requests(t, "flight-one-fetch")
	t.Logf("the fixture server's log for %d opens: %v", n, requests)
	if got := requests["GET /chtypes/v1/manifests/26.8"]; got != 1 {
		t.Errorf("the tag was fetched %d times, want once: %v", got, requests)
	}
	if got := requests["GET "+fx.layerPath]; got != 1 {
		t.Errorf("the layer was fetched %d times, want once: %v", got, requests)
	}
	for req, count := range requests {
		if count != 1 {
			t.Errorf("%s was requested %d times, want once", req, count)
		}
	}
	if again, err := reg.For("26.8"); err != nil || again != first {
		t.Errorf("the memo after the shared fetch = %p, %v, want %p", again, err, first)
	}
	if libs := reg.Libraries(); len(libs) != 1 || libs[0] != first {
		t.Errorf("Libraries = %v, want the one Library", libs)
	}
}

// TestRegistryAFailedFetchReachesEveryWaiterAndIsNotRemembered: N opens of a
// request nothing serves share one failing fetch and all get its error; the
// next open makes a new request.
func TestRegistryAFailedFetchReachesEveryWaiterAndIsNotRemembered(t *testing.T) {
	const n = 8
	fx := newFlightFixture(t)
	reg := fx.registry(t, "flight-fails")
	waiting := make(chan string, n+1)
	reg.onWait = func(request string) { waiting <- request }
	fx.closeGate(t, "flight-fails")
	errs := make(chan error, n)
	for range n {
		go func() {
			_, err := reg.For("26.3")
			errs <- err
		}()
	}
	for i := range n {
		receive(t, waiting, fmt.Sprintf("open %d of %d waiting", i+1, n))
	}
	fx.waitParked(t, "flight-fails", 1)
	fx.openGate(t, "flight-fails")

	var first *ArtifactError
	for i := range n {
		err := receive(t, errs, fmt.Sprintf("open %d of %d", i+1, n))
		var ae *ArtifactError
		if !errors.As(err, &ae) {
			t.Fatalf("an open of 26.3, which nothing serves = %v, want an *ArtifactError", err)
		}
		if first == nil {
			first = ae
		} else if ae != first {
			t.Fatalf("two opens sharing one fetch got two errors: %v and %v", first, ae)
		}
	}
	const tag = "GET /chtypes/v1/manifests/26.3"
	if got := fx.requests(t, "flight-fails")[tag]; got != 1 {
		t.Fatalf("%d opens sharing one fetch requested the tag %d times, want once", n, got)
	}
	if _, err := reg.For("26.3"); err == nil || err.Error() != first.Error() {
		t.Fatalf("the open after the failed fetch = %v, want the same failure", err)
	}
	if got := fx.requests(t, "flight-fails")[tag]; got != 2 {
		t.Fatalf("after a failed fetch the next open requested the tag %d times in all, want 2: a failure is never remembered", got)
	}
}

// TestRegistryACanceledOpenLeavesTheFetchToTheOthers: an open whose context
// is canceled returns its context's error, and the fetch goes on for the other
// open of the same request, which gets the Library from that one fetch.
func TestRegistryACanceledOpenLeavesTheFetchToTheOthers(t *testing.T) {
	fx := newFlightFixture(t)
	reg := fx.registry(t, "flight-cancel")
	waiting := make(chan string, 2)
	reg.onWait = func(request string) { waiting <- request }
	fx.closeGate(t, "flight-cancel")
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	canceled := make(chan error, 1)
	go func() {
		_, err := reg.ForContext(ctx, "26.8")
		canceled <- err
	}()
	receive(t, waiting, "the first open waiting")
	other := make(chan openResult, 1)
	go func() {
		l, err := reg.For("26.8")
		other <- openResult{l, err}
	}()
	receive(t, waiting, "the second open waiting")
	fx.waitParked(t, "flight-cancel", 1)

	cancel()
	if err := receive(t, canceled, "the canceled open"); !errors.Is(err, context.Canceled) {
		t.Fatalf("the canceled open = %v, want context.Canceled", err)
	}
	select {
	case got := <-other:
		t.Fatalf("the other open returned while the fetch was held: %v, %v", got.lib, got.err)
	default:
	}
	fx.openGate(t, "flight-cancel")
	got := receive(t, other, "the other open")
	if got.err != nil || got.lib == nil || got.lib.Version != "26.8.15.10" {
		t.Fatalf("the other open = %v, %v", got.lib, got.err)
	}
	if n := fx.requests(t, "flight-cancel")["GET /chtypes/v1/manifests/26.8"]; n != 1 {
		t.Errorf("the tag was fetched %d times, want once: the canceled open must not end the shared fetch", n)
	}
}

// TestRegistryAFetchNoOpenWaitsForIsAbandoned: when the only open waiting on a
// fetch is canceled, the fetch is canceled and the attempt abandoned, so the
// next open of the request starts a new one (its request reaches the gate too).
func TestRegistryAFetchNoOpenWaitsForIsAbandoned(t *testing.T) {
	fx := newFlightFixture(t)
	reg := fx.registry(t, "flight-abandon")
	fx.closeGate(t, "flight-abandon")
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	canceled := make(chan error, 1)
	go func() {
		_, err := reg.ForContext(ctx, "26.8")
		canceled <- err
	}()
	fx.waitParked(t, "flight-abandon", 1)
	cancel()
	if err := receive(t, canceled, "the canceled open"); !errors.Is(err, context.Canceled) {
		t.Fatalf("the canceled open = %v, want context.Canceled", err)
	}
	next := make(chan openResult, 1)
	go func() {
		l, err := reg.For("26.8")
		next <- openResult{l, err}
	}()
	if parked := fx.waitParked(t, "flight-abandon", 2); parked != 2 {
		t.Fatalf("%d requests parked, want the abandoned attempt's and the new one's", parked)
	}
	fx.openGate(t, "flight-abandon")
	got := receive(t, next, "the next open")
	if got.err != nil || got.lib == nil || got.lib.Version != "26.8.15.10" {
		t.Fatalf("the next open = %v, %v", got.lib, got.err)
	}
	if n := fx.requests(t, "flight-abandon")["GET /chtypes/v1/manifests/26.8"]; n != 1 {
		t.Errorf("the tag was fetched %d times, want once: the abandoned fetch was canceled before it", n)
	}
}
