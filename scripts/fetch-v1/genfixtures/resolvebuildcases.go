package main

// resolvebuildcases.go — `chtypes resolve` (docs/guides/fetch-v1.md §3, "What
// a request resolves to"; public issue #493). A case id starting
// `resolve-build-` exercises the binding's resolve instead of ensure(), and
// expect.resolutions is its answer, one row per platform the index offers, in
// the constants' platform order: the signed version and build and the platform
// manifest's digest. Every online case names each offered platform's layer in
// requests.none_matching, so a resolve that downloads one fails on the
// registry's own log; offline, the answer is the cache's, with no request.

func buildResolveBuildCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("resolve-build")

	line := buildFullIndex(tree, "26.8.15.10", "20261001.183455", "lts", []string{"26.8", "26.8.15", "26.8.15.10"})
	exact := buildFullIndex(tree, "26.8.14.2", "20260915.120000", "lts", []string{"26.8.14.2"})
	partial := buildIndexForPlatforms(tree, "26.6.1.1", "20260601.000000", "lts", "26.6", []string{"linux-amd64", "linux-arm64"})
	// A build signed with the other key, which no case trusts.
	untrusted := buildPlatformArtifact(tree, otherKey, "linux-arm64", "26.5.1.1", "20260501.000000", "lts", ArtifactOptions{LibraryContentSeed: "resolve-build-untrusted"})
	tree.PutManifest("26.5", ociIndexMediaType, artifactIndexBytes(tree, untrusted))

	// Two installed builds of 26.1 on linux-arm64, for the offline answer.
	older := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.1.2.1", "20260102.000001", "lts", ArtifactOptions{LibraryContentSeed: "resolve-build-older"})
	newer := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.1.5.2", "20260105.000002", "lts", ArtifactOptions{LibraryContentSeed: "resolve-build-newer"})
	installed := NewLayout("resolve-build-installed")
	copyArtifactIntoLayout(installed, tree, older, "26.1.2.1")
	copyArtifactIntoLayout(installed, tree, newer, "26.1.5.2")
	installed.SetInstalled(older.ManifestDesc.Digest, newer.ManifestDesc.Digest)
	installed.Flush(fs)
	flushTrees(fs, tree)

	mk := func(id, spelling string) Case {
		c := newCase(id, "resolve-build", "file", "http")
		c.Request.Spelling = spelling
		return c
	}
	refused := func(c *Case, code string) {
		c.Expect.OK = false
		c.Expect.Code = strp(code)
	}

	// 1. A line: every platform's newest build, each verified, and no layer.
	lineCase := mk("resolve-build-line", "26.8")
	lineCase.Expect.Resolutions = resolutionsOf(line)
	lineCase.Expect.Requests.NoneMatching = layerRequests(line)
	cases = append(cases, lineCase)

	// 2. An exact version an index of its own names: its build, not the line's.
	exactCase := mk("resolve-build-exact", "26.8.14.2")
	exactCase.Expect.Resolutions = resolutionsOf(exact)
	exactCase.Expect.Requests.NoneMatching = layerRequests(exact)
	cases = append(cases, exactCase)

	// 3. An index that offers two of the three platforms: two rows, and the
	// third is left out rather than refused.
	partialCase := mk("resolve-build-partial-index", "26.6")
	partialCase.Expect.Resolutions = resolutionsOf(partial)
	partialCase.Expect.Requests.NoneMatching = layerRequests(partial)
	cases = append(cases, partialCase)

	// 4. A statement no trusted key signed: refused as a fetch refuses it,
	// before any layer.
	untrustedCase := mk("resolve-build-untrusted", "26.5")
	refused(&untrustedCase, "CHTYPES_ARTIFACT_UNTRUSTED")
	untrustedCase.Expect.Requests.NoneMatching = []string{"GET .*/blobs/" + untrusted.LayerDesc.Digest}
	cases = append(cases, untrustedCase)

	// 5. A line the registry never published.
	unpublished := mk("resolve-build-unpublished", "26.11")
	refused(&unpublished, "CHTYPES_ARTIFACT_UNPUBLISHED")
	cases = append(cases, unpublished)

	// 6. Offline: the cache's answer, the newest installed build of the line,
	// with no request at all.
	offline := mk("resolve-build-offline", "26.1")
	offline.Request.Offline = true
	offline.Setup.Cache = "resolve-build-installed"
	offline.Expect.Resolutions = []Resolution{resolutionOf(newer)}
	offline.Expect.Requests.Max = intp(0)
	cases = append(cases, offline)

	// 7. Offline with nothing installed: the offline miss, with no request.
	offlineMiss := mk("resolve-build-offline-miss", "26.1")
	offlineMiss.Request.Offline = true
	refused(&offlineMiss, "CHTYPES_ARTIFACT_MISSING")
	offlineMiss.Expect.Requests.Max = intp(0)
	cases = append(cases, offlineMiss)

	return cases
}

// resolutionOf is one platform artifact's expected row.
func resolutionOf(art PlatformArtifact) Resolution {
	return Resolution{
		Platform: art.PlatformKey,
		Version:  art.Predicate.ClickHouseVersion,
		Build:    art.Predicate.Build,
		Manifest: art.ManifestDesc.Digest,
	}
}

// resolutionsOf is an index's expected rows, in the constants' platform order.
func resolutionsOf(arts map[string]PlatformArtifact) []Resolution {
	var out []Resolution
	for _, pk := range C.platformKeys() {
		if art, ok := arts[pk]; ok {
			out = append(out, resolutionOf(art))
		}
	}
	return out
}

// layerRequests are the none_matching patterns for every layer of an index's
// platforms: a resolve never downloads one.
func layerRequests(arts map[string]PlatformArtifact) []string {
	var out []string
	for _, pk := range C.platformKeys() {
		if art, ok := arts[pk]; ok {
			out = append(out, "GET .*/blobs/"+art.LayerDesc.Digest)
		}
	}
	return out
}
