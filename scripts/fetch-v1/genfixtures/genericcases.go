package main

import "encoding/base64"

// genericcases.go — the generic signed-artifact fetch (`fetch_signed`),
// which goldens and the SDK's own fetch fixtures reuse (plan §1.3/§7.7;
// docs/guides/fetch-v1.md §9, §10).
//
// Convention (decided here, lane 0B, recorded in docs/guides/fetch-v1.md
// §10): a case whose id starts with "goldens-" or "fixtures-" exercises
// `fetch_signed(repository, ref, predicate_type)` instead of `ensure()`.
// `request.spelling` carries the exact ref to resolve — always a bare
// `sha256:<hex>` digest, since neither goldens nor the fixtures repository
// is ever discovered by tag — and `expect.manifest`/`expect.library_sha256`
// name the GOLDENS or FIXTURES artifact's own manifest digest and content
// hash, not a platform manifest's. For goldens, the ref is the SUBJECT
// platform manifest and fetch_signed itself discovers, verifies and selects
// among its goldens referrers (highest `revision`; §9). cases.schema.json has no separate case
// shape for this because the request/expect fields it already defines are
// sufficient; inventing a second shape would be a change to the frozen
// contract (lane 0A's, not this lane's to make).
func buildGenericCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("generic")

	// --- goldens-artifact-ok: fetch_signed finds and verifies the one
	// goldens referrer of a platform manifest. ------------------------------
	subjectOK := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.13.1.1", "20261301.000001", "lts", ArtifactOptions{LibraryContentSeed: "generic-goldens-ok"})
	goldensOK := attachGoldens(tree, subjectOK.ManifestDesc, "generic-ok", C.PredicateTypes.Goldens)
	goldensOKContent := mustGetBlob(tree, goldensOnlyLayerDigest(tree, goldensOK))
	goldensOKCase := newCase("goldens-artifact-ok", "generic", "file", "http")
	goldensOKCase.Request.Spelling = subjectOK.ManifestDesc.Digest
	goldensOKCase.Expect.OK = true
	goldensOKCase.Expect.Manifest = strp(goldensOK.Digest)
	goldensOKCase.Expect.LibrarySHA256 = strp(sha256Hex(goldensOKContent))
	cases = append(cases, goldensOKCase)

	// --- goldens-predicate-type-wrong: the goldens referrer's OWN
	// signature statement carries the ARTIFACT predicate type, not
	// goldens's — CORRUPT, the same "predicateType must match" rule §4
	// applies to every signed object, not only platform manifests. ---------
	subjectWrong := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.13.1.2", "20261301.000002", "lts", ArtifactOptions{LibraryContentSeed: "generic-goldens-wrong"})
	goldensWrong := attachGoldens(tree, subjectWrong.ManifestDesc, "generic-wrong", C.PredicateTypes.Artifact)
	goldensWrongCase := newCase("goldens-predicate-type-wrong", "generic", "file", "http")
	goldensWrongCase.Request.Spelling = subjectWrong.ManifestDesc.Digest
	goldensWrongCase.Expect.OK = false
	goldensWrongCase.Expect.Code = strp("CHTYPES_ARTIFACT_CORRUPT")
	_ = goldensWrong
	cases = append(cases, goldensWrongCase)

	// --- goldens revision selection (docs/guides/fetch-v1.md §9): the
	// registry is append-only, so a corrected goldens set is a SECOND goldens
	// referrer of the same platform manifest; fetch_signed verifies every
	// candidate, reads each verified predicate's integer `revision`, and picks
	// the highest. `request.spelling` is the platform manifest; `expect.manifest`
	// and `expect.library_sha256` name the CHOSEN document. ------------------
	revCase := func(id string, subject PlatformArtifact) Case {
		c := newCase(id, "generic", "file", "http")
		c.Request.Spelling = subject.ManifestDesc.Digest
		return c
	}
	revExpectDoc := func(c *Case, g Descriptor) {
		c.Expect.OK = true
		c.Expect.Manifest = strp(g.Digest)
		c.Expect.LibrarySHA256 = strp(sha256Hex(mustGetBlob(tree, goldensOnlyLayerDigest(tree, g))))
	}
	revExpectCorrupt := func(c *Case) {
		c.Expect.OK = false
		c.Expect.Code = strp("CHTYPES_ARTIFACT_CORRUPT")
	}
	newSubject := func(n, seed string) PlatformArtifact {
		return buildPlatformArtifact(tree, testKey, "linux-arm64", "26.13.2."+n, "20261302.00000"+n, "lts", ArtifactOptions{LibraryContentSeed: seed})
	}

	// goldens-revision-highest: revisions 1 and 2, the lower listed first.
	subjHigh := newSubject("1", "generic-rev-highest")
	gHigh1 := attachGoldensSpec(tree, subjHigh.ManifestDesc, GoldensSpec{Seed: "rev-highest-1", PredicateType: C.PredicateTypes.Goldens, Revision: 1})
	gHigh2 := attachGoldensSpec(tree, subjHigh.ManifestDesc, GoldensSpec{Seed: "rev-highest-2", PredicateType: C.PredicateTypes.Goldens, Revision: 2})
	_ = gHigh1
	highCase := revCase("goldens-revision-highest", subjHigh)
	revExpectDoc(&highCase, gHigh2)
	cases = append(cases, highCase)

	// goldens-revision-tie: two different documents, both revision 3.
	subjTie := newSubject("2", "generic-rev-tie")
	attachGoldensSpec(tree, subjTie.ManifestDesc, GoldensSpec{Seed: "rev-tie-a", PredicateType: C.PredicateTypes.Goldens, Revision: 3})
	attachGoldensSpec(tree, subjTie.ManifestDesc, GoldensSpec{Seed: "rev-tie-b", PredicateType: C.PredicateTypes.Goldens, Revision: 3})
	tieCase := revCase("goldens-revision-tie", subjTie)
	revExpectCorrupt(&tieCase)
	cases = append(cases, tieCase)

	// goldens-revision-missing: one candidate whose verified predicate has no
	// `revision`.
	subjMissing := newSubject("3", "generic-rev-missing")
	attachGoldensSpec(tree, subjMissing.ManifestDesc, GoldensSpec{Seed: "rev-missing", PredicateType: C.PredicateTypes.Goldens, OmitRevision: true})
	missingCase := revCase("goldens-revision-missing", subjMissing)
	revExpectCorrupt(&missingCase)
	cases = append(cases, missingCase)

	// goldens-revision-unverified-higher: revision 5 with a bad signature is
	// skipped, never chosen; revision 4 (valid) wins.
	subjUnv := newSubject("4", "generic-rev-unverified")
	attachGoldensSpec(tree, subjUnv.ManifestDesc, GoldensSpec{Seed: "rev-unverified-5", PredicateType: C.PredicateTypes.Goldens, Revision: 5, CorruptSignature: true})
	gUnv4 := attachGoldensSpec(tree, subjUnv.ManifestDesc, GoldensSpec{Seed: "rev-unverified-4", PredicateType: C.PredicateTypes.Goldens, Revision: 4})
	unvCase := revCase("goldens-revision-unverified-higher", subjUnv)
	revExpectDoc(&unvCase, gUnv4)
	cases = append(cases, unvCase)

	// goldens-revision-same-doc: the same blob under revision 2 with two
	// bundles is one document, not a tie.
	subjSame := newSubject("5", "generic-rev-same")
	gSame := attachGoldensSpec(tree, subjSame.ManifestDesc, GoldensSpec{Seed: "rev-same", PredicateType: C.PredicateTypes.Goldens, Revision: 2, SecondBundle: true})
	sameCase := revCase("goldens-revision-same-doc", subjSame)
	revExpectDoc(&sameCase, gSame)
	cases = append(cases, sameCase)

	// A boolean is not an integer, and neither is a float such as 1.0: each is
	// a verified candidate with an unusable revision, so CORRUPT.
	subjBool := newSubject("6", "generic-rev-bool")
	attachGoldensSpec(tree, subjBool.ManifestDesc, GoldensSpec{Seed: "rev-bool", PredicateType: C.PredicateTypes.Goldens, Revision: true})
	boolCase := revCase("goldens-revision-not-integer-bool", subjBool)
	revExpectCorrupt(&boolCase)
	cases = append(cases, boolCase)

	subjFloat := newSubject("7", "generic-rev-float")
	attachGoldensSpec(tree, subjFloat.ManifestDesc, GoldensSpec{Seed: "rev-float", PredicateType: C.PredicateTypes.Goldens, Revision: rawJSON("1.0")})
	floatCase := revCase("goldens-revision-not-integer-float", subjFloat)
	revExpectCorrupt(&floatCase)
	cases = append(cases, floatCase)

	flushTrees(fs, tree)

	// --- fixtures-digest-pin-no-tag-fallback: the SDK's own fetch-fixtures
	// repository, pinned by digest, with NO tag anywhere — proving a tag
	// fallback is never tried for it (lane 0B's brief, 2026-10-01). ---------
	fixturesTree := NewFixturesRepoTree("fixtures-digest-pin")
	fixturesContent := []byte(`{"schema":1,"note":"a stand-in sdk-fetch-fixtures artifact; see tests/fixtures/fetch-v1/README or docs/guides/fetch-v1.md §10"}` + "\n")
	fixturesContentDesc := fixturesTree.PutBlob(C.MediaTypes.Layer, fixturesContent)
	fixturesManifest := buildReferrerManifestStandalone(fixturesTree, C.MediaTypes.FixturesArtifactType, fixturesContentDesc)
	fixturesStatement := buildStatement("sdk-fetch-fixtures.tar.zst", digestHexPart(fixturesContentDesc.Digest), C.PredicateTypes.Fixtures,
		map[string]any{"schema": 1})
	attachSignatureReferrer(fixturesTree, fixturesManifest, signBundle(testKey.KeyID, testKey.Private, fixturesStatement))
	flushTrees(fs, fixturesTree)

	fixturesCase := newCase("fixtures-digest-pin-no-tag-fallback", "fixtures-digest-pin", "file", "http")
	fixturesCase.Request.Spelling = fixturesManifest.Digest
	fixturesCase.Expect.OK = true
	fixturesCase.Expect.Manifest = strp(fixturesManifest.Digest)
	fixturesCase.Expect.LibrarySHA256 = strp(sha256Hex(fixturesContent))
	fixturesCase.Expect.Requests.NoneMatching = []string{"GET .*/manifests/(?!sha256:)"} // no tag-shaped manifest GET
	cases = append(cases, fixturesCase)

	return cases
}

