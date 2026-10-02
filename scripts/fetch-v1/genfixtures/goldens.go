package main

// goldens.go — building a goldens referrer (D7: one per platform manifest,
// artifactType media_types.goldens_artifact_type, predicateType
// predicate_types.goldens), signed exactly like any other artifact. Shared
// by trust.go (referrers-goldens-alongside-signature) and genericcases.go
// (goldens-artifact-ok, goldens-predicate-type-wrong), which exercise it
// from two different angles: "does verifying the SIGNATURE referrer still
// work when a goldens referrer sits beside it" versus "does the generic
// fetch_signed entry point itself verify a goldens artifact correctly".

// attachGoldens builds a small goldens content blob, wraps it as a
// referrer of subjectManifestDesc, signs IT (with its own
// signature-bundle referrer, predicateType statementPredicateType), and
// returns the goldens artifact's own manifest descriptor.
func attachGoldens(tree *Tree, subjectManifestDesc Descriptor, seed, statementPredicateType string) Descriptor {
	content := []byte(`{"schema":1,"cases":[],"seed":"` + seed + `"}` + "\n")
	contentDesc := tree.PutBlob("application/json", content)
	goldensManifestDesc := buildReferrerManifest(tree, subjectManifestDesc, C.MediaTypes.GoldensArtifactType, contentDesc)
	tree.AddReferrer(subjectManifestDesc.Digest, goldensManifestDesc)

	statement := buildStatement("goldens-"+seed+".json", digestHexPart(contentDesc.Digest), statementPredicateType,
		map[string]any{"schema": 1, "seed": seed})
	bundle := signBundle(testKey.KeyID, testKey.Private, statement)
	attachSignatureReferrer(tree, goldensManifestDesc, bundle)
	return goldensManifestDesc
}
