package chtypes

// resolve_test.go — exact-patch resolution, the flat/patches layout, the
// same-line warned fallback, and lock schema 2 (issue #284). Cases are
// labelled with the design's own P-numbers (the design comment on #284)
// where one applies.
//
// Most of this file drives Ensure/FetchAll/ListInstalled/VerifyInstalled
// against synthetic two-patch releases built by twoPatchRelease below (no
// dlopen: those functions never open a library). The pure resolution seam
// (patchesInDir, locateLineAnywhere, locatePatchAnywhere) is tested directly
// against fake manifests, per the design's own "Resolution seam" note. A
// handful of cases need a REAL, loadable library (the warning, the R-c
// throttle) and reuse autofetch_test.go's real-artifact machinery
// (smallestInstalled), skipping loudly without a registry like every other
// registry test here.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// twoPatchRelease builds a signed release publishing TWO patches of one
// line — the shape the design's §8 fixture calls "two-patches" — with a
// fresh key the environment trusts.
func twoPatchRelease(t *testing.T) (dir string, older, newer testArtifact) {
	t.Helper()
	pub, priv := newTestKey(t)
	older = fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	newer = fakeArtifact(HostPlatform(), "25.8.33.5-lts")
	dir = filepath.Join(t.TempDir(), "release")
	writeRelease(t, dir, priv, older, newer)
	trustKey(t, pub)
	return dir, older, newer
}

// writeManifestOnlyPatch lays out one artifact directory holding a
// manifest.json (and nothing loadable) — for the resolution-seam tests,
// which never dlopen (the design's "Resolution seam" test-support note).
func writeManifestOnlyPatch(t *testing.T, dir, version, minor string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	manifest := fmt.Sprintf(`{"library":"libchtypes.so","clickhouse_version":%q,"clickhouse_minor":%q}`, version, minor)
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
}

// ---------------------------------------------------------- resolution seam
// (P11: resolution without dlopen, over directories built from fake
// manifests, including a flat install.)

func TestPatchesInDirFindsBothTheFlatSlotAndPatches(t *testing.T) {
	dir := t.TempDir()
	writeManifestOnlyPatch(t, filepath.Join(dir, "25.8"), "25.8.28.1-lts", "25.8")
	writeManifestOnlyPatch(t, filepath.Join(dir, "patches", "25.8", "25.8.33.5-lts"), "25.8.33.5-lts", "25.8")
	// A dot-prefixed sibling (in-flight staging) must never be read as a patch.
	writeManifestOnlyPatch(t, filepath.Join(dir, "patches", "25.8", ".25.8.99.9-lts.incoming.123"), "25.8.99.9-lts", "25.8")

	got := patchesInDir(dir, "25.8")
	if len(got) != 2 {
		t.Fatalf("patchesInDir = %+v, want 2 (flat + one nested, dot-prefixed excluded)", got)
	}
	var sawFlat, sawNested bool
	for _, p := range got {
		switch {
		case p.flat && p.version == "25.8.28.1-lts":
			sawFlat = true
		case !p.flat && p.version == "25.8.33.5-lts":
			sawNested = true
		default:
			t.Fatalf("unexpected entry %+v", p)
		}
	}
	if !sawFlat || !sawNested {
		t.Fatalf("got %+v, missing flat or nested", got)
	}

	// A different line has nothing here.
	if got := patchesInDir(dir, "26.7"); len(got) != 0 {
		t.Fatalf("patchesInDir for an absent line = %+v", got)
	}
}

