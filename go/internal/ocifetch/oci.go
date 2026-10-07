package ocifetch

// oci.go — resolve (docs/guides/fetch-v1.md §3): the OCI index and manifest
// shapes this package reads, base-URL construction per transport, the
// tag/digest 404 policies (§7, constants_gen.go's Digest404Policy and
// Tag404Policy), and the version-spelling rules (§3, constants_gen.go's
// SpellingRegex/SpellingRefuseHintRegex).

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
)

// Digest is a content digest in OCI's "sha256:<64 lowercase hex>" form.
type Digest string

var digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Valid reports whether d has the exact "sha256:<64 hex>" shape.
func (d Digest) Valid() bool { return digestPattern.MatchString(string(d)) }

// Hex returns the 64 hex characters after "sha256:", or "" if d is not Valid.
func (d Digest) Hex() string {
	if !d.Valid() {
		return ""
	}
	return string(d)[len("sha256:"):]
}

func digestOf(b []byte) Digest {
	sum := sha256.Sum256(b)
	return Digest("sha256:" + hex.EncodeToString(sum[:]))
}

// PlatformDescriptor is the OCI image-spec platform object.
type PlatformDescriptor struct {
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
}

// Descriptor is the subset of an OCI content descriptor this package reads.
type Descriptor struct {
	MediaType    string              `json:"mediaType"`
	Digest       Digest              `json:"digest"`
	Size         int64               `json:"size"`
	ArtifactType string              `json:"artifactType,omitempty"`
	Platform     *PlatformDescriptor `json:"platform,omitempty"`
	Annotations  map[string]string   `json:"annotations,omitempty"`
}

// ImageIndex is the OCI image index this package reads (one manifest per
// platform; docs/guides/fetch-v1.md §3).
type ImageIndex struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Manifests     []Descriptor `json:"manifests"`
}

// ImageManifest is the OCI image manifest this package reads: one platform's
// config and exactly one layer (the library tarball).
type ImageManifest struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	ArtifactType  string       `json:"artifactType,omitempty"`
	Config        Descriptor   `json:"config"`
	Layers        []Descriptor `json:"layers"`
	// Subject is the OCI image-spec v1.1 field a referrer manifest carries
	// back to what it refers to. The registry referrers API and the
	// fallback tag both make this redundant for a live host (they already
	// filter by subject), but a purely local OCI layout has neither, so
	// finding a referrer there means scanning every blob for one whose own
	// Subject matches (layout.go's local pre-seed verification).
	Subject *Descriptor `json:"subject,omitempty"`
}

// ReferrersIndex is the shape both the referrers API and the fallback tag
// return: an OCI image index whose entries carry their own artifactType
// (image-spec v1.1 descriptors), never assumed to hold only one kind of
// referrer (docs/guides/fetch-v1.md §4).
type ReferrersIndex = ImageIndex

func platformByKey(key string) (Platform, bool) {
	for _, p := range Platforms {
		if p.Key == key {
			return p, true
		}
	}
	return Platform{}, false
}

var (
	spellingRegex           = regexp.MustCompile(SpellingRegex)
	spellingRefuseHintRegex = regexp.MustCompile(SpellingRefuseHintRegex)
)

// SpellingError is returned when a requested version spelling fails
// spelling.regex, or matches refuse_hint_regex (a `v`-prefix or a
// `-lts`/`-stable` channel suffix) — checked before any network call
// (docs/guides/fetch-v1.md §3). It is distinct from the shared
// CHTYPES_* codes: a bad request never reaches the point of talking to a
// source.
type SpellingError struct {
	Spelling string
	Hint     string
}

func (e *SpellingError) Error() string {
	if e.Hint != "" {
		return fmt.Sprintf("chtypes: %q is not a valid v1 version spelling (%s)", e.Spelling, e.Hint)
	}
	return fmt.Sprintf("chtypes: %q is not a valid v1 version spelling", e.Spelling)
}

