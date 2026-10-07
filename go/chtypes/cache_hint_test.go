package chtypes

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// TestMissingFromAZeroXRegistryNamesIt: the registry's own MISSING carries the
// fetch layer's 0.x hint, the same sentence the offline fetch gives (public
// issue #486).
func TestMissingFromAZeroXRegistryNamesIt(t *testing.T) {
	// The 0.x upgrade hint is the v1 contract's: a dev SDK reads an explicit
	// cache through its v2-dev subroot, where no 0.x registry sits.
	t.Cleanup(ocifetch.UseFetchV1ForTests())
	zeroX := filepath.Join(t.TempDir(), "zero-x")
	if err := os.MkdirAll(filepath.Join(zeroX, "26.1"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(zeroX, "26.1", "manifest.json"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	r, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: zeroX, SystemDirs: []string{}}), WithAutoFetch(false))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.For("26.1")
	var ae *ArtifactError
	hint := zeroX + " holds a 0.x registry (26.1/manifest.json)"
	if !errors.As(err, &ae) || !errors.Is(err, ErrArtifactMissing) || !strings.Contains(ae.Msg, hint) {
		t.Fatalf("For on a 0.x registry = %v, want MISSING carrying %q", err, hint)
	}
}

// TestStrictCacheIsTheCacheUnusableClass: with StrictCache, the registry's
// open of a 0.x registry is an *ArtifactError with CodeCacheUnusable naming
// the path and the reason, and errors.Is matches ErrCacheUnusable (public
// issue #486).
func TestStrictCacheIsTheCacheUnusableClass(t *testing.T) {
	// The 0.x upgrade hint is the v1 contract's: a dev SDK reads an explicit
	// cache through its v2-dev subroot, where no 0.x registry sits.
	t.Cleanup(ocifetch.UseFetchV1ForTests())
	zeroX := filepath.Join(t.TempDir(), "zero-x")
	if err := os.MkdirAll(filepath.Join(zeroX, "26.1"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(zeroX, "26.1", "manifest.json"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	strict := true
	r, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: zeroX, SystemDirs: []string{}, StrictCache: &strict}), WithAutoFetch(false))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.For("26.1")
	var ae *ArtifactError
	if !errors.As(err, &ae) || !errors.Is(err, ErrCacheUnusable) || ae.Code != CodeCacheUnusable ||
		ae.Path != zeroX || ae.Reason != "layout_0x" {
		t.Fatalf("For on a 0.x registry in strict mode = %#v", err)
	}
	if _, err := r.Installed(); !errors.Is(err, ErrCacheUnusable) {
		t.Fatalf("Installed on a 0.x registry in strict mode = %v", err)
	}
}

// TestARetiredRepositoryIsTheSourceRetiredCode: a registry open that fetches
// from a retired repository (every route answers 410 Gone) is an
// *ArtifactError with CodeSourceRetired, errors.Is matches ErrSourceRetired,
// and the message carries the registry's own (fetch-v1.md section 2, public
// issue #571).
func TestARetiredRepositoryIsTheSourceRetiredCode(t *testing.T) {
	t.Cleanup(ocifetch.UseFetchV1ForTests())
	const message = "chtypes/v1 is retired: use chtypes/v2"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
		_, _ = w.Write([]byte(`{"errors":[{"code":"DENIED","message":"` + message + `"}]}`))
	}))
	defer srv.Close()
	r, err := NewRegistry(WithFetchOptions(FetchOptions{Bases: []string{srv.URL + "/chtypes/v1"}, CacheDir: t.TempDir(), SystemDirs: []string{}}), WithAutoFetch(true))
	if err != nil {
		t.Fatal(err)
	}
	_, err = r.For("26.9")
	var ae *ArtifactError
	if !errors.As(err, &ae) || !errors.Is(err, ErrSourceRetired) || ae.Code != CodeSourceRetired || !strings.Contains(ae.Msg, message) {
		t.Fatalf("For on a retired repository = %#v", err)
	}
}
