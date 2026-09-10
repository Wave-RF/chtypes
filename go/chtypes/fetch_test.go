package chtypes

// fetch_test.go — the fetch contract (docs/fetch.md), offline.
//
// Every test here builds a miniature release in a temp directory — tiny
// fake libraries whose manifests hash correctly, signed with an ephemeral
// ed25519 key — and drives Ensure at it through file://, a plain
// directory, or a loopback httptest server that counts what was read.
// The shared fixtures under spec/fixtures/fetch/ are exercised by
// fetch_fixtures_test.go with the same expectations.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// isolateEnv points every environment knob the fetch reads at nothing (or
// at a fresh temp cache) so a test sees only what it set up. Returns the
// XDG_CACHE_HOME it created.
func isolateEnv(t *testing.T) string {
	t.Helper()
	cache := t.TempDir()
	for _, k := range []string{envRegistry, envAutoFetch, envTrustedKeys, envAllowUnsign, envTarget, envArtifactsURL, envDownloadTok} {
		t.Setenv(k, "")
	}
	t.Setenv("XDG_CACHE_HOME", cache)
	return cache
}

func newTestKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}

func trustKey(t *testing.T, pub ed25519.PublicKey) {
	t.Helper()
	t.Setenv(envTrustedKeys, hex.EncodeToString(pub))
}

// testArtifact is one fake artifact: a tiny "library" whose bytes are
// whatever the test says.
type testArtifact struct {
	os, arch, version, minor, library string
	content                           []byte
}

func fakeArtifact(platform, version string) testArtifact {
	parts := strings.SplitN(platform, "-", 2)
	lib := "libchtypes.so"
	if parts[0] == "darwin" {
		lib = "libchtypes.dylib"
	}
	return testArtifact{
		os: parts[0], arch: parts[1], version: version, minor: minorOf(version), library: lib,
		content: []byte("fake chtypes library for ClickHouse " + version + " on " + platform + "\n"),
	}
}

