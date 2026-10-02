package main

// trust.go — §4's trust case group: one tree, "trust", with one tag per
// scenario (docs/guides/fetch-v1.md §4). Each scenario builds its own
// platform manifest so referrer sets never collide between cases.

func buildTrustCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("trust")

	mk := func(seed string) (PlatformArtifact, string) {
		// A manifest with NO referrer yet — callers attach exactly the
		// referrer(s) their scenario needs.
		art := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.8.1."+seed, "20260801.0000"+seed+"0", "lts",
			ArtifactOptions{SkipSigning: true, LibraryContentSeed: "trust-" + seed})
		return art, art.ManifestDesc.Digest
	}
	statementFor := func(art PlatformArtifact, predicateType string, predicate any) []byte {
		subjectName := libraryNameFor(art.PlatformKey) + "-" + art.PlatformKey + ".tar.zst"
		return buildStatement(subjectName, digestHexPart(art.LayerDesc.Digest), predicateType, predicate)
	}

	addCase := func(id string, art PlatformArtifact, tag string, ok bool, code string, warn ...string) Case {
		tree.PutManifest(tag, ociManifestMediaType, artifactIndexBytes(tree, art))
		c := newCase(id, "trust", "file", "http")
		c.Request.Spelling = tag
		c.Expect.OK = ok
		if ok {
			c.Expect.Version = strp(art.Predicate.ClickHouseVersion)
			c.Expect.Build = strp(art.Predicate.Build)
			c.Expect.Manifest = strp(art.ManifestDesc.Digest)
			c.Expect.LibrarySHA256 = strp(art.Predicate.LibrarySHA256)
		} else {
			c.Expect.Code = strp(code)
		}
		if len(warn) > 0 {
			c.Expect.Warnings = warn
		}
		return c
	}

	// untrusted-key: valid signature, wrong (untrusted) key.
	art1, _ := mk("1")
	st1 := statementFor(art1, C.PredicateTypes.Artifact, art1.Predicate)
	attachSignatureReferrer(tree, art1.ManifestDesc, signBundle(otherKey.KeyID, otherKey.Private, st1))
	cases = append(cases, addCase("untrusted-key", art1, "t-untrusted-key", false, "CHTYPES_ARTIFACT_UNTRUSTED"))

	// bad-signature: correct hint, corrupted signature bytes.
	art2, _ := mk("2")
	st2 := statementFor(art2, C.PredicateTypes.Artifact, art2.Predicate)
	attachSignatureReferrer(tree, art2.ManifestDesc, signBundleCorruptSignature(testKey.KeyID, testKey.Private, st2))
	cases = append(cases, addCase("bad-signature", art2, "t-bad-signature", false, "CHTYPES_ARTIFACT_UNTRUSTED"))

	// subject-not-layer: statement subject is the CONFIG digest, not the layer's.
	art3, _ := mk("3")
	subjectName3 := libraryNameFor(art3.PlatformKey) + "-" + art3.PlatformKey + ".tar.zst"
	st3 := buildStatement(subjectName3, digestHexPart(art3.ConfigDesc.Digest), C.PredicateTypes.Artifact, art3.Predicate)
	attachSignatureReferrer(tree, art3.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st3))
	cases = append(cases, addCase("subject-not-layer", art3, "t-subject-not-layer", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// predicate-wrong-platform: predicate says linux/amd64, request is linux-arm64.
	art4, _ := mk("4")
	pred4 := art4.Predicate
	pred4.OS, pred4.Arch = "linux", "amd64"
	st4 := statementFor(art4, C.PredicateTypes.Artifact, pred4)
	attachSignatureReferrer(tree, art4.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st4))
	cases = append(cases, addCase("predicate-wrong-platform", art4, "t-wrong-platform", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// predicate-wrong-version: requested under tag t-wrong-version (a 26.8
	// spelling), predicate claims 26.7.1.1 — outside that prefix.
	art5, _ := mk("5")
	pred5 := art5.Predicate
	pred5.ClickHouseVersion = "26.7.1.1"
	pred5.ClickHouseMinor = "26.7"
	st5 := statementFor(art5, C.PredicateTypes.Artifact, pred5)
	attachSignatureReferrer(tree, art5.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st5))
	wrongVersionCase := addCase("predicate-wrong-version", art5, "t-wrong-version", false, "CHTYPES_ARTIFACT_CORRUPT")
	cases = append(cases, wrongVersionCase)

	// predicate-abi-revision-not-abi: the statement carries "abi_revision"
	// (v0's key) instead of "abi" — built as a raw map, not the Predicate
	// struct, specifically so the "abi" key is genuinely absent.
	art6, _ := mk("6")
	pred6 := predicateAsMap(art6.Predicate)
	delete(pred6, "abi")
	pred6["abi_revision"] = 1
	st6 := statementFor(art6, C.PredicateTypes.Artifact, pred6)
	attachSignatureReferrer(tree, art6.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st6))
	cases = append(cases, addCase("predicate-abi-revision-not-abi", art6, "t-abi-revision", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// predicate-type-wrong: predicateType names the GOLDENS type, not artifact's.
	art7, _ := mk("7")
	st7 := statementFor(art7, C.PredicateTypes.Goldens, art7.Predicate)
	attachSignatureReferrer(tree, art7.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st7))
	cases = append(cases, addCase("predicate-type-wrong", art7, "t-predicate-type", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// statement-duplicate-key: a duplicate "_type" member in the raw JSON.
	art8, _ := mk("8")
	st8 := injectDuplicateTopLevelKey(statementFor(art8, C.PredicateTypes.Artifact, art8.Predicate))
	attachSignatureReferrer(tree, art8.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st8))
	cases = append(cases, addCase("statement-duplicate-key", art8, "t-dup-key", false, "CHTYPES_ARTIFACT_CORRUPT"))

	// multiple-referrers-one-valid: one bundle with a corrupted signature,
	// one normal valid bundle — the client accepts as soon as ANY referrer
	// of the signature artifactType verifies.
	art9, _ := mk("9")
	st9 := statementFor(art9, C.PredicateTypes.Artifact, art9.Predicate)
	attachSignatureReferrer(tree, art9.ManifestDesc, signBundleCorruptSignature(testKey.KeyID, testKey.Private, st9))
	attachSignatureReferrer(tree, art9.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st9))
	cases = append(cases, addCase("multiple-referrers-one-valid", art9, "t-multi-referrer", true, ""))

	// hint-names-unknown-key-but-valid: the hint is garbage, the signature
	// itself verifies under a trusted key the client checks regardless.
	art10, _ := mk("10")
	st10 := statementFor(art10, C.PredicateTypes.Artifact, art10.Predicate)
	attachSignatureReferrer(tree, art10.ManifestDesc, signBundle("0000000000000000", testKey.Private, st10))
	cases = append(cases, addCase("hint-names-unknown-key-but-valid", art10, "t-hint-unknown", true, ""))

	// no-bundle / no-bundle-allow-unsigned: zero referrers at all.
	art11, _ := mk("11")
	tree.PutManifest("t-no-bundle", ociManifestMediaType, artifactIndexBytes(tree, art11))
	noBundle := newCase("no-bundle", "trust", "file", "http")
	noBundle.Request.Spelling = "t-no-bundle"
	noBundle.Expect.OK = false
	noBundle.Expect.Code = strp("CHTYPES_ARTIFACT_UNTRUSTED")
	cases = append(cases, noBundle)

	art12, _ := mk("12")
	tree.PutManifest("t-no-bundle-allow-unsigned", ociManifestMediaType, artifactIndexBytes(tree, art12))
	noBundleAllow := newCase("no-bundle-allow-unsigned", "trust", "file", "http")
	noBundleAllow.Request.Spelling = "t-no-bundle-allow-unsigned"
	noBundleAllow.Request.AllowUnsigned = true
	noBundleAllow.Expect.OK = true
	noBundleAllow.Expect.Version = strp(art12.Predicate.ClickHouseVersion)
	noBundleAllow.Expect.Build = strp(art12.Predicate.Build)
	noBundleAllow.Expect.Manifest = strp(art12.ManifestDesc.Digest)
	noBundleAllow.Expect.LibrarySHA256 = strp(art12.Predicate.LibrarySHA256)
	noBundleAllow.Expect.Warnings = []string{"unsigned"}
	cases = append(cases, noBundleAllow)

	// no-referrers-api-uses-fallback-tag: referrers/<digest> 404s; the
	// fallback tag carries the same (valid) referrer list.
	art13, _ := mk("13")
	st13 := statementFor(art13, C.PredicateTypes.Artifact, art13.Predicate)
	attachSignatureReferrer(tree, art13.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st13))
	tree.SetReferrerServing(art13.ManifestDesc.Digest, referrersAbsent, fallbackNormal)
	noReferrersAPICase := addCase("no-referrers-api-uses-fallback-tag", art13, "t-fallback-only", true, "")
	// Plan §3.2's registry-transport list names this case explicitly: which
	// referrers path registry:2 actually takes is recorded `measured` by
	// the v1-network job (docs/guides/fetch-v1.md §10).
	noReferrersAPICase.Transports = append(noReferrersAPICase.Transports, "registry")
	cases = append(cases, noReferrersAPICase)

	// referrers-empty-fallback-tag-ok / referrers-empty-no-fallback: the
	// referrers API returns 200 with an empty index (R2/Workers Cache can
	// serve this for up to five minutes right after a push; coordinator's
	// addition, 2026-10-01). "empty" must not be read as "unsupported" —
	// only an outright 404 (referrersAbsent, above) licenses the fallback
	// WITHOUT also finding the real content there.
	art14, _ := mk("14")
	st14 := statementFor(art14, C.PredicateTypes.Artifact, art14.Predicate)
	attachSignatureReferrer(tree, art14.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st14))
	tree.SetReferrerServing(art14.ManifestDesc.Digest, referrersEmpty, fallbackNormal)
	cases = append(cases, addCase("referrers-empty-fallback-tag-ok", art14, "t-referrers-empty-ok", true, ""))

	art15, _ := mk("15")
	st15 := statementFor(art15, C.PredicateTypes.Artifact, art15.Predicate)
	attachSignatureReferrer(tree, art15.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st15))
	tree.SetReferrerServing(art15.ManifestDesc.Digest, referrersEmpty, fallbackAbsent)
	cases = append(cases, addCase("referrers-empty-no-fallback", art15, "t-referrers-empty-no-fallback", false, "CHTYPES_ARTIFACT_UNTRUSTED"))

	// key-rotation-two-bundles-one-valid: TWO bundles signed by DIFFERENT
	// keys — one by the key that rotated out (otherKey, here standing in
	// for a retired release key), one by the current trusted key. Distinct
	// from multiple-referrers-one-valid above (one bad signature, one
	// good, same key): this is specifically "a rotation leaves both old
	// and new bundles attached, and one of them is simply not ours to
	// trust any more", lane 0B's brief.
	art16, _ := mk("16")
	st16 := statementFor(art16, C.PredicateTypes.Artifact, art16.Predicate)
	attachSignatureReferrer(tree, art16.ManifestDesc, signBundle(otherKey.KeyID, otherKey.Private, st16))
	attachSignatureReferrer(tree, art16.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st16))
	cases = append(cases, addCase("key-rotation-two-bundles-one-valid", art16, "t-rotation-two-bundles", true, ""))

	// referrers-goldens-alongside-signature: the manifest carries BOTH its
	// signature referrer and a goldens referrer (D7: one goldens referrer
	// per platform manifest) — the client must filter by artifactType and
	// verify the signature exactly as if the goldens referrer were not
	// there. The goldens artifact itself is also signed, like any artifact
	// (docs/guides/fetch-v1.md §4/§10; see also genericcases.go's
	// goldens-artifact-ok, which exercises `fetch_signed` on an otherwise
	// identical setup without the signature referrer alongside it).
	art17, _ := mk("17")
	st17 := statementFor(art17, C.PredicateTypes.Artifact, art17.Predicate)
	attachSignatureReferrer(tree, art17.ManifestDesc, signBundle(testKey.KeyID, testKey.Private, st17))
	attachGoldens(tree, art17.ManifestDesc, "trust-goldens-17", C.PredicateTypes.Goldens)
	cases = append(cases, addCase("referrers-goldens-alongside-signature", art17, "t-goldens-alongside", true, ""))

	flushTrees(fs, tree)
	return cases
}

// predicateAsMap round-trips a Predicate through JSON into a
// map[string]any, so a case can delete/rename a key the typed struct would
// never allow (predicate-abi-revision-not-abi's whole point).
func predicateAsMap(p Predicate) map[string]any {
	b := canonicalJSON(p)
	m := map[string]any{}
	if err := unmarshalJSON(b, &m); err != nil {
		panic(err)
	}
	return m
}

// artifactIndexBytes wraps a single platform's manifest in a one-platform
// image index, for the trust cases, which test signature verification --
// resolve's own platform-selection logic is resolve.go's job, so every
// trust-case index here carries exactly the one platform it requests.
func artifactIndexBytes(tree *Tree, art PlatformArtifact) []byte {
	idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{
		platformDescriptor(art.ManifestDesc, art.PlatformKey),
	}}
	return canonicalJSON(idx)
}