// validateSpelling checks spelling against spelling.regex and
// refuse_hint_regex before any network call.
func validateSpelling(spelling string) error {
	// Only the two documented mistakes (a "v" prefix, a "-lts"/"-stable"
	// channel suffix) are refused here, before any network call (§3). A
	// spelling that is simply not numeric at all is not one of those two
	// mistakes: resolve's own tag lookup is what answers for it (as
	// CHTYPES_ARTIFACT_UNPUBLISHED, if nothing matches), the same path an
	// arbitrary OCI tag takes. spelling.regex itself is a POSITIVE pattern
	// this package matches against elsewhere (versionWithin) to decide
	// whether a resolved predicate's version is even checked against the
	// request at all — not a gate on every string Ensure ever receives.
	if spellingRefuseHintRegex.MatchString(spelling) {
		return &SpellingError{Spelling: spelling,
			Hint: `v1 spells versions exactly as "SELECT version()" does, e.g. "26.8.15.10": no "v" prefix, no "-lts"/"-stable" suffix`}
	}
	return nil
}

// versionWithin reports whether resolved (always four-part, e.g.
// "26.8.15.10") lies within request: equal for a four-part request, a
// component prefix for a floating (two- or three-part) one
// (docs/guides/fetch-v1.md §4, §9).
func versionWithin(request, resolved string) bool {
	reqParts := strings.Split(request, ".")
	fullParts := strings.Split(resolved, ".")
	if len(reqParts) > len(fullParts) {
		return false
	}
	for i, p := range reqParts {
		if p != fullParts[i] {
			return false
		}
	}
	return true
}

// buildRequestURL joins base and suffix (e.g. "manifests/26.8",
// "blobs/sha256:…") the way each transport requires:
//   - file:// names the repository path directly; suffix is appended as a
//     plain path segment, and no query string is ever added.
//   - http:// and https:// insert "/v2/" right after the authority, then the
//     base's own path (the repository, possibly carrying a test routing
//     prefix), then suffix — the OCI distribution API shape.
func buildRequestURL(base, suffix string) (string, error) {
	u, err := parseBaseURL(base)
	if err != nil {
		return "", err
	}
	switch u.scheme {
	case "file":
		return strings.TrimRight(base, "/") + "/" + suffix, nil
	case "http", "https":
		full := u.scheme + "://" + u.authority + "/v2/"
		if u.path != "" {
			full += u.path + "/"
		}
		return full + suffix, nil
	default:
		return "", fmt.Errorf("base %q uses unsupported scheme %q (allowed: %v)", base, u.scheme, AllowedSchemes)
	}
}

type baseURL struct {
	scheme    string
	authority string
	path      string // trimmed of leading/trailing slashes
}

func parseBaseURL(base string) (baseURL, error) {
	idx := strings.Index(base, "://")
	if idx < 0 {
		return baseURL{}, fmt.Errorf("base %q is not a URL", base)
	}
	scheme := base[:idx]
	rest := base[idx+3:]
	if scheme == "file" {
		return baseURL{scheme: scheme, path: strings.Trim(rest, "/")}, nil
	}
	slash := strings.Index(rest, "/")
	authority := rest
	path := ""
	if slash >= 0 {
		authority = rest[:slash]
		path = strings.Trim(rest[slash:], "/")
	}
	if authority == "" {
		return baseURL{}, fmt.Errorf("base %q names no host", base)
	}
	return baseURL{scheme: scheme, authority: authority, path: path}, nil
}

func fallbackTag(d Digest) string { return "sha256-" + d.Hex() }

// notFoundPolicy picks what a 404 means for one kind of request
// (docs/guides/fetch-v1.md §7, A5).
type notFoundPolicy int

const (
	// notFoundUnpublished is the tag policy: move to the next base; if every
	// base 404s, CHTYPES_ARTIFACT_UNPUBLISHED.
	notFoundUnpublished notFoundPolicy = iota
	// notFoundRetryOnLast is the digest policy: move to the next base; on
	// the LAST base, retry within the normal budget before giving up — a
	// listed digest that still 404s is a host fault, never "unpublished".
	notFoundRetryOnLast
	// notFoundAlias is the dev channel's alias policy (§3): a 404 moves to
	// the next base, as for a tag, but only a 404 on EVERY base is "not
	// found" (errAliasNotFound, which the caller answers with the tag
	// itself). Any other failure on any base — a 5xx or a transport error
	// once the retries are spent, a 401, a 403 — is returned as that
	// failure, so a transient error can never route a request to the tag,
	// which may be a build of another fingerprint.
	notFoundAlias
)

