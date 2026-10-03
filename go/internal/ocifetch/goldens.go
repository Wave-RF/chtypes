package ocifetch

// goldens.go — goldens revision selection (docs/guides/fetch-v1.md §9).
//
// The registry is append-only, so a corrected goldens set for the same build
// is published as a SECOND goldens referrer of the same platform manifest;
// the first can never be deleted. The artifact producer therefore puts an
// integer `revision` in the goldens predicate and never reuses one for a
// different document. FetchSigned, called with the goldens predicate type and
// a platform manifest digest, verifies EVERY goldens referrer and returns the
// one with the highest revision. The rule lives here, in the fetch layer, so
// every caller gets it; no test runner repeats it.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
)

// maxGoldensRevision is the largest revision every binding accepts, the
// largest integer a JavaScript number holds exactly (2^53 - 1).
const maxGoldensRevision = 1<<53 - 1

var canonicalRevisionRE = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)

// goldensCandidate is one goldens referrer whose own signature verified.
type goldensCandidate struct {
	manifest       Digest // the goldens referrer manifest
	blob           Digest // its layer: the signed goldens document
	revision       int64
	body           []byte
	predicate      map[string]any
	keyID          string
	bundle         Digest
	bundleManifest Digest
}

// statementRevision reads the integer `revision` from a verified statement's
// predicate, straight from the signed bytes: it must be a JSON integer
// written without a fraction, an exponent, a sign or a leading zero. A
// missing key, a boolean, a string, null, a float such as 1.0 and a number
// beyond maxGoldensRevision are all refused (the caller reports
// CHTYPES_ARTIFACT_CORRUPT).
func statementRevision(payload []byte) (int64, error) {
	var doc struct {
		Predicate map[string]json.RawMessage `json:"predicate"`
	}
	if err := json.Unmarshal(payload, &doc); err != nil {
		return 0, fmt.Errorf("statement is not valid JSON: %w", err)
	}
	raw, ok := doc.Predicate["revision"]
	if !ok {
		return 0, fmt.Errorf(`predicate carries no "revision"`)
	}
	text := string(bytes.TrimSpace(raw))
	if !canonicalRevisionRE.MatchString(text) {
		return 0, fmt.Errorf(`predicate "revision" is %s, not a non-negative JSON integer`, text)
	}
	n, err := strconv.ParseInt(text, 10, 64)
	if err != nil || n > maxGoldensRevision {
		return 0, fmt.Errorf(`predicate "revision" %s is larger than %d`, text, int64(maxGoldensRevision))
	}
	return n, nil
}

// selectGoldens picks the verified candidate with the highest revision. Two
// or more candidates at that revision with DIFFERENT blob digests are a tie,
// refused as CHTYPES_ARTIFACT_CORRUPT naming both digests and the revision;
// the same blob digest under the same revision (two bundles, say) is one
// document, not a tie. The earliest-listed candidate of the winning document
// is returned.
func selectGoldens(cands []goldensCandidate, ref string) (*goldensCandidate, error) {
	if len(cands) == 0 {
		return nil, fmt.Errorf("no goldens candidates")
	}
	best := &cands[0]
	for i := 1; i < len(cands); i++ {
		if cands[i].revision > best.revision {
			best = &cands[i]
		}
	}
	for i := range cands {
		if cands[i].revision == best.revision && cands[i].blob != best.blob {
			return nil, newError(CodeArtifactCorrupt, ref, "", "", nil,
				"goldens revision %d is carried by two different documents, %s and %s", best.revision, best.blob, cands[i].blob)
		}
	}
	return best, nil
}