func TestLocateLineAnywhereTakesNewestInFirstDirWithAny(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	// `a` holds only an OLDER patch of 25.8, nested; `b` holds a NEWER one,
	// flat. The first search-path directory that holds ANY patch of the
	// line wins — here that is `a` — and within it, the newest patch found.
	writeManifestOnlyPatch(t, filepath.Join(a, "patches", "25.8", "25.8.10.1-lts"), "25.8.10.1-lts", "25.8")
	writeManifestOnlyPatch(t, filepath.Join(a, "patches", "25.8", "25.8.9.1-lts"), "25.8.9.1-lts", "25.8")
	writeManifestOnlyPatch(t, filepath.Join(b, "25.8"), "25.8.99.1-lts", "25.8")

	p, ok := locateLineAnywhere([]string{a, b}, "25.8")
	if !ok {
		t.Fatal("locateLineAnywhere found nothing")
	}
	if p.version != "25.8.10.1-lts" {
		t.Fatalf("got %s, want the newest patch IN THE FIRST directory (25.8.10.1-lts), not the newer one in the second (25.8.99.1-lts)", p.version)
	}

	// A line neither directory has anything for is MISSING.
	if _, ok := locateLineAnywhere([]string{a, b}, "26.7"); ok {
		t.Fatal("locateLineAnywhere found a line that does not exist")
	}
}

func TestLocatePatchAnywhereSearchesEveryDirectoryNotOnlyTheFirst(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	// `a` holds a DIFFERENT patch of the line; the one actually requested is
	// only in `b`, nested. R4 step 2 must not stop at the first directory
	// that merely holds the LINE.
	writeManifestOnlyPatch(t, filepath.Join(a, "25.8"), "25.8.1.1-lts", "25.8")
	writeManifestOnlyPatch(t, filepath.Join(b, "patches", "25.8", "25.8.2.2-lts"), "25.8.2.2-lts", "25.8")

	p, ok := locatePatchAnywhere([]string{a, b}, "25.8", "25.8.2.2-lts")
	if !ok || p.version != "25.8.2.2-lts" {
		t.Fatalf("locatePatchAnywhere = %+v, %v", p, ok)
	}

	// Decision 7: an unspelled channel matches that patch on any channel.
	p, ok = locatePatchAnywhere([]string{a, b}, "25.8", "25.8.2.2")
	if !ok || p.version != "25.8.2.2-lts" {
		t.Fatalf("channel-less spelling: %+v, %v", p, ok)
	}
	// A spelled channel matches only itself.
	if _, ok := locatePatchAnywhere([]string{a, b}, "25.8", "25.8.2.2-stable"); ok {
		t.Fatal("a spelled channel matched a different one")
	}
	// A patch neither directory has is a miss.
	if _, ok := locatePatchAnywhere([]string{a, b}, "25.8", "25.8.3.3-lts"); ok {
		t.Fatal("found a patch that does not exist")
	}
}

// TestRegistryHoldingOnlyNestedInstallsIsNotEmpty is P19: a registry that
// holds ONLY a patches/ tree (no flat install at all for the line) must not
// be reported empty at construction.
func TestRegistryHoldingOnlyNestedInstallsIsNotEmpty(t *testing.T) {
	isolateEnv(t)
	dir := t.TempDir()
	writeManifestOnlyPatch(t, filepath.Join(dir, "patches", "25.8", "25.8.28.1-lts"), "25.8.28.1-lts", "25.8")

	r, err := NewRegistry(dir)
	if err != nil {
		t.Fatalf("a nested-only registry was reported empty: %v", err)
	}
	versions := r.Versions()
	if len(versions) != 1 || versions[0] != "25.8" {
		t.Fatalf("Versions() = %v, want [25.8]", versions)
	}
}

// -------------------------------------------------------------- pinLine (R8)

// TestPinLineNeverRePointsAnAlreadyPinnedLine is R3/R8's core mechanic at
// the unit level: dlopen requires a real artifact this sandbox may not
// have two distinct patches of, so this exercises pinLine directly —
// exactly the function both Load and resolveLine call to set the pin.
func TestPinLineNeverRePointsAnAlreadyPinnedLine(t *testing.T) {
	r := &Registry{byVersion: map[string]*Library{}, linePin: map[string]*Library{}}
	first := &Library{Version: "25.8.1.1-x", Minor: "25.8"}
	second := &Library{Version: "25.8.2.2-x", Minor: "25.8"}
	r.pinLine("25.8", first)
	r.pinLine("25.8", second)
	got, ok := r.pinnedLine("25.8")
	if !ok || got != first {
		t.Fatalf("pinLine re-pointed an already-pinned line: got %+v, want %+v", got, first)
	}
}

