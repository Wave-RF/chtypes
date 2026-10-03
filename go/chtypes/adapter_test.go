package chtypes

// adapter_test.go — the Resolved -> LoadInput adapter, driven by the REAL fetch
// derivation (bindings-v1.md section 6): the test stub is packaged as an OCI
// layout signed with the SDK TEST key (tests/fixtures/fetch-v1/test-key, never
// trusted by default), resolve_installed verifies it with that key trusted, the
// registry adapts the record and loads it. A hand-built LoadInput could not
// test the adapter: the predicate crosses it verbatim, and loader step 5 then
// cross-checks it against the library's own build_info.

import (
	"archive/tar"
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"

	"github.com/klauspost/compress/zstd"

	"github.com/wave-rf/chtypes/go/internal/ocifetch"
)

func repoRootDir(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	// go/chtypes/adapter_test.go is two directories below the repository root.
	return filepath.Join(filepath.Dir(file), "..", "..")
}

func loadTestKey(t *testing.T) (ed25519.PrivateKey, string) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(repoRootDir(t), "tests", "fixtures", "fetch-v1", "test-key", "private.pem"))
	if err != nil {
		t.Fatal(err)
	}
	blk, _ := pem.Decode(raw)
	if blk == nil {
		t.Fatal("the test key has no PEM block")
	}
	k, err := x509.ParsePKCS8PrivateKey(blk.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	priv := k.(ed25519.PrivateKey)
	pub := priv.Public().(ed25519.PublicKey)
	return priv, hex.EncodeToString(pub)
}

func sha256Digest(b []byte) ocifetch.Digest {
	sum := sha256.Sum256(b)
	return ocifetch.Digest("sha256:" + hex.EncodeToString(sum[:]))
}

type layoutWriter struct {
	t   *testing.T
	dir string
}

func (w *layoutWriter) blob(mediaType string, content []byte) ocifetch.Descriptor {
	w.t.Helper()
	d := sha256Digest(content)
	dir := filepath.Join(w.dir, "blobs", "sha256")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		w.t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, d.Hex()), content, 0o644); err != nil {
		w.t.Fatal(err)
	}
	return ocifetch.Descriptor{MediaType: mediaType, Digest: d, Size: int64(len(content))}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// writeSignedLayout packages libBytes as one platform's signed artifact in an
