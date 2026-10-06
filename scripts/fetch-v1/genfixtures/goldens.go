package main

import "encoding/json"

// goldens.go — building a goldens referrer (D7: one per platform manifest,
// artifactType media_types.goldens_artifact_type, predicateType
// predicate_types.goldens), signed exactly like any other artifact. The
// artifact producer's model, which this mirrors: a goldens manifest G is a
// one-layer manifest whose `subject` is the platform manifest, and G has its
// OWN signature referrer — a Sigstore bundle whose DSSE statement subject is
// the goldens BLOB (G's layer), predicateType predicate_types.goldens, and
// whose predicate carries an integer `revision` (the registry is append-only,
// so a corrected set is a second goldens referrer of the same platform
// manifest and the client picks the highest revision; docs/guides/fetch-v1.md
// §9). Shared by trust.go (referrers-goldens-alongside-signature) and
// genericcases.go, which exercise it from two angles: "does verifying the
// SIGNATURE referrer still work when a goldens referrer sits beside it"
// versus "does the generic fetch_signed entry point itself select and verify
// a goldens artifact correctly".

// GoldensSpec describes one goldens referrer to attach.
type GoldensSpec struct {
	// Seed makes the content blob (and so the document's digest) unique. Two
	// specs with the same Seed are the same document.
	Seed string
	// PredicateType is the statement's predicateType (C.PredicateTypes.Goldens
	// unless a case is probing a wrong one).
	PredicateType string
	// Revision is the predicate's `revision` value, emitted verbatim as JSON:
	// pass an int for an integer, json.RawMessage(`1.0`) for a float, a bool or a
	// string for the wrong types. Ignored when OmitRevision is set.
	Revision     any
	OmitRevision bool
	// CorruptSignature flips a bit of the bundle's signature, so it verifies
	// under no trusted key.
	CorruptSignature bool
	// SecondBundle attaches a second, differently hinted bundle of the same
	// statement (the same document signed twice, never a different one).
	SecondBundle bool
}

// attachGoldens attaches one goldens referrer at revision 1 with the standard
// predicate type; the shape every pre-revision case used.
func attachGoldens(tree *Tree, subjectManifestDesc Descriptor, seed, statementPredicateType string) Descriptor {
	return attachGoldensSpec(tree, subjectManifestDesc, GoldensSpec{Seed: seed, PredicateType: statementPredicateType, Revision: 1})
}

// attachGoldensSpec builds a small goldens content blob, wraps it as a
// referrer of subjectManifestDesc, signs IT (with its own signature-bundle
// referrer), and returns the goldens artifact's own manifest descriptor.
func attachGoldensSpec(tree *Tree, subjectManifestDesc Descriptor, spec GoldensSpec) Descriptor {
	content := []byte(`{"schema":1,"cases":[],"seed":"` + spec.Seed + `"}` + "\n")
	contentDesc := tree.PutBlob("application/json", content)
	goldensManifestDesc := buildReferrerManifest(tree, subjectManifestDesc, C.MediaTypes.GoldensArtifactType, contentDesc)
	tree.AddReferrer(subjectManifestDesc.Digest, goldensManifestDesc)

	predicate := map[string]any{"schema": 1, "seed": spec.Seed}
	if !spec.OmitRevision {
		predicate["revision"] = spec.Revision
	}
	statement := buildStatement("goldens-"+spec.Seed+".json", digestHexPart(contentDesc.Digest), spec.PredicateType, predicate)
	var bundle []byte
	if spec.CorruptSignature {
		bundle = signBundleCorruptSignature(testKey.KeyID, testKey.Private, statement)
	} else {
		bundle = signBundle(testKey.KeyID, testKey.Private, statement)
	}
	attachSignatureReferrer(tree, goldensManifestDesc, bundle)
	if spec.SecondBundle {
		// Same statement, same key, a different hint: a distinct bundle blob
		// (and referrer manifest) for the SAME document.
		attachSignatureReferrer(tree, goldensManifestDesc, signBundle("second-bundle-hint", testKey.Private, statement))
	}
	return goldensManifestDesc
}

// rawJSON is a revision value emitted byte-for-byte, for the types Go's own
// marshaller would normalize (1.0 would become 1).
func rawJSON(s string) json.RawMessage { return json.RawMessage(s) }