// ------------------------------------------------------------- lock schema 2

func TestLockSchema1IsConvertedToSchema2OnRead(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "chtypes.lock")
	platform := HostPlatform()
	file := fmt.Sprintf("chtypes-25.8.28.1-lts-%s.tar.gz", platform)
	os.WriteFile(path, []byte(fmt.Sprintf(
		`{"schema": 1, "artifacts": {%q: {"file": %q, "sha256": "ab"}}}`,
		LockKey(platform, "25.8"), file)), 0o644)

	l, err := ReadLockFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if l.Schema != LockSchema {
		t.Fatalf("in-memory schema = %d, want %d", l.Schema, LockSchema)
	}
	key := LockKey(platform, "25.8.28.1-lts")
	e, ok := l.Artifacts[key]
	if !ok || e.File != file {
		t.Fatalf("converted entry missing or wrong: %+v (have %v)", e, l.Artifacts)
	}
	if _, ok := l.Artifacts[LockKey(platform, "25.8")]; ok {
		t.Fatal("the line-shaped schema-1 key survived conversion")
	}
}

func TestLockSchema1EntryThatDoesNotParseIsRefused(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chtypes.lock")
	platform := HostPlatform()
	os.WriteFile(path, []byte(fmt.Sprintf(
		`{"schema": 1, "artifacts": {%q: {"file": "not-a-chtypes-asset.tar.gz", "sha256": "ab"}}}`,
		LockKey(platform, "25.8"))), 0o644)
	_, err := ReadLockFile(path)
	if err == nil {
		t.Fatal("an unparseable schema-1 entry was accepted")
	}
	if !strings.Contains(err.Error(), LockKey(platform, "25.8")) {
		t.Fatalf("error does not name the entry: %v", err)
	}
}

func TestLockSchema2WithALineShapedKeyIsRefused(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chtypes.lock")
	key := LockKey(HostPlatform(), "25.8")
	os.WriteFile(path, []byte(fmt.Sprintf(`{"schema": 2, "artifacts": {%q: {"file": "x", "sha256": "ab"}}}`, key)), 0o644)
	_, err := ReadLockFile(path)
	if err == nil {
		t.Fatal("a schema-2 lock with a line-shaped key was accepted")
	}
	if !strings.Contains(err.Error(), key) {
		t.Fatalf("error does not name the key: %v", err)
	}
}

func TestLockSchemaOutOfRangeNamesBothAccepted(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chtypes.lock")
	os.WriteFile(path, []byte(`{"schema": 3, "artifacts": {}}`), 0o644)
	_, err := ReadLockFile(path)
	if err == nil || !strings.Contains(err.Error(), "1") || !strings.Contains(err.Error(), "2") {
		t.Fatalf("schema 3: %v", err)
	}
}

// ----------------------------------------------------- P1-P10: the fixture
// shape, against a synthetic two-patch release (no dlopen: Ensure/FetchAll/
// ListInstalled/VerifyInstalled never open a library).

