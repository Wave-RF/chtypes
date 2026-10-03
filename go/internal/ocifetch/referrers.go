package ocifetch

// referrers.go — trust and release-level discovery (docs/guides/fetch-v1.md
// §4, §9 seam's fetch_signed): the OCI referrers API, the `sha256-<hex>`
// fallback tag every binding also implements, and resolving one referrer
// descriptor down to its actual bytes (a referrer is itself a small OCI
// manifest with one layer).
//
// A manifest can carry several referrers of the signature artifactType at
// once (a key rotation adds a second bundle) and a separate goldens referrer
// with its own artifactType, so every lookup here filters by artifactType
// itself — it never assumes a referrers listing holds only one kind of
// object (§4).
//
// PM rule (2026-10-01): a referrers-API response of 200 with no entry of the
// artifactType being searched for — an empty list, or a list that holds only
// (for example) the goldens referrer — is treated exactly like the API not
// being served at all: the fallback tag is tried too, before concluding
// nothing exists. The host's referrers endpoint is served no-store, so this
// is not about a cache lagging a just-pushed manifest on our own host — it
// is for mirrors, which may serve only the fallback tag and never the
// referrers API at all (§4 of the guide: "both are implemented in every
// binding — never only one, because which a given host serves is a property
// of that host, not of this fetcher").

import (
	"context"
	"crypto/ed25519"
	"net/http"
)

// fetchAuxIndex fetches one index-shaped document (the referrers API
// response, or the fallback tag's synthetic index) from the first base that
// serves it with a 200. A 404, an unreachable base, or a body that fails to
// parse are all "this source has nothing to offer" here, never a hard
// error: by the time trust discovery runs, the subject manifest itself has
// already been fetched successfully, so the honest outcome of finding
// nothing at all is CHTYPES_ARTIFACT_UNTRUSTED from the caller, not a fetch
// error blamed on this lookup.
func (s *session) fetchAuxIndex(ctx context.Context, bases []string, suffix, accept string) (*ReferrersIndex, bool) {
	for _, base := range bases {
		u, err := buildRequestURL(base, suffix)
		if err != nil {
			continue
		}
		result, err := s.client.doGet(ctx, u, requestOptions{maxBytes: ManifestMaxBytes, accept: accept})
		if err != nil || result.status != http.StatusOK {
			continue
		}
		var idx ReferrersIndex
		if uerr := strictUnmarshal(result.body, &idx); uerr != nil {
			continue
		}
		return &idx, true
	}
	return nil, false
}

// findReferrers returns every referrer descriptor of artifactType for
// subjectDigest: the referrers API's matches, and — whenever that set comes
// up empty, for any reason — also the fallback tag's matches. Capped at
// MaxReferrers.
func (s *session) findReferrers(ctx context.Context, bases []string, subjectDigest Digest, artifactType string) []Descriptor {
	var matches []Descriptor
	// The referrers API is not a manifests/<ref> GET, so it carries no
	// Accept header requirement here.
	if idx, ok := s.fetchAuxIndex(ctx, bases, "referrers/"+string(subjectDigest), ""); ok {
		matches = appendMatchingArtifactType(matches, idx, artifactType)
	}
	if len(matches) == 0 {
		// The fallback tag IS a manifests/<ref> GET (ref being the
		// sha256-<hex> tag), so it sends manifestAccept like every other one.
		if idx, ok := s.fetchAuxIndex(ctx, bases, "manifests/"+fallbackTag(subjectDigest), manifestAccept); ok {
			matches = appendMatchingArtifactType(matches, idx, artifactType)
		}
	}
	if len(matches) > MaxReferrers {
		matches = matches[:MaxReferrers]
	}
	return matches
}

func appendMatchingArtifactType(into []Descriptor, idx *ReferrersIndex, artifactType string) []Descriptor {
	for _, m := range idx.Manifests {
		if m.ArtifactType == artifactType {
			into = append(into, m)
		}
	}
	return into
}

// fetchReferrerContent resolves one referrer descriptor (from findReferrers)
// to its content bytes. A referrer is itself a one-layer OCI manifest; this
// fetches that manifest by digest, verifies it against desc, then fetches
// and verifies its single layer. It returns the layer bytes, the layer's own
// digest ("bundle" in Resolved.Digests) and the referrer manifest's digest
// ("bundle_manifest").
func (s *session) fetchReferrerContent(ctx context.Context, bases []string, desc Descriptor) (body []byte, layerDigest, manifestDigest Digest, err error) {
	return s.fetchReferrerContentMax(ctx, bases, desc, BundleMaxBytes)
}

