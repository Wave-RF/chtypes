package chtypes

// fetch_goldens_dest_test.go — issue #49: installGoldens wrote to f.opts.Dest,
// the raw fetch option, in all four places it needed the RESOLVED registry
// directory (f.dest). With Dest empty (no --dest), os.MkdirAll("", 0o755)
// cannot succeed, installGoldens takes its deliberately non-fatal "cannot
// write" branch, and the fetch still exits 0 — the artifact installs, only
// the golden set silently never appears. This file drives that path the same
// way fetch_test.go drives every other one: a miniature release built and
// signed with an ephemeral key in a temp directory, this time also carrying
// a signed sdk-goldens.json row so installGoldens has something to install.

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeReleaseWithGoldens is writeRelease (fetch_test.go) plus a served,
// signed sdk-goldens.json: a row in SHA256SUMS like any tarball, so it
// verifies through the same signature.
func writeReleaseWithGoldens(t *testing.T, dir string, priv ed25519.PrivateKey, goldens []byte, arts ...testArtifact) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	var rows []map[string]any
	var sums strings.Builder
	for _, a := range arts {
		tb := tarballOf(t, a)
		file := fmt.Sprintf("chtypes-%s-%s-%s.tar.gz", a.version, a.os, a.arch)
		if err := os.WriteFile(filepath.Join(dir, file), tb, 0o644); err != nil {
			t.Fatal(err)
		}
		rows = append(rows, map[string]any{
			"os": a.os, "arch": a.arch, "file": file, "sha256": sha256Hex(tb), "bytes": len(tb),
			"clickhouse_version": a.version, "clickhouse_minor": a.minor,
			"library": a.library, "library_sha256": sha256Hex(a.content),
		})
		fmt.Fprintf(&sums, "%s  %s\n", sha256Hex(tb), file)
	}
	if err := os.WriteFile(filepath.Join(dir, goldensAsset), goldens, 0o644); err != nil {
		t.Fatal(err)
	}
	fmt.Fprintf(&sums, "%s  %s\n", sha256Hex(goldens), goldensAsset)
	index, _ := json.MarshalIndent(map[string]any{
		"schema": 1, "generated_at": "2026-09-09T00:00:00Z", "release_tag": "test",
		"license": "Elastic License 2.0", "license_url": "https://www.elastic.co/licensing/elastic-license",
		"artifacts": rows,
	}, "", " ")
	if err := os.WriteFile(filepath.Join(dir, "index.json"), index, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "SHA256SUMS"), []byte(sums.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	sig := ed25519.Sign(priv, []byte(sums.String()))
	body := "untrusted comment: chtypes artifacts, ed25519 key " + KeyID(priv.Public().(ed25519.PublicKey)) + "\n" +
		base64.StdEncoding.EncodeToString(sig) + "\n"
	if err := os.WriteFile(filepath.Join(dir, "SHA256SUMS.sig"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestFetchInstallsGoldensAtDefaultDest is the issue #49 regression: a fetch
// with NO --dest must still install sdk-goldens.json, next to the artifact,
// in the directory the fetch itself resolved (here $CHTYPES_REGISTRY,
// standing in for the defaulted registry per docs/guides/fetch.md §1).
//
// Asserting only that Ensure returned no error is not enough — the bug's
// whole shape is a fetch that exits 0 while installGoldens silently takes
// its non-fatal "cannot write" branch. This asserts the file that branch was
// supposed to produce.
func TestFetchInstallsGoldensAtDefaultDest(t *testing.T) {
	isolateEnv(t)
	registry := t.TempDir()
	t.Setenv(EnvRegistry, registry)

	pub, priv := newTestKey(t)
	rel := filepath.Join(t.TempDir(), "release")
	goldens := []byte(`{"schema":1,"cases":[]}`)
	writeReleaseWithGoldens(t, rel, priv, goldens, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	trustKey(t, pub)

	var progress bytes.Buffer
	// Dest is deliberately left empty: this is the "no --dest" path.
	opts := FetchOptions{URL: "file://" + rel, Progress: &progress}
	inst, err := Ensure(context.Background(), "25.8", opts)
	if err != nil {
		t.Fatalf("Ensure: %v\n%s", err, progress.String())
	}
	if inst.Dir != filepath.Join(registry, "25.8") {
		t.Fatalf("artifact installed at %s, want under %s", inst.Dir, registry)
	}

	wantPath := filepath.Join(registry, goldensAsset)
	got, err := os.ReadFile(wantPath)
	if err != nil {
		t.Fatalf("%s was not installed: %v\nprogress:\n%s", wantPath, err, progress.String())
	}
	if !bytes.Equal(got, goldens) {
		t.Fatalf("%s = %q, want %q", wantPath, got, goldens)
	}
	if !strings.Contains(progress.String(), "golden set verified and installed: "+wantPath) {
		t.Fatalf("progress does not name the installed path %s:\n%s", wantPath, progress.String())
	}
	// Never the failure branch this bug hid behind.
	if strings.Contains(progress.String(), "could not create") || strings.Contains(progress.String(), "the golden tests will skip") {
		t.Fatalf("progress still shows the non-fatal failure branch:\n%s", progress.String())
	}
}

// TestFetchInstallsGoldensAtExplicitDest is the acceptance criterion that an
// explicit --dest is unchanged: it was never affected (the artifact install
// path already used the resolved directory), and it must stay that way now
// that installGoldens uses the same resolved field.
func TestFetchInstallsGoldensAtExplicitDest(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	rel := filepath.Join(t.TempDir(), "release")
	goldens := []byte(`{"schema":1,"cases":[]}`)
	writeReleaseWithGoldens(t, rel, priv, goldens, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	trustKey(t, pub)

	dest := filepath.Join(t.TempDir(), "reg")
	var progress bytes.Buffer
	opts := FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress}
	if _, err := Ensure(context.Background(), "25.8", opts); err != nil {
		t.Fatalf("Ensure: %v\n%s", err, progress.String())
	}

	wantPath := filepath.Join(dest, goldensAsset)
	got, err := os.ReadFile(wantPath)
	if err != nil {
		t.Fatalf("%s was not installed: %v\nprogress:\n%s", wantPath, err, progress.String())
	}
	if !bytes.Equal(got, goldens) {
		t.Fatalf("%s = %q, want %q", wantPath, got, goldens)
	}
	if !strings.Contains(progress.String(), "golden set verified and installed: "+wantPath) {
		t.Fatalf("progress does not name the installed path %s:\n%s", wantPath, progress.String())
	}
}