func sha256Hex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// tarballOf packs one artifact directory the way a release does: manifest,
// library, CH_VERSION, unsafe_families.txt at the tar root.
func tarballOf(t *testing.T, a testArtifact) []byte {
	t.Helper()
	manifest, _ := json.MarshalIndent(map[string]any{
		"os": a.os, "arch": a.arch, "clickhouse_version": a.version, "clickhouse_minor": a.minor,
		"library": a.library, "library_bytes": len(a.content), "library_sha256": sha256Hex(a.content),
	}, "", " ")
	files := []struct {
		name string
		mode int64
		body []byte
	}{
		{"manifest.json", 0o644, manifest},
		{a.library, 0o755, a.content},
		{"CH_VERSION", 0o644, []byte(a.version + "\n")},
		{"unsafe_families.txt", 0o644, nil},
	}
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, f := range files {
		if err := tw.WriteHeader(&tar.Header{Name: f.name, Mode: f.mode, Size: int64(len(f.body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(f.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// writeRelease lays out a release directory: the tarballs, index.json,
// SHA256SUMS and — with a key — SHA256SUMS.sig in the §4 format.
func writeRelease(t *testing.T, dir string, priv ed25519.PrivateKey, arts ...testArtifact) {
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
	if priv != nil {
		sig := ed25519.Sign(priv, []byte(sums.String()))
		body := "untrusted comment: chtypes artifacts, ed25519 key " + KeyID(priv.Public().(ed25519.PublicKey)) + "\n" +
			base64.StdEncoding.EncodeToString(sig) + "\n"
		if err := os.WriteFile(filepath.Join(dir, "SHA256SUMS.sig"), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// signedRelease is the common setup: one fake artifact for this host,
// signed by a fresh key that the environment trusts.
func signedRelease(t *testing.T, version string) (dir string, pub ed25519.PublicKey, priv ed25519.PrivateKey) {
	t.Helper()
	pub, priv = newTestKey(t)
	dir = filepath.Join(t.TempDir(), "release")
	writeRelease(t, dir, priv, fakeArtifact(HostPlatform(), version))
	trustKey(t, pub)
	return dir, pub, priv
}

func rewrite(t *testing.T, path string, fn func([]byte) []byte) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, fn(b), 0o644); err != nil {
		t.Fatal(err)
	}
}

// countingServer serves a release directory over loopback and counts
// every request by asset name.
type countingServer struct {
	*httptest.Server
	mu   sync.Mutex
	hits map[string]int
	all  atomic.Int64
}

func serveRelease(t *testing.T, dir string) *countingServer {
	t.Helper()
	cs := &countingServer{hits: map[string]int{}}
	fs := http.FileServer(http.Dir(dir))
	cs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cs.mu.Lock()
		cs.hits[strings.TrimPrefix(r.URL.Path, "/")]++
		cs.mu.Unlock()
		cs.all.Add(1)
		fs.ServeHTTP(w, r)
	}))
	t.Cleanup(cs.Close)
	return cs
}

func (cs *countingServer) count(name string) int {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	return cs.hits[name]
}

// wantCode asserts the §7 code, the errors.Is sentinel, the errors.As
// struct and the §6 exit status, all four.
func wantCode(t *testing.T, err error, code ErrorCode) *ArtifactError {
	t.Helper()
	if err == nil {
		t.Fatalf("want %s, got success", code)
	}
	var ae *ArtifactError
	if !errors.As(err, &ae) {
		t.Fatalf("want *ArtifactError %s, got %T: %v", code, err, err)
	}
	if ae.Code != code {
		t.Fatalf("want code %s, got %s: %v", code, ae.Code, err)
	}
	if !errors.Is(err, code.Sentinel()) {
		t.Fatalf("errors.Is(err, %v) is false for %v", code.Sentinel(), err)
	}
	if got := ExitCode(err); got != code.ExitCode() {
		t.Fatalf("ExitCode = %d, want %d", got, code.ExitCode())
	}
	// The §7 missing message is verbatim (no code suffix); every other
	// message carries its code in brackets.
	if code != CodeArtifactMissing && !strings.Contains(err.Error(), string(code)) {
		t.Fatalf("message does not carry the code: %q", err.Error())
	}
	return ae
}

func noArtifactDirs(t *testing.T, dest string) {
	t.Helper()
	entries, err := os.ReadDir(dest)
	if err != nil {
		return // never created: fine
	}
	for _, e := range entries {
		t.Errorf("unexpected entry left in %s: %s", dest, e.Name())
	}
}

// ---------------------------------------------------------------- §4

func TestReleaseKeyReferenceVector(t *testing.T) {
	// docs/fetch.md §4: "hello\n" signs under the release key to this
	// signature; flipping one byte of the message fails.
	sig, err := hex.DecodeString("0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04")
	if err != nil {
		t.Fatal(err)
	}
	sigFile := []byte("untrusted comment: chtypes artifacts, ed25519 key " + ReleaseKeyID + "\n" + base64.StdEncoding.EncodeToString(sig) + "\n")
	key, err := VerifySignature([]byte("hello\n"), sigFile, []ed25519.PublicKey{ReleasePublicKey()})
	if err != nil {
		t.Fatalf("reference vector does not verify under the embedded key: %v", err)
	}
	if KeyID(key) != ReleaseKeyID {
		t.Fatalf("key id %s, want %s", KeyID(key), ReleaseKeyID)
	}
	if _, err := VerifySignature([]byte("hellp\n"), sigFile, []ed25519.PublicKey{ReleasePublicKey()}); err == nil {
		t.Fatal("a flipped message byte verified")
	}
	if KeyID(ReleasePublicKey()) != ReleaseKeyID {
		t.Fatalf("KeyID(embedded) = %s, want %s", KeyID(ReleasePublicKey()), ReleaseKeyID)
	}
}

func TestParseTrustedKeys(t *testing.T) {
	pub, _ := newTestKey(t)
	keys, err := ParseTrustedKeys(" " + hex.EncodeToString(pub) + " , " + ReleasePublicKeyHex + ",")
	if err != nil || len(keys) != 2 {
		t.Fatalf("keys=%d err=%v", len(keys), err)
	}
	for _, bad := range []string{"", "zz", ReleasePublicKeyHex[:60], "not hex at all"} {
		if _, err := ParseTrustedKeys(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

func TestParseSignatureFile(t *testing.T) {
	_, sig, err := ParseSignatureFile([]byte("untrusted comment: x\n" + base64.RawStdEncoding.EncodeToString(make([]byte, 64)) + "\n"))
	if err != nil || len(sig) != 64 {
		t.Fatalf("raw base64: sig=%d err=%v", len(sig), err)
	}
	for _, bad := range []string{"", "untrusted comment: only\n", "not base64!\n", base64.StdEncoding.EncodeToString(make([]byte, 63)) + "\n"} {
		if _, _, err := ParseSignatureFile([]byte(bad)); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

// ---------------------------------------------------------------- §2

func TestParseSpelling(t *testing.T) {
	cases := []struct{ in, line, exact string }{
		{"25.8", "25.8", ""}, {"v25.8", "25.8", ""}, {"25.10", "25.10", ""},
		{"25.8.28.1", "25.8", "25.8.28.1"}, {"v25.8.28.1-lts", "25.8", "25.8.28.1-lts"},
		{"26.7.3.19-stable", "26.7", "26.7.3.19-stable"}, {"25.8.28", "25.8", ""},
	}
	for _, c := range cases {
		line, exact, err := parseSpelling(c.in)
		if err != nil || line != c.line || exact != c.exact {
			t.Errorf("%q -> (%q, %q, %v), want (%q, %q)", c.in, line, exact, err, c.line, c.exact)
		}
	}
	for _, bad := range []string{"", "25", "abc", "25.x", "25..8", "latest"} {
		if _, _, err := parseSpelling(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

// ---------------------------------------------------------------- §3

func TestFetchSignedRelease(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	dest := filepath.Join(t.TempDir(), "reg")
	var progress bytes.Buffer
	opts := FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress}

	inst, err := Ensure(context.Background(), "25.8", opts)
	if err != nil {
		t.Fatalf("Ensure: %v\n%s", err, progress.String())
	}
	want := fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	if inst.Dir != filepath.Join(dest, "25.8") || inst.Line != "25.8" || inst.Version != "25.8.28.1-lts" ||
		inst.Library != want.library || inst.LibrarySHA256 != sha256Hex(want.content) || inst.AlreadyInstalled ||
		inst.Platform != HostPlatform() || inst.SignedBy == "" || inst.File == "" || inst.SHA256 == "" {
		t.Fatalf("Installed = %+v", inst)
	}
	// The registry layout: manifest, library, CH_VERSION, unsafe_families.txt.
	for _, name := range []string{"manifest.json", want.library, "CH_VERSION", "unsafe_families.txt"} {
		if _, err := os.Stat(filepath.Join(inst.Dir, name)); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	got, _ := os.ReadFile(filepath.Join(inst.Dir, want.library))
	if !bytes.Equal(got, want.content) {
		t.Fatalf("installed library bytes differ")
	}
	// No temp siblings survive.
	entries, _ := os.ReadDir(dest)
	if len(entries) != 1 || entries[0].Name() != "25.8" {
		t.Fatalf("dest holds %v, want [25.8]", entries)
	}
	if !strings.Contains(progress.String(), "signature verified") || !strings.Contains(progress.String(), "installed and verified") {
		t.Fatalf("progress:\n%s", progress.String())
	}

	// Idempotent: installed-and-verified is a no-op.
	progress.Reset()
	again, err := Ensure(context.Background(), "25.8", opts)
	if err != nil || !again.AlreadyInstalled || again.Dir != inst.Dir {
		t.Fatalf("second Ensure: %+v %v", again, err)
	}
	if !strings.Contains(progress.String(), "already installed and verified") || strings.Contains(progress.String(), "downloading") {
		t.Fatalf("second progress:\n%s", progress.String())
	}

	// --force re-downloads.
	progress.Reset()
	opts.Force = true
	forced, err := Ensure(context.Background(), "25.8", opts)
	if err != nil || forced.AlreadyInstalled {
		t.Fatalf("forced Ensure: %+v %v", forced, err)
	}
	if !strings.Contains(progress.String(), "downloading") {
		t.Fatalf("forced progress:\n%s", progress.String())
	}

	// A registry can be opened on it — the fake library will not dlopen,
	// which is the loud error, not a skip.
	if _, err := NewRegistry(dest); err == nil {
		t.Fatal("a fake library dlopen'd")
	}
	// ListInstalled / VerifyInstalled see it.
	list, err := ListInstalled(dest)
	if err != nil || len(list) != 1 || list[0].Version != "25.8.28.1-lts" || list[0].Platform != HostPlatform() {
		t.Fatalf("ListInstalled = %+v, %v", list, err)
	}
	vr, err := VerifyInstalled(dest)
	if err != nil || len(vr) != 1 || !vr[0].OK {
		t.Fatalf("VerifyInstalled = %+v, %v", vr, err)
	}
	// Corrupt the installed library: verify says so, and Ensure replaces it.
	os.WriteFile(filepath.Join(inst.Dir, want.library), []byte("rot"), 0o755)
	vr, _ = VerifyInstalled(dest)
	if vr[0].OK {
		t.Fatal("a rotted library verified")
	}
	opts.Force = false
	progress.Reset()
	repaired, err := Ensure(context.Background(), "25.8", opts)
	if err != nil || repaired.AlreadyInstalled {
		t.Fatalf("repair: %+v %v\n%s", repaired, err, progress.String())
	}
	if !strings.Contains(progress.String(), "replacing") {
		t.Fatalf("repair progress:\n%s", progress.String())
	}
	if vr, _ = VerifyInstalled(dest); !vr[0].OK {
		t.Fatal("repair did not verify")
	}
}

func TestFetchBadSignature(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	rewrite(t, filepath.Join(rel, "SHA256SUMS.sig"), func(b []byte) []byte {
		// Flip one byte inside the base64 of the signature (the second line).
		lines := strings.SplitN(string(b), "\n", 2)
		sig, _ := base64.StdEncoding.DecodeString(strings.TrimSpace(lines[1]))
		sig[10] ^= 0xff
		return []byte(lines[0] + "\n" + base64.StdEncoding.EncodeToString(sig) + "\n")
	})
	dest := filepath.Join(t.TempDir(), "reg")
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	wantCode(t, err, CodeArtifactUntrusted)
	noArtifactDirs(t, dest)

	// Signed by a key the environment does not trust: also untrusted.
	rel2 := filepath.Join(t.TempDir(), "other")
	_, otherPriv := newTestKey(t)
	writeRelease(t, rel2, otherPriv, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel2, Dest: dest})
	wantCode(t, err, CodeArtifactUntrusted)
	noArtifactDirs(t, dest)

	// The SUMS were edited after signing: the signature no longer covers them.
	rel3 := filepath.Join(t.TempDir(), "edited")
	pub3, priv3 := newTestKey(t)
	writeRelease(t, rel3, priv3, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	rewrite(t, filepath.Join(rel3, "SHA256SUMS"), func(b []byte) []byte { return append(b, []byte("0000  extra\n")...) })
	trustKey(t, pub3)
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel3, Dest: dest})
	wantCode(t, err, CodeArtifactUntrusted)
	noArtifactDirs(t, dest)
}

func TestFetchUnsignedRefusedUnlessAllowed(t *testing.T) {
	isolateEnv(t)
	rel := filepath.Join(t.TempDir(), "release")
	writeRelease(t, rel, nil, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	dest := filepath.Join(t.TempDir(), "reg")
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	ae := wantCode(t, err, CodeArtifactUntrusted)
	if !strings.Contains(ae.Msg, "unsigned") {
		t.Fatalf("message: %s", ae.Msg)
	}
	noArtifactDirs(t, dest)

	// CHTYPES_ALLOW_UNSIGNED=1: installs, with one loud warning naming the source.
	t.Setenv(envAllowUnsign, "1")
	var progress bytes.Buffer
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress})
	if err != nil {
		t.Fatalf("allowed: %v", err)
	}
	if inst.SignedBy != "" {
		t.Fatalf("SignedBy = %q for an unsigned release", inst.SignedBy)
	}
	if !strings.Contains(progress.String(), "WARNING") || !strings.Contains(progress.String(), rel) {
		t.Fatalf("no loud warning naming the source:\n%s", progress.String())
	}
	// The option spelling, with the sig present but wrong: also skipped, loudly.
	t.Setenv(envAllowUnsign, "")
	os.WriteFile(filepath.Join(rel, "SHA256SUMS.sig"), []byte("untrusted comment: nonsense\n"+base64.StdEncoding.EncodeToString(make([]byte, 64))+"\n"), 0o644)
	progress.Reset()
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress, Force: true, AllowUnsigned: true}); err != nil {
		t.Fatalf("AllowUnsigned option: %v", err)
	}
	if !strings.Contains(progress.String(), "WARNING") {
		t.Fatalf("no warning:\n%s", progress.String())
	}
}

func TestFetchTamperedTarball(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	// Same length, different bytes — so only the hash, never a size check, catches it.
	entries, _ := filepath.Glob(filepath.Join(rel, "*.tar.gz"))
	rewrite(t, entries[0], func(b []byte) []byte { b[len(b)-1] ^= 0x01; return b })
	dest := filepath.Join(t.TempDir(), "reg")
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	ae := wantCode(t, err, CodeArtifactCorrupt)
	if !strings.Contains(ae.Msg, "NOT unpacking") {
		t.Fatalf("message: %s", ae.Msg)
	}
	noArtifactDirs(t, dest)

	// A truncated download: the byte count disagrees first.
	rewrite(t, entries[0], func(b []byte) []byte { return b[:len(b)-7] })
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	ae = wantCode(t, err, CodeArtifactCorrupt)
	if !strings.Contains(ae.Msg, "bytes") {
		t.Fatalf("message: %s", ae.Msg)
	}
	noArtifactDirs(t, dest)
}

func TestFetchTamperedInside(t *testing.T) {
	// The tarball hashes right (the index and sums were built from it) but
	// its manifest disagrees with the index: a release built from a
	// different artifact than it lists.
	isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	a := fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	rel := filepath.Join(t.TempDir(), "release")
	writeRelease(t, rel, priv, a)
	rewrite(t, filepath.Join(rel, "index.json"), func(b []byte) []byte {
		return bytes.Replace(b, []byte(`"library": "`+a.library+`"`), []byte(`"library": "libother.so"`), 1)
	})
	dest := filepath.Join(t.TempDir(), "reg")
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	ae := wantCode(t, err, CodeArtifactCorrupt)
	if !strings.Contains(ae.Msg, "disagrees with index.json") {
		t.Fatalf("message: %s", ae.Msg)
	}
	noArtifactDirs(t, dest)
}

func TestFetchSumsIndexMismatch(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	// index.json is not signed; a wrong sha256 there must be caught by the
	// signed SHA256SUMS disagreeing with it, before any download.
	rewrite(t, filepath.Join(rel, "index.json"), func(b []byte) []byte {
		var doc map[string]any
		json.Unmarshal(b, &doc)
		row := doc["artifacts"].([]any)[0].(map[string]any)
		row["sha256"] = strings.Repeat("0", 64)
		out, _ := json.Marshal(doc)
		return out
	})
	dest := filepath.Join(t.TempDir(), "reg")
	var progress bytes.Buffer
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress})
	ae := wantCode(t, err, CodeArtifactCorrupt)
	if !strings.Contains(ae.Msg, "disagrees with itself") {
		t.Fatalf("message: %s", ae.Msg)
	}
	if strings.Contains(progress.String(), "downloading") {
		t.Fatalf("downloaded despite the mismatch:\n%s", progress.String())
	}
	noArtifactDirs(t, dest)
}

func TestFetchUnpublished(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	rel := filepath.Join(t.TempDir(), "release")
	writeRelease(t, rel, priv, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	dest := filepath.Join(t.TempDir(), "reg")
	base := FetchOptions{URL: "file://" + rel, Dest: dest}

	// A line the release does not carry.
	_, err := Ensure(context.Background(), "26.7", base)
	wantCode(t, err, CodeArtifactUnpublished)
	// An exact patch it does not carry — a hard requirement, never the neighbour.
	_, err = Ensure(context.Background(), "25.8.30.16-lts", base)
	ae := wantCode(t, err, CodeArtifactUnpublished)
	if !strings.Contains(ae.Msg, "exactly") {
		t.Fatalf("message: %s", ae.Msg)
	}
	// The exact patch it does carry.
	if _, err := Ensure(context.Background(), "v25.8.28.1-lts", base); err != nil {
		t.Fatalf("exact: %v", err)
	}
	// The channel may be left off — "25.8.28.1" is the "-lts" patch — but a
	// spelled channel must match (docs/fetch.md, Decisions).
	if _, err := Ensure(context.Background(), "25.8.28.1", base); err != nil {
		t.Fatalf("exact without channel: %v", err)
	}
	_, err = Ensure(context.Background(), "25.8.28.1-stable", base)
	wantCode(t, err, CodeArtifactUnpublished)
	// A platform it does not carry.
	other := "linux-amd64"
	if HostPlatform() == other {
		other = "linux-arm64"
	}
	o := base
	o.Platform = other
	o.Dest = filepath.Join(t.TempDir(), other)
	_, err = Ensure(context.Background(), "25.8", o)
	ae = wantCode(t, err, CodeArtifactUnpublished)
	if ae.Platform != other {
		t.Fatalf("Platform = %s", ae.Platform)
	}
	noArtifactDirs(t, o.Dest)
}

func TestFetchOfflineNeverTouchesTheSource(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")

	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest, Offline: true})
	wantCode(t, err, CodeSourceUnreachable)
	if n := srv.all.Load(); n != 0 {
		t.Fatalf("--offline made %d request(s)", n)
	}
	noArtifactDirs(t, dest)

	// Install it online, then --offline answers from the manifest with no request.
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest}); err != nil {
		t.Fatal(err)
	}
	before := srv.all.Load()
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest, Offline: true})
	if err != nil || !inst.AlreadyInstalled || inst.Version != "25.8.28.1-lts" {
		t.Fatalf("offline installed: %+v %v", inst, err)
	}
	if srv.all.Load() != before {
		t.Fatal("--offline made a request for an installed line")
	}
	// --force needs the source.
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest, Offline: true, Force: true})
	wantCode(t, err, CodeSourceUnreachable)
	// A rotted install is corrupt, not silently accepted, and not re-fetched offline.
	os.WriteFile(filepath.Join(dest, "25.8", fakeArtifact(HostPlatform(), "x").library), []byte("rot"), 0o755)
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest, Offline: true})
	wantCode(t, err, CodeArtifactCorrupt)
	if srv.all.Load() != before {
		t.Fatal("--offline made a request")
	}
	// --all and list need the index.
	_, err = FetchAll(context.Background(), FetchOptions{URL: srv.URL, Dest: dest, Offline: true})
	wantCode(t, err, CodeSourceUnreachable)
	_, err = ListRelease(context.Background(), FetchOptions{URL: srv.URL, Offline: true})
	wantCode(t, err, CodeSourceUnreachable)
	if srv.all.Load() != before {
		t.Fatal("--offline made a request")
	}
}