// goldensOnlyLayerDigest reads back the one layer digest a goldens
// referrer manifest carries, so the case can assert its content hash
// without the caller having to thread an extra return value through
// attachGoldens.
func goldensOnlyLayerDigest(tree *Tree, goldensManifestDesc Descriptor) string {
	var m ImageManifest
	if err := unmarshalJSON(mustGetManifest(tree, goldensManifestDesc.Digest), &m); err != nil {
		panic(err)
	}
	return m.Layers[0].Digest
}

// buildReferrerManifestStandalone is buildReferrerManifest without a
// subject — the fixtures-repo artifact is not itself a referrer of
// anything (it is a top-level artifact in its own repository), but it
// shares the same empty-config, one-layer manifest shape every referrer
// here uses.
func buildReferrerManifestStandalone(tree *Tree, artifactType string, layerDesc Descriptor) Descriptor {
	// The empty config must exist as a real blob, not only as an embedded
	// `data` convenience — see buildReferrerManifest's comment (measured
	// against a real registry host, 2026-10-02: BLOB_UNKNOWN otherwise).
	tree.PutBlob(C.MediaTypes.EmptyConfig, emptyConfigBlob)
	manifest := ImageManifest{
		SchemaVersion: 2,
		MediaType:     ociManifestMediaType,
		ArtifactType:  artifactType,
		Config: Descriptor{
			MediaType: C.MediaTypes.EmptyConfig,
			Digest:    digestOf(emptyConfigBlob),
			Size:      int64(len(emptyConfigBlob)),
			Data:      base64.StdEncoding.EncodeToString(emptyConfigBlob),
		},
		Layers: []Descriptor{layerDesc},
	}
	desc := tree.PutManifest("", ociManifestMediaType, canonicalJSON(manifest))
	desc.ArtifactType = artifactType
	return desc
}
