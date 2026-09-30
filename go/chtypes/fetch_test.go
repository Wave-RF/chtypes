package chtypes

// fetch_test.go — the fetch contract (docs/guides/fetch.md), offline.
//
// Every test here builds a miniature release in a temp directory — tiny
// fake libraries whose manifests hash correctly, signed with an ephemeral
// ed25519 key — and drives Ensure at it through file://, a plain
// directory, or a loopback httptest server that counts what was read.
// The shared fixtures under tests/fixtures/fetch/ are exercised by
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
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/wave-rf/chtypes/go/internal/testhook"
)

// isolateEnv points every environment knob the fetch reads at nothing (or
// at a fresh temp cache) so a test sees only what it set up. Returns the
// XDG_CACHE_HOME it created.
func isolateEnv(t *testing.T) string {
	t.Helper()
	cache := t.TempDir()
	for _, k := range []string{EnvRegistry, EnvAutoFetch, envTrustedKeys, envAllowUnsign, envTarget, envArtifactsURL, envDownloadTok} {
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
	// revision is the row's abi_revision; 0 is the revision fetch selects at
	// (fetchABIRevision). undeclared writes no abi_revision at all.
	revision   int
	undeclared bool
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
		row := map[string]any{
			"os": a.os, "arch": a.arch, "file": file, "sha256": sha256Hex(tb), "bytes": len(tb),
			"clickhouse_version": a.version, "clickhouse_minor": a.minor,
			"library": a.library, "library_sha256": sha256Hex(a.content),
		}
		switch {
		case a.undeclared:
		case a.revision != 0:
			row["abi_revision"] = a.revision
		default:
			// The revision fetch is selecting at: the package's own, or the
			// shared fixtures' while their suite runs.
			row["abi_revision"] = fetchABIRevision()
		}
		rows = append(rows, row)
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

	hookMu sync.Mutex
	hooks  map[string]func()
}

func serveRelease(t *testing.T, dir string) *countingServer {
	t.Helper()
	cs := &countingServer{hits: map[string]int{}, hooks: map[string]func(){}}
	fs := http.FileServer(http.Dir(dir))
	cs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(r.URL.Path, "/")
		cs.mu.Lock()
		cs.hits[name]++
		firstServe := cs.hits[name] == 1
		cs.mu.Unlock()
		cs.all.Add(1)
		// Serve first, so the response the caller is waiting on (the bad
		// signature, on the first read) is already written before any hook
		// below is allowed to mutate the file on disk.
		fs.ServeHTTP(w, r)
		if firstServe {
			cs.hookMu.Lock()
			hook := cs.hooks[name]
			cs.hookMu.Unlock()
			if hook != nil {
				hook()
			}
		}
	}))
	t.Cleanup(cs.Close)
	return cs
}

func (cs *countingServer) count(name string) int {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	return cs.hits[name]
}