// OCI image layout at dir, as the artifact producer's push does, with
// predicate as the signed statement's predicate.
func writeSignedLayout(t *testing.T, dir, request, libName, platformKey string, libBytes []byte, predicate map[string]any) {
	t.Helper()
	priv, _ := loadTestKey(t)
	w := &layoutWriter{t: t, dir: dir}

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
	layer := w.blob(ocifetch.MediaTypeLayer, zbuf.Bytes())
	config := w.blob(ocifetch.MediaTypeConfig, mustJSON(t, predicate))
	manifest := ocifetch.ImageManifest{
		SchemaVersion: 2, MediaType: ocifetch.MediaTypeManifest, ArtifactType: ocifetch.ArtifactType,
		Config: config, Layers: []ocifetch.Descriptor{layer},
	}
	mdesc := w.blob(ocifetch.MediaTypeManifest, mustJSON(t, manifest))

	statement := mustJSON(t, map[string]any{
		"_type":         ocifetch.StatementType,
		"subject":       []any{map[string]any{"name": libName + "-" + platformKey + ".tar.zst", "digest": map[string]string{"sha256": layer.Digest.Hex()}}},
		"predicateType": ocifetch.PredicateTypeArtifact,
		"predicate":     predicate,
	})
	statement = append(statement, '\n')
	pae := []byte("DSSEv1 " + strconv.Itoa(len(ocifetch.DSSEPayloadType)) + " " + ocifetch.DSSEPayloadType + " " + strconv.Itoa(len(statement)) + " ")
	pae = append(pae, statement...)
	sig := ed25519.Sign(priv, pae)
	pub := priv.Public().(ed25519.PublicKey)
	keyID := sha256.Sum256(pub)
	bundle := mustJSON(t, map[string]any{
		"mediaType":            ocifetch.MediaTypeBundle,
		"verificationMaterial": map[string]any{"publicKey": map[string]string{"hint": hex.EncodeToString(keyID[:8])}},
		"dsseEnvelope": map[string]any{
			"payload": base64.StdEncoding.EncodeToString(statement), "payloadType": ocifetch.DSSEPayloadType,
			"signatures": []any{map[string]string{"sig": base64.StdEncoding.EncodeToString(sig)}},
		},
	})
	bdesc := w.blob(ocifetch.MediaTypeBundle, bundle)
	empty := w.blob(ocifetch.MediaTypeEmptyConfig, []byte("{}"))
	referrer := ocifetch.ImageManifest{
		SchemaVersion: 2, MediaType: ocifetch.MediaTypeManifest, ArtifactType: ocifetch.MediaTypeBundle,
		Config: empty, Layers: []ocifetch.Descriptor{bdesc},
		Subject: &ocifetch.Descriptor{MediaType: mdesc.MediaType, Digest: mdesc.Digest, Size: mdesc.Size},
	}
	w.blob(ocifetch.MediaTypeManifest, mustJSON(t, referrer))

	platform := ocifetch.Platform{}
	for _, p := range ocifetch.Platforms {
		if p.Key == platformKey {
			platform = p
		}
	}
	mdesc.ArtifactType = ocifetch.ArtifactType
	mdesc.Platform = &ocifetch.PlatformDescriptor{OS: platform.OS, Architecture: platform.Architecture}
	mdesc.Annotations = map[string]string{"org.opencontainers.image.ref.name": request}
	index := ocifetch.ImageIndex{SchemaVersion: 2, MediaType: ocifetch.MediaTypeIndex, Manifests: []ocifetch.Descriptor{mdesc}}
	if err := os.WriteFile(filepath.Join(dir, "oci-layout"), []byte(`{"imageLayoutVersion":"1.0.0"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "index.json"), mustJSON(t, index), 0o644); err != nil {
		t.Fatal(err)
	}
}

// stubPredicate is the stub's own predicate (the one build-stubs.sh writes for
// the "ok" variant) plus the three library fields the artifact producer adds.
func stubPredicate(t *testing.T, lib []byte, libName string) (map[string]any, string) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(stubDir(t), "stubs.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Variants map[string]struct {
			Predicate map[string]any `json:"predicate"`
		} `json:"variants"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	p := m.Variants["ok"].Predicate
	if p == nil {
		t.Fatal("stubs.json has no ok variant")
	}
	pred := map[string]any{}
	for k, v := range p {
		pred[k] = v
	}
	sum := sha256.Sum256(lib)
	pred["library"], pred["library_sha256"], pred["library_bytes"] = libName, hex.EncodeToString(sum[:]), len(lib)
	return pred, pred["os"].(string) + "-" + pred["arch"].(string)
}

func hostLibName() string {
	if runtime.GOOS == "darwin" {
		return "libchtypes.dylib"
	}
	return "libchtypes.so"
}

func TestRegistryOverTheRealFetchDerivation(t *testing.T) {
	resetSetup(t)
	stubDir(t) // skips loudly without the stubs
	lib, err := os.ReadFile(stubFile(t, "ok"))
	if err != nil {
		t.Fatal(err)
	}
	pred, platformKey := stubPredicate(t, lib, hostLibName())
	if _, ok := map[string]bool{"linux-amd64": true, "linux-arm64": true, "darwin-arm64": true}[platformKey]; !ok || platformKey != runtime.GOOS+"-"+runtime.GOARCH {
		t.Skipf("SKIPPED: this host (%s-%s) is not one of the v1 platforms; the adapter case did not run", runtime.GOOS, runtime.GOARCH)
	}
	cache := t.TempDir()
	writeSignedLayout(t, cache, "26.8", hostLibName(), platformKey, lib, pred)
	_, trusted := loadTestKey(t)

	reg, err := NewRegistry(WithFetchOptions(FetchOptions{
		CacheDir: cache, SystemDirs: []string{t.TempDir()}, TrustedKeys: []string{trusted}, Offline: true,
	}), WithAutoFetch(false))
	if err != nil {
		t.Fatal(err)
	}
	if got := reg.Libraries(); len(got) != 0 {
		t.Fatalf("construction opened %d libraries; it must open nothing", len(got))
	}
	l, err := reg.For("26.8")
	if err != nil {
		t.Fatalf("For(26.8) over the signed layout: %v", err)
	}
	r := l.Resolved()
	if r == nil || r.Request != "26.8" || r.Version != "26.8.15.10" || r.LibraryPath == "" || r.SignedBy == "" {
		t.Fatalf("Resolved = %+v", r)
	}
	// The predicate crossed the adapter verbatim: the record's own map is
	// what loader step 5 cross-checked against the library's build_info.
	if r.Predicate["core_commit"] != pred["core_commit"] || r.Predicate["library_sha256"] != pred["library_sha256"] {
		t.Errorf("the predicate was changed on the way: %v", r.Predicate)
	}
	if l.Version != "26.8.15.10" || l.Path != r.LibraryPath {
		t.Errorf("library = %s at %s", l.Version, l.Path)
	}
	// The memo: a request opened before is the same Library for the registry's life.
	again, err := reg.For("26.8")
	if err != nil || again != l {
		t.Errorf("the memo returned %v, %v", again, err)
	}
	if got := reg.Libraries(); len(got) != 1 || got[0] != l {
		t.Errorf("Libraries = %v", got)
	}
	inst, err := reg.Installed()
	if err != nil || len(inst) != 1 || inst[0].Version != "26.8.15.10" {
		t.Errorf("Installed = %+v, %v", inst, err)
	}
	// A request nothing installed answers, with autofetch off: the ordinary
	// missing-artifact error, naming the request and the platform.
	_, err = reg.For("25.8")
	var ae *ArtifactError
	if !errors.Is(err, ErrArtifactMissing) || !errors.As(err, &ae) || ae.Request != "25.8" || ae.Platform != platformKey {
		t.Errorf("a miss = %v", err)
	}
	// A refused spelling is misuse, not an artifact problem.
	var ue *UsageError
	if _, err := reg.For("v26.8"); !errors.As(err, &ue) {
		t.Errorf("a refused spelling = %v, want a *UsageError", err)
	}
	// The library works.
	if _, err := l.ValidateType("UInt8"); err != nil {
		t.Errorf("the library opened through the registry: %v", err)
	}
	// Preload opens at construction and never fetches; a request nothing
	// installed answers is the ordinary missing error, from NewRegistry.
	reg2, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: cache, SystemDirs: []string{t.TempDir()}, TrustedKeys: []string{trusted}, Offline: true}), WithPreload("26.8"))
	if err != nil || len(reg2.Libraries()) != 1 {
		t.Errorf("preload: %v, %d libraries", err, len(reg2.Libraries()))
	}
	if _, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: cache, SystemDirs: []string{t.TempDir()}, TrustedKeys: []string{trusted}, Offline: true}), WithPreload("24.3")); !errors.Is(err, ErrArtifactMissing) {
		t.Errorf("a preload nothing answers = %v", err)
	}
}