// errAliasNotFound is notFoundAlias's "every base answered 404".
var errAliasNotFound = errors.New("chtypes: no base has the alias tag")

// fetchAcrossBases tries suffix against each base in order. A temporary
// failure (a transport error, or a retry-exhausted 5xx) moves to the next
// base; a single verification failure on content successfully fetched is
// never cause to try another base — that check happens after this function
// returns, in the caller (docs/guides/fetch-v1.md §2: "never on a
// verification failure").
func (s *session) fetchAcrossBases(ctx context.Context, bases []string, suffix string, policy notFoundPolicy, opts requestOptions) (*httpResult, string, error) {
	if len(bases) == 0 {
		return nil, "", errors.New("chtypes: no base URL is configured")
	}
	var lastErr error
	for i, base := range bases {
		u, err := buildRequestURL(base, suffix)
		if err != nil {
			return nil, "", err
		}
		isLast := i == len(bases)-1
		reqOpts := opts
		if policy == notFoundRetryOnLast && isLast {
			reqOpts.extraRetryStatuses = append(append([]int{}, opts.extraRetryStatuses...), http.StatusNotFound)
		}
		result, err := s.client.doGet(ctx, u, reqOpts)
		if err != nil {
			// doGet only returns an error once its retry schedule is
			// exhausted: a transport failure, repeated 5xx, or — on the
			// last base under the digest policy only — a 404 that never
			// clears. Any of these is cause to try the next base, if any.
			lastErr = err
			continue
		}
		if result.status == http.StatusNotFound {
			if policy == notFoundAlias {
				continue // not found here; only a 404 on every base is "not found"
			}
			if policy == notFoundUnpublished {
				lastErr = newError(CodeArtifactUnpublished, "", "", u, nil, "no tag at %s", u)
			} else {
				lastErr = newError(CodeSourceUnreachable, "", "", u, nil, "digest not found at %s", u)
			}
			continue
		}
		if result.status == http.StatusUnauthorized {
			lastErr = newError(CodeSourceUnauthorized, "", "", u, nil, "%s returned 401", u)
			continue
		}
		if result.status == http.StatusForbidden {
			lastErr = newError(CodeSourceForbidden, "", "", u, nil, "%s returned 403", u)
			continue
		}
		if result.status != http.StatusOK {
			lastErr = newError(CodeSourceUnreachable, "", "", u, nil, "%s returned unexpected status %d", u, result.status)
			continue
		}
		return result, base, nil
	}
	if policy == notFoundAlias && lastErr == nil {
		return nil, "", errAliasNotFound
	}
	return nil, "", lastErr
}

// fetchTag fetches manifests/<tag> across bases under the tag policy. Under a
// contract with an alias fingerprint (the dev channel) and for a version
// spelling, it first fetches manifests/<tag>--fp-<fingerprint> under the alias
// policy, and falls back to the tag only when every base answered the alias
// 404 (docs/guides/fetch-v1.md §3). What it returns is then trusted exactly as
// the tag's own answer would be: nothing about an alias is a credential.
func (s *session) fetchTag(ctx context.Context, bases []string, tag string, opts requestOptions) (*httpResult, string, error) {
	if alias := s.ch.aliasTag(tag); alias != "" {
		result, base, err := s.fetchAcrossBases(ctx, bases, "manifests/"+alias, notFoundAlias, opts)
		if !errors.Is(err, errAliasNotFound) {
			return result, base, err
		}
	}
	return s.fetchAcrossBases(ctx, bases, "manifests/"+tag, notFoundUnpublished, opts)
}

// resolveIndex fetches the OCI image index for spelling, trying bases in
// order under the tag-404 policy, through the dev channel's alias first
// (fetchTag). The returned digest is computed from the
// bytes received: the index itself is informational only (§7.4; it is
// computed by the host, not stored, so there is no descriptor to verify it
// against).
func (s *session) resolveIndex(ctx context.Context, bases []string, spelling string) (*ImageIndex, Digest, string, error) {
	result, base, err := s.fetchTag(ctx, bases, spelling, requestOptions{maxBytes: ManifestMaxBytes, accept: manifestAccept})
	if err != nil {
		return nil, "", "", err
	}
	var idx ImageIndex
	if uerr := strictUnmarshal(result.body, &idx); uerr != nil {
		return nil, "", "", newError(CodeArtifactCorrupt, spelling, "", result.url, uerr, "index at %s is not valid JSON: %v", result.url, uerr)
	}
	if idx.MediaType != MediaTypeIndex {
		return nil, "", "", newError(CodeSourceIncompatible, spelling, "", result.url, nil,
			"index at %s has unrecognized mediaType %q", result.url, idx.MediaType)
	}
	return &idx, digestOf(result.body), base, nil
}

