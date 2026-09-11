package chtypes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// Multi-version dispatch: N vendored builds in one process, dlopen'd with
// RTLD_LOCAL so each keeps its own copy of ClickHouse's symbols and globals.
func TestRegistryLoadsAndDispatches(t *testing.T) {
	// The library to duplicate comes out of the artifact registry the other
	// registry tests use ($CHTYPES_REGISTRY, else the per-user cache), through its own
	// manifest.json — never out of a core build tree, which the dlopen-only build of
	// this package (no chtypes_linked tag) does not know exists.
	src, soext := testRegistryLibrary(t)
	dir := t.TempDir()
	// Two directories, same artifact: proves the loader, the isolation and the
	// dispatch. The genuine two-ClickHouse test is release.sh + two tags.
	for _, v := range []string{"a", "b"} {
		sub := filepath.Join(dir, v)
		os.MkdirAll(sub, 0o755)
		blob, _ := os.ReadFile(src)
		os.WriteFile(filepath.Join(sub, "libchtypes"+soext), blob, 0o755)
		os.WriteFile(filepath.Join(sub, "manifest.json"),
			[]byte(`{"library":"libchtypes`+soext+`","clickhouse_version":"x","clickhouse_minor":"x"}`), 0o644)
	}
	r, err := NewRegistry(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("registry versions: %v", r.Versions())

	lib, err := r.For(Version(r.Versions()[0]))
	if err != nil {
		t.Fatal(err)
	}
	cs, err := lib.CompileDDL("x UInt8")
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.Rows(JSONEachRow, []byte(`{"x":256}`), nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Accepted || len(res.Rows) != 1 || res.Rows[0].Values[0].Text != "0" {
		t.Fatalf("got %+v", res)
	}
	if len(res.Transformed) != 1 || res.Transformed[0].Reason != ReasonOverflowWrap {
		t.Fatalf("transformed = %+v", res.Transformed)
	}
}

// testRegistryDir resolves the artifact registry the registry-path tests
// load: $CHTYPES_REGISTRY when set, else the per-user artifact cache for this
// host — ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>, where
// scripts/fetch.sh installs and where a core-repo build lands (that is the
// one directory every SDK, playground and test here agrees on). When that
// directory holds no installed line — absent, empty, or pointed somewhere
// wrong — the test skips through skipNoArtifacts: loudly, by name, saying
// where it looked and how to fill it. Only the directory it was pointed at
// counts; a test that fell through to some other directory on the search
// path would be asserting against artifacts nobody asked for.
func testRegistryDir(t *testing.T) string {
	t.Helper()
	dir, source := os.Getenv("CHTYPES_REGISTRY"), "$CHTYPES_REGISTRY"
	if dir == "" {
		dir, source = DefaultRegistryDir(), "the per-user cache; $CHTYPES_REGISTRY is unset"
	}
	installed, err := ListInstalled(dir)
	switch {
	case err != nil:
		skipNoArtifacts(t, dir, err.Error()+" ("+source+")")
	case len(installed) == 0:
		if _, statErr := os.Stat(dir); statErr != nil {
			skipNoArtifacts(t, dir, "no such directory ("+source+")")
		}
		skipNoArtifacts(t, dir, "no <line>/manifest.json naming an installed library ("+source+")")
	}
	return dir
}

// skipNoArtifacts is the one skip every registry test lands on when the
// registry it was pointed at holds nothing. It is the right verdict — a
// hosted CI runner has no artifacts, and the artifact-backed proof runs in
// the core repository's certify workflow against this same tree — but never a
// quiet one: the message names the directory, what was wrong with it, and the
// one command that fills it.
func skipNoArtifacts(t *testing.T, dir, detail string) {
	t.Helper()
	t.Skipf("no chtypes artifacts under %s: %s — fetch one with scripts/fetch.sh 25.8 (docs/fetch.md), or point $CHTYPES_REGISTRY at a registry", dir, detail)
}

// testRegistryLibrary returns one artifact's shared-library path from the
// test registry, resolved the way the loader contract says (docs/reference/artifact.md:
// the file name comes from manifest.json's `library` field, never guessed),
// plus the file's extension.
func testRegistryLibrary(t *testing.T) (path, ext string) {
	t.Helper()
	root := testRegistryDir(t)
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Skipf("registry %s unreadable: %v", root, err)
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		blob, err := os.ReadFile(filepath.Join(root, e.Name(), "manifest.json"))
		if err != nil {
			continue
		}
		var m struct {
			Library string `json:"library"`
		}
		if json.Unmarshal(blob, &m) != nil || m.Library == "" {
			continue
		}
		p := filepath.Join(root, e.Name(), m.Library)
		if _, err := os.Stat(p); err == nil {
			return p, filepath.Ext(m.Library)
		}
	}
	t.Skipf("registry %s holds no loadable artifact (no <dir>/manifest.json naming an existing library)", root)
	return "", ""
}
