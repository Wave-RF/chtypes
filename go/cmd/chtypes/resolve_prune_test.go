package main

// resolve_prune_test.go — `chtypes resolve` (public issue #493) and `chtypes
// prune` (public issue #494) over the conformance fixtures' basic tree, under
// the v1 fetch contract (seam_test.go).

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolveUsageErrorsExitTwo(t *testing.T) {
	for _, args := range [][]string{
		{"resolve"},
		{"resolve", "26.8", "26.9"},
		{"resolve", "v26.8"},
		{"resolve", "26.8", "--platform", "linux-arm64"},
		{"prune", "26.8"},
		{"prune", "--keep", "0"},
		{"prune", "--keep", "x"},
		{"prune", "--line", "26.8.15"},
		{"prune", "--line", "v26.8"},
		{"prune", "--offline"},
		{"prune", "--platform", "linux-arm64"},
	} {
		if code, out, _ := runCLI(t, map[string]string{"CHTYPES_CACHE": t.TempDir()}, args...); code != 2 || out != "" {
			t.Errorf("%v: exit %d, stdout %q; want 2 and nothing on stdout", args, code, out)
		}
	}
}

func TestResolveAgainstTheFixtureTree(t *testing.T) {
	base, key := fixtureBase(t)
	cache := filepath.Join(t.TempDir(), "cache")
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": base, "CHTYPES_TRUSTED_KEYS": key, "CHTYPES_CACHE": cache}

	code, out, errText := runCLI(t, env, "resolve", "26.8")
	lines := strings.Split(strings.TrimSuffix(out, "\n"), "\n")
	if code != 0 || len(lines) != 3 {
		t.Fatalf("resolve 26.8 = %d %q %q, want one line per platform", code, out, errText)
	}
	for i, platform := range []string{"linux-amd64", "linux-arm64", "darwin-arm64"} {
		f := strings.Fields(lines[i])
		if len(f) != 5 || f[0] != "resolved" || f[1] != "26.8.15.10" || f[2] != platform || f[3] != "20261001.183455" || !strings.HasPrefix(f[4], "sha256:") {
			t.Errorf("line %d = %q, want resolved 26.8.15.10 %s 20261001.183455 sha256:<hex>", i, lines[i], platform)
		}
	}
	// Nothing was installed: resolve downloads no layer and writes nothing.
	if _, err := os.Stat(cache); !os.IsNotExist(err) {
		t.Errorf("resolve created the cache %s (%v)", cache, err)
	}

	code, out, errText = runCLI(t, env, "resolve", "--json", "26.8")
	var doc []map[string]string
	if code != 0 || json.Unmarshal([]byte(out), &doc) != nil || len(doc) != 3 || !strings.HasSuffix(out, "]\n") || strings.Count(out, "\n") != 1 {
		t.Fatalf("resolve --json 26.8 = %d %q %q, want one JSON array on one line", code, out, errText)
	}
	for i, row := range doc {
		f := strings.Fields(lines[i])
		if len(row) != 4 || row["platform"] != f[2] || row["version"] != f[1] || row["build"] != f[3] || row["manifest"] != f[4] {
			t.Errorf("--json row %d = %v, want the line %q", i, row, lines[i])
		}
	}
	if !strings.HasPrefix(out, `[{"platform":"linux-amd64","version":"26.8.15.10","build":"20261001.183455","manifest":"sha256:`) {
		t.Errorf("--json = %q, want members in the order platform, version, build, manifest", out)
	}

	// Offline: the cache's answer, and the offline miss before anything is installed.
	if code, out, errText = runCLI(t, env, "resolve", "26.8", "--offline"); code != 1 || out != "" || !strings.Contains(errText, "CHTYPES_ARTIFACT_MISSING") {
		t.Errorf("resolve --offline with nothing installed = %d %q %q, want CHTYPES_ARTIFACT_MISSING", code, out, errText)
	}
	env["CHTYPES_TARGET"] = "linux-arm64"
	if code, _, errText = runCLI(t, env, "fetch", "26.8"); code != 0 {
		t.Fatalf("fetch 26.8 = %d %q", code, errText)
	}
	code, out, errText = runCLI(t, env, "resolve", "26.8", "--offline")
	if code != 0 || out != lines[1]+"\n" {
		t.Errorf("resolve --offline after fetching linux-arm64 = %d %q %q, want %q", code, out, errText, lines[1])
	}
	if code, _, errText = runCLI(t, env, "resolve", "1.1"); code != 4 || !strings.Contains(errText, "CHTYPES_ARTIFACT_UNPUBLISHED") {
		t.Errorf("resolve of a line nothing publishes = %d %q", code, errText)
	}
}

func TestPruneAgainstTheFixtureTree(t *testing.T) {
	base, key := fixtureBase(t)
	cache := t.TempDir()
	env := map[string]string{"CHTYPES_ARTIFACTS_URL": base, "CHTYPES_TRUSTED_KEYS": key, "CHTYPES_CACHE": cache, "CHTYPES_TARGET": "linux-arm64"}
	dirs := map[string]string{}
	for _, spelling := range []string{"26.7.10.3", "26.7", "26.8"} {
		code, out, errText := runCLI(t, env, "fetch", spelling)
		if code != 0 {
			t.Fatalf("fetch %s = %d %q", spelling, code, errText)
		}
		dirs[spelling] = strings.TrimSpace(out)
	}
	older := "would-prune 26.7.10.3 linux-arm64 " + dirs["26.7.10.3"] + "\n"

	code, out, errText := runCLI(t, env, "prune", "--dry-run")
	if code != 0 || out != older || !strings.Contains(errText, "would prune 1 build(s) under "+cache) {
		t.Fatalf("prune --dry-run = %d %q %q, want %q", code, out, errText, older)
	}
	if _, err := os.Stat(dirs["26.7.10.3"]); err != nil {
		t.Fatalf("the dry run removed %s: %v", dirs["26.7.10.3"], err)
	}
	if code, out, _ = runCLI(t, env, "prune", "--line", "26.8"); code != 0 || out != "" {
		t.Errorf("prune --line 26.8, one build = %d %q, want nothing", code, out)
	}
	if code, out, _ = runCLI(t, env, "prune", "--keep", "2"); code != 0 || out != "" {
		t.Errorf("prune --keep 2 = %d %q, want nothing", code, out)
	}
	code, out, errText = runCLI(t, env, "prune", "--line", "26.7")
	if code != 0 || out != strings.Replace(older, "would-prune", "pruned", 1) || !strings.Contains(errText, "pruned 1 build(s)") {
		t.Fatalf("prune --line 26.7 = %d %q %q", code, out, errText)
	}
	if _, err := os.Stat(dirs["26.7.10.3"]); !os.IsNotExist(err) {
		t.Errorf("the pruned entry %s is still there (%v)", dirs["26.7.10.3"], err)
	}
	code, out, _ = runCLI(t, env, "list", "--offline")
	if code != 0 || strings.Contains(out, "26.7.10.3") || !strings.Contains(out, "installed 26.7.15.5 ") || !strings.Contains(out, "installed 26.8.15.10 ") {
		t.Errorf("list --offline after the prune = %d %q", code, out)
	}
	if code, out, _ = runCLI(t, env, "prune"); code != 0 || out != "" {
		t.Errorf("a second prune = %d %q, want nothing left to prune", code, out)
	}
}