func TestFetchHTTPSource(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL + "/", Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"index.json", "SHA256SUMS", "SHA256SUMS.sig", inst.File} {
		if srv.count(name) != 1 {
			t.Errorf("%s fetched %d times", name, srv.count(name))
		}
	}
	// A listed asset the host does not have: unreachable, nothing installed.
	os.Remove(filepath.Join(rel, inst.File))
	dest2 := filepath.Join(t.TempDir(), "reg2")
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest2})
	wantCode(t, err, CodeSourceUnreachable)
	noArtifactDirs(t, dest2)
	// No index.json at all: not a release.
	os.Remove(filepath.Join(rel, "index.json"))
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest2})
	wantCode(t, err, CodeSourceUnreachable)
	// A closed port: unreachable (after the retries).
	dead := httptest.NewServer(http.NotFoundHandler())
	url := dead.URL
	dead.Close()
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: url, Dest: dest2})
	wantCode(t, err, CodeSourceUnreachable)
	// The default source is the artifacts host under the tag, both from the env.
	t.Setenv(envArtifactsURL, srv.URL)
	src, err := newSource("", "", nil)
	if err != nil || src.String() != srv.URL+"/"+DefaultReleaseTag || !src.remote {
		t.Fatalf("default source = %v, %v", src, err)
	}
	src, _ = newSource("", "v1.2.0", nil)
	if src.String() != srv.URL+"/v1.2.0" {
		t.Fatalf("tagged source = %v", src)
	}
	if _, err := newSource("file:///x", "v1", nil); err == nil {
		t.Fatal("--url with --tag accepted")
	}
}

