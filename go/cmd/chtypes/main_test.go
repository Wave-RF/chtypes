package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/internal/ocifetch"
)

func runCLI(t *testing.T, env map[string]string, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	for _, k := range []string{"CHTYPES_ARTIFACTS_URL", "CHTYPES_CACHE", "CHTYPES_TRUSTED_KEYS", "CHTYPES_ALLOW_UNSIGNED", "CHTYPES_TARGET"} {
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
		{"fetch", "26.8", "--frozen", "--offline"},
		{"fetch", "v26.8", "--offline"},
		{"verify", "extra"},
		{"list", "extra"},
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

// The exit status of every shared code is the generated table's, so this
// asserts the table is what the CLI reads, one code at a time.
func TestExitStatusIsTheGeneratedTable(t *testing.T) {
	want := map[ocifetch.ErrorCode]int{
		ocifetch.CodeArtifactMissing: 1, ocifetch.CodeArtifactUntrusted: 1, ocifetch.CodeArtifactCorrupt: 1,
		ocifetch.CodeArtifactPinned: 1, ocifetch.CodeArtifactUnpublished: 4, ocifetch.CodeSourceUnreachable: 3,
		ocifetch.CodeSourceUnauthorized: 5, ocifetch.CodeSourceForbidden: 6, ocifetch.CodeSourceIncompatible: 7,
		ocifetch.CodeArtifactIncompatible: 8,
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
	if code, out, _ := runCLI(t, env, "verify"); code != 0 || !strings.Contains(out, "nothing installed") {
		t.Errorf("verify = %d %q", code, out)
	}
	if code, out, _ := runCLI(t, env, "list", "--offline"); code != 0 || !strings.Contains(out, "not read (--offline)") {
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
	if code, out, errText = runCLI(t, env, "verify"); code != 0 || !strings.Contains(out, "1 installed, 1 verified, 0 bad") {
		t.Errorf("verify = %d %q %q", code, out, errText)
	}
	if code, out, errText = runCLI(t, env, "list"); code != 0 || !strings.Contains(out, "26.8.15.10") || !strings.Contains(out, "published lines (support unknown)") {
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
