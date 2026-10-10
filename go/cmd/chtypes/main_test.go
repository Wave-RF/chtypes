package main

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

func runCLI(t *testing.T, env map[string]string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	for _, k := range []string{"CHTYPES_ARTIFACTS_URL", "CHTYPES_CACHE", "CHTYPES_TRUSTED_KEYS", "CHTYPES_ALLOW_UNSIGNED", "CHTYPES_TARGET", "CHTYPES_CACHE_STRICT", "CHTYPES_OFFLINE"} {
		t.Setenv(k, "")
	}
	for k, v := range env {
		t.Setenv(k, v)
	}
	var out, errb bytes.Buffer
	code = run(context.Background(), args, &out, &errb)
	return code, out.String(), errb.String()
}

func TestUsageErrorsExitTwo(t *testing.T) {
	for _, args := range [][]string{
		nil,
		{"frobnicate"},
		{"fetch"},
		{"fetch", "26.8", "--all"},
		{"fetch", "26.8", "--platform", "plan9-mips"},
		{"fetch", "v26.8", "--offline"},
		{"verify", "extra"},
		{"list", "extra"},
		{"list", "--platform", "linux-arm64"},
		{"verify", "--platform", "linux-arm64"},
		{"where", "--platform", "linux-arm64"},
		{"fetch", "--update", "--lock", "f", "--offline"},
		{"fetch", "26.8", "--update", "--frozen", "--lock", "x"},
		{"fetch", "26.8", "--update"},
		{"where", "extra"},
		{"fetch", "--nope"},
	} {
		if code, _, _ := runCLI(t, map[string]string{"CHTYPES_CACHE": t.TempDir()}, args...); code != 2 {
			t.Errorf("%v: exit %d, want 2", args, code)
		}
	}
}

func TestWhereIsTheCacheRoot(t *testing.T) {
	dir := t.TempDir()
	code, out, _ := runCLI(t, map[string]string{"CHTYPES_CACHE": dir}, "where")
	if code != 0 || strings.TrimSpace(out) != dir {
		t.Errorf("where = %d %q, want %q", code, out, dir)
	}
	code, out, _ = runCLI(t, nil, "where", "--cache", dir)
	if code != 0 || strings.TrimSpace(out) != dir {
		t.Errorf("where --cache = %d %q", code, out)
	}
}

// `where --all` lists every directory searched, the cache root first (#530);
// the default output stays the root alone.
func TestWhereAllListsTheSearchDirs(t *testing.T) {
	dir := t.TempDir()
	code, out, _ := runCLI(t, nil, "where", "--all", "--cache", dir)
	want := dir + "\n/usr/local/share/chtypes/v1\n/opt/chtypes/v1\n"
	if code != 0 || out != want {
		t.Errorf("where --all = %d %q, want %q", code, out, want)
	}
	if code, out, _ := runCLI(t, nil, "where", "--cache", dir); code != 0 || out != dir+"\n" {
		t.Errorf("where = %d %q, want the root alone", code, out)
	}
}

// The exit status of every shared code is the generated table's, so this
// asserts the table is what the CLI reads, one code at a time.
func TestExitStatusIsTheGeneratedTable(t *testing.T) {
	want := map[ocifetch.ErrorCode]int{
		ocifetch.CodeArtifactMissing: 1, ocifetch.CodeArtifactUntrusted: 1, ocifetch.CodeArtifactCorrupt: 1,
		ocifetch.CodeArtifactPinned: 1, ocifetch.CodeArtifactUnpublished: 4, ocifetch.CodeSourceUnreachable: 3,
		ocifetch.CodeSourceUnauthorized: 5, ocifetch.CodeSourceForbidden: 6, ocifetch.CodeSourceIncompatible: 7,
		ocifetch.CodeArtifactIncompatible: 8, ocifetch.CodeCacheUnusable: 9, ocifetch.CodeSourceRetired: 10,
	}
	if len(want) != len(ocifetch.ErrorExitCodes) {
		t.Fatalf("the generated table has %d codes, this test %d", len(ocifetch.ErrorExitCodes), len(want))
	}
	for c, n := range want {
		if got := exitStatus(&ocifetch.FetchError{Code: c}); got != n {
			t.Errorf("%s exits %d, want %d", c, got, n)
		}
	}
}