// P1.
func TestFetchTwoExactPatchesCoexistAndDoNotTouchEachOther(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	dest := t.TempDir()

	i1, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	i2, err := Ensure(context.Background(), newer.version, FetchOptions{URL: "file://" + dir, Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	wantOlder := filepath.Join(dest, patchesDirName, "25.8", older.version)
	wantNewer := filepath.Join(dest, patchesDirName, "25.8", newer.version)
	if i1.Dir != wantOlder {
		t.Fatalf("older installed at %s, want %s", i1.Dir, wantOlder)
	}
	if i2.Dir != wantNewer {
		t.Fatalf("newer installed at %s, want %s", i2.Dir, wantNewer)
	}
	if _, err := os.Stat(filepath.Join(dest, "25.8")); err == nil {
		t.Fatal("two exact-patch fetches populated the flat slot; neither is a line request")
	}
	g1, err := fileSHA256(filepath.Join(i1.Dir, i1.Library))
	if err != nil || g1 != sha256Hex(older.content) {
		t.Fatalf("older library hash = %s, %v", g1, err)
	}
	g2, err := fileSHA256(filepath.Join(i2.Dir, i2.Library))
	if err != nil || g2 != sha256Hex(newer.content) {
		t.Fatalf("newer library hash = %s, %v", g2, err)
	}
}

// P2.
func TestFetchLineInstallsTheNewestPatchFlat(t *testing.T) {
	isolateEnv(t)
	dir, _, newer := twoPatchRelease(t)
	dest := t.TempDir()
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(dest, "25.8")
	if inst.Dir != want || inst.Version != newer.version {
		t.Fatalf("got dir=%s version=%s, want %s / %s", inst.Dir, inst.Version, want, newer.version)
	}
}

// P3.
func TestFetchExactPatchDecision7AndFetchNeverFallsBack(t *testing.T) {
	isolateEnv(t)
	dir, older, _ := twoPatchRelease(t)
	dest := t.TempDir()
	stripped := channelSuffix.ReplaceAllString(older.version, "")
	inst, err := Ensure(context.Background(), stripped, FetchOptions{URL: "file://" + dir, Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	if inst.Version != older.version {
		t.Fatalf("fetch %s installed %s, want %s (Decision 7)", stripped, inst.Version, older.version)
	}

	missing := "25.8.30.2-lts" // strictly between the two served patches
	_, err = Ensure(context.Background(), missing, FetchOptions{URL: "file://" + dir, Dest: t.TempDir()})
	ae := wantCode(t, err, CodeArtifactUnpublished)
	if ae.Line != missing {
		t.Fatalf("error names %s, want %s: fetch must never fall back", ae.Line, missing)
	}
}

// P4.
func TestFetchLockRecordsBothPatchesAndRemovesNothing(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	dest := t.TempDir()
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil {
		t.Fatal(err)
	}
	if len(l.Artifacts) != 2 {
		t.Fatalf("lock has %d entries, want 2: %+v", len(l.Artifacts), l.Artifacts)
	}
	for _, v := range []string{older.version, newer.version} {
		if _, ok := l.Artifacts[LockKey(HostPlatform(), v)]; !ok {
			t.Fatalf("lock does not pin %s: %+v", v, l.Artifacts)
		}
	}
}

// P5, the headline case.
func TestFetchFrozenLineInstallsThePinnedOlderPatch(t *testing.T) {
	isolateEnv(t)
	dir, older, _ := twoPatchRelease(t)
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: t.TempDir(), LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil || len(l.Artifacts) != 1 {
		t.Fatalf("setup lock: %+v %v", l, err)
	}

	dest := t.TempDir()
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock, Frozen: true})
	if err != nil {
		t.Fatalf("frozen line fetch with only the older patch pinned: %v", err)
	}
	if inst.Version != older.version || inst.Dir != filepath.Join(dest, "25.8") {
		t.Fatalf("installed %s at %s, want %s flat", inst.Version, inst.Dir, older.version)
	}
}

// P6.
func TestFetchFrozenExactPatchPinsNothingForTheUnpinnedOne(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: t.TempDir(), LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	dest := t.TempDir()
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock, Frozen: true}); err != nil {
		t.Fatalf("frozen older: %v", err)
	}
	_, err := Ensure(context.Background(), newer.version, FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock, Frozen: true})
	ae := wantCode(t, err, CodeArtifactPinned)
	if !strings.Contains(ae.Msg, "pins nothing") {
		t.Fatalf("message: %s", ae.Msg)
	}
}

