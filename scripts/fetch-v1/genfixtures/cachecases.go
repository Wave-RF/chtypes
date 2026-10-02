package main

// cachecases.go — §1/§6's "Cache" case group (docs/guides/fetch-v1.md §1,
// §6). Every layout here is a plain OCI image layout (oci-layout,
// index.json, blobs/sha256/<hex>) copied byte-for-byte from a tree this
// file also builds, so "the cache already has these bytes" is literally
// true rather than approximated. See layout.go's header comment for the
// `installed.json` convention the three cases that need an
// already-installed state (not merely present blobs) use.

func buildCacheCases(fs *FileSet) []Case {
	var cases []Case
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
	tagArtifact("c-existing-install-noop", artNoop)
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
	tagArtifact("c-index-race-reapply", artRace)
	raceCase := newCase("index-race-reapply", "cache", "file", "http")
	raceCase.Request.Spelling = "26.1.7.7"
	raceCase.Setup.Cache = "empty"
	raceCase.Setup.BeforeIndexRenameHook = strp("index-race-reapply")
	raceCase.Expect.OK = true
	raceCase.Expect.Version = strp(artRace.Predicate.ClickHouseVersion)
	raceCase.Expect.Build = strp(artRace.Predicate.Build)
	raceCase.Expect.LibrarySHA256 = strp(artRace.Predicate.LibrarySHA256)
	cases = append(cases, raceCase)

	// --- preseed-oras: PENDING. layouts/oras-preseed/ is written ONLY by a
	// real `oras copy -r --platform linux/arm64 --to-oci-layout` in the
	// v1-oras-preseed job (workflow_dispatch only; plan §3.2/§4 lane 0B).
	// This generator never writes it (lane 0B's MERGE NOTES record whether
	// that job has been dispatched yet). The case is declared now so the
	// contract is complete and no binding needs a second wiring pass once
	// the layout lands.
	preseed := newCase("preseed-oras", "cache", "file")
	preseed.Request.Spelling = "26.1"
	preseed.Request.Offline = true
	preseed.Setup.Cache = "oras-preseed"
	preseed.Expect.OK = true
	preseed.Expect.Requests.Max = intp(0)
	cases = append(cases, preseed)

	flushTrees(fs, tree, monoTree)
	for _, l := range []*Layout{layoutHit, layoutMiss, layoutTwoVersions, layoutTwoBuilds, layoutNoop, layoutMono, layoutSys} {
		l.Flush(fs)
	}
	return cases
}
