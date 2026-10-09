package main

// resolve.go — §3.2/§3.3's "Resolve" case group: picking a platform
// manifest out of a multi-platform index for a floating or exact version
// request (docs/guides/fetch-v1.md §3).

func buildResolveCases(fs *FileSet) []Case {
	var cases []Case
	basic := NewTree("basic")

	// --- line-ok / patch-float-ok / exact-ok: one index, three tags -------
	//
	// The 26.8 line's newest (and only) published patch is 26.8.15.10,
	// built 20261001.183455 — layout-v2 spec §4.1's own worked example, so
	// a reader cross-checking a fixture against the spec sees the same
	// numbers. All three tags point at byte-identical index content.
	art826, art826IndexDesc := buildFullIndexWithDesc(basic, "26.8.15.10", "20261001.183455", "lts", []string{"26.8", "26.8.15", "26.8.15.10"})

	lineOK := newCase("line-ok", "basic", "file", "http", "registry")
	lineOK.Request.Spelling = "26.8"
	lineOK.Expect.Version = strp("26.8.15.10")
	lineOK.Expect.Build = strp("20261001.183455")
	lineOK.Expect.Manifest = strp(art826["linux-arm64"].ManifestDesc.Digest)
	lineOK.Expect.LibrarySHA256 = strp(art826["linux-arm64"].Predicate.LibrarySHA256)
	cases = append(cases, lineOK)

	patchFloatOK := newCase("patch-float-ok", "basic", "file", "http")
	patchFloatOK.Request.Spelling = "26.8.15"
	patchFloatOK.Expect.Version = strp("26.8.15.10")
	patchFloatOK.Expect.Build = strp("20261001.183455")
	patchFloatOK.Expect.Manifest = strp(art826["linux-arm64"].ManifestDesc.Digest)
	patchFloatOK.Expect.LibrarySHA256 = strp(art826["linux-arm64"].Predicate.LibrarySHA256)
	cases = append(cases, patchFloatOK)

	exactOK := newCase("exact-ok", "basic", "file", "http", "registry")
	exactOK.Request.Spelling = "26.8.15.10"
	exactOK.Expect.Version = strp("26.8.15.10")
	exactOK.Expect.Build = strp("20261001.183455")
	exactOK.Expect.Manifest = strp(art826["linux-arm64"].ManifestDesc.Digest)
	exactOK.Expect.LibrarySHA256 = strp(art826["linux-arm64"].Predicate.LibrarySHA256)
	cases = append(cases, exactOK)

	// --- exact-older-version: an exact tag is never shadowed by a line's
	// current floating tag -----------------------------------------------
	buildFullIndex(basic, "26.7.15.5", "20260915.093000", "lts", []string{"26.7"})
	art7older := buildFullIndex(basic, "26.7.10.3", "20260801.120000", "lts", []string{"26.7.10.3"})

	exactOlder := newCase("exact-older-version", "basic", "file", "http")
	exactOlder.Request.Spelling = "26.7.10.3"
	exactOlder.Expect.Version = strp("26.7.10.3")
	exactOlder.Expect.Build = strp("20260801.120000")
	exactOlder.Expect.Manifest = strp(art7older["linux-arm64"].ManifestDesc.Digest)
	exactOlder.Expect.LibrarySHA256 = strp(art7older["linux-arm64"].Predicate.LibrarySHA256)
	cases = append(cases, exactOlder)

	// --- two-builds-newest: the registry's current build for an exact tag
	// is trusted as-is, never recomputed client-side -----------------------
	art938 := buildFullIndex(basic, "26.9.3.38", "20260920.101500", "stable", []string{"26.9.3.38"})
	twoBuilds := newCase("two-builds-newest", "basic", "file", "http")
	twoBuilds.Request.Spelling = "26.9.3.38"
	twoBuilds.Expect.Version = strp("26.9.3.38")
	twoBuilds.Expect.Build = strp("20260920.101500")
	twoBuilds.Expect.Manifest = strp(art938["linux-arm64"].ManifestDesc.Digest)
	twoBuilds.Expect.LibrarySHA256 = strp(art938["linux-arm64"].Predicate.LibrarySHA256)
	cases = append(cases, twoBuilds)

	// --- unpublished-line: no tag 26.11 anywhere --------------------------
	unpublished := newCase("unpublished-line", "basic", "file", "http")
	unpublished.Request.Spelling = "26.11"
	unpublished.Expect.OK = false
	unpublished.Expect.Code = strp("CHTYPES_ARTIFACT_UNPUBLISHED")
	cases = append(cases, unpublished)

	// --- unpublished-empty-repository / list-tags-empty-repository: a
	// repository that exists and holds nothing. tags/list answers 200 with an
	// empty list and every manifest 404s (the production generation-2
	// repository reads exactly so before its first publish). A fetch is the
	// contract's "not published" outcome, CHTYPES_ARTIFACT_UNPUBLISHED, never
	// a transport or internal error; the listing is empty, not an error. ----
	emptyTree := NewTree("empty-repository")
	emptyTree.EmptyList = true
	flushTrees(fs, emptyTree)
	emptyRepo := newCase("unpublished-empty-repository", "empty-repository", "file", "http")
	emptyRepo.Request.Spelling = "26.9"
	emptyRepo.Expect.OK = false
	emptyRepo.Expect.Code = strp("CHTYPES_ARTIFACT_UNPUBLISHED")
	cases = append(cases, emptyRepo)
	emptyListing := newCase("list-tags-empty-repository", "empty-repository", "file", "http")
	emptyListing.Expect.Tags = []string{}
	cases = append(cases, emptyListing)

	// --- missing-platform: the index exists but omits darwin-arm64 -------
	missingPlatformArt := buildIndexForPlatforms(basic, "26.6.1.1", "20260601.000000", "lts", "26.6",
		[]string{"linux-amd64", "linux-arm64"})
	_ = missingPlatformArt
	missingPlatform := newCase("missing-platform", "basic", "file", "http")
	missingPlatform.Request.Spelling = "26.6"
	missingPlatform.Request.Platform = "darwin-arm64"
	missingPlatform.Expect.OK = false
	missingPlatform.Expect.Code = strp("CHTYPES_ARTIFACT_UNPUBLISHED")
	cases = append(cases, missingPlatform)

	// --- index-duplicate-platform: two manifests both claim linux-arm64 --
	dupTree := NewTree("resolve-duplicate-platform")
	artA := buildPlatformArtifact(dupTree, testKey, "linux-arm64", "26.5.1.1", "20260501.000000", "lts", ArtifactOptions{LibraryContentSeed: "dup-a"})
	artB := buildPlatformArtifact(dupTree, testKey, "linux-arm64", "26.5.1.1", "20260501.000000", "lts", ArtifactOptions{LibraryContentSeed: "dup-b"})
	dupIndex := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{
		platformDescriptor(artA.ManifestDesc, "linux-arm64"),
		platformDescriptor(artB.ManifestDesc, "linux-arm64"),
	}}
	dupTree.PutManifest("26.5", ociIndexMediaType, canonicalJSON(dupIndex))
	dupCase := newCase("index-duplicate-platform", "resolve-duplicate-platform", "file", "http")
	dupCase.Request.Spelling = "26.5"
	dupCase.Expect.OK = false
	dupCase.Expect.Code = strp("CHTYPES_ARTIFACT_CORRUPT")
	cases = append(cases, dupCase)

	// --- unknown-manifest-mediatype ---------------------------------------
	unkTree := NewTree("resolve-unknown-mediatype")
	badManifestContent := []byte(`{"not":"a recognized manifest shape"}` + "\n")
	badDesc := descriptorFor("application/vnd.example.unknown+json", badManifestContent)
	unkTree.manifestByDigest[badDesc.Digest] = badManifestContent
	unkIndex := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{
		{MediaType: badDesc.MediaType, Digest: badDesc.Digest, Size: badDesc.Size, Platform: &Platform{OS: "linux", Architecture: "arm64"}},
	}}
	unkTree.PutManifest("26.4", ociIndexMediaType, canonicalJSON(unkIndex))
	unkCase := newCase("unknown-manifest-mediatype", "resolve-unknown-mediatype", "file", "http")
	unkCase.Request.Spelling = "26.4"
	unkCase.Expect.OK = false
	unkCase.Expect.Code = strp("CHTYPES_SOURCE_INCOMPATIBLE")
	cases = append(cases, unkCase)

	// --- oversize-manifest: padded past limits.manifest_bytes -------------
	oversizeTree := NewTree("resolve-oversize-manifest")
	oversizeArt := buildPlatformArtifact(oversizeTree, testKey, "linux-arm64", "26.3.1.1", "20260301.000000", "lts", ArtifactOptions{})
	paddedManifest := ImageManifest{
		SchemaVersion: 2,
		MediaType:     ociManifestMediaType,
		ArtifactType:  C.MediaTypes.ArtifactType,
		Config:        oversizeArt.ConfigDesc,
		Layers:        []Descriptor{oversizeArt.LayerDesc},
		Annotations:   map[string]string{"x-padding": paddingString(int(C.Limits.ManifestBytes) + 4096)},
	}
	paddedBytes := canonicalJSON(paddedManifest)
	paddedDesc := oversizeTree.PutManifest("", ociManifestMediaType, paddedBytes)
	oversizeIndex := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{
		platformDescriptor(paddedDesc, "linux-arm64"),
	}}
	oversizeTree.PutManifest("26.3", ociIndexMediaType, canonicalJSON(oversizeIndex))
	oversizeCase := newCase("oversize-manifest", "resolve-oversize-manifest", "file", "http")
	oversizeCase.Request.Spelling = "26.3"
	oversizeCase.Expect.OK = false
	oversizeCase.Expect.Code = strp("CHTYPES_ARTIFACT_CORRUPT")
	cases = append(cases, oversizeCase)

	// --- spelling-refused: checked before any network call ----------------
	spellRefused := newCase("spelling-refused", "basic", "file")
	spellRefused.Request.Spelling = "26.8.15.10-lts"
	spellRefused.Expect.OK = false
	spellRefused.Expect.Requests.Max = intp(0)
	cases = append(cases, spellRefused)

	spellRefusedV := newCase("spelling-refused-v-prefix", "basic", "file")
	spellRefusedV.Request.Spelling = "v26.8"
	spellRefusedV.Expect.OK = false
	spellRefusedV.Expect.Requests.Max = intp(0)
	cases = append(cases, spellRefusedV)

	// --- layouts/basic: an OCI image layout of the SAME 26.8.15.10 index
	// line-ok/exact-ok use, for the v1-network job to push into registry:2
	// with `oras copy -r --from-oci-layout` (plan §3.2). The ROOT entry is
	// the multi-platform INDEX itself (every platform fans out from one
	// tag), matching what a real push publishes — `oras copy --platform
	// <p> --to-oci-layout` (one platform, no index; used by every OTHER
	// layout in this package) is a different shape and was the wrong one
	// here (measured locally: oras resolves a layout root ambiguously
	// across identically-tagged single-platform entries, picking just
	// one). Confirmed against a real `oras cp -r --from-oci-layout
	// --to-oci-layout` round-trip before this file was committed. --------
	basicLayout := NewLayout("basic")
	copyIndexIntoLayout(basicLayout, basic, art826IndexDesc, art826, "26.8.15.10")
	basicLayout.Flush(fs)

	flushTrees(fs, basic, dupTree, unkTree, oversizeTree)
	return cases
}

