package ocifetch

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

// fakeRegistry is a minimal OCI distribution server built directly from
// this package's own wire types, so Ensure's full pipeline (resolve, trust,
// bytes, cache) runs against something that actually speaks the protocol —
// not a stand-in for it. It is deliberately independent of the fixtures
// lane's own scripted server (lane 0B's, not yet on this branch): this is a
// self-contained check that this binding's own client logic is internally
// consistent, not a substitute for the conformance suite that server drives.
type fakeRegistry struct {
	routes map[string][]byte
}

func (f *fakeRegistry) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		const prefix = "/v2/chtypes/v1/"
		if len(r.URL.Path) <= len(prefix) {
			http.NotFound(w, r)
			return
		}
		key := r.URL.Path[len(prefix):]
		body, ok := f.routes[key]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	})
}

// buildFakeRegistry assembles one platform (linux-arm64) manifest, correctly
// signed with the repository's own test key, for spelling "26.8" resolving
// to version "26.8.15.10".
func buildFakeRegistry(t *testing.T) (*fakeRegistry, []byte /* libBody */) {
	t.Helper()
	priv, _ := loadTestKey(t)

	libBody := []byte("pretend shared library bytes for the ensure_test.go integration check")
	layerBytes := buildZstdTar(t, []tarEntry{
		{name: "manifest.json", body: []byte(`{"library":"libchtypes.so"}`)},
		{name: "libchtypes.so", body: libBody},
	})
	layerDigest := digestOf(layerBytes)

	manifest := ImageManifest{
		MediaType: MediaTypeManifest,
		Config:    Descriptor{MediaType: MediaTypeEmptyConfig, Digest: digestOf([]byte("{}")), Size: 2},
		Layers:    []Descriptor{{MediaType: MediaTypeLayer, Digest: layerDigest, Size: int64(len(layerBytes))}},
	}
	manifestBytes, err := json.Marshal(manifest)
	if err != nil {
		t.Fatalf("marshal manifest: %v", err)
	}
	manifestDigest := digestOf(manifestBytes)

	idx := ImageIndex{
		MediaType: MediaTypeIndex,
		Manifests: []Descriptor{{
			MediaType: MediaTypeManifest, Digest: manifestDigest, Size: int64(len(manifestBytes)),
			Platform: &PlatformDescriptor{OS: "linux", Architecture: "arm64"},
		}},
	}
	indexBytes, err := json.Marshal(idx)
	if err != nil {
		t.Fatalf("marshal index: %v", err)
	}

	librarySum := digestOf(libBody)
	statement := map[string]any{
		"_type": StatementType,
		"subject": []map[string]any{
			{"name": "chtypes-26.8.15.10-linux-arm64.tar.zst", "digest": map[string]string{"sha256": layerDigest.Hex()}},
		},
		"predicateType": PredicateTypeArtifact,
		"predicate": map[string]any{
			"abi": 1, "clickhouse_version": "26.8.15.10", "channel": "lts",
			"os": "linux", "arch": "arm64", "build": "20261001.183455",
			"library": "libchtypes.so", "library_sha256": librarySum.Hex(), "library_bytes": len(libBody),
		},
	}
	bundleBytes := signBundle(t, priv, statement)
	bundleDigest := digestOf(bundleBytes)

	bundleWrapper := ImageManifest{
		MediaType:    MediaTypeManifest,
		ArtifactType: MediaTypeBundle,
		Config:       Descriptor{MediaType: MediaTypeEmptyConfig, Digest: digestOf([]byte("{}")), Size: 2},
		Layers:       []Descriptor{{MediaType: MediaTypeBundle, Digest: bundleDigest, Size: int64(len(bundleBytes))}},
	}
	bundleWrapperBytes, err := json.Marshal(bundleWrapper)
	if err != nil {
		t.Fatalf("marshal bundle wrapper manifest: %v", err)
	}
	bundleWrapperDigest := digestOf(bundleWrapperBytes)

	referrers := ImageIndex{
		MediaType: MediaTypeIndex,
		Manifests: []Descriptor{{
			MediaType: MediaTypeManifest, ArtifactType: MediaTypeBundle,
			Digest: bundleWrapperDigest, Size: int64(len(bundleWrapperBytes)),
		}},
	}
	referrersBytes, err := json.Marshal(referrers)
	if err != nil {
		t.Fatalf("marshal referrers index: %v", err)
	}

	routes := map[string][]byte{
		"manifests/26.8":                           indexBytes,
		"manifests/" + string(manifestDigest):      manifestBytes,
		"manifests/" + string(bundleWrapperDigest): bundleWrapperBytes,
		"referrers/" + string(manifestDigest):      referrersBytes,
		"blobs/" + string(layerDigest):             layerBytes,
		"blobs/" + string(bundleDigest):            bundleBytes,
	}
	return &fakeRegistry{routes: routes}, libBody
}