// selectPlatformDescriptor finds the single descriptor in idx for
// platformKey. More than one match is CHTYPES_ARTIFACT_CORRUPT
// ("index-duplicate-platform"); no match is CHTYPES_ARTIFACT_UNPUBLISHED,
// naming what the index does offer.
func selectPlatformDescriptor(idx *ImageIndex, platformKey string) (*Descriptor, error) {
	plat, ok := platformByKey(platformKey)
	if !ok {
		return nil, fmt.Errorf("chtypes: %q is not a known v1 platform", platformKey)
	}
	var found *Descriptor
	var offered []string
	for i := range idx.Manifests {
		m := &idx.Manifests[i]
		if m.Platform == nil {
			continue
		}
		offered = append(offered, m.Platform.OS+"-"+m.Platform.Architecture)
		if m.Platform.OS != plat.OS || m.Platform.Architecture != plat.Architecture {
			continue
		}
		if found != nil {
			return nil, newError(CodeArtifactCorrupt, "", platformKey, "", nil,
				"index lists the %s platform more than once", platformKey)
		}
		found = m
	}
	if found == nil {
		return nil, newError(CodeArtifactUnpublished, "", platformKey, "", nil,
			"index offers no %s manifest (it offers: %s)", platformKey, strings.Join(offered, ", "))
	}
	return found, nil
}

// fetchManifestByDigest fetches the platform manifest by digest, verifying
// the received bytes' sha256 and size against desc before parsing anything
// inside them (docs/guides/fetch-v1.md §3 step 2).
func (s *session) fetchManifestByDigest(ctx context.Context, bases []string, desc Descriptor) (*ImageManifest, []byte, string, error) {
	result, base, err := s.fetchAcrossBases(ctx, bases, "manifests/"+string(desc.Digest), notFoundRetryOnLast,
		requestOptions{maxBytes: ManifestMaxBytes, accept: manifestAccept})
	if err != nil {
		return nil, nil, "", err
	}
	if err := verifyDescriptor(result.body, desc); err != nil {
		return nil, nil, "", newError(CodeArtifactCorrupt, "", "", result.url, err, "manifest at %s: %v", result.url, err)
	}
	var m ImageManifest
	if uerr := strictUnmarshal(result.body, &m); uerr != nil {
		return nil, nil, "", newError(CodeArtifactCorrupt, "", "", result.url, uerr, "manifest at %s is not valid JSON: %v", result.url, uerr)
	}
	if m.MediaType != MediaTypeManifest {
		return nil, nil, "", newError(CodeSourceIncompatible, "", "", result.url, nil,
			"manifest at %s has unrecognized mediaType %q", result.url, m.MediaType)
	}
	if len(m.Layers) != 1 {
		return nil, nil, "", newError(CodeArtifactCorrupt, "", "", result.url, nil,
			"manifest at %s carries %d layers, expected exactly 1", result.url, len(m.Layers))
	}
	if m.Layers[0].MediaType != MediaTypeLayer {
		return nil, nil, "", newError(CodeSourceIncompatible, "", "", result.url, nil,
			"manifest at %s has an unrecognized layer mediaType %q", result.url, m.Layers[0].MediaType)
	}
	return &m, result.body, base, nil
}

// verifyDescriptor checks body's size and sha256 against desc.
func verifyDescriptor(body []byte, desc Descriptor) error {
	if int64(len(body)) != desc.Size {
		return fmt.Errorf("size %d does not match the descriptor's %d", len(body), desc.Size)
	}
	got := digestOf(body)
	if got != desc.Digest {
		return fmt.Errorf("sha256 %s does not match the descriptor's %s", got, desc.Digest)
	}
	return nil
}