func TestOfflineMissIsArtifactMissing(t *testing.T) {
	code, _, errText := runCLI(t, map[string]string{"CHTYPES_CACHE": t.TempDir()}, "fetch", "26.8", "--offline", "--platform", "linux-arm64")
	if code != 1 || !strings.Contains(errText, "CHTYPES_ARTIFACT_MISSING") {
		t.Errorf("exit %d, stderr %q", code, errText)
	}
}

func TestVerifyAndListOnAnEmptyCache(t *testing.T) {
	env := map[string]string{"CHTYPES_CACHE": t.TempDir()}
	if code, out, _ := runCLI(t, env, "verify"); code != 0 || out != "" {
		t.Errorf("verify = %d %q", code, out)
	}
	if code, out, _ := runCLI(t, env, "list", "--offline"); code != 0 || out != "" {
		t.Errorf("list --offline = %d %q", code, out)
	}
}

// fixtureBase is the conformance suite's file:// tree, or a loud skip.
func fixtureBase(t *testing.T) (base, keyHex string) {
	t.Helper()
	root, err := filepath.Abs("../../../tests/fixtures/fetch-v1")
	if err != nil {
		t.Fatal(err)
	}
	key, err := os.ReadFile(filepath.Join(root, "test-key", "public.hex"))
	if err != nil {
		t.Skipf("SKIPPED: the fetch-v1 fixtures are not beside this checkout (%v)", err)
	}
	return "file://" + filepath.Join(root, "trees", "basic", "v2", "chtypes", "v1"), strings.TrimSpace(string(key))
}

func TestFetchVerifyListAgainstTheFixtureTree(t *testing.T) {
	base, key := fixtureBase(t)
	cache := t.TempDir()
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": base, "CHTYPES_TRUSTED_KEYS": key, "CHTYPES_CACHE": cache, "CHTYPES_TARGET": "linux-arm64"}

	code, out, errText := runCLI(t, env, "fetch", "26.8")
	dir := strings.TrimSpace(out)
	if code != 0 || !strings.HasPrefix(dir, cache) || strings.Contains(dir, "\n") {
		t.Fatalf("fetch = %d stdout %q stderr %q", code, out, errText)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Errorf("the printed directory does not exist: %v", err)
	}
	if code, out, errText = runCLI(t, env, "verify"); code != 0 || out != "" {
		t.Errorf("verify = %d %q %q", code, out, errText)
	}
	if code, out, errText = runCLI(t, env, "list"); code != 0 || !strings.Contains(out, "26.8.15.10") || !strings.Contains(out, "installed 26.8.15.10 ") || !strings.Contains(out, "published 26.8 support unknown\n") {
		t.Errorf("list = %d %q %q", code, out, errText)
	}

	// --lock writes the lock, and --frozen then fetches from it alone.
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if code, _, errText = runCLI(t, env, "fetch", "26.8", "--lock", lock); code != 0 {
		t.Fatalf("fetch --lock = %d %q", code, errText)
	}
	env["CHTYPES_CACHE"] = t.TempDir()
	if code, out, errText = runCLI(t, env, "fetch", "26.8", "--frozen", "--lock", lock); code != 0 || strings.TrimSpace(out) == "" {
		t.Errorf("fetch --frozen = %d %q %q", code, out, errText)
	}
	// A request the lock does not pin is CHTYPES_ARTIFACT_PINNED (exit 1).
	if code, _, errText = runCLI(t, env, "fetch", "26.7", "--frozen", "--lock", lock); code != 1 || !strings.Contains(errText, "CHTYPES_ARTIFACT_PINNED") {
		t.Errorf("an unpinned frozen request = %d %q", code, errText)
	}
	// A tag nothing publishes is CHTYPES_ARTIFACT_UNPUBLISHED (exit 4).
	if code, _, errText = runCLI(t, env, "fetch", "1.1"); code != 4 || !strings.Contains(errText, "CHTYPES_ARTIFACT_UNPUBLISHED") {
		t.Errorf("an unpublished line = %d %q", code, errText)
	}
	// Without the test key trusted, the same tree is CHTYPES_ARTIFACT_UNTRUSTED (exit 1).
	env["CHTYPES_TRUSTED_KEYS"] = ""
	env["CHTYPES_CACHE"] = t.TempDir()
	if code, _, errText = runCLI(t, env, "fetch", "26.8"); code != 1 || !strings.Contains(errText, "CHTYPES_ARTIFACT_UNTRUSTED") {
		t.Errorf("an untrusted tree = %d %q", code, errText)
	}
}