// afterFirstServe registers fn to run once name has been served for the
// first time — synchronously, in the handler's own goroutine, right after
// the response bytes for that first read are written. It exists so a test
// can heal a file the instant the bad version has actually been read, not
// after some wall-clock guess at when that read will have happened.
func (cs *countingServer) afterFirstServe(name string, fn func()) {
	cs.hookMu.Lock()
	defer cs.hookMu.Unlock()
	cs.hooks[name] = fn
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
	// docs/guides/fetch.md §4: "hello\n" signs under the release key to this
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

func TestParseVersionSpellingIntoLineOrExactPatch(t *testing.T) {
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
	// which is the loud error, not a skip. Construction reads manifests and
	// dlopens nothing, so the open is the thing that has to be asked for.
	reg, err := NewRegistry(dest)
	if err != nil {
		t.Fatalf("a registry over the installed line must construct: %v", err)
	}
	if _, err := reg.For("25.8"); err == nil {
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

func TestFetchRefusesAFlippedSignatureByte(t *testing.T) {
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

func TestFetchRefusesAManifestThatDisagreesWithTheIndex(t *testing.T) {
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
	// An exact patch it does not carry — a hard requirement, never the neighbor.
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
	// spelled channel must match (docs/guides/fetch.md, Decisions).
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

// TestFetchLockRecordsABIRevisionAndFrozenNamesAMismatch is issue #253: a
// lock written by an SDK at one ABI revision must not be silently accepted,
// or surface as a bare drifted-pin or unpublished-line error, once the SDK
// speaks another.
func TestFetchLockRecordsABIRevisionAndFrozenNamesAMismatch(t *testing.T) {
	isolateEnv(t)
	rel, _, _ := signedRelease(t, "25.8.28.1-lts")
	dest := filepath.Join(t.TempDir(), "reg")
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	key := LockKey(HostPlatform(), "25.8")

	// (a) --lock records the row's abi_revision, this package's own.
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: dest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil {
		t.Fatal(err)
	}
	entry := l.Artifacts[key]
	if entry.ABIRevision == nil || *entry.ABIRevision != ABIRevision {
		t.Fatalf("lock entry %+v, want abi_revision %d", entry, ABIRevision)
	}
	raw, _ := os.ReadFile(lock)
	if !strings.Contains(string(raw), fmt.Sprintf(`"abi_revision": %d`, ABIRevision)) {
		t.Fatalf("lock bytes carry no abi_revision:\n%s", raw)
	}

	// (b) A lock entry at a different revision: PINNED, naming both
	// numbers, and — proven by pointing at a source that does not exist —
	// refused WITHOUT ever reading a release.
	mismatched := *l
	mismatched.Artifacts = map[string]LockEntry{}
	for k, v := range l.Artifacts {
		mismatched.Artifacts[k] = v
	}
	other := ABIRevision + 1
	mismatched.Artifacts[key] = LockEntry{File: entry.File, SHA256: entry.SHA256, ABIRevision: &other}
	mismatchedLock := filepath.Join(t.TempDir(), "mismatched.lock")
	if err := mismatched.Write(mismatchedLock); err != nil {
		t.Fatal(err)
	}
	unreachable := filepath.Join(t.TempDir(), "does-not-exist")
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + unreachable, Dest: filepath.Join(t.TempDir(), "reg2"), LockFile: mismatchedLock, Frozen: true})
	ae := wantCode(t, err, CodeArtifactPinned)
	if !strings.Contains(ae.Msg, fmt.Sprintf("ABI revision %d", other)) || !strings.Contains(ae.Msg, fmt.Sprintf("ABI revision %d", ABIRevision)) || !strings.Contains(ae.Msg, "re-lock with:") {
		t.Fatalf("message does not name both revisions and the remedy: %s", ae.Msg)
	}

	// (c) A lock entry with no abi_revision at all (an older SDK's lock):
	// the old path, plus one appended sentence, for both PINNED (a real
	// drift) and UNPUBLISHED (the line is not offered at this revision).
	noRevLock := filepath.Join(t.TempDir(), "no-rev.lock")
	os.WriteFile(noRevLock, []byte(fmt.Sprintf(`{"schema": 1, "artifacts": {%q: {"file": "not-the-file.tar.gz", "sha256": "00"}}}`, key)), 0o644)
	_, err = Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: filepath.Join(t.TempDir(), "reg3"), LockFile: noRevLock, Frozen: true})
	ae = wantCode(t, err, CodeArtifactPinned)
	if !strings.Contains(ae.Msg, "records no ABI revision") || !strings.Contains(ae.Msg, "older SDK") || !strings.Contains(ae.Msg, fmt.Sprintf("ABI revision %d", ABIRevision)) {
		t.Fatalf("drift message carries no old-lock sentence: %s", ae.Msg)
	}

	noRevLock2 := filepath.Join(t.TempDir(), "no-rev2.lock")
	otherKey := LockKey(HostPlatform(), "26.7")
	os.WriteFile(noRevLock2, []byte(fmt.Sprintf(`{"schema": 1, "artifacts": {%q: {"file": "x.tar.gz", "sha256": "00"}}}`, otherKey)), 0o644)
	_, err = Ensure(context.Background(), "26.7", FetchOptions{URL: "file://" + rel, Dest: filepath.Join(t.TempDir(), "reg4"), LockFile: noRevLock2, Frozen: true})
	ae = wantCode(t, err, CodeArtifactUnpublished)
	if !strings.Contains(ae.Msg, "records no ABI revision") || !strings.Contains(ae.Msg, "older SDK") {
		t.Fatalf("unpublished message carries no old-lock sentence: %s", ae.Msg)
	}

	// (d) A lock entry at the SDK's own (matching) revision installs
	// exactly as before — no sentence, no different code.
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: filepath.Join(t.TempDir(), "reg5"), LockFile: lock, Frozen: true}); err != nil {
		t.Fatalf("frozen, matching revision: %v", err)
	}

	// (e) --offline --frozen stays unaffected: no source is ever read, so a
	// mismatched-revision lock is simply not consulted against one.
	offlineDest := filepath.Join(t.TempDir(), "reg6")
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Dest: offlineDest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{Dest: offlineDest, LockFile: mismatchedLock, Frozen: true, Offline: true}); err != nil {
		t.Fatalf("offline+frozen should read only the installed manifest: %v", err)
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
	t.Setenv(EnvRegistry, filepath.Join(t.TempDir(), "hostreg"))
	var progress bytes.Buffer
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + rel, Platform: other, Progress: &progress})
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(cache, "chtypes", "artifacts", "abi"+strconv.Itoa(ABIRevision), other, "25.8")
	if inst.Dir != want || inst.Platform != other {
		t.Fatalf("Dir = %s, want %s (%+v)", inst.Dir, want, inst)
	}
	if !strings.Contains(progress.String(), "note — fetching "+other) {
		t.Fatalf("no cross-platform note:\n%s", progress.String())
	}
	// For this host, $CHTYPES_REGISTRY is where a fetch writes (§1).
	if got := FetchRegistryDir(""); got != os.Getenv(EnvRegistry) {
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
	cacheDir := filepath.Join(cache, "chtypes", "artifacts", "abi"+strconv.Itoa(ABIRevision), host)
	sys := SystemRegistryDirs(host)
	got := RegistrySearchPath("")
	want := append([]string{cacheDir}, sys...)
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("search path = %v, want %v", got, want)
	}
	t.Setenv(EnvRegistry, "/env/reg")
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
	t.Setenv(EnvRegistry, "")
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
	cacheDir := filepath.Join(cache, "chtypes", "artifacts", "abi"+strconv.Itoa(ABIRevision), host)
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
	// never a skip to a neighbor and never "missing".
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
	t.Setenv(EnvAutoFetch, "1")
	reg, err = NewRegistry(dest, WithFetchOptions(FetchOptions{URL: url}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("25.8")
	wantCode(t, err, CodeSourceUnreachable)
	// A canceled context stops the fetch.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := reg.ForContext(ctx, "25.8"); err == nil {
		t.Fatal("a canceled fetch succeeded")
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

	// The publish completes the instant the bad signature has actually been
	// read once — not on a wall-clock guess at when that read happens. Under
	// load, Ensure's first read can be delayed past any fixed sleep, which
	// would let it see the already-healed signature and never retry at all.
	srv.afterFirstServe("SHA256SUMS.sig", func() {
		_ = os.WriteFile(sigPath, good, 0o644)
	})

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

// addReleaseFile adds one release-level file (like sdk-goldens.json) to an
// already-written release: writes its bytes, appends its row to SHA256SUMS,
// and re-signs — exactly what a real publish of a release-level file does.
func addReleaseFile(t *testing.T, dir string, priv ed25519.PrivateKey, name string, content []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), content, 0o644); err != nil {
		t.Fatal(err)
	}
	sums, err := os.ReadFile(filepath.Join(dir, "SHA256SUMS"))
	if err != nil {
		t.Fatal(err)
	}
	sums = append(sums, []byte(fmt.Sprintf("%s  %s\n", sha256Hex(content), name))...)
	if err := os.WriteFile(filepath.Join(dir, "SHA256SUMS"), sums, 0o644); err != nil {
		t.Fatal(err)
	}
	sig := ed25519.Sign(priv, sums)
	body := "untrusted comment: chtypes artifacts, ed25519 key " + KeyID(priv.Public().(ed25519.PublicKey)) + "\n" +
		base64.StdEncoding.EncodeToString(sig) + "\n"
	if err := os.WriteFile(filepath.Join(dir, "SHA256SUMS.sig"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestInstallGoldensWindowHealsAndInstalls is the new
// window symptom (docs/guides/fetch.md §3a): SHA256SUMS, its signature and
// index.json all agree throughout — the served GOLDEN SET is what is stale,
// exactly what an edge cache does to a release-level file just after a
// republish. installGoldens must re-read the whole set (never just re-fetch
// the golden file against its first look's by-then-stale f.sums) and heal
// once the source catches up.
func TestInstallGoldensWindowHealsAndInstalls(t *testing.T) {
	isolateEnv(t)
	defer func(d time.Duration) { releaseRetryDelay = d }(releaseRetryDelay)
	releaseRetryDelay = 20 * time.Millisecond

	rel, _, priv := signedRelease(t, "25.8.28.1-lts")
	good := []byte(`{"generated":{"at":"2026-09-26T00:00:00Z"},"cases":[]}` + "\n")
	stale := []byte(`{"generated":{"at":"2026-09-01T00:00:00Z"},"cases":[]}` + "\n")
	addReleaseFile(t, rel, priv, goldensAsset, good)
	goldenPath := filepath.Join(rel, goldensAsset)
	if err := os.WriteFile(goldenPath, stale, 0o644); err != nil {
		t.Fatal(err)
	}

	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")
	srv.afterFirstServe(goldensAsset, func() {
		_ = os.WriteFile(goldenPath, good, 0o644)
	})

	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: srv.URL, Dest: dest})
	if err != nil {
		t.Fatalf("a healing golden-set window must still install the artifact, got %v", err)
	}
	if inst.Version != "25.8.28.1-lts" {
		t.Fatalf("Version = %s", inst.Version)
	}
	got, err := os.ReadFile(filepath.Join(dest, goldensAsset))
	if err != nil {
		t.Fatalf("golden set was not installed: %v", err)
	}
	if !bytes.Equal(got, good) {
		t.Fatalf("installed golden set = %q, want the healed %q", got, good)
	}
	if n := srv.count(goldensAsset); n < 2 {
		t.Fatalf("%s read %d time(s), want >= 2 (the retry did not happen)", goldensAsset, n)
	}
}

// TestInstallGoldensWindowThatNeverHealsSkipsWithoutFailingTheFetch is the
// same window, but it never closes: the golden set never hashes to what
// SHA256SUMS says. installGoldens must retry through every attempt, then give
// up loudly WITHOUT failing Ensure — a release-level file is best-effort, and
// the artifact the caller asked for is already installed by the time this
// runs.
func TestInstallGoldensWindowThatNeverHealsSkipsWithoutFailingTheFetch(t *testing.T) {
	isolateEnv(t)
	defer func(d time.Duration) { releaseRetryDelay = d }(releaseRetryDelay)
	releaseRetryDelay = 5 * time.Millisecond

	rel, _, priv := signedRelease(t, "25.8.28.1-lts")
	good := []byte(`{"generated":{"at":"2026-09-26T00:00:00Z"},"cases":[]}` + "\n")
	stale := []byte(`{"generated":{"at":"2026-09-01T00:00:00Z"},"cases":[]}` + "\n") // never becomes `good`
	addReleaseFile(t, rel, priv, goldensAsset, good)
	if err := os.WriteFile(filepath.Join(rel, goldensAsset), stale, 0o644); err != nil {
		t.Fatal(err)
	}

	srv := serveRelease(t, rel)
	dest := filepath.Join(t.TempDir(), "reg")
	var buf bytes.Buffer
	opts := FetchOptions{URL: srv.URL, Dest: dest, Progress: &buf}
	inst, err := Ensure(context.Background(), "25.8", opts)
	if err != nil {
		t.Fatalf("a golden-set-only window must not fail Ensure, got %v", err)
	}
	if inst.Version != "25.8.28.1-lts" {
		t.Fatalf("Version = %s", inst.Version)
	}
	if _, err := os.Stat(filepath.Join(dest, goldensAsset)); err == nil {
		t.Fatal("the never-healed golden set must not be installed")
	}
	if n := srv.count(goldensAsset); n != releaseLoadAttempts {
		t.Fatalf("%s read %d time(s), want exactly %d", goldensAsset, n, releaseLoadAttempts)
	}
	if out := buf.String(); !strings.Contains(out, "hashes to") || !strings.Contains(out, "the golden tests will skip") {
		t.Fatalf("progress did not mention the refusal:\n%s", out)
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

// TestBuildNumberOrdering pins the consumer rule for rebuilds: resolve a line to
// its newest ClickHouse version, and among rows of that version take the highest
// wrapper build. Getting the sort backwards would silently install an older
// build, so the ordering is asserted directly and through selection.
func TestBuildNumberOrdering(t *testing.T) {
	rev := ABIRevision
	row := func(version, file string, build int) ReleaseArtifact {
		return ReleaseArtifact{
			OS: "linux", Arch: "amd64", File: file, SHA256: strings.Repeat("a", 64),
			Bytes: 1, ClickHouseVersion: version, ClickHouseMinor: minorOf(version),
			Library: "libchtypes.so", LibrarySHA256: strings.Repeat("b", 64), Build: build,
			ABIRevision: &rev,
		}
	}

	// build comes from the field when present, else the -b<N> suffix, else 0
	for _, tc := range []struct {
		a    ReleaseArtifact
		want int
	}{
		{row("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64.tar.gz", 0), 0},
		{row("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b3.tar.gz", 0), 3},
		{row("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b3.tar.gz", 7), 7},
		{row("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b12.tar.gz", 0), 12},
	} {
		if got := tc.a.BuildNumber(); got != tc.want {
			t.Fatalf("buildOf(%s, Build=%d) = %d, want %d", tc.a.File, tc.a.Build, got, tc.want)
		}
	}

	// a higher build of the same version wins; a newer version wins regardless
	older := row("25.8.28.1-lts", "a-b1.tar.gz", 1)
	newerBuild := row("25.8.28.1-lts", "a-b2.tar.gz", 2)
	newerVersion := row("25.8.33.6-lts", "b-b0.tar.gz", 0)
	if !newerRow(newerBuild, older) {
		t.Fatal("build 2 must beat build 1 of the same version")
	}
	if newerRow(older, newerBuild) {
		t.Fatal("build 1 must not beat build 2")
	}
	if !newerRow(newerVersion, newerBuild) {
		t.Fatal("a newer ClickHouse version must beat a higher build of an older one")
	}
	if newerRow(newerBuild, newerVersion) {
		t.Fatal("a higher build must not beat a newer ClickHouse version")
	}

	// and selectAll takes the highest build, whatever order the index lists them
	for _, order := range [][]ReleaseArtifact{
		{older, newerBuild},
		{newerBuild, older},
	} {
		f := &fetcher{platform: "linux-amd64", abiRevision: ABIRevision, index: &ReleaseIndex{Schema: 1, Artifacts: order}}
		got, err := f.selectAll()
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != 1 || got[0].BuildNumber() != 2 {
			t.Fatalf("selectAll picked %+v, want the build-2 row", got)
		}
	}
}

// ---------------------------------------------------------------- §2: the ABI revision

// revisionRow is a synthetic index row for linux-amd64 at ABI revision rev
// (nil: the row declares none).
func revisionRow(version, file string, build int, rev *int) ReleaseArtifact {
	return ReleaseArtifact{
		OS: "linux", Arch: "amd64", File: file, SHA256: strings.Repeat("a", 64),
		Bytes: 1, ClickHouseVersion: version, ClickHouseMinor: minorOf(version),
		Library: "libchtypes.so", LibrarySHA256: strings.Repeat("b", 64), Build: build,
		ABIRevision: rev,
	}
}

// revisionFetcher selects over rows exactly as Ensure/FetchAll would, at this
// package's own ABIRevision — the value a consumer's fetch uses.
func revisionFetcher(t *testing.T, rows ...ReleaseArtifact) *fetcher {
	t.Helper()
	src, err := newSource("file://"+t.TempDir(), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	return &fetcher{platform: "linux-amd64", abiRevision: ABIRevision, src: src,
		index: &ReleaseIndex{Schema: 1, Artifacts: rows}}
}

// (a) A row at another revision with a HIGHER build, and a NEWER version in a
// row that declares no revision, both lose to the one row at the binding's own
// revision — in every listing order, for a line, an exact patch and --all.
func TestSelectionTakesOnlyTheBindingsOwnABIRevision(t *testing.T) {
	own, other := ABIRevision, ABIRevision+1
	mine := revisionRow("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b10.tar.gz", 10, &own)
	higherBuildElsewhere := revisionRow("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b20.tar.gz", 20, &other)
	newerUndeclared := revisionRow("25.8.33.6-lts", "chtypes-25.8.33.6-lts-linux-amd64-b30.tar.gz", 30, nil)
	for _, order := range [][]ReleaseArtifact{
		{mine, higherBuildElsewhere, newerUndeclared},
		{newerUndeclared, higherBuildElsewhere, mine},
		{higherBuildElsewhere, mine, newerUndeclared},
	} {
		f := revisionFetcher(t, order...)
		for _, req := range []struct{ spelling, line, exact string }{
			{"25.8", "25.8", ""},
			{"25.8.28.1-lts", "25.8", "25.8.28.1-lts"},
			{"25.8.28.1", "25.8", "25.8.28.1"},
		} {
			got, err := f.selectArtifact(req.spelling, req.line, req.exact)
			if err != nil {
				t.Fatalf("%s: %v", req.spelling, err)
			}
			if got.File != mine.File {
				t.Fatalf("%s: selected %s, want %s (the row at ABI revision %d)", req.spelling, got.File, mine.File, own)
			}
		}
		all, err := f.selectAll()
		if err != nil {
			t.Fatal(err)
		}
		if len(all) != 1 || all[0].File != mine.File {
			t.Fatalf("selectAll = %+v, want only %s", all, mine.File)
		}
	}
}

// (b) Only other-revision rows: CHTYPES_ARTIFACT_UNPUBLISHED (exit 4), never a
// fallback, and the message names the binding's revision and the served one.
func TestSelectionRefusesWhenOnlyAnotherRevisionIsServed(t *testing.T) {
	own, other := ABIRevision, ABIRevision+1
	f := revisionFetcher(t,
		revisionRow("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64-b20.tar.gz", 20, &other),
		revisionRow("26.7.3.19-stable", "chtypes-26.7.3.19-stable-linux-amd64-b20.tar.gz", 20, &other))
	names := []string{"ABI revision " + strconv.Itoa(own) + " (this SDK's)", "only at ABI revision " + strconv.Itoa(other)}
	check := func(what string, err error) {
		t.Helper()
		ae := wantCode(t, err, CodeArtifactUnpublished)
		if ExitCode(err) != 4 {
			t.Fatalf("%s: exit %d, want 4", what, ExitCode(err))
		}
		for _, n := range names {
			if !strings.Contains(ae.Msg, n) {
				t.Fatalf("%s: message does not name %q: %s", what, n, ae.Msg)
			}
		}
	}
	_, err := f.selectArtifact("25.8", "25.8", "")
	check("line", err)
	_, err = f.selectArtifact("25.8.28.1-lts", "25.8", "25.8.28.1-lts")
	check("exact", err)
	_, err = f.selectAll()
	check("--all", err)
}

// (c) A row with no abi_revision is never selected — whether the field is
// absent or not an integer — even when it is the only row there is.
func TestSelectionNeverTakesARowWithoutAnABIRevision(t *testing.T) {
	f := revisionFetcher(t, revisionRow("25.8.28.1-lts", "chtypes-25.8.28.1-lts-linux-amd64.tar.gz", 0, nil))
	_, lineErr := f.selectArtifact("25.8", "25.8", "")
	_, allErr := f.selectAll()
	for _, err := range []error{lineErr, allErr} {
		ae := wantCode(t, err, CodeArtifactUnpublished)
		// The reason is named — rows built before revisions were recorded —
		// never that the release serves "none".
		if !strings.Contains(ae.Msg, "for linux-amd64 only in rows that record no ABI revision (built before revisions were recorded)") ||
			!strings.Contains(ae.Msg, "ABI revision "+strconv.Itoa(ABIRevision)+" (this SDK's)") || strings.Contains(ae.Msg, "none") {
			t.Fatalf("message: %s", ae.Msg)
		}
	}
	// What index.json can actually carry: the binding's revision as a number
	// is a revision; the same digits as a string, a float, null or a bool are
	// not — and none of them fails the rest of the row.
	own := strconv.Itoa(ABIRevision)
	for raw, want := range map[string]bool{
		own: true, `"` + own + `"`: false, own + ".0": false, "null": false, "true": false,
	} {
		var a ReleaseArtifact
		if err := json.Unmarshal([]byte(`{"os":"linux","arch":"amd64","file":"x.tar.gz","abi_revision":`+raw+`}`), &a); err != nil {
			t.Fatalf("abi_revision %s failed the whole row: %v", raw, err)
		}
		if got := a.atRevision(ABIRevision); got != want || a.File != "x.tar.gz" {
			t.Fatalf("abi_revision %s: at revision %d = %v (file %q), want %v", raw, ABIRevision, got, a.File, want)
		}
	}
	var absent ReleaseArtifact
	if err := json.Unmarshal([]byte(`{"os":"linux","arch":"amd64","file":"x.tar.gz"}`), &absent); err != nil || absent.ABIRevision != nil {
		t.Fatalf("absent abi_revision: %+v %v", absent, err)
	}
}

// --all never skips a line silently: a line the release has only at another
// revision, or only in rows that record none, is named in one loud line —
// line, platform, the SDK's revision and what the release does serve — and
// every line at the SDK's revision still installs, with no error.
func TestFetchAllNamesEveryLineServedOnlyAtAnotherRevision(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	rel := filepath.Join(t.TempDir(), "release")
	own := fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	elsewhere := fakeArtifact(HostPlatform(), "26.7.3.19-stable")
	elsewhere.revision = ABIRevision + 1
	undeclared := fakeArtifact(HostPlatform(), "24.8.14.39-lts")
	undeclared.undeclared = true
	writeRelease(t, rel, priv, own, elsewhere, undeclared)
	dest := filepath.Join(t.TempDir(), "reg")
	var progress bytes.Buffer
	got, err := FetchAll(context.Background(), FetchOptions{URL: "file://" + rel, Dest: dest, Progress: &progress})
	if err != nil {
		t.Fatalf("FetchAll: %v\n%s", err, progress.String())
	}
	if len(got) != 1 || got[0].Line != "25.8" {
		t.Fatalf("installed %+v, want only 25.8", got)
	}
	for _, want := range []string{
		fmt.Sprintf("chtypes: WARNING: ClickHouse line 24.8 on %s is not installed: the release has that line for %s only in rows that record no ABI revision (built before revisions were recorded), and this SDK speaks ABI revision %d\n",
			HostPlatform(), HostPlatform(), ABIRevision),
		fmt.Sprintf("chtypes: WARNING: ClickHouse line 26.7 on %s is not installed: the release has that line for %s only at ABI revision %d, and this SDK speaks ABI revision %d\n",
			HostPlatform(), HostPlatform(), ABIRevision+1, ABIRevision),
	} {
		if !strings.Contains(progress.String(), want) {
			t.Fatalf("no loud line %q in:\n%s", want, progress.String())
		}
	}
	for _, line := range []string{"24.8", "26.7"} {
		if _, err := os.Stat(filepath.Join(dest, line)); err == nil {
			t.Fatalf("%s was installed", line)
		}
	}
}

// The per-user cache is keyed by the binding's own ABI revision — read off
// ABIRevision, never typed — and the fetch test override does not move it.
func TestDefaultRegistryDirIsKeyedByTheABIRevision(t *testing.T) {
	cache := isolateEnv(t)
	abi := "abi" + strconv.Itoa(ABIRevision)
	want := filepath.Join(cache, "chtypes", "artifacts", abi, HostPlatform())
	if DefaultRegistryDir() != want || FetchRegistryDir("") != want || RegistrySearchPath("")[0] != want {
		t.Fatalf("DefaultRegistryDir = %s, FetchRegistryDir = %s, want %s", DefaultRegistryDir(), FetchRegistryDir(""), want)
	}
	if got := DefaultRegistryDirFor("linux-amd64"); got != filepath.Join(cache, "chtypes", "artifacts", abi, "linux-amd64") {
		t.Fatalf("DefaultRegistryDirFor = %s", got)
	}
	f, err := newFetcher(FetchOptions{Platform: HostPlatform()})
	if err != nil || f.dest != want || f.abiRevision != ABIRevision {
		t.Fatalf("fetch selects at %d and writes to %s (%v), want %d and %s", f.abiRevision, f.dest, err, ABIRevision, want)
	}
	prev := testhook.FetchABIRevision
	testhook.FetchABIRevision = ABIRevision + 1
	t.Cleanup(func() { testhook.FetchABIRevision = prev })
	if DefaultRegistryDir() != want {
		t.Fatalf("the fetch override moved the registry: %s", DefaultRegistryDir())
	}
	if f, _ := newFetcher(FetchOptions{Platform: HostPlatform()}); f.abiRevision != ABIRevision+1 || f.dest != want {
		t.Fatalf("override: fetcher selects at %d and writes to %s", f.abiRevision, f.dest)
	}
}

// The fixture suites' override is the fixtures' own declaration, never typed:
// expected.json's fixtures_abi_revision is the answer, and a set that does not
// declare one — or declares something that is not an integer — is an error.
func TestFixturesABIRevisionIsTheDeclaredField(t *testing.T) {
	write := func(t *testing.T, body string) string {
		t.Helper()
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "expected.json"), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return dir
	}
	if n, err := testhook.FixturesABIRevision(write(t, `{"schema": 1, "fixtures_abi_revision": 7}`)); err != nil || n != 7 {
		t.Fatalf("declared 7: got %d, %v", n, err)
	}
	for body, want := range map[string]string{
		`{"schema": 1}`: "declares no fixtures_abi_revision",
		`{"schema": 1, "fixtures_abi_revision": "7"}`: "not an integer",
		`{"schema": 1, "fixtures_abi_revision": 7.5}`: "not an integer",
	} {
		if _, err := testhook.FixturesABIRevision(write(t, body)); err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("%s: err = %v, want %q", body, err, want)
		}
	}
	if _, err := testhook.FixturesABIRevision(t.TempDir()); err == nil {
		t.Fatal("no expected.json at all was an answer")
	}
}