// flushTrees is a small variadic convenience so category files can flush
// every tree they built in one line.
func flushTrees(fs *FileSet, trees ...*Tree) {
	for _, t := range trees {
		t.Flush(fs)
	}
}

func platformDescriptor(d Descriptor, platformKey string) Descriptor {
	p := C.platform(platformKey)
	d.Platform = &Platform{OS: p.OS, Architecture: p.Architecture}
	return d
}

func paddingString(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = 'p'
	}
	return string(b)
}

// buildFullIndex builds one platform manifest per constants.json platform
// (the common case: every case that is not specifically about a missing
// platform), wires them into one index, and registers that index under
// every tag given. Returns the per-platform artifacts for the caller to
// read expect.* values off of.
func buildFullIndex(t *Tree, version, build, channel string, tags []string) map[string]PlatformArtifact {
	arts, _ := buildIndexForPlatformsMultiWithDesc(t, version, build, channel, tags, C.platformKeys())
	return arts
}

// buildFullIndexWithDesc is buildFullIndex plus the index's OWN
// descriptor — needed only by layouts/basic/ (below), which copies the
// whole multi-platform index into an OCI layout rather than one
// platform's manifest at a time.
func buildFullIndexWithDesc(t *Tree, version, build, channel string, tags []string) (map[string]PlatformArtifact, Descriptor) {
	return buildIndexForPlatformsMultiWithDesc(t, version, build, channel, tags, C.platformKeys())
}

func buildIndexForPlatforms(t *Tree, version, build, channel, tag string, platformKeys []string) map[string]PlatformArtifact {
	arts, _ := buildIndexForPlatformsMultiWithDesc(t, version, build, channel, []string{tag}, platformKeys)
	return arts
}

func buildIndexForPlatformsMultiWithDesc(t *Tree, version, build, channel string, tags []string, platformKeys []string) (map[string]PlatformArtifact, Descriptor) {
	arts := map[string]PlatformArtifact{}
	var descs []Descriptor
	for _, pk := range platformKeys {
		art := buildPlatformArtifact(t, testKey, pk, version, build, channel, ArtifactOptions{})
		arts[pk] = art
		descs = append(descs, platformDescriptor(art.ManifestDesc, pk))
	}
	index := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: descs}
	content := canonicalJSON(index)
	indexDesc := t.PutManifest("", ociIndexMediaType, content) // always stored by digest
	for _, tag := range tags {
		t.PutManifest(tag, ociIndexMediaType, content)
	}
	return arts, indexDesc
}