// fetchGoldens is FetchSigned for the goldens predicate type: ref is a
// PLATFORM manifest digest, and the answer is its highest-revision verified
// goldens referrer.
func (s *session) fetchGoldens(ctx context.Context, ro resolvedOptions, bases []string, ref string) (*SignedArtifact, error) {
	if !Digest(ref).Valid() {
		return nil, newError(CodeArtifactCorrupt, ref, "", "", nil, "a goldens fetch names a platform manifest by digest, not %q", ref)
	}
	listed := s.findReferrers(ctx, bases, Digest(ref), GoldensArtifactType)
	if len(listed) == 0 {
		return nil, newError(CodeArtifactUnpublished, ref, "", "", nil, "no goldens referrer of %s", ref)
	}

	var verified []goldensCandidate
	var firstFailure error
	note := func(err error) {
		if firstFailure == nil {
			firstFailure = err
		}
	}
	// What an unsigned run (CHTYPES_ALLOW_UNSIGNED) can still return: only an
	// unambiguous single document.
	type unsignedDoc struct {
		manifest, blob Digest
		body           []byte
	}
	var unsigned []unsignedDoc
	seen := map[Digest]bool{}

	for _, cand := range listed {
		if seen[cand.Digest] {
			continue
		}
		seen[cand.Digest] = true
		body, blobDigest, manifestDigest, ferr := s.fetchReferrerContentMax(ctx, bases, cand, 0)
		if ferr != nil {
			note(ferr)
			continue
		}
		unsigned = append(unsigned, unsignedDoc{manifestDigest, blobDigest, body})

		var found *goldensCandidate
		for _, sigDesc := range s.findReferrers(ctx, bases, manifestDigest, MediaTypeBundle) {
			bundleBody, bundleDigest, bundleManifestDigest, berr := s.fetchReferrerContent(ctx, bases, sigDesc)
			if berr != nil {
				continue
			}
			vr, verr := verifyBundle(bundleBody, ro.trustedKeys)
			if verr != nil {
				note(newError(CodeArtifactCorrupt, ref, "", "", verr, "signed statement is corrupt: %v", verr))
				continue
			}
			if !vr.verified {
				note(newError(CodeArtifactUntrusted, ref, "", "", nil, "no goldens signature of %s verifies under a trusted key", manifestDigest))
				continue
			}
			if cerr := validateStatement(vr.statement, PredicateTypeGoldens, blobDigest, nil, ""); cerr != nil {
				note(newError(CodeArtifactCorrupt, ref, "", "", cerr, "%v", cerr))
				continue
			}
			rev, rerr := statementRevision(vr.payload)
			if rerr != nil {
				// A verified candidate whose revision is unusable is never
				// skipped: it could be the newest document, and quietly
				// choosing an older set would be the stale pick this rule
				// exists to prevent.
				return nil, newError(CodeArtifactCorrupt, ref, "", "", rerr, "goldens %s: %v", manifestDigest, rerr)
			}
			found = &goldensCandidate{
				manifest: manifestDigest, blob: blobDigest, revision: rev, body: body,
				predicate: vr.statement.Predicate, keyID: vr.keyID, bundle: bundleDigest, bundleManifest: bundleManifestDigest,
			}
			break
		}
		if found != nil {
			verified = append(verified, *found)
		}
	}

	if len(verified) > 0 {
		best, err := selectGoldens(verified, ref)
		if err != nil {
			return nil, err
		}
		return &SignedArtifact{
			Bytes:     best.body,
			Statement: best.predicate,
			Digests:   Digests{Manifest: best.manifest, Layer: best.blob, Bundle: best.bundle, BundleManifest: best.bundleManifest},
			SignedBy:  best.keyID,
		}, nil
	}
	if ro.allowUnsigned && len(unsigned) == 1 {
		return &SignedArtifact{
			Bytes:    unsigned[0].body,
			Digests:  Digests{Manifest: unsigned[0].manifest, Layer: unsigned[0].blob},
			Warnings: []string{"chtypes: no signature found; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned"},
		}, nil
	}
	if ro.allowUnsigned && len(unsigned) > 1 {
		return nil, newError(CodeArtifactCorrupt, ref, "", "", nil, "%d unsigned goldens documents of %s: no signature to read a revision from, so none can be chosen", len(unsigned), ref)
	}
	if firstFailure != nil {
		return nil, firstFailure
	}
	return nil, newError(CodeArtifactUntrusted, ref, "", "", nil, "no goldens referrer of %s verifies under a trusted key", ref)
}