func testOptions(t *testing.T, base string) *Options {
	t.Helper()
	_, pub := loadTestKey(t)
	return &Options{
		Bases:       []string{base + "/chtypes/v1"},
		CacheDir:    t.TempDir(),
		TrustedKeys: []string{hexString(pub)},
	}
}

func hexString(b []byte) string {
	const hextable = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, v := range b {
		out[i*2] = hextable[v>>4]
		out[i*2+1] = hextable[v&0x0f]
	}
	return string(out)
}

func TestEnsureHappyPath(t *testing.T) {
	reg, libBody := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()

	opts := testOptions(t, srv.URL)
	resolved, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
	if err != nil {
		t.Fatalf("Ensure: %v", err)
	}
	if resolved.Version != "26.8.15.10" {
		t.Fatalf("Version = %q, want 26.8.15.10", resolved.Version)
	}
	if resolved.Platform != "linux-arm64" {
		t.Fatalf("Platform = %q", resolved.Platform)
	}
	if resolved.AlreadyInstalled {
		t.Fatalf("a fresh fetch reported AlreadyInstalled=true")
	}
	if resolved.SignedBy == "" {
		t.Fatalf("SignedBy is empty; expected the test key's id")
	}
	installed, err := os.ReadFile(resolved.LibraryPath)
	if err != nil {
		t.Fatalf("reading installed library at %s: %v", resolved.LibraryPath, err)
	}
	if string(installed) != string(libBody) {
		t.Fatalf("installed library content does not match what the registry served")
	}
}

func TestEnsureSecondCallIsAlreadyInstalled(t *testing.T) {
	reg, _ := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()

	opts := testOptions(t, srv.URL)
	req := Request{Spelling: "26.8", Platform: "linux-arm64"}
	if _, err := Ensure(context.Background(), req, opts); err != nil {
		t.Fatalf("first Ensure: %v", err)
	}
	resolved, err := Ensure(context.Background(), req, opts)
	if err != nil {
		t.Fatalf("second Ensure: %v", err)
	}
	if !resolved.AlreadyInstalled {
		t.Fatalf("second Ensure of the same manifest digest should report AlreadyInstalled=true")
	}
}

func TestEnsureThenResolveInstalledOffline(t *testing.T) {
	reg, _ := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()

	opts := testOptions(t, srv.URL)
	if _, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts); err != nil {
		t.Fatalf("Ensure: %v", err)
	}

	offlineOpts := *opts
	offlineOpts.Offline = true
	resolved, err := ResolveInstalled(Request{Spelling: "26.8"}, "linux-arm64", &offlineOpts)
	if err != nil {
		t.Fatalf("ResolveInstalled: %v", err)
	}
	if resolved == nil {
		t.Fatalf("ResolveInstalled found nothing after an online Ensure installed it")
	}
	if resolved.Version != "26.8.15.10" {
		t.Fatalf("ResolveInstalled version = %q", resolved.Version)
	}
}

func TestEnsureOfflineMissReturnsMissing(t *testing.T) {
	opts := testOptions(t, "http://127.0.0.1:1") // never contacted
	opts.Offline = true
	_, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
	if err == nil {
		t.Fatalf("Ensure(offline, nothing installed) should fail")
	}
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeArtifactMissing {
		t.Fatalf("error = %v, want CHTYPES_ARTIFACT_MISSING", err)
	}
}

func TestEnsureUntrustedWithoutMatchingKey(t *testing.T) {
	reg, _ := buildFakeRegistry(t)
	srv := httptest.NewServer(reg.handler())
	defer srv.Close()

	opts := testOptions(t, srv.URL)
	opts.TrustedKeys = []string{hexString(mustGenerateKey(t))} // some OTHER key, not the signer
	_, err := Ensure(context.Background(), Request{Spelling: "26.8", Platform: "linux-arm64"}, opts)
	if err == nil {
		t.Fatalf("Ensure should refuse a manifest signed by an untrusted key")
	}
	var fe *FetchError
	if !errors.As(err, &fe) || fe.Code != CodeArtifactUntrusted {
		t.Fatalf("error = %v, want CHTYPES_ARTIFACT_UNTRUSTED", err)
	}
}

func TestEnsureRefusesBadSpelling(t *testing.T) {
	opts := testOptions(t, "http://127.0.0.1:1")
	_, err := Ensure(context.Background(), Request{Spelling: "v26.8", Platform: "linux-arm64"}, opts)
	if err == nil {
		t.Fatalf("Ensure should refuse a v-prefixed spelling before any network call")
	}
}

func mustGenerateKey(t *testing.T) []byte {
	t.Helper()
	_, pub, err := ed25519GenerateForTest(t)
	if err != nil {
		t.Fatalf("generating a key: %v", err)
	}
	return pub
}