func TestFetchPlainDirectoryAndTrustedKeys(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	rel := filepath.Join(t.TempDir(), "release")
	writeRelease(t, rel, priv, fakeArtifact(HostPlatform(), "25.8.28.1-lts"))
	dest := filepath.Join(t.TempDir(), "reg")
	// With only the embedded key trusted, a test-key release is untrusted…
	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: rel, Dest: dest})
	wantCode(t, err, CodeArtifactUntrusted)
	// …the TrustedKeys option admits it, through a plain directory source…
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: rel, Dest: dest, TrustedKeys: []ed25519.PublicKey{pub}}); err != nil {
		t.Fatal(err)
	}
	// …and CHTYPES_TRUSTED_KEYS REPLACES the embedded list: the release key
	// alone is no longer trusted once the env names another key.
	trustKey(t, pub)
	keys, from, err := trustedKeys(nil)
	if err != nil || from != envTrustedKeys || len(keys) != 1 || !bytes.Equal(keys[0], pub) {
		t.Fatalf("trustedKeys = %v %s %v", keys, from, err)
	}
	t.Setenv(envTrustedKeys, "garbage")
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: rel, Dest: dest, Force: true}); err == nil {
		t.Fatal("a malformed trust list was accepted")
	}
}

func TestFetchLockAndFrozen(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	dest := filepath.Join(t.TempDir(), "reg")
	lock := filepath.Join(t.TempDir(), "chtypes.lock")

	// --lock records what was installed.
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, LockFile: lock})
	if err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil || l.Schema != 1 {
		t.Fatalf("lock: %+v %v", l, err)
	}
	key := LockKey(HostPlatform(), "25.8")
	if e := l.Artifacts[key]; e.File != inst.File || e.SHA256 != inst.SHA256 {
		t.Fatalf("lock entry %+v, want %s %s", e, inst.File, inst.SHA256)
	}
	raw, _ := os.ReadFile(lock)
	if !strings.Contains(string(raw), `"schema": 1`) || !strings.Contains(string(raw), key) {
		t.Fatalf("lock bytes:\n%s", raw)
	}

	// --frozen with a matching lock: fine, both installed and fresh.
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, LockFile: lock, Frozen: true}); err != nil {
		t.Fatalf("frozen match: %v", err)
	}
	fresh := filepath.Join(t.TempDir(), "fresh")
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: fresh, LockFile: lock, Frozen: true}); err != nil {
		t.Fatalf("frozen fresh: %v", err)
	}

	// The release moved on: a different asset for the line is refused.
	rel2 := filepath.Join(t.TempDir(), "release2")
	pub2, priv2 := newTestKey(t)
	writeRelease(t, rel2, priv2, fakeArtifact(HostPlatform(), "25.8.30.16-lts"))
	trustKey(t, pub2)
	dest2 := filepath.Join(t.TempDir(), "reg2")
	var progress bytes.Buffer
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel2, Dest: dest2, LockFile: lock, Frozen: true, Progress: &progress})
	wantCode(t, err, CodeArtifactPinned)
	if strings.Contains(progress.String(), "downloading") {
		t.Fatal("downloaded under --frozen mismatch")
	}
	noArtifactDirs(t, dest2)
	// Same asset name, different bytes (a republished tarball): refused too.
	rel3 := filepath.Join(t.TempDir(), "release3")
	a := fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	a.content = append(a.content, '!')
	writeRelease(t, rel3, priv2, a)
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel3, Dest: dest2, LockFile: lock, Frozen: true})
	wantCode(t, err, CodeArtifactPinned)
	// A line the lock does not pin: refused.
	rel4 := filepath.Join(t.TempDir(), "release4")
	writeRelease(t, rel4, priv2, fakeArtifact(HostPlatform(), "26.7.3.19-stable"))
	_, err = Ensure(context.Background(), "26.7", FetchOptions{URL: "file://" + rel4, Dest: dest2, LockFile: lock, Frozen: true})
	wantCode(t, err, CodeArtifactPinned)
	// No lock file at all: refused (rel4 is signed by the key now trusted;
	// the signature verdict comes before the pin verdict).
	_, err = Ensure(context.Background(), "26.7", FetchOptions{URL: "file://" + rel4, Dest: dest2, LockFile: filepath.Join(t.TempDir(), "none.lock"), Frozen: true})
	wantCode(t, err, CodeArtifactPinned)
	noArtifactDirs(t, dest2)
	// --frozen is read-only: the lock never gains the 26.7 entry.
	l, _ = ReadLockFile(lock)
	if len(l.Artifacts) != 1 {
		t.Fatalf("lock grew under --frozen: %+v", l.Artifacts)
	}
	// A second --lock fetch merges rather than overwrites.
	trustKey(t, pub2)
	if _, err := Ensure(context.Background(), "26.7", FetchOptions{URL: "file://" + rel4, Dest: dest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	l, _ = ReadLockFile(lock)
	if len(l.Artifacts) != 2 {
		t.Fatalf("lock after merge: %+v", l.Artifacts)
	}
	// An unreadable lock schema is refused.
	os.WriteFile(lock, []byte(`{"schema": 2, "artifacts": {}}`), 0o644)
	if _, err := ReadLockFile(lock); err == nil {
		t.Fatal("schema 2 read")
	}
}

func TestFetchAllAndListRelease(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	rel := filepath.Join(t.TempDir(), "release")
	other := "linux-amd64"
	if HostPlatform() == other {
		other = "linux-arm64"
	}
	writeRelease(t, rel, priv,
		fakeArtifact(HostPlatform(), "25.10.7.6-stable"),
		fakeArtifact(HostPlatform(), "25.8.28.1-lts"),
		fakeArtifact(HostPlatform(), "25.8.30.16-lts"), // two patches on one line: the newer wins
		fakeArtifact(HostPlatform(), "24.8.14.39-lts"),
		fakeArtifact(other, "25.8.28.1-lts"),
	)
	dest := filepath.Join(t.TempDir(), "reg")
	var progress bytes.Buffer
	all, err := FetchAll(context.Background(), FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress})
	if err != nil {
		t.Fatalf("FetchAll: %v\n%s", err, progress.String())
	}
	var lines, versions []string
	for _, inst := range all {
		lines = append(lines, inst.Line)
		versions = append(versions, inst.Version)
	}
	if strings.Join(lines, " ") != "24.8 25.8 25.10" || strings.Join(versions, " ") != "24.8.14.39-lts 25.8.30.16-lts 25.10.7.6-stable" {
		t.Fatalf("FetchAll installed %v / %v", lines, versions)
	}
	list, _ := ListInstalled(dest)
	if len(list) != 3 || list[2].Line != "25.10" {
		t.Fatalf("ListInstalled = %+v", list)
	}
	// The line "25.8" resolves to the newest patch published for it.
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest})
	if err != nil || inst.Version != "25.8.30.16-lts" || !inst.AlreadyInstalled {
		t.Fatalf("25.8 -> %+v %v", inst, err)
	}
	idx, err := ListRelease(context.Background(), FetchOptions{URL: "file://" + rel})
	if err != nil || len(idx.Artifacts) != 5 || idx.SignedBy != KeyID(pub) || idx.Source != rel || idx.License == "" {
		t.Fatalf("ListRelease = %+v %v", idx, err)
	}
	// Schema drift is refused rather than guessed at.
	rewrite(t, filepath.Join(rel, "index.json"), func(b []byte) []byte { return bytes.Replace(b, []byte(`"schema": 1`), []byte(`"schema": 2`), 1) })
	_, err = ListRelease(context.Background(), FetchOptions{URL: "file://" + rel})
	wantCode(t, err, CodeArtifactCorrupt)
}

