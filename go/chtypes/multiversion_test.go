package chtypes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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
// the core repository's server-truth suites against this same tree — but never a
// quiet one: the message names the directory, what was wrong with it, and the
// one command that fills it.
func skipNoArtifacts(t *testing.T, dir, detail string) {
	t.Helper()
	t.Skipf("no chtypes artifacts under %s: %s — fetch one with scripts/fetch.sh 25.8 (docs/guides/fetch.md), or point $CHTYPES_REGISTRY at a registry", dir, detail)
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

// ------------------------------------------------- load-time verification
//
// WithVerifyChecksums is the Go half of docs/reference/artifact.md
// §Verification, and the thing under test is the ORDER: the hash decides
// before dlopen, because an image cannot be unmapped once it is mapped. The
// artifact-free cases below therefore use a library that could never load — a
// text file — and read the verdict off WHICH error came back: a checksum
// error means the check ran first, a dlopen error means it passed and the
// load went on.

// writeFakeArtifact lays out <dir>/<line>/ with a manifest and a stand-in
// "library" that is plain text: enough for the loader to reach dlopen, and
// never enough to survive it.
func writeFakeArtifact(t *testing.T, dir, line string, manifest map[string]any, body []byte) string {
	t.Helper()
	sub := filepath.Join(dir, line)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	name, _ := manifest["library"].(string)
	if err := os.WriteFile(filepath.Join(sub, name), body, 0o644); err != nil {
		t.Fatal(err)
	}
	blob, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "manifest.json"), blob, 0o644); err != nil {
		t.Fatal(err)
	}
	return sub
}

func TestVerifyChecksumsRefusesBytesTheManifestDoesNotClaim(t *testing.T) {
	body := []byte("not a shared library")
	dir := t.TempDir()
	writeFakeArtifact(t, dir, "25.8", map[string]any{
		"library":            "libchtypes.so",
		"library_bytes":      len(body),
		"library_sha256":     strings.Repeat("00", 32),
		"clickhouse_version": "25.8.1.1",
		"clickhouse_minor":   "25.8",
	}, body)

	_, err := NewRegistry(dir, WithVerifyChecksums(true))
	if err == nil {
		t.Fatal("a library whose bytes do not match its manifest loaded anyway")
	}
	if !strings.Contains(err.Error(), "does not match manifest") {
		t.Fatalf("want the checksum refusal BEFORE dlopen, got: %v", err)
	}

	// The same directory without the option: the load gets as far as dlopen,
	// which is what proves the option — and not merely the broken file —
	// produced the verdict above.
	_, err = NewRegistry(dir)
	if err == nil || strings.Contains(err.Error(), "does not match manifest") {
		t.Fatalf("without WithVerifyChecksums the hash must not be consulted; got: %v", err)
	}
}

func TestVerifyChecksumsAcceptsMatchingBytesAndLoadsOn(t *testing.T) {
	body := []byte("not a shared library")
	dir := t.TempDir()
	writeFakeArtifact(t, dir, "25.8", map[string]any{
		"library":            "libchtypes.so",
		"library_bytes":      len(body),
		"library_sha256":     sha256Hex(body),
		"clickhouse_version": "25.8.1.1",
		"clickhouse_minor":   "25.8",
	}, body)

	// The hash matches, so verification passes and the load proceeds to dlopen,
	// which is where a text file dies. A checksum error here would mean the
	// check refused bytes it had just been told were correct.
	_, err := NewRegistry(dir, WithVerifyChecksums(true))
	if err == nil {
		t.Fatal("a text file cannot dlopen; the load must still fail")
	}
	if strings.Contains(err.Error(), "does not match manifest") || strings.Contains(err.Error(), "cannot verify") {
		t.Fatalf("matching bytes must pass verification; got: %v", err)
	}
	if !strings.Contains(err.Error(), "dlopen") {
		t.Fatalf("want the dlopen failure that follows a passing check, got: %v", err)
	}
}

func TestVerifyChecksumsRefusesAManifestWithNoHash(t *testing.T) {
	body := []byte("not a shared library")
	dir := t.TempDir()
	writeFakeArtifact(t, dir, "25.8", map[string]any{
		"library":            "libchtypes.so",
		"clickhouse_version": "25.8.1.1",
		"clickhouse_minor":   "25.8",
	}, body)

	// Verification asked for and not possible is not verification: a manifest
	// carrying no library_sha256 is refused, never passed over in silence.
	_, err := NewRegistry(dir, WithVerifyChecksums(true))
	if err == nil || !strings.Contains(err.Error(), "cannot verify") {
		t.Fatalf("want a refusal naming the unverifiable artifact, got: %v", err)
	}
}

func TestVerifyChecksumsAgainstARealArtifact(t *testing.T) {
	inst := smallestInstalled(t)
	// One line, hard-linked rather than copied: the bytes (and so the hash) are
	// the artifact's own, and nothing duplicates 230 MB to prove it.
	dir := t.TempDir()
	sub := filepath.Join(dir, inst.Line)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(inst.Dir, inst.Library), filepath.Join(sub, inst.Library)); err != nil {
		t.Skipf("cannot hard-link %s into %s: %v", inst.Library, sub, err)
	}
	for _, name := range []string{"manifest.json", "unsafe_families.txt"} {
		blob, err := os.ReadFile(filepath.Join(inst.Dir, name))
		if err != nil {
			continue
		}
		if err := os.WriteFile(filepath.Join(sub, name), blob, 0o644); err != nil {
			t.Fatal(err)
		}
	}

	r, err := NewRegistry(dir, WithVerifyChecksums(true))
	if err != nil {
		t.Fatalf("a real artifact must survive its own checksum: %v", err)
	}
	lib, err := r.For(Version(inst.Line))
	if err != nil {
		t.Fatal(err)
	}
	cs, err := lib.CompileDDL("x UInt8")
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	res, err := cs.Rows(JSONEachRow, []byte(`{"x":1}`), nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Outcome != Accepted || len(res.Rows) != 1 {
		t.Fatalf("verified artifact answered %+v", res)
	}

	// The load above passing is not proof that anything was hashed. Same real
	// library, same hard link, a manifest that claims a different digest: the
	// refusal has to name the artifact's OWN sha256, which only a hash that
	// actually ran over those bytes can produce.
	bad := t.TempDir()
	badSub := filepath.Join(bad, inst.Line)
	if err := os.MkdirAll(badSub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(inst.Dir, inst.Library), filepath.Join(badSub, inst.Library)); err != nil {
		t.Fatal(err)
	}
	blob, err := os.ReadFile(filepath.Join(inst.Dir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(blob, &m); err != nil {
		t.Fatal(err)
	}
	m["library_sha256"] = strings.Repeat("00", 32)
	tampered, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(badSub, "manifest.json"), tampered, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = NewRegistry(bad, WithVerifyChecksums(true))
	if err == nil {
		t.Fatal("a manifest claiming the wrong digest for a real library was accepted")
	}
	if !strings.Contains(err.Error(), strings.ToLower(inst.LibrarySHA256)) {
		t.Fatalf("the refusal must carry the digest it computed (%s); got: %v", inst.LibrarySHA256, err)
	}
}
