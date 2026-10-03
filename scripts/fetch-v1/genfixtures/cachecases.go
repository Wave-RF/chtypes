package main

import (
	"fmt"
	"os"
)

// cachecases.go — §1/§6's "Cache" case group (docs/guides/fetch-v1.md §1,
// §6). Every layout here is a plain OCI image layout (oci-layout,
// index.json, blobs/sha256/<hex>) copied byte-for-byte from a tree this
// file also builds, so "the cache already has these bytes" is literally
// true rather than approximated. See layout.go's header comment for the
// `installed.json` convention the three cases that need an
// already-installed state (not merely present blobs) use.

func buildCacheCases(fs *FileSet) []Case {
	var cases []Case
	var extraLayouts []*Layout
	tree := NewTree("cache")

	newArtifact := func(version, build, seed string) PlatformArtifact {
		return buildPlatformArtifact(tree, testKey, "linux-arm64", version, build, "lts", ArtifactOptions{LibraryContentSeed: seed})
	}
	tagArtifact := func(tag string, art PlatformArtifact) {
		idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(art.ManifestDesc, "linux-arm64")}}
		tree.PutManifest(tag, ociIndexMediaType, canonicalJSON(idx))
	}

	offlineCase := func(id, cacheFixture, spelling string, ok bool, code string, version, build, librarySHA256 *string) Case {
		c := newCase(id, "cache", "file", "http")
		c.Request.Spelling = spelling
		c.Request.Offline = true
		c.Setup.Cache = cacheFixture
		c.Expect.OK = ok
		c.Expect.Requests.Max = intp(0)
		if ok {
			c.Expect.Version, c.Expect.Build, c.Expect.LibrarySHA256 = version, build, librarySHA256
		} else {
			c.Expect.Code = strp(code)
		}
		return c
	}

	// --- offline-hit: the cache has exactly what --offline asks for ------
	artHit := newArtifact("26.1.1.1", "20260101.000001", "cache-hit")
	tagArtifact("c-offline-hit", artHit)
	layoutHit := NewLayout("offline-hit")
	copyArtifactIntoLayout(layoutHit, tree, artHit, "26.1")
	cases = append(cases, offlineCase("offline-hit", "offline-hit", "26.1", true, "",
		strp(artHit.Predicate.ClickHouseVersion), strp(artHit.Predicate.Build), strp(artHit.Predicate.LibrarySHA256)))

	// --- offline-miss: an empty cache, nothing to serve -------------------
	layoutMiss := NewLayout("offline-miss")
	cases = append(cases, offlineCase("offline-miss", "offline-miss", "26.1", false, "CHTYPES_ARTIFACT_MISSING", nil, nil, nil))

	// --- offline-newest-of-two: two installed VERSIONS under one line,
	// pick the newer -------------------------------------------------------
	artV1 := newArtifact("26.1.2.1", "20260102.000001", "cache-v-older")
	artV2 := newArtifact("26.1.5.2", "20260105.000002", "cache-v-newer")
	layoutTwoVersions := NewLayout("offline-newest-of-two")
	copyArtifactIntoLayout(layoutTwoVersions, tree, artV1, "26.1.2.1")
	copyArtifactIntoLayout(layoutTwoVersions, tree, artV2, "26.1.5.2")
	layoutTwoVersions.SetInstalled(artV1.ManifestDesc.Digest, artV2.ManifestDesc.Digest)
	cases = append(cases, offlineCase("offline-newest-of-two", "offline-newest-of-two", "26.1", true, "",
		strp(artV2.Predicate.ClickHouseVersion), strp(artV2.Predicate.Build), strp(artV2.Predicate.LibrarySHA256)))

	// --- offline-newest-build: same version, two different builds, pick
	// the higher build (compared as a fixed-width string) ------------------
	artB1 := newArtifact("26.1.9.9", "20260109.000001", "cache-b-older")
	artB2 := newArtifact("26.1.9.9", "20260109.000002", "cache-b-newer")
	layoutTwoBuilds := NewLayout("offline-newest-build")
	copyArtifactIntoLayout(layoutTwoBuilds, tree, artB1, "26.1.9.9")
	copyArtifactIntoLayout(layoutTwoBuilds, tree, artB2, "26.1.9.9")
	layoutTwoBuilds.SetInstalled(artB1.ManifestDesc.Digest, artB2.ManifestDesc.Digest)
	cases = append(cases, offlineCase("offline-newest-build", "offline-newest-build", "26.1.9.9", true, "",
		strp(artB2.Predicate.ClickHouseVersion), strp(artB2.Predicate.Build), strp(artB2.Predicate.LibrarySHA256)))

	// --- existing-install-noop: online, but the layer is already present
	// locally by digest — zero LAYER requests, though the manifest/bundle
	// may still be re-resolved over the network. --------------------------
	artNoop := newArtifact("26.1.4.4", "20260104.000004", "cache-noop")
	tagArtifact("26.1.4.4", artNoop)
	layoutNoop := NewLayout("existing-install-noop")
	copyArtifactIntoLayout(layoutNoop, tree, artNoop, "26.1.4.4")
	layoutNoop.SetInstalled(artNoop.ManifestDesc.Digest)
	noop := newCase("existing-install-noop", "cache", "file", "http")
	noop.Request.Spelling = "26.1.4.4"
	noop.Setup.Cache = "existing-install-noop"
	noop.Expect.OK = true
	noop.Expect.Version = strp(artNoop.Predicate.ClickHouseVersion)
	noop.Expect.Build = strp(artNoop.Predicate.Build)
	noop.Expect.LibrarySHA256 = strp(artNoop.Predicate.LibrarySHA256)
	noop.Expect.Requests.NoneMatching = []string{"GET .*/blobs/" + artNoop.LayerDesc.Digest}
	cases = append(cases, noop)

	// --- monotonic-warning: a HIGHER build is already installed than what
	// the live registry now offers; the existing (newer) install is kept,
	// with a warning. -------------------------------------------------------
	monoTree := NewTree("monotonic")
	artMonoLive := buildPlatformArtifact(monoTree, testKey, "linux-arm64", "26.1.3.1", "20260101.000001", "lts", ArtifactOptions{LibraryContentSeed: "mono-live-lower"})
	monoLiveIdx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(artMonoLive.ManifestDesc, "linux-arm64")}}
	monoTree.PutManifest("26.1.3.1", ociIndexMediaType, canonicalJSON(monoLiveIdx))
	artMonoInstalled := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.1.3.1", "20269912.999999", "lts", ArtifactOptions{LibraryContentSeed: "mono-installed-higher"})
	layoutMono := NewLayout("monotonic-warning")
	copyArtifactIntoLayout(layoutMono, tree, artMonoInstalled, "26.1.3.1")
	layoutMono.SetInstalled(artMonoInstalled.ManifestDesc.Digest)
	monoCase := newCase("monotonic-warning", "monotonic", "file", "http")
	monoCase.Request.Spelling = "26.1.3.1"
	monoCase.Setup.Cache = "monotonic-warning"
	monoCase.Expect.OK = true
	monoCase.Expect.Version = strp(artMonoInstalled.Predicate.ClickHouseVersion)
	monoCase.Expect.Build = strp(artMonoInstalled.Predicate.Build)
	monoCase.Expect.LibrarySHA256 = strp(artMonoInstalled.Predicate.LibrarySHA256)
	monoCase.Expect.Warnings = []string{"monotonic"}
	cases = append(cases, monoCase)
	_ = artMonoLive

	// --- system-dir-readonly: the system dir is searched after the user
	// cache and is never written to. ---------------------------------------
	artSys := newArtifact("26.1.6.6", "20260106.000006", "cache-sysdir")
	tagArtifact("c-system-dir-readonly", artSys)
	layoutSys := NewLayout("system-dir-readonly")
	copyArtifactIntoLayout(layoutSys, tree, artSys, "26.1.6.6")
	sysDirCase := newCase("system-dir-readonly", "cache", "file", "http")
	sysDirCase.Request.Spelling = "26.1.6.6"
	sysDirCase.Request.Offline = true
	sysDirCase.Setup.Cache = "empty"
	sysDirCase.Setup.SystemDirs = []string{"system-dir-readonly"}
	sysDirCase.Expect.OK = true
	sysDirCase.Expect.Version = strp(artSys.Predicate.ClickHouseVersion)
	sysDirCase.Expect.Build = strp(artSys.Predicate.Build)
	sysDirCase.Expect.LibrarySHA256 = strp(artSys.Predicate.LibrarySHA256)
	cases = append(cases, sysDirCase)

	// --- index-race-reapply: the runner's own hook races a competing
	// index.json write against this fixture's normal online resolve;
	// both entries must survive the atomic rename. -------------------------
	artRace := newArtifact("26.1.7.7", "20260107.000007", "cache-race")
	tagArtifact("26.1.7.7", artRace)
	raceCase := newCase("index-race-reapply", "cache", "file", "http")
	raceCase.Request.Spelling = "26.1.7.7"
	raceCase.Setup.Cache = "empty"
	raceCase.Setup.BeforeIndexRenameHook = strp("index-race-reapply")
	raceCase.Expect.OK = true
	raceCase.Expect.Version = strp(artRace.Predicate.ClickHouseVersion)
	raceCase.Expect.Build = strp(artRace.Predicate.Build)
	raceCase.Expect.LibrarySHA256 = strp(artRace.Predicate.LibrarySHA256)
	cases = append(cases, raceCase)

	// --- preseed-oras: layouts/oras-preseed/ is written ONLY by a real
	// `oras copy -r --platform linux/arm64 --to-oci-layout` in the
	// v1-oras-preseed job (workflow_dispatch only; plan §3.2/§4 lane 0B) —
	// never by this generator.
	//
	// Measured (three fetch lanes independently, 2026-10-02): that job had
	// not landed the layout in this tree, so the case was shipped in
	// cases.json anyway, naming a setup.cache no binding could ever
	// satisfy — every runner either 404'd/ENOENT'd on it or had to invent
	// its own special-case to avoid doing so, for a GENERATOR reason, not a
	// binding defect.
	//
	// Fixed here: gate the case's very existence on the layout's presence,
	// checked fresh on every --write/--check/--selftest
	// (layoutPresentOnDisk, main.go). Absent, it is skipped LOUDLY (stderr,
	// every run, never silent — the same discipline
	// CHTYPES_V1_CONFORMANCE unset already follows, docs/guides/fetch-v1.md
	// §10) and left out of cases.json entirely, so no binding is ever asked
	// to pass a case that cannot succeed and `parity.py`'s required-pairs
	// completeness check never has a hole to paper over. Once the real job
	// lands the directory, the very next regeneration picks the case back
	// up with no second wiring pass. checkCacheLayoutsExist (cases.go) is
	// this same invariant's second, independent check: it would catch a
	// future edit that re-adds this case unconditionally.
	if layoutPresentOnDisk("oras-preseed") {
		preseed := newCase("preseed-oras", "cache", "file")
		preseed.Request.Spelling = "26.1"
		preseed.Request.Offline = true
		preseed.Setup.Cache = "oras-preseed"
		preseed.Expect.OK = true
		preseed.Expect.Requests.Max = intp(0)
		cases = append(cases, preseed)
	} else {
		fmt.Fprintln(os.Stderr, "genfixtures: SKIPPING preseed-oras — tests/fixtures/fetch-v1/layouts/oras-preseed/ "+
			"is not on disk (written only by the workflow_dispatch-only v1-oras-preseed job); dispatch it and "+
			"commit its output, then re-run this generator to pick the case back up")
	}

	// --- installed-request-*: a build that is merely PRESENT must never
	// answer a request it does not satisfy (docs/guides/fetch-v1.md §4, §9: the
	// predicate's version equals an exact request or lies within a floating
	// one, on EVERY resolution path). The cache holds one signed build,
	// 26.8.15.10, installed and listed in index.json; each request is run
	// through ensure with `offline`, through resolve_installed (the
	// `resolve-installed-` id prefix, docs/guides/fetch-v1.md §10), and through
	// ensure with `frozen` against a lock that pins only the two matching
	// spellings. A non-matching request is MISSING (offline, resolve_installed:
	// §6, the offline-miss precedent) or PINNED (frozen: §6, no lock entry);
	// it is never the installed build. ---------------------------------------
	artInst := newArtifact("26.8.15.10", "20260815.000010", "cache-installed-request")
	layoutInst := NewLayout("installed-request")
	copyArtifactIntoLayout(layoutInst, tree, artInst, "26.8")
	layoutInst.SetInstalled(artInst.ManifestDesc.Digest)
	instLock := Lock3{Schema: 3, ABI: 1, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"26.8":       {"linux-arm64": pinFor(artInst, artInst.BundleDigest, "")},
			"26.8.15.10": {"linux-arm64": pinFor(artInst, artInst.BundleDigest, "")},
		}}
	putInputLock(fs, "installed-request", instLock)
	instRequests := []struct {
		slug, spelling string
		match          bool
	}{
		{"1-1", "1.1", false},
		{"26-9", "26.9", false},
		{"wrong-patch", "26.8.15.9", false},
		{"26-8", "26.8", true},
		{"exact", "26.8.15.10", true},
	}
	for _, r := range instRequests {
		for _, mode := range []string{"offline", "resolve", "frozen"} {
			id := "installed-request-" + r.slug + "-" + mode
			if mode == "resolve" {
				id = "resolve-installed-request-" + r.slug
			}
			c := newCase(id, "cache", "file", "http")
			c.Request.Spelling = r.spelling
			c.Setup.Cache = "installed-request"
			switch mode {
			case "offline", "resolve":
				c.Request.Offline = true
				c.Expect.Requests.Max = intp(0)
			case "frozen":
				c.Request.Frozen = true
				c.Setup.Lock = strp("installed-request")
			}
			if r.match {
				c.Expect.OK = true
				c.Expect.Version = strp(artInst.Predicate.ClickHouseVersion)
				c.Expect.Build = strp(artInst.Predicate.Build)
				c.Expect.LibrarySHA256 = strp(artInst.Predicate.LibrarySHA256)
			} else {
				c.Expect.OK = false
				if mode == "frozen" {
					c.Expect.Code = strp("CHTYPES_ARTIFACT_PINNED")
				} else {
					c.Expect.Code = strp("CHTYPES_ARTIFACT_MISSING")
				}
			}
			cases = append(cases, c)
		}
	}

	// --- label-mismatch-*: no local LABEL may stand in for the signed
	// version. The layout's index.json entry for the build is annotated with
	// the ref name "26.9" (what a real `oras copy` of that tag writes), while
	// the build's SIGNED predicate says 26.8.15.10. A request for 26.9 (the
	// label) must not resolve to it; 26.8 (the signed version) is the
	// control. Two layouts: one with the build already installed
	// (installed.json), one only pre-seeded. A path component cannot carry a
	// version at all (the unpacked directory is named by the manifest digest,
	// and verified.json is written by the binding from the signed statement,
	// never fabricated by this generator), so the index.json annotation is the
	// one local label a fixture can plant. ------------------------------------
	for _, kind := range []string{"installed", "preseeded"} {
		layoutLbl := NewLayout("label-mismatch-" + kind)
		copyArtifactIntoLayout(layoutLbl, tree, artInst, "26.9")
		if kind == "installed" {
			layoutLbl.SetInstalled(artInst.ManifestDesc.Digest)
		}
		extraLayouts = append(extraLayouts, layoutLbl)
		for _, r := range []struct {
			slug, spelling string
			match          bool
		}{{"label", "26.9", false}, {"signed", "26.8", true}} {
			for _, mode := range []string{"offline", "resolve"} {
				id := "label-mismatch-" + kind + "-" + r.slug + "-" + mode
				if mode == "resolve" {
					id = "resolve-installed-label-mismatch-" + kind + "-" + r.slug
				}
				c := newCase(id, "cache", "file", "http")
				c.Request.Spelling = r.spelling
				c.Request.Offline = true
				c.Setup.Cache = "label-mismatch-" + kind
				c.Expect.Requests.Max = intp(0)
				if r.match {
					c.Expect.OK = true
					c.Expect.Version = strp(artInst.Predicate.ClickHouseVersion)
					c.Expect.Build = strp(artInst.Predicate.Build)
					c.Expect.LibrarySHA256 = strp(artInst.Predicate.LibrarySHA256)
				} else {
					c.Expect.OK = false
					c.Expect.Code = strp("CHTYPES_ARTIFACT_MISSING")
				}
				cases = append(cases, c)
			}
		}
	}

	flushTrees(fs, tree, monoTree)
	for _, l := range extraLayouts {
		l.Flush(fs)
	}
	for _, l := range []*Layout{layoutInst, layoutHit, layoutMiss, layoutTwoVersions, layoutTwoBuilds, layoutNoop, layoutMono, layoutSys} {
		l.Flush(fs)
	}
	return cases
}