// A retired repository (fetch-v1.md section 2, public issue #571): every
// route answers 410 with the registry's document, so fetch and list print the
// registry's own message and exit 10, after one request each.
func TestFetchAndListAgainstARetiredRepository(t *testing.T) {
	const message = "chtypes/v1 is retired: use chtypes/v2 (this registry no longer serves chtypes/v1)"
	var requests []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests = append(requests, r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusGone)
		_, _ = w.Write([]byte(`{"errors":[{"code":"DENIED","message":"` + message + `"}]}`))
	}))
	defer srv.Close()
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": srv.URL + "/chtypes/v1", "CHTYPES_CACHE": t.TempDir(), "CHTYPES_TARGET": "linux-arm64"}
	for _, args := range [][]string{{"fetch", "26.9"}, {"list"}} {
		requests = nil
		code, out, errText := runCLI(t, env, args...)
		if code != 10 || out != "" || !strings.Contains(errText, "CHTYPES_SOURCE_RETIRED") || !strings.Contains(errText, message) || len(requests) != 1 {
			t.Errorf("%v = exit %d, stdout %q, stderr %q, %d request(s) %v; want exit 10, the registry's message, one request", args, code, out, errText, len(requests), requests)
		}
	}
}

func TestVersionFlag(t *testing.T) {
	code, out, _ := runCLI(t, nil, "--version")
	if code != 0 || !strings.HasPrefix(out, "chtypes ") || strings.Count(out, "\n") != 1 || strings.Contains(out, "devel") {
		t.Errorf("--version = %d %q", code, out)
	}
}

func TestHelpIsStdoutAnywhere(t *testing.T) {
	for _, args := range [][]string{{"--help"}, {"-h"}, {"fetch", "--help"}, {"list", "26.8", "-h"}} {
		code, out, errText := runCLI(t, nil, args...)
		if code != 0 || !strings.HasPrefix(out, "usage:") || errText != "" {
			t.Errorf("%v = %d stdout %q stderr %q", args, code, out, errText)
		}
	}
}

func TestListOfflineIsInstalledOnly(t *testing.T) {
	base, key := fixtureBase(t)
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": base, "CHTYPES_TRUSTED_KEYS": key, "CHTYPES_CACHE": t.TempDir(), "CHTYPES_TARGET": "linux-arm64"}
	if code, _, e := runCLI(t, env, "fetch", "26.8"); code != 0 {
		t.Fatal(e)
	}
	code, out, _ := runCLI(t, env, "list", "--offline")
	if code != 0 || !strings.Contains(out, "installed 26.8.15.10 ") || strings.Contains(out, "published") {
		t.Errorf("list --offline = %d %q", code, out)
	}
}