func TestFetchCrossPlatformDest(t *testing.T) {
	cache := isolateEnv(t)
	other := "linux-amd64"
	if HostPlatform() == other {
		other = "linux-arm64"
	}
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	rel := filepath.Join(t.TempDir(), "release")
	writeRelease(t, rel, priv, fakeArtifact(other, "25.8.28.1-lts"))
	// $CHTYPES_REGISTRY is a directory this host dlopens from; a fetch for
	// another platform must not write there, only into that platform's cache.
	t.Setenv(envRegistry, filepath.Join(t.TempDir(), "hostreg"))
	var progress bytes.Buffer
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Platform: other, Progress: &progress})
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(cache, "chtypes", "artifacts", other, "25.8")
	if inst.Dir != want || inst.Platform != other {
		t.Fatalf("Dir = %s, want %s (%+v)", inst.Dir, want, inst)
	}
	if !strings.Contains(progress.String(), "note — fetching "+other) {
		t.Fatalf("no cross-platform note:\n%s", progress.String())
	}
	// For this host, $CHTYPES_REGISTRY is where a fetch writes (§1).
	if got := FetchRegistryDir(""); got != os.Getenv(envRegistry) {
		t.Fatalf("FetchRegistryDir = %s", got)
	}
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Platform: "windows-amd64"}); err == nil {
		t.Fatal("a bad platform key was accepted")
	}
}

