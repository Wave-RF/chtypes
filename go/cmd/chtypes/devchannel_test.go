package main

// devchannel_test.go — the command line under the ABI v2 dev channel
// (spec/abi-v2/docs.md, rules r5 and r6), which a 2.0.0-dev SDK spoke. The
// first tests run the in-process CLI switched to the dev channel; the last
// builds the real binary (no test seam can reach it: ocifetch's seams panic
// outside a test binary) and proves that since the lock it speaks the
// production generation-2 channel instead. Nothing here reaches the network.

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

// #530 and #527: `where --all` lists the search directories, the root through
// its v2-dev subroot first, and the built-in system directories are v2-dev's.
func TestDevWhereAllListsTheSubrootedSearchDirs(t *testing.T) {
	t.Cleanup(ocifetch.UseDevChannelForTests())
	dir := t.TempDir()
	want := filepath.Join(dir, "v2-dev") + "\n/usr/local/share/chtypes/v2-dev\n/opt/chtypes/v2-dev\n"
	if code, out, _ := runCLI(t, nil, "where", "--all", "--cache", dir); code != 0 || out != want {
		t.Errorf("where --all = %d %q, want %q", code, out, want)
	}
}

// #528: CHTYPES_OFFLINE=1 is `--offline`: `list` prints what is installed and
// asks the registry for nothing (no "published" line), and `fetch` with
// nothing installed is CHTYPES_ARTIFACT_MISSING, not a network error. The
// zero-request count is in internal/ocifetch (TestDevOfflineEnv...).
func TestDevOfflineEnvIsTheOfflineFlag(t *testing.T) {
	t.Cleanup(ocifetch.UseDevChannelForTests())
	dir := t.TempDir()
	env := map[string]string{"CHTYPES_CACHE": dir, "CHTYPES_OFFLINE": "1"}
	if code, out, errs := runCLI(t, env, "list"); code != 0 || strings.Contains(out, "published") {
		t.Errorf("list under CHTYPES_OFFLINE=1 = %d %q %q; want exit 0 and no published lines", code, out, errs)
	}
	code, _, errs := runCLI(t, env, "fetch", "26.8", "--platform", "linux-amd64")
	if code == 0 || !strings.Contains(errs, "CHTYPES_ARTIFACT_MISSING") {
		t.Errorf("fetch under CHTYPES_OFFLINE=1 = %d %q; want CHTYPES_ARTIFACT_MISSING", code, errs)
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

// TestRealBinarySpeaksTheProductionChannel builds cmd/chtypes as a user's `go
// install` would and runs it: since the lock, a binary no test seam reaches
// speaks the production generation-2 channel (docs/guides/fetch-v1.md,
// "Generation 2 after the lock"), never the dev channel. Its default root and
// an explicit cache's subroot are v2, its system directories are v2's, a pin
// is honored (never the dev channel's refusal), and no override is ignored.
func TestRealBinarySpeaksTheProductionChannel(t *testing.T) {
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
		cmd.Dir = t.TempDir()
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
	if code, out, _ := run(nil, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(xdg, "chtypes", "v2") {
		t.Errorf("the real binary's default root = %d %q, want ${XDG_CACHE_HOME}/chtypes/v2", code, out)
	}
	cache := t.TempDir()
	if code, out, _ := run([]string{"CHTYPES_CACHE=" + cache}, "where"); code != 0 || strings.TrimSpace(out) != filepath.Join(cache, "v2") {
		t.Errorf("the real binary under CHTYPES_CACHE = %d %q, want <CHTYPES_CACHE>/v2", code, out)
	}
	want := filepath.Join(cache, "v2") + "\n/usr/local/share/chtypes/v2\n/opt/chtypes/v2\n"
	if code, out, _ := run(nil, "where", "--all", "--cache", cache); code != 0 || out != want {
		t.Errorf("the real binary's where --all = %d %q, want %q", code, out, want)
	}
	// A pin is honored: --frozen with no lock file reads the lock and fails on
	// it, never with the dev channel's refusal.
	if code, _, errText := run([]string{"CHTYPES_CACHE=" + cache}, "fetch", "26.8", "--frozen"); strings.Contains(errText, ocifetch.PinningRefused) || !strings.Contains(errText, "chtypes.lock") {
		t.Errorf("the real binary's fetch --frozen = %d %q, want the lock read, not the dev-channel refusal", code, errText)
	}
	overrides := []string{
		"CHTYPES_CACHE=" + cache,
		"CHTYPES_ARTIFACTS_URL=http://127.0.0.1:9/chtypes/v2",
		"CHTYPES_TRUSTED_KEYS=" + ocifetch.TestKeys[0].Ed25519Hex,
		"CHTYPES_ALLOW_UNSIGNED=1",
	}
	code, out, errText := run(overrides, "fetch", "26.8", "--offline", "--platform", "linux-amd64")
	if code != 1 || out != "" || !strings.Contains(errText, string(ocifetch.CodeArtifactMissing)) {
		t.Errorf("an offline fetch from an empty cache = %d %q %q, want CHTYPES_ARTIFACT_MISSING", code, out, errText)
	}
	if strings.Contains(errText, "is set and IGNORED") {
		t.Errorf("the real binary ignored an override, as only the dev channel does:\n%s", errText)
	}
}