func TestFetchUpdateRewritesTheLock(t *testing.T) {
	base, key := fixtureBase(t)
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": base, "CHTYPES_TRUSTED_KEYS": key, "CHTYPES_CACHE": t.TempDir(), "CHTYPES_TARGET": "linux-arm64"}
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if code, _, e := runCLI(t, env, "fetch", "26.8", "--lock", lock); code != 0 {
		t.Fatal(e)
	}
	if err := os.WriteFile(lock+".bak", mustRead(t, lock), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(lock); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(lock, mustRead(t, lock+".bak"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, out, e := runCLI(t, env, "fetch", "--update", "--lock", lock)
	if code != 0 || strings.TrimSpace(out) == "" {
		t.Fatalf("fetch --update = %d %q %q", code, out, e)
	}
	if string(mustRead(t, lock)) != string(mustRead(t, lock+".bak")) {
		t.Error("an update against an unchanged registry must rewrite the same lock")
	}
}

func mustRead(t *testing.T, p string) []byte {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// writeZeroXRegistry lays out what a 0.x install left behind: a registry
// directory of <minor>/manifest.json entries, with no oci-layout.
func writeZeroXRegistry(t *testing.T, root string) {
	t.Helper()
	for rel, body := range map[string]string{
		"26.1/manifest.json":             `{}`,
		"26.1/libchtypes.so":             "0.x library",
		"patches/26.1.3.4/manifest.json": `{}`,
	} {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// TestVerifyOfNothingSaysSo: a verify that verified nothing says so on stderr,
// so an empty pass never looks like a good one, and a 0.x registry used as
// the cache is named (public issue #486).
func TestVerifyOfNothingSaysSo(t *testing.T) {
	empty := t.TempDir()
	code, out, errText := runCLI(t, map[string]string{"CHTYPES_CACHE": empty}, "verify")
	if code != 0 || out != "" || !strings.Contains(errText, "chtypes: verified 0 builds under "+empty+"\n") {
		t.Errorf("verify of an empty cache = %d %q %q", code, out, errText)
	}
	zeroX := filepath.Join(t.TempDir(), "zero-x")
	writeZeroXRegistry(t, zeroX)
	hint := zeroX + " holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at ${XDG_CACHE_HOME:-~/.cache}/chtypes/v1"
	code, _, errText = runCLI(t, nil, "verify", "--cache", zeroX)
	if code != 0 || !strings.Contains(errText, "verified 0 builds under "+zeroX) || !strings.Contains(errText, hint) {
		t.Errorf("verify of a 0.x registry = %d %q", code, errText)
	}
	code, _, errText = runCLI(t, nil, "fetch", "26.1", "--offline", "--platform", "linux-arm64", "--cache", zeroX)
	if code != 1 || !strings.Contains(errText, "CHTYPES_ARTIFACT_MISSING") || !strings.Contains(errText, hint) {
		t.Errorf("fetch --offline from a 0.x registry = %d %q", code, errText)
	}
	if _, err := os.Lstat(filepath.Join(zeroX, "oci-layout")); err == nil {
		t.Errorf("a read-only command wrote %s", filepath.Join(zeroX, "oci-layout"))
	}
}

// TestStrictFlag: --strict (or CHTYPES_CACHE_STRICT=1) turns every command's
// cache fault into CHTYPES_CACHE_UNUSABLE (exit 9) naming the path and the
// reason, and a verify of nothing into CHTYPES_ARTIFACT_MISSING (exit 1); the
// default mode lists nothing, exits 0 and warns (public issue #486).
func TestStrictFlag(t *testing.T) {
	empty := t.TempDir()
	if code, _, errText := runCLI(t, nil, "verify", "--strict", "--cache", empty); code != 1 || !strings.Contains(errText, "CHTYPES_ARTIFACT_MISSING") {
		t.Errorf("verify --strict of an empty cache = %d %q", code, errText)
	}
	if code, _, errText := runCLI(t, map[string]string{"CHTYPES_CACHE_STRICT": "1"}, "verify", "--cache", empty); code != 1 {
		t.Errorf("verify with CHTYPES_CACHE_STRICT=1 = %d %q", code, errText)
	}
	if code, out, _ := runCLI(t, nil, "where", "--strict", "--cache", empty); code != 0 || strings.TrimSpace(out) != empty {
		t.Errorf("where --strict of a good cache = %d %q", code, out)
	}
	zeroX := filepath.Join(t.TempDir(), "zero-x")
	writeZeroXRegistry(t, zeroX)
	want := zeroX + " is unusable as a cache: layout_0x"
	for _, args := range [][]string{
		{"where", "--strict"}, {"list", "--offline", "--strict"}, {"verify", "--strict"},
		{"fetch", "26.1", "--offline", "--platform", "linux-arm64", "--strict"},
	} {
		code, out, errText := runCLI(t, nil, append(args, "--cache", zeroX)...)
		if code != 9 || out != "" || !strings.Contains(errText, want) || !strings.Contains(errText, "CHTYPES_CACHE_UNUSABLE") {
			t.Errorf("%v on a 0.x registry = %d %q %q", args, code, out, errText)
		}
	}
	// Default mode: an unreadable cache is not installed, with a warning naming it.
	if os.Geteuid() == 0 {
		t.Skip("SKIPPED: running as root, which a mode cannot deny")
	}
	locked := filepath.Join(t.TempDir(), "locked")
	if err := os.MkdirAll(filepath.Join(locked, "unpacked", "sha256"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(locked, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o755) })
	if _, err := os.ReadDir(locked); err == nil {
		t.Fatal("positive control: a 000 directory could be listed")
	}
	code, out, errText := runCLI(t, nil, "list", "--offline", "--cache", locked)
	if code != 0 || out != "" || !strings.Contains(errText, locked+" could not be read (EACCES); treated as not installed. Set CHTYPES_CACHE_STRICT=1 to make this an error.") {
		t.Errorf("list --offline of an unreadable cache = %d %q %q", code, out, errText)
	}
	if code, _, errText = runCLI(t, nil, "list", "--offline", "--strict", "--cache", locked); code != 9 || !strings.Contains(errText, locked+" is unusable as a cache: unreadable_root (EACCES)") {
		t.Errorf("list --offline --strict of an unreadable cache = %d %q", code, errText)
	}
}