// TestRegistryStep5DisagreementIsCorrupt: the signed statement and the bytes
// disagree (the predicate names another core_commit than the library reports),
// which is the one family the fetch layer's own corruption belongs to.
func TestRegistryStep5DisagreementIsCorrupt(t *testing.T) {
	resetSetup(t)
	stubDir(t)
	lib, err := os.ReadFile(stubFile(t, "ok"))
	if err != nil {
		t.Fatal(err)
	}
	pred, platformKey := stubPredicate(t, lib, hostLibName())
	if platformKey != runtime.GOOS+"-"+runtime.GOARCH {
		t.Skipf("SKIPPED: this host (%s-%s) is not the stub's platform", runtime.GOOS, runtime.GOARCH)
	}
	pred["core_commit"] = "dddddddddddddddddddddddddddddddddddddddd"
	cache := t.TempDir()
	writeSignedLayout(t, cache, "26.8", hostLibName(), platformKey, lib, pred)
	_, trusted := loadTestKey(t)
	reg, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: cache, SystemDirs: []string{t.TempDir()}, TrustedKeys: []string{trusted}, Offline: true}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("26.8")
	var ae *ArtifactError
	if !errors.Is(err, ErrArtifactCorrupt) || !errors.As(err, &ae) || ae.Reason != "build_info_mismatch:core_commit" {
		t.Errorf("a step 5 disagreement = %v, want ErrArtifactCorrupt with the field named", err)
	}
}

// TestRegistryUntrustedWithoutTheTestKey: the test key is trusted only by
// explicit opt-in; by default the same layout is refused.
func TestRegistryUntrustedWithoutTheTestKey(t *testing.T) {
	resetSetup(t)
	stubDir(t)
	lib, err := os.ReadFile(stubFile(t, "ok"))
	if err != nil {
		t.Fatal(err)
	}
	pred, platformKey := stubPredicate(t, lib, hostLibName())
	if platformKey != runtime.GOOS+"-"+runtime.GOARCH {
		t.Skipf("SKIPPED: this host (%s-%s) is not the stub's platform", runtime.GOOS, runtime.GOARCH)
	}
	cache := t.TempDir()
	writeSignedLayout(t, cache, "26.8", hostLibName(), platformKey, lib, pred)
	reg, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: cache, SystemDirs: []string{t.TempDir()}, Offline: true}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("26.8")
	if err == nil {
		t.Fatal("a layout signed by the test key opened with the default trust: the test key must never be trusted by default")
	}
	// The same layout opened with the key trusted (the control above) works,
	// so this refusal is the trust decision and not a malformed layout.
	t.Logf("refused by default trust: %v", err)
}