// P7.
func TestFetchLockSchema1RewrittenToSchema2HoldingBothPatches(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	platform := HostPlatform()
	olderFile := fmt.Sprintf("chtypes-%s-%s.tar.gz", older.version, platform)
	sums, err := os.ReadFile(filepath.Join(dir, "SHA256SUMS"))
	if err != nil {
		t.Fatal(err)
	}
	olderSHA, ok := parseSums(sums)[olderFile]
	if !ok {
		t.Fatalf("SHA256SUMS has no entry for %s", olderFile)
	}

	lock := filepath.Join(t.TempDir(), "schema1.lock")
	schema1Key := LockKey(platform, "25.8")
	os.WriteFile(lock, []byte(fmt.Sprintf(`{"schema": 1, "artifacts": {%q: {"file": %q, "sha256": %q}}}`,
		schema1Key, olderFile, olderSHA)), 0o644)

	dest := t.TempDir()
	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock, Frozen: true})
	if err != nil {
		t.Fatalf("frozen against a schema-1 lock: %v", err)
	}
	if inst.Version != older.version {
		t.Fatalf("installed %s, want %s", inst.Version, older.version)
	}

	// A non-frozen fetch --lock on the SAME file selects the newest served
	// patch (newer), installs it (demoting older to patches/), and rewrites
	// the lock as schema 2 — holding BOTH entries, since --lock never
	// removes one.
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil {
		t.Fatal(err)
	}
	if l.Schema != LockSchema {
		t.Fatalf("schema = %d, want %d", l.Schema, LockSchema)
	}
	for _, v := range []string{older.version, newer.version} {
		if _, ok := l.Artifacts[LockKey(platform, v)]; !ok {
			t.Fatalf("lock does not pin %s after rewrite: %+v", v, l.Artifacts)
		}
	}
	raw, _ := os.ReadFile(lock)
	if !strings.Contains(string(raw), `"schema": 2`) {
		t.Fatalf("lock bytes still read schema 1:\n%s", raw)
	}
	// The demotion this second fetch triggered left older reachable.
	if _, err := readManifest(filepath.Join(dest, patchesDirName, "25.8", older.version, "manifest.json")); err != nil {
		t.Fatalf("older was not demoted: %v", err)
	}
}

// P8.
func TestFetchAllFrozenInstallsOnlyThePinnedPatch(t *testing.T) {
	isolateEnv(t)
	dir, older, _ := twoPatchRelease(t)
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: t.TempDir(), LockFile: lock}); err != nil {
		t.Fatal(err)
	}
	dest := t.TempDir()
	all, err := FetchAll(context.Background(), FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock, Frozen: true})
	if err != nil {
		t.Fatalf("--all --frozen: %v", err)
	}
	if len(all) != 1 || all[0].Version != older.version {
		t.Fatalf("--all --frozen installed %+v, want only %s", all, older.version)
	}
}

// P9.
func TestFetchOfflineAfterTwoExactPatchesUsesDecision7AndNewestOfLine(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	dest := t.TempDir()
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest}); err != nil {
		t.Fatal(err)
	}
	if _, err := Ensure(context.Background(), newer.version, FetchOptions{URL: "file://" + dir, Dest: dest}); err != nil {
		t.Fatal(err)
	}

	stripped := channelSuffix.ReplaceAllString(older.version, "")
	inst, err := Ensure(context.Background(), stripped, FetchOptions{Dest: dest, Offline: true})
	if err != nil {
		t.Fatalf("offline %s: %v", stripped, err)
	}
	if inst.Version != older.version {
		t.Fatalf("offline %s resolved %s, want %s", stripped, inst.Version, older.version)
	}

	inst, err = Ensure(context.Background(), "25.8", FetchOptions{Dest: dest, Offline: true})
	if err != nil {
		t.Fatalf("offline line: %v", err)
	}
	if inst.Version != newer.version {
		t.Fatalf("offline line resolved %s, want the newest (%s)", inst.Version, newer.version)
	}

	missing := "25.8.30.2-lts"
	if _, err := Ensure(context.Background(), missing, FetchOptions{Dest: dest, Offline: true}); err == nil {
		t.Fatal("offline resolved a patch that was never installed")
	} else {
		wantCode(t, err, CodeSourceUnreachable)
	}

	// --offline --frozen with no lock file at all still succeeds: offline's
	// verdict never depends on the lock (docs/guides/fetch.md §6).
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{
		Dest: dest, Offline: true, Frozen: true, LockFile: filepath.Join(t.TempDir(), "none.lock"),
	}); err != nil {
		t.Fatalf("offline+frozen with no lock file: %v", err)
	}
}

