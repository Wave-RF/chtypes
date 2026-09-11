package main

// main_test.go — the §6 surface: exit codes, stdout discipline (fetch prints
// the installed directory alone), progress on stderr. The fetch/verify/list
// flows run against the shared fixtures (tests/fixtures/fetch, reached by
// $CHTYPES_FETCH_FIXTURES or by path) and SKIP LOUDLY without them; the
// usage and `where` checks need nothing.

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func isolate(t *testing.T) string {
	t.Helper()
	cache := t.TempDir()
	for _, k := range []string{"CHTYPES_REGISTRY", "CHTYPES_AUTOFETCH", "CHTYPES_TRUSTED_KEYS", "CHTYPES_ALLOW_UNSIGNED", "CHTYPES_TARGET", "CHTYPES_ARTIFACTS_URL", "CHTYPES_DOWNLOAD_TOKEN"} {
		t.Setenv(k, "")
	}
	t.Setenv("XDG_CACHE_HOME", cache)
	return cache
}

func fixtures(t *testing.T) (dir, key string) {
	t.Helper()
	dir = os.Getenv("CHTYPES_FETCH_FIXTURES")
	if dir == "" {
		dir = filepath.Join("..", "..", "..", "tests", "fixtures", "fetch")
	}
	abs, _ := filepath.Abs(dir)
	b, err := os.ReadFile(filepath.Join(abs, "test-key", "public.hex"))
	if err != nil {
		t.Skipf("shared fetch fixtures not found: %v (set CHTYPES_FETCH_FIXTURES to tests/fixtures/fetch)", err)
	}
	return abs, strings.TrimSpace(string(b))
}

func exec(t *testing.T, args ...string) (rc int, stdout, stderr string) {
	t.Helper()
	var out, errb bytes.Buffer
	rc = run(context.Background(), args, &out, &errb)
	return rc, out.String(), errb.String()
}

func TestUsageAndWhere(t *testing.T) {
	cache := isolate(t)
	for _, args := range [][]string{{}, {"bogus"}, {"fetch"}, {"fetch", "--all", "25.8"}, {"fetch", "25.8", "--tag", "v1", "--url", "x"},
		{"fetch", "25.8", "--platform", "windows-amd64"}, {"fetch", "25.8", "--nope"}, {"where", "extra"}, {"verify", "extra"}, {"list", "extra"}} {
		rc, out, errs := exec(t, args...)
		if rc != 2 || out != "" || !strings.Contains(errs, "usage:") {
			t.Errorf("%v: rc=%d stdout=%q stderr=%q", args, rc, out, errs)
		}
	}
	rc, out, _ := exec(t, "help")
	if rc != 0 || !strings.Contains(out, "chtypes fetch") {
		t.Fatalf("help: rc=%d %q", rc, out)
	}
	host := chtypes.HostPlatform()
	// `where` prints the directory on its own FIRST line — that is the
	// scriptable contract, `cd "$(chtypes where | head -1)"` — and then the
	// served golden set, which is the other half of "what is in my registry".
	firstLine := func(s string) string { return strings.SplitN(strings.TrimSpace(s), "\n", 2)[0] }
	rc, out, _ = exec(t, "where")
	if rc != 0 || firstLine(out) != filepath.Join(cache, "chtypes", "artifacts", host) {
		t.Fatalf("where: rc=%d %q", rc, out)
	}
	if !strings.Contains(out, "sdk-goldens.json") {
		t.Fatalf("where does not name the golden set: %q", out)
	}
	t.Setenv("CHTYPES_REGISTRY", "/env/reg")
	if _, out, _ = exec(t, "where"); firstLine(out) != "/env/reg" {
		t.Fatalf("where with CHTYPES_REGISTRY: %q", out)
	}
	// Another platform's default is its own cache, never CHTYPES_REGISTRY.
	other := "linux-amd64"
	if host == other {
		other = "linux-arm64"
	}
	if _, out, _ = exec(t, "where", "--platform", other); firstLine(out) != filepath.Join(cache, "chtypes", "artifacts", other) {
		t.Fatalf("where --platform: %q", out)
	}
	// verify/list on an empty registry: honest, exit 0.
	t.Setenv("CHTYPES_REGISTRY", "")
	rc, out, _ = exec(t, "verify")
	if rc != 0 || !strings.Contains(out, "nothing installed") {
		t.Fatalf("verify empty: rc=%d %q", rc, out)
	}
	rc, out, _ = exec(t, "list", "--offline")
	if rc != 0 || !strings.Contains(out, "(nothing)") || !strings.Contains(out, "--offline") {
		t.Fatalf("list --offline: rc=%d %q", rc, out)
	}
	// offline fetch of nothing: 3, no network, nothing on stdout.
	rc, out, errs := exec(t, "fetch", "25.8", "--offline", "--url", "http://127.0.0.1:9/never")
	if rc != 3 || out != "" || !strings.Contains(errs, "CHTYPES_SOURCE_UNREACHABLE") {
		t.Fatalf("offline: rc=%d stdout=%q stderr=%q", rc, out, errs)
	}
}