func TestUntarRefusesEscapes(t *testing.T) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	tw.WriteHeader(&tar.Header{Name: "../escape", Mode: 0o644, Size: 1, Typeflag: tar.TypeReg})
	tw.Write([]byte("x"))
	tw.Close()
	gz.Close()
	path := filepath.Join(t.TempDir(), "bad.tar.gz")
	os.WriteFile(path, buf.Bytes(), 0o644)
	if err := untarGz(path, t.TempDir()); err == nil || !strings.Contains(err.Error(), "escapes") {
		t.Fatalf("err = %v", err)
	}
	buf.Reset()
	gz = gzip.NewWriter(&buf)
	tw = tar.NewWriter(gz)
	tw.WriteHeader(&tar.Header{Name: "link", Linkname: "/etc/passwd", Typeflag: tar.TypeSymlink})
	tw.Close()
	gz.Close()
	os.WriteFile(path, buf.Bytes(), 0o644)
	if err := untarGz(path, t.TempDir()); err == nil || !strings.Contains(err.Error(), "only files") {
		t.Fatalf("err = %v", err)
	}
}

// ---------------------------------------------------------------- §1 / §7

func TestRegistrySearchPathOrder(t *testing.T) {
	cache := isolateEnv(t)
	host := HostPlatform()
	cacheDir := filepath.Join(cache, "chtypes", "artifacts", host)
	sys := SystemRegistryDirs(host)
	got := RegistrySearchPath("")
	want := append([]string{cacheDir}, sys...)
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("search path = %v, want %v", got, want)
	}
	t.Setenv(envRegistry, "/env/reg")
	got = RegistrySearchPath("/explicit")
	want = append([]string{"/explicit", "/env/reg", cacheDir}, sys...)
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("search path = %v, want %v", got, want)
	}
	// Duplicates collapse; the write directory is the first of (1), (2), (3).
	if got := RegistrySearchPath("/env/reg"); got[0] != "/env/reg" || got[1] != cacheDir {
		t.Fatalf("dedupe: %v", got)
	}
	if FetchRegistryDir("") != "/env/reg" || FetchRegistryDir("/x") != "/x" {
		t.Fatal("FetchRegistryDir")
	}
	t.Setenv(envRegistry, "")
	if FetchRegistryDir("") != cacheDir || DefaultRegistryDir() != cacheDir {
		t.Fatalf("FetchRegistryDir = %s, DefaultRegistryDir = %s, want %s", FetchRegistryDir(""), DefaultRegistryDir(), cacheDir)
	}
	sort.Strings(sys)
	for _, p := range []string{"linux-arm64", "linux-amd64", "darwin-arm64", "darwin-amd64"} {
		if !ValidPlatform(p) {
			t.Errorf("%s invalid", p)
		}
	}
	for _, p := range []string{"", "windows-amd64", "linux-x86_64", "darwin-aarch64", "linux-arm64/"} {
		if ValidPlatform(p) {
			t.Errorf("%s valid", p)
		}
	}
}

