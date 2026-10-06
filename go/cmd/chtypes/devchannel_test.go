package main

// devchannel_test.go — the command line under the ABI v2 dev channel
// (spec/abi-v2/docs.md, rules r5 and r6). The first tests run the in-process
// CLI switched to the dev channel; the last builds the real binary (no test
// seam can reach it: ocifetch's seams panic outside a test binary) and proves
// the same answers from it. Nothing here reaches the network.

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

func TestDevWhereIsTheSubroot(t *testing.T) {
	t.Cleanup(ocifetch.UseDevChannelForTests())
	dir := t.TempDir()
	if code, out, _ := runCLI(t, map[string]string{"CHTYPES_CACHE": dir}, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(dir, "v2-dev") {
		t.Errorf("where under CHTYPES_CACHE = %d %q, want %s/v2-dev (rule r5)", code, out, dir)
	}
	if code, out, _ := runCLI(t, nil, "where", "--cache", dir); code != 0 || strings.TrimSpace(out) != filepath.Join(dir, "v2-dev") {
		t.Errorf("where --cache = %d %q, want %s/v2-dev (rule r5)", code, out, dir)
	}
	xdg := t.TempDir()
	t.Setenv("XDG_CACHE_HOME", xdg)
	if code, out, _ := runCLI(t, nil, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(xdg, "chtypes", "v2-dev") {
		t.Errorf("where = %d %q, want ${XDG_CACHE_HOME}/chtypes/v2-dev (rule r5)", code, out)
	}
}

func TestDevRefusesLockFrozenAndUpdate(t *testing.T) {
	t.Cleanup(ocifetch.UseDevChannelForTests())
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	for _, args := range [][]string{
		{"fetch", "26.8", "--lock", lock},
		{"fetch", "26.8", "--frozen"},
		{"fetch", "26.8", "--frozen", "--lock", lock},
		{"fetch", "--update", "--lock", lock},
		{"fetch", "--all", "--frozen"},
	} {
		code, out, errText := runCLI(t, map[string]string{"CHTYPES_CACHE": t.TempDir()}, args...)
		if code != 2 || out != "" || !strings.Contains(errText, ocifetch.PinningRefused) {
			t.Errorf("%v = %d stdout %q stderr %q: want exit 2 and the dev-channel refusal", args, code, out, errText)
		}
	}
	if _, err := os.Stat(lock); !os.IsNotExist(err) {
		t.Errorf("a refused --lock wrote %s", lock)
	}
}

// TestRealBinarySpeaksTheDevChannel builds cmd/chtypes as a user's `go
// install` would and runs it: the subroot, the default root, the pinning
// refusals and the one-time warnings for each ignored override all come from
// a binary no test seam reaches.
func TestRealBinarySpeaksTheDevChannel(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skipf("SKIPPED: no go toolchain on PATH to build the real binary (%v)", err)
	}
	bin := filepath.Join(t.TempDir(), "chtypes")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.CommandContext(t.Context(), "go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	xdg := t.TempDir()
	run := func(env []string, args ...string) (int, string, string) {
		t.Helper()
		cmd := exec.CommandContext(t.Context(), bin, args...)
		cmd.Env = append([]string{"HOME=" + t.TempDir(), "XDG_CACHE_HOME=" + xdg, "PATH=" + os.Getenv("PATH")}, env...)
		var out, errb strings.Builder
		cmd.Stdout, cmd.Stderr = &out, &errb
		err := cmd.Run()
		code := 0
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else if err != nil {
			t.Fatal(err)
		}
		return code, out.String(), errb.String()
	}
	if code, out, _ := run(nil, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(xdg, "chtypes", "v2-dev") {
		t.Errorf("the real binary's default root = %d %q, want ${XDG_CACHE_HOME}/chtypes/v2-dev", code, out)
	}
	cache := t.TempDir()
	if code, out, _ := run([]string{"CHTYPES_CACHE=" + cache}, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(cache, "v2-dev") {
		t.Errorf("the real binary under CHTYPES_CACHE = %d %q, want <CHTYPES_CACHE>/v2-dev", code, out)
	}
	if code, _, errText := run([]string{"CHTYPES_CACHE=" + cache}, "fetch", "26.8", "--frozen"); code != 2 || !strings.Contains(errText, ocifetch.PinningRefused) {
		t.Errorf("the real binary's fetch --frozen = %d %q, want exit 2 and the dev-channel refusal", code, errText)
	}
	overrides := []string{
		"CHTYPES_CACHE=" + cache,
		"CHTYPES_ARTIFACTS_URL=http://127.0.0.1:9/chtypes/v1",
		"CHTYPES_TRUSTED_KEYS=" + ocifetch.TestKeys[0].Ed25519Hex,
		"CHTYPES_ALLOW_UNSIGNED=1",
	}
	code, out, errText := run(overrides, "fetch", "26.8", "--offline", "--platform", "linux-amd64")
	if code != 1 || out != "" || !strings.Contains(errText, string(ocifetch.CodeArtifactMissing)) {
		t.Errorf("an offline fetch from an empty dev cache = %d %q %q, want CHTYPES_ARTIFACT_MISSING", code, out, errText)
	}
	for _, name := range []string{"CHTYPES_ARTIFACTS_URL", "CHTYPES_TRUSTED_KEYS", "CHTYPES_ALLOW_UNSIGNED"} {
		if n := strings.Count(errText, "WARNING: "+name+" is set and IGNORED"); n != 1 {
			t.Errorf("%s was warned about %d times by the real binary, want exactly once:\n%s", name, n, errText)
		}
	}
}