// fetchReferrerContentMax is fetchReferrerContent with the layer's size cap
// named by the caller: a signature bundle is small, a goldens document is
// not (maxBytes 0 means the transport's default ceiling).
func (s *session) fetchReferrerContentMax(ctx context.Context, bases []string, desc Descriptor, maxBytes int64) (body []byte, layerDigest, manifestDigest Digest, err error) {
	result, _, err := s.fetchAcrossBases(ctx, bases, "manifests/"+string(desc.Digest), notFoundRetryOnLast,
		requestOptions{maxBytes: ManifestMaxBytes, accept: manifestAccept})
	if err != nil {
		return nil, "", "", err
	}
	if verr := verifyDescriptor(result.body, desc); verr != nil {
		return nil, "", "", newError(CodeArtifactCorrupt, "", "", result.url, verr, "referrer manifest at %s: %v", result.url, verr)
	}
	var m ImageManifest
	if uerr := strictUnmarshal(result.body, &m); uerr != nil {
		return nil, "", "", newError(CodeArtifactCorrupt, "", "", result.url, uerr, "referrer manifest at %s is not valid JSON: %v", result.url, uerr)
	}
	if len(m.Layers) != 1 {
		return nil, "", "", newError(CodeArtifactCorrupt, "", "", result.url, nil,
			"referrer manifest at %s carries %d layers, expected exactly 1", result.url, len(m.Layers))
	}
	layerDesc := m.Layers[0]
	blobResult, _, err := s.fetchAcrossBases(ctx, bases, "blobs/"+string(layerDesc.Digest), notFoundRetryOnLast,
		requestOptions{maxBytes: maxBytes})
	if err != nil {
		return nil, "", "", err
	}
	if verr := verifyDescriptor(blobResult.body, layerDesc); verr != nil {
		return nil, "", "", newError(CodeArtifactCorrupt, "", "", blobResult.url, verr, "referrer blob at %s: %v", blobResult.url, verr)
	}
	return blobResult.body, layerDesc.Digest, desc.Digest, nil
}

// verifyManifestTrust finds and verifies the signature for a platform
// manifest: discovery via findReferrers, then a signature and content check
// on each candidate until one verifies (docs/guides/fetch-v1.md §4). It
// returns the accepted statement, or warnings and a nil statement when
// allowUnsigned let it through unsigned.
func (s *session) verifyManifestTrust(ctx context.Context, bases []string, manifestDigest, layerDigest Digest, platform Platform, request string, trustedKeys []ed25519.PublicKey, allowUnsigned bool) (*verifiedStatement, []string, error) {
	candidates := s.findReferrers(ctx, bases, manifestDigest, MediaTypeBundle)

	for _, cand := range candidates {
		body, bundleDigest, bundleManifestDigest, ferr := s.fetchReferrerContent(ctx, bases, cand)
		if ferr != nil {
			continue
		}
		vr, verr := verifyBundle(body, trustedKeys)
		if verr != nil {
			return nil, nil, newError(CodeArtifactCorrupt, request, platform.Key, "", verr, "signed statement is corrupt: %v", verr)
		}
		if !vr.verified {
			continue
		}
		if cerr := validateStatement(vr.statement, PredicateTypeArtifact, layerDigest, &platform, request); cerr != nil {
			return nil, nil, newError(CodeArtifactCorrupt, request, platform.Key, "", cerr, "%v", cerr)
		}
		return &verifiedStatement{
			KeyID:                vr.keyID,
			Statement:            *vr.statement,
			BundleDigest:         bundleDigest,
			BundleManifestDigest: bundleManifestDigest,
		}, nil, nil
	}
	if allowUnsigned {
		return nil, []string{"chtypes: no referrer signature verified under a trusted key; CHTYPES_ALLOW_UNSIGNED is set, continuing unsigned"}, nil
	}
	return nil, nil, newError(CodeArtifactUntrusted, request, platform.Key, "", nil,
		"no referrer signature for manifest %s verifies under a trusted key", manifestDigest)
}

// verifiedStatement is the accepted signature for one signed object.
type verifiedStatement struct {
	KeyID                string
	Statement            Statement
	BundleDigest         Digest // the layer digest of the bundle blob itself
	BundleManifestDigest Digest // the referrer's own wrapping manifest digest
}