// P10.
func TestListInstalledAndVerifyReportBothPatches(t *testing.T) {
	isolateEnv(t)
	dir, older, newer := twoPatchRelease(t)
	dest := t.TempDir()
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest}); err != nil {
		t.Fatal(err)
	}
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + dir, Dest: dest}); err != nil {
		t.Fatal(err)
	}
	installed, err := ListInstalled(dest)
	if err != nil {
		t.Fatal(err)
	}
	if len(installed) != 2 {
		t.Fatalf("ListInstalled = %+v, want 2 entries", installed)
	}
	dirOf := map[string]string{}
	for _, inst := range installed {
		dirOf[inst.Version] = inst.Dir
	}
	if dirOf[older.version] != filepath.Join(dest, patchesDirName, "25.8", older.version) {
		t.Fatalf("older listed at %s", dirOf[older.version])
	}
	if dirOf[newer.version] != filepath.Join(dest, "25.8") {
		t.Fatalf("newer listed at %s", dirOf[newer.version])
	}
	results, err := VerifyInstalled(dest)
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 2 {
		t.Fatalf("VerifyInstalled = %+v", results)
	}
	for _, r := range results {
		if !r.OK {
			t.Fatalf("%+v not OK", r)
		}
	}
}

// The demotion rule itself (amendment on #284, comment 5919216061): a line
// fetch that moves the flat slot's occupant demotes the outgoing patch into
// patches/<minor>/<its version>/, byte-identical, rather than deleting it,
// and an exact request for it then resolves there without a fetch.
func TestFetchDemotesTheOutgoingFlatPatchOnLineChange(t *testing.T) {
	isolateEnv(t)
	pub, priv := newTestKey(t)
	trustKey(t, pub)
	older := fakeArtifact(HostPlatform(), "25.8.28.1-lts")
	newer := fakeArtifact(HostPlatform(), "25.8.33.5-lts")

	onlyOlderDir := filepath.Join(t.TempDir(), "only-older")
	writeRelease(t, onlyOlderDir, priv, older)
	bothDir := filepath.Join(t.TempDir(), "both")
	writeRelease(t, bothDir, priv, older, newer)

	dest := t.TempDir()
	if _, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + onlyOlderDir, Dest: dest}); err != nil {
		t.Fatal(err)
	}
	flatDir := filepath.Join(dest, "25.8")
	m, err := readManifest(filepath.Join(flatDir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m.ClickHouseVersion != older.version {
		t.Fatalf("flat slot holds %s, want %s", m.ClickHouseVersion, older.version)
	}
	beforeHash, err := fileSHA256(filepath.Join(flatDir, m.Library))
	if err != nil {
		t.Fatal(err)
	}

	inst, err := Ensure(context.Background(), "25.8", FetchOptions{URL: "file://" + bothDir, Dest: dest})
	if err != nil {
		t.Fatal(err)
	}
	if inst.Version != newer.version {
		t.Fatalf("line fetch against the two-patch release installed %s, want %s", inst.Version, newer.version)
	}

	demotedDir := filepath.Join(dest, patchesDirName, "25.8", older.version)
	dm, err := readManifest(filepath.Join(demotedDir, "manifest.json"))
	if err != nil {
		t.Fatalf("older was not demoted to %s: %v", demotedDir, err)
	}
	if dm.ClickHouseVersion != older.version {
		t.Fatalf("demoted manifest names %s", dm.ClickHouseVersion)
	}
	afterHash, err := fileSHA256(filepath.Join(demotedDir, dm.Library))
	if err != nil {
		t.Fatal(err)
	}
	if afterHash != beforeHash {
		t.Fatalf("demoted library hashes %s, want the original %s (a rename, not a copy)", afterHash, beforeHash)
	}

	// It resolves offline, exactly, with no fetch — no re-download, no
	// duplicated bytes.
	got, err := Ensure(context.Background(), older.version, FetchOptions{Dest: dest, Offline: true})
	if err != nil {
		t.Fatalf("offline exact resolve of the demoted patch: %v", err)
	}
	if got.Dir != demotedDir {
		t.Fatalf("offline resolved at %s, want %s", got.Dir, demotedDir)
	}

	// The flat slot now holds newer, and only newer.
	m2, err := readManifest(filepath.Join(flatDir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	if m2.ClickHouseVersion != newer.version {
		t.Fatalf("flat slot now holds %s, want %s", m2.ClickHouseVersion, newer.version)
	}
}

// P16 (the remaining case): --lock without --frozen re-pins an entry that
// differs in sha256 — the regression test for the design's §0 item 4 (a
// Python-only bug at the time; asserted here as the shared contract every
// binding now follows).
func TestLockWithoutFrozenRewritesADriftedEntry(t *testing.T) {
	isolateEnv(t)
	dir, older, _ := twoPatchRelease(t)
	dest := t.TempDir()
	lock := filepath.Join(t.TempDir(), "chtypes.lock")
	inst, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock})
	if err != nil {
		t.Fatal(err)
	}
	l, err := ReadLockFile(lock)
	if err != nil {
		t.Fatal(err)
	}
	key := LockKey(HostPlatform(), older.version)
	e := l.Artifacts[key]
	e.SHA256 = strings.Repeat("0", 64)
	l.Artifacts[key] = e
	if err := l.Write(lock); err != nil {
		t.Fatal(err)
	}
	// --lock (no --frozen) over the now-drifted entry installs (it is
	// already installed and verified) and REWRITES the entry — it must
	// never refuse because of the existing, wrong pin.
	if _, err := Ensure(context.Background(), older.version, FetchOptions{URL: "file://" + dir, Dest: dest, LockFile: lock}); err != nil {
		t.Fatalf("--lock without --frozen refused on an existing drifted entry: %v", err)
	}
	l2, err := ReadLockFile(lock)
	if err != nil {
		t.Fatal(err)
	}
	if l2.Artifacts[key].SHA256 != strings.ToLower(inst.SHA256) {
		t.Fatalf("the drifted entry was not rewritten: %+v", l2.Artifacts[key])
	}
}

// --------------------------------------------------- real-artifact cases:
// the warning and the R-c 60s throttle. Both need something Resolve can
// actually dlopen, so they reuse autofetch_test.go's real-artifact
// machinery and skip loudly without a registry, like every registry test
// here.

// P15 (the once-per-pair half; the exact body text is asserted, the wording
// itself is fixed by §3).
func TestResolveFallbackWarnsOncePerPairAndSetsExactFalse(t *testing.T) {
	inst := smallestInstalled(t)
	isolateEnv(t)
	dir := t.TempDir()
	sub := filepath.Join(dir, inst.Line)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(inst.Dir, inst.Library), filepath.Join(sub, inst.Library)); err != nil {
		t.Skipf("cannot hard-link %s: %v", inst.Library, err)
	}
	for _, name := range []string{"manifest.json", "unsafe_families.txt"} {
		if b, err := os.ReadFile(filepath.Join(inst.Dir, name)); err == nil {
			os.WriteFile(filepath.Join(sub, name), b, 0o644)
		}
	}

	var progress strings.Builder
	r, err := NewRegistry(dir, WithFetchOptions(FetchOptions{Progress: &progress}))
	if err != nil {
		t.Fatal(err)
	}
	missing := Version(inst.Line + ".999.1-lts")
	res, err := r.Resolve(missing)
	if err != nil {
		t.Fatalf("fallback resolve: %v", err)
	}
	if res.Exact {
		t.Fatal("Exact = true for a patch that was never installed")
	}
	if res.Requested != missing {
		t.Fatalf("Requested = %s, want %s", res.Requested, missing)
	}
	if string(res.Version) != inst.Version {
		t.Fatalf("Version = %s, want the installed patch %s", res.Version, inst.Version)
	}
	if res.Library == nil {
		t.Fatal("nil Library on a successful fallback")
	}
	body := progress.String()
	if strings.Count(body, "chtypes: WARNING:") != 1 {
		t.Fatalf("want exactly one warning, got:\n%s", body)
	}
	if !strings.Contains(body, string(missing)) || !strings.Contains(body, inst.Version) || !strings.Contains(body, "Behavior can differ between patches") {
		t.Fatalf("warning missing expected text:\n%s", body)
	}

	// A second resolve of the SAME pair warns no further.
	if _, err := r.Resolve(missing); err != nil {
		t.Fatal(err)
	}
	if strings.Count(progress.String(), "chtypes: WARNING:") != 1 {
		t.Fatalf("a second resolve of the same pair warned again:\n%s", progress.String())
	}
}

