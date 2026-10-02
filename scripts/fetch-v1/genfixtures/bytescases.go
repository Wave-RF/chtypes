package main

// bytescases.go — §5's "Bytes" case group: verify-then-decode ordering,
// the zstd window cap, the tar entry-type refusals, and the library
// re-hash (docs/guides/fetch-v1.md §5). One tree, "bytes", one tag per
// scenario — each tag's manifest and signature are entirely ordinary; only
// the LAYER bytes (or, for tampered-manifest, the manifest bytes
// themselves) deviate from what the signature vouches for.

func buildBytesCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("bytes")

	// A normal, fully-signed single-platform artifact under a given tag —
	// every bytes case starts here and then corrupts something AFTER
	// signing, never before (the signature must be the honest one; what is
	// served must be the dishonest bytes).
	signedArtifact := func(seed string) PlatformArtifact {
		art := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.2.1."+seed, "20260201.0000"+seed+"0", "lts",
			ArtifactOptions{LibraryContentSeed: "bytes-" + seed})
		return art
	}
	indexFor := func(art PlatformArtifact) []byte {
		idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{
			platformDescriptor(art.ManifestDesc, art.PlatformKey),
		}}
		return canonicalJSON(idx)
	}
	addCase := func(id, tag string, ok bool, code string) Case {
		c := newCase(id, "bytes", "file", "http")
		c.Request.Spelling = tag
		c.Expect.OK = ok
		if !ok {
			c.Expect.Code = strp(code)
		}
		return c
	}

	// --- tampered-layer: the SERVED layer blob's bytes do not match the
	// digest the manifest (and the signed predicate) both commit to. Must
	// be refused BEFORE decompression, from the sha256/size check alone.
	art1 := signedArtifact("1")
	tree.PutManifest("b-tampered-layer", ociManifestMediaType, indexFor(art1))
	tamperedLayer := append([]byte(nil), mustGetBlob(tree, art1.LayerDesc.Digest)...)
	tamperedLayer[len(tamperedLayer)-1] ^= 0xFF
	tree.PutTamperedBlobByDigest(art1.LayerDesc.Digest, tamperedLayer)
	cases = append(cases, addCase("tampered-layer", "b-tampered-layer", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// --- tampered-manifest: the SERVED manifest bytes (by digest) do not
	// match their own claimed digest — a host fault discovered the instant
	// the client hashes what it downloaded.
	art2 := signedArtifact("2")
	tree.PutManifest("b-tampered-manifest", ociManifestMediaType, indexFor(art2))
	tamperedManifest := append([]byte(nil), mustGetManifest(tree, art2.ManifestDesc.Digest)...)
	tamperedManifest = append(tamperedManifest, '\n') // still valid JSON trailing whitespace, wrong digest
	tree.PutTamperedManifestByDigest(art2.ManifestDesc.Digest, tamperedManifest)
	cases = append(cases, addCase("tampered-manifest", "b-tampered-manifest", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// --- library-hash-mismatch: the tarball is untouched and matches the
	// LAYER digest exactly; the signed predicate's library_sha256/bytes
	// describe a DIFFERENT library than what is actually packed.
	art3 := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.2.1.3", "20260201.000030", "lts",
		ArtifactOptions{
			LibraryContentSeed:             "bytes-3-actual",
			LibraryContentForPredicateOnly: fakeLibraryContent("bytes-3-predicate-claims-this-instead"),
		})
	tree.PutManifest("b-library-hash-mismatch", ociManifestMediaType, indexFor(art3))
	cases = append(cases, addCase("library-hash-mismatch", "b-library-hash-mismatch", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// --- the six bad tar-entry kinds -------------------------------------
	badTarCase := func(id, tagSuffix string, kind badTarKind) {
		libName := "libchtypes.so"
		libContent := fakeLibraryContent("bytes-tar-" + tagSuffix)
		tarBytes := buildBadTar(libName, libContent, kind)
		layerBytes := zstdEncode(tarBytes)
		layerDesc := tree.PutBlob(C.MediaTypes.Layer, layerBytes)

		predicate := Predicate{
			ABI: 1, ABIFingerprint: "sha256:" + fakeHex("abi-fp-tar-"+tagSuffix, 64),
			ClickHouseVersion: "26.2.1.9", Channel: "lts", ClickHouseMinor: "26.2",
			ClickHouseCommit: fakeHex("chc-tar-"+tagSuffix, 40),
			OS:               "linux", Arch: "arm64", Build: "20260201.0000" + tagSuffix + "0",
			CoreCommit:    fakeHex("cc-tar-"+tagSuffix, 40),
			InputsSHA256:  fakeHex("inputs-tar-"+tagSuffix, 64),
			Library:       libName,
			LibrarySHA256: sha256Hex(libContent),
			LibraryBytes:  int64(len(libContent)),
			GlibcFloor:    "2.17",
		}
		configBytes := canonicalJSON(predicate)
		configDesc := tree.PutBlob(C.MediaTypes.Config, configBytes)
		manifest := ImageManifest{SchemaVersion: 2, MediaType: ociManifestMediaType, ArtifactType: C.MediaTypes.ArtifactType,
			Config: configDesc, Layers: []Descriptor{layerDesc}}
		manifestDesc := tree.PutManifest("", ociManifestMediaType, canonicalJSON(manifest))
		manifestDesc.ArtifactType = C.MediaTypes.ArtifactType
		statement := buildStatement(libName+"-linux-arm64.tar.zst", digestHexPart(layerDesc.Digest), C.PredicateTypes.Artifact, predicate)
		attachSignatureReferrer(tree, manifestDesc, signBundle(testKey.KeyID, testKey.Private, statement))

		idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(manifestDesc, "linux-arm64")}}
		tag := "b-" + tagSuffix
		tree.PutManifest(tag, ociIndexMediaType, canonicalJSON(idx))
		cases = append(cases, addCase(id, tag, false, "CHTYPES_ARTIFACT_CORRUPT"))
	}
	badTarCase("tar-symlink", "symlink", badTarSymlink)
	badTarCase("tar-hardlink", "hardlink", badTarHardlink)
	badTarCase("tar-device", "device", badTarDevice)
	badTarCase("tar-abs-path", "abspath", badTarAbsPath)
	badTarCase("tar-dotdot", "dotdot", badTarDotDot)
	badTarCase("tar-duplicate-entry", "dupentry", badTarDuplicateEntry)

	// --- zstd-multiframe: two concatenated zstd frames decode to the same
	// valid tar stream a single-frame encoding would have produced.
	art4 := buildPlatformArtifactWithTarMode(tree, "26.2.1.9", "20260201.000090", "mf", "multiframe")
	tree.PutManifest("b-zstd-multiframe", ociManifestMediaType, indexFor(art4))
	cases = append(cases, addCase("zstd-multiframe", "b-zstd-multiframe", true, ""))

	// --- zstd-window-too-large: the frame header alone names a window
	// above limits.zstd_window_log_max; refused before the decoder commits
	// to allocating it.
	art5 := buildPlatformArtifactWithTarMode(tree, "26.2.1.10", "20260201.0000a0", "ow", "oversizewindow")
	tree.PutManifest("b-zstd-window-too-large", ociManifestMediaType, indexFor(art5))
	cases = append(cases, addCase("zstd-window-too-large", "b-zstd-window-too-large", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// --- unpacked-over-cap: a highly compressible stream whose DECOMPRESSED
	// size is one byte over limits.max_unpacked_bytes. The compressed blob
	// this generator commits is a few hundred bytes; the cap exists
	// precisely so a client never has to materialize the other end of that
	// ratio to find out it should refuse.
	bombSeed := "bomb"
	bombLibName := "libchtypes.so"
	bombTarZstd := buildZipBombTarZstd(bombLibName, C.Limits.MaxUnpackedBytes+1)
	bombLayerDesc := tree.PutBlob(C.MediaTypes.Layer, bombTarZstd)
	bombPredicate := Predicate{
		ABI: 1, ABIFingerprint: "sha256:" + fakeHex("abi-fp-"+bombSeed, 64),
		ClickHouseVersion: "26.2.1.11", Channel: "lts", ClickHouseMinor: "26.2",
		ClickHouseCommit: fakeHex("chc-"+bombSeed, 40),
		OS:               "linux", Arch: "arm64", Build: "20260201.000011",
		CoreCommit:    fakeHex("cc-"+bombSeed, 40),
		InputsSHA256:  fakeHex("inputs-"+bombSeed, 64),
		Library:       bombLibName,
		LibrarySHA256: fakeHex("bomb-library-sha256-is-never-reached", 64),
		LibraryBytes:  C.Limits.MaxUnpackedBytes + 1,
		GlibcFloor:    "2.17",
	}
	bombConfigDesc := tree.PutBlob(C.MediaTypes.Config, canonicalJSON(bombPredicate))
	bombManifest := ImageManifest{SchemaVersion: 2, MediaType: ociManifestMediaType, ArtifactType: C.MediaTypes.ArtifactType,
		Config: bombConfigDesc, Layers: []Descriptor{bombLayerDesc}}
	bombManifestDesc := tree.PutManifest("", ociManifestMediaType, canonicalJSON(bombManifest))
	bombManifestDesc.ArtifactType = C.MediaTypes.ArtifactType
	bombStatement := buildStatement(bombLibName+"-linux-arm64.tar.zst", digestHexPart(bombLayerDesc.Digest), C.PredicateTypes.Artifact, bombPredicate)
	attachSignatureReferrer(tree, bombManifestDesc, signBundle(testKey.KeyID, testKey.Private, bombStatement))
	bombIndex := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(bombManifestDesc, "linux-arm64")}}
	tree.PutManifest("b-unpacked-over-cap", ociIndexMediaType, canonicalJSON(bombIndex))
	cases = append(cases, addCase("unpacked-over-cap", "b-unpacked-over-cap", false, "CHTYPES_ARTIFACT_CORRUPT"))

	flushTrees(fs, tree)
	return cases
}

// buildPlatformArtifactWithTarMode is buildPlatformArtifact specialized for
// the two zstd-shape cases, which need a full signed single-platform
// artifact with ArtifactOptions.TarMode set.
func buildPlatformArtifactWithTarMode(tree *Tree, version, build, seed, tarMode string) PlatformArtifact {
	return buildPlatformArtifact(tree, testKey, "linux-arm64", version, build, "lts",
		ArtifactOptions{LibraryContentSeed: "bytes-" + seed, TarMode: tarMode})
}

func mustGetBlob(tree *Tree, digest string) []byte {
	b, ok := tree.blobs[digest]
	if !ok {
		panic("genfixtures: no blob " + digest + " in tree " + tree.Name)
	}
	return b
}

func mustGetManifest(tree *Tree, digest string) []byte {
	b, ok := tree.manifestByDigest[digest]
	if !ok {
		panic("genfixtures: no manifest " + digest + " in tree " + tree.Name)
	}
	return b
}