func TestFetchVerifyListAgainstFixtures(t *testing.T) {
	dir, key := fixtures(t)
	isolate(t)
	t.Setenv("CHTYPES_TRUSTED_KEYS", key)
	dest := filepath.Join(t.TempDir(), "reg")
	signed := "file://" + filepath.Join(dir, "signed")

	// fetch: the installed directory alone on stdout, progress on stderr.
	rc, out, errs := exec(t, "fetch", "25.8", "--url", signed, "--dest", dest)
	if rc != 0 || strings.TrimSpace(out) != filepath.Join(dest, "25.8") || !strings.Contains(errs, "==> installed and verified") {
		t.Fatalf("fetch: rc=%d stdout=%q stderr=%q", rc, out, errs)
	}
	// Flags interleave with positionals, several lines at once, one dir per line.
	rc, out, _ = exec(t, "fetch", "--dest", dest, "25.8", "26.7", "--url", signed)
	if rc != 0 || out != filepath.Join(dest, "25.8")+"\n"+filepath.Join(dest, "26.7")+"\n" {
		t.Fatalf("fetch two: rc=%d stdout=%q", rc, out)
	}
	rc, out, _ = exec(t, "verify", "--dest", dest)
	if rc != 0 || strings.Count(out, "\nok ")+strings.Count(out, "ok ") < 2 || !strings.Contains(out, "2 installed, 2 verified, 0 bad") {
		t.Fatalf("verify: rc=%d %q", rc, out)
	}
	rc, out, _ = exec(t, "list", "--dest", dest, "--url", signed)
	if rc != 0 || !strings.Contains(out, "installed ("+dest+")") || !strings.Contains(out, "(installed)") || !strings.Contains(out, "signed by key") {
		t.Fatalf("list: rc=%d %q", rc, out)
	}
	// A rotted install: verify says so and exits 1.
	inst, _ := chtypes.ListInstalled(dest)
	os.WriteFile(filepath.Join(inst[0].Dir, inst[0].Library), []byte("rot"), 0o755)
	rc, out, errs = exec(t, "verify", "--dest", dest)
	if rc != 1 || !strings.Contains(out, "MISMATCH") || !strings.Contains(errs, "CHTYPES_ARTIFACT_CORRUPT") {
		t.Fatalf("verify rotted: rc=%d %q %q", rc, out, errs)
	}
	// --all with a lock, then --frozen against it.
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	dest2 := filepath.Join(t.TempDir(), "reg2")
	rc, out, _ = exec(t, "fetch", "--all", "--url", signed, "--dest", dest2, "--lock", lock)
	if rc != 0 || out != filepath.Join(dest2, "25.8")+"\n"+filepath.Join(dest2, "26.7")+"\n" {
		t.Fatalf("fetch --all: rc=%d %q", rc, out)
	}
	if rc, _, _ = exec(t, "fetch", "25.8", "--url", signed, "--dest", dest2, "--lock", lock, "--frozen"); rc != 0 {
		t.Fatalf("frozen: rc=%d", rc)
	}
	rc, out, errs = exec(t, "fetch", "25.8", "--url", "file://"+filepath.Join(dir, "signed"), "--dest", dest2, "--lock", filepath.Join(t.TempDir(), "none.lock"), "--frozen")
	if rc != 1 || out != "" || !strings.Contains(errs, "CHTYPES_ARTIFACT_PINNED") {
		t.Fatalf("frozen without lock: rc=%d %q %q", rc, out, errs)
	}

	// The §6 exit codes, one fixture each.
	cases := []struct {
		fixture, line, platform, code string
		rc                            int
		allowUnsigned                 bool
	}{
		{"bad-signature", "25.8", "", "CHTYPES_ARTIFACT_UNTRUSTED", 1, false},
		{"unsigned", "25.8", "", "CHTYPES_ARTIFACT_UNTRUSTED", 1, false},
		{"unsigned", "25.8", "", "", 0, true},
		{"tampered-tarball", "25.8", "", "CHTYPES_ARTIFACT_CORRUPT", 1, false},
		{"sums-index-mismatch", "25.8", "", "CHTYPES_ARTIFACT_CORRUPT", 1, false},
		{"signed", "24.8", "", "CHTYPES_ARTIFACT_UNPUBLISHED", 4, false},
		{"signed", "25.8.99.1-lts", "", "CHTYPES_ARTIFACT_UNPUBLISHED", 4, false},
		{"signed", "25.8", "darwin-amd64", "CHTYPES_ARTIFACT_UNPUBLISHED", 4, false},
	}
	for _, c := range cases {
		t.Setenv("CHTYPES_ALLOW_UNSIGNED", "")
		if c.allowUnsigned {
			t.Setenv("CHTYPES_ALLOW_UNSIGNED", "1")
		}
		d := filepath.Join(t.TempDir(), c.fixture)
		args := []string{"fetch", c.line, "--url", "file://" + filepath.Join(dir, c.fixture), "--dest", d}
		if c.platform != "" {
			args = append(args, "--platform", c.platform)
		}
		rc, out, errs := exec(t, args...)
		if rc != c.rc {
			t.Errorf("%s %s: rc=%d want %d\n%s", c.fixture, c.line, rc, c.rc, errs)
		}
		if c.code != "" && (out != "" || !strings.Contains(errs, c.code)) {
			t.Errorf("%s %s: stdout=%q stderr=%q", c.fixture, c.line, out, errs)
		}
		if c.code == "" && (strings.TrimSpace(out) != filepath.Join(d, "25.8") || !strings.Contains(errs, "WARNING")) {
			t.Errorf("%s allow-unsigned: stdout=%q stderr=%q", c.fixture, out, errs)
		}
	}
	// A release signed by the test key is untrusted under the embedded key alone.
	t.Setenv("CHTYPES_TRUSTED_KEYS", "")
	t.Setenv("CHTYPES_ALLOW_UNSIGNED", "")
	rc, _, errs = exec(t, "fetch", "25.8", "--url", signed, "--dest", filepath.Join(t.TempDir(), "x"))
	if rc != 1 || !strings.Contains(errs, "CHTYPES_ARTIFACT_UNTRUSTED") {
		t.Fatalf("embedded key only: rc=%d %q", rc, errs)
	}
}