// R-c: the fallback re-check is throttled to once per 60s per (registry,
// requested patch) — not a directory read on every call. Proven by pulling
// the installed artifact out from under the registry between two calls:
// within the window, the SECOND call must still answer (from the R-c
// cache), even though a fresh scan would now find nothing; once the window
// is shrunk to nothing, the NEXT call re-scans and fails.
func TestResolveRcThrottlesTheFallbackRescan(t *testing.T) {
	inst := smallestInstalled(t)
	isolateEnv(t)
	dir := t.TempDir()
	sub := filepath.Join(dir, inst.Line)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(inst.Dir, inst.Library), filepath.Join(sub, inst.Library)); err != nil {
		t.Skipf("cannot hard-link %s: %v", inst.Library, err)
	}
	for _, name := range []string{"manifest.json", "unsafe_families.txt"} {
		if b, err := os.ReadFile(filepath.Join(inst.Dir, name)); err == nil {
			os.WriteFile(filepath.Join(sub, name), b, 0o644)
		}
	}

	r, err := NewRegistry(dir)
	if err != nil {
		t.Fatal(err)
	}
	missing := Version(inst.Line + ".999.2-lts")
	if _, err := r.Resolve(missing); err != nil {
		t.Fatalf("first fallback resolve: %v", err)
	}

	// Pull the only installed artifact out from under it.
	if err := os.RemoveAll(sub); err != nil {
		t.Fatal(err)
	}

	// Still within the (default, 60s) window: the cached fallback answers,
	// no re-scan.
	res, err := r.Resolve(missing)
	if err != nil {
		t.Fatalf("within the R-c window, the cached fallback must still answer: %v", err)
	}
	if string(res.Version) != inst.Version {
		t.Fatalf("cached fallback answered %s, want %s", res.Version, inst.Version)
	}

	// Shrink the window to nothing and let it lapse; the NEXT call re-scans,
	// finds nothing, and fails.
	old := fallbackRecheckInterval
	fallbackRecheckInterval = time.Nanosecond
	t.Cleanup(func() { fallbackRecheckInterval = old })
	time.Sleep(time.Millisecond)
	if _, err := r.Resolve(missing); err == nil {
		t.Fatal("after the window lapsed, resolve did not re-scan the (now empty) destination")
	}
}

// R7: WithPreload never fetches, even with AutoFetch on.
func TestPreloadNeverFetchesEvenWithAutoFetchOn(t *testing.T) {
	isolateEnv(t)
	unreachable := "file://" + filepath.Join(t.TempDir(), "does-not-exist")
	_, err := NewRegistry(t.TempDir(),
		WithAutoFetch(true),
		WithFetchOptions(FetchOptions{URL: unreachable}),
		WithPreload("25.8"),
	)
	if err == nil {
		t.Fatal("preload of a missing line succeeded")
	}
	var ae *ArtifactError
	if !errors.As(err, &ae) {
		t.Fatalf("want *ArtifactError (ErrArtifactMissing), got %T: %v", err, err)
	}
	if ae.Code != CodeArtifactMissing {
		t.Fatalf("preload fetched (code=%s) instead of failing with ErrArtifactMissing: %v", ae.Code, err)
	}
}