func TestMissingArtifactErrorVerbatim(t *testing.T) {
	// A registry opened on the search path alone dlopens nothing until
	// asked, so a fake artifact directory in the cache is enough to make
	// construction succeed and a miss for another line reach §7.
	cache := isolateEnv(t)
	host := HostPlatform()
	cacheDir := filepath.Join(cache, "chtypes", "artifacts", host)
	fake := fakeArtifact(host, "25.8.28.1-lts")
	sub := filepath.Join(cacheDir, "25.8")
	os.MkdirAll(sub, 0o755)
	os.WriteFile(filepath.Join(sub, fake.library), fake.content, 0o755)
	os.WriteFile(filepath.Join(sub, "manifest.json"), []byte(`{"library":"`+fake.library+`","clickhouse_version":"25.8.28.1-lts","clickhouse_minor":"25.8"}`), 0o644)

	reg, err := NewRegistry("")
	if err != nil {
		t.Fatal(err)
	}
	if v := reg.Versions(); len(v) != 1 || v[0] != "25.8" {
		t.Fatalf("Versions = %v", v)
	}
	_, err = reg.For("99.9")
	ae := wantCode(t, err, CodeArtifactMissing)
	if ae.Line != "99.9" || ae.Platform != host {
		t.Fatalf("%+v", ae)
	}
	want := fmt.Sprintf("chtypes: no artifact for ClickHouse 99.9 (%s). Looked in: %s.\n"+
		"Install it:  go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 99.9\n"+
		"or set CHTYPES_AUTOFETCH=1 to fetch on first use.", host, strings.Join(RegistrySearchPath(""), ", "))
	if err.Error() != want {
		t.Fatalf("message:\n%q\nwant:\n%q", err.Error(), want)
	}
	if errors.Is(err, ErrArtifactUntrusted) {
		t.Fatal("matched the wrong sentinel")
	}
	// An exact patch spelling is reported as typed.
	_, err = reg.For("99.9.1.1-lts")
	if ae := wantCode(t, err, CodeArtifactMissing); ae.Line != "99.9.1.1-lts" || !strings.Contains(ae.Msg, "fetch 99.9.1.1-lts") {
		t.Fatalf("%+v", ae)
	}
	// The line that IS on the path is a loud load failure (a fake library),
	// never a skip to a neighbour and never "missing".
	_, err = reg.For("25.8")
	if err == nil || errors.Is(err, ErrArtifactMissing) {
		t.Fatalf("fake library: %v", err)
	}
	// Nothing anywhere and no autofetch: construction says so, naming the path.
	os.RemoveAll(cacheDir)
	if _, err := NewRegistry(""); err == nil || !strings.Contains(err.Error(), cacheDir) {
		t.Fatalf("empty search path: %v", err)
	}
	// With autofetch on, construction succeeds on nothing.
	if _, err := NewRegistry("", WithAutoFetch(true)); err != nil {
		t.Fatalf("autofetch on nothing: %v", err)
	}
	if _, err := NewRegistry(filepath.Join(t.TempDir(), "absent"), WithAutoFetch(true)); err != nil {
		t.Fatalf("autofetch on an absent dir: %v", err)
	}
	if _, err := NewRegistry(filepath.Join(t.TempDir(), "absent")); err == nil {
		t.Fatal("an absent dir opened without autofetch")
	}
}

