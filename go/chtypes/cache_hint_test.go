package chtypes

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestMissingFromAZeroXRegistryNamesIt: the registry's own MISSING carries the
// fetch layer's 0.x hint, the same sentence the offline fetch gives (public
// issue #486).
func TestMissingFromAZeroXRegistryNamesIt(t *testing.T) {
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