func TestAutoFetchFailureIsTheFetchError(t *testing.T) {
	// A missing line with autofetch on and a dead source: the fetch's own
	// error comes back (not "missing"), and the next open may try again.
	isolateEnv(t)
	dead := httptest.NewServer(http.NotFoundHandler())
	url := dead.URL
	dead.Close()
	dest := filepath.Join(t.TempDir(), "reg")
	reg, err := NewRegistry(dest, WithAutoFetch(true), WithFetchOptions(FetchOptions{URL: url}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("25.8")
	wantCode(t, err, CodeSourceUnreachable)
	_, err = reg.For("25.8")
	wantCode(t, err, CodeSourceUnreachable)
	// The env spelling turns it on too.
	t.Setenv(envAutoFetch, "1")
	reg, err = NewRegistry(dest, WithFetchOptions(FetchOptions{URL: url}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("25.8")
	wantCode(t, err, CodeSourceUnreachable)
	// A cancelled context stops the fetch.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := reg.ForContext(ctx, "25.8"); err == nil {
		t.Fatal("a cancelled fetch succeeded")
	}
}

// TestLoadReleaseRetriesThroughAPublishWindow is the mid-publish window, made
// real: SHA256SUMS.sig is briefly the wrong signature for the SHA256SUMS beside
// it, exactly as it is while the three objects of a rolling release are being
// replaced one at a time. The first read must refuse it, and the fetch must
// still succeed once the publish lands.
func TestLoadReleaseRetriesThroughAPublishWindow(t *testing.T) {
	isolateEnv(t)
	defer func(d time.Duration) { releaseRetryDelay = d }(releaseRetryDelay)
	releaseRetryDelay = 150 * time.Millisecond

	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	sigPath := filepath.Join(rel, "SHA256SUMS.sig")
	good, err := os.ReadFile(sigPath)
	if err != nil {
		t.Fatal(err)
	}
	// The window: a signature that does not cover these sums.
	rewrite(t, sigPath, mangleSignature)

	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")

	// The publish completes while the first retry is sleeping.
	go func() {
		time.Sleep(50 * time.Millisecond)
		_ = os.WriteFile(sigPath, good, 0o644)
	}()

	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest})
	if err != nil {
		t.Fatalf("a healing publish window must install, got %v", err)
	}
	if inst.Version != "25.8.28.1-lts" {
		t.Fatalf("Version = %s", inst.Version)
	}
	// It really did read the release twice: once refused, once trusted.
	if n := srv.count("SHA256SUMS.sig"); n < 2 {
		t.Fatalf("SHA256SUMS.sig read %d time(s), want >= 2 (the retry did not happen)", n)
	}
}

// TestLoadReleaseStopsRetryingAndRefuses is the same window that never closes:
// the attempts run out and the original code surfaces, unchanged.
func TestLoadReleaseStopsRetryingAndRefuses(t *testing.T) {
	isolateEnv(t)
	defer func(d time.Duration) { releaseRetryDelay = d }(releaseRetryDelay)
	releaseRetryDelay = 10 * time.Millisecond

	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	rewrite(t, filepath.Join(rel, "SHA256SUMS.sig"), mangleSignature)
	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")

	_, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest})
	wantCode(t, err, CodeArtifactUntrusted)
	noArtifactDirs(t, dest)
	if n := srv.count("SHA256SUMS.sig"); n != releaseLoadAttempts {
		t.Fatalf("SHA256SUMS.sig read %d time(s), want exactly %d", n, releaseLoadAttempts)
	}
}

// mangleSignature flips one byte of the base64 signature, leaving the two-line
// shape intact. It must not touch the "untrusted comment:" line: that line is
// not covered by the signature — which is the whole point of its name — so
// editing it changes nothing a verifier looks at.
func mangleSignature(b []byte) []byte {
	lines := bytes.SplitN(b, []byte("\n"), 2)
	if len(lines) != 2 {
		panic("SHA256SUMS.sig is not the two-line format")
	}
	sig := bytes.TrimRight(lines[1], "\n")
	raw, err := base64.StdEncoding.DecodeString(string(sig))
	if err != nil {
		panic("SHA256SUMS.sig line 2 is not base64: " + err.Error())
	}
	raw[0] ^= 0xff
	return []byte(string(lines[0]) + "\n" + base64.StdEncoding.EncodeToString(raw) + "\n")
}
