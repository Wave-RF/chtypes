package main

import (
	"encoding/base64"
)

// artifact.go — the "happy path" recipe every platform manifest in these
// fixtures follows: a predicate, a config blob that mirrors it, a one-layer
// library tarball, an OCI image manifest, and a Sigstore bundle referrer.
// Each trust/bytes-negative case calls the smaller pieces directly with one
// deliberate deviation, rather than duplicating the whole recipe.

// C holds the frozen spec/fetch-v1/constants.json this run was invoked
// against. Set once by main() before any tree is built.
var C constantsFile

// Predicate is layout-v2 spec §4.1's in-toto predicate, byte-identical
// (field-for-field) to the OCI config blob every tarball's manifest.json
// also is ("This is core's firm predicate ... carries the same fields,
// value-equal"). GlibcFloor is omitted on darwin (omitempty), matching
// "absent on darwin" in §4.1.
type Predicate struct {
	ABI               int    `json:"abi"`
	ABIFingerprint    string `json:"abi_fingerprint"`
	ClickHouseVersion string `json:"clickhouse_version"`
	Channel           string `json:"channel"`
	ClickHouseMinor   string `json:"clickhouse_minor"`
	ClickHouseCommit  string `json:"clickhouse_commit"`
	OS                string `json:"os"`
	Arch              string `json:"arch"`
	Build             string `json:"build"`
	CoreCommit        string `json:"core_commit"`
	InputsSHA256      string `json:"inputs_sha256"`
	Library           string `json:"library"`
	LibrarySHA256     string `json:"library_sha256"`
	LibraryBytes      int64  `json:"library_bytes"`
	GlibcFloor        string `json:"glibc_floor,omitempty"`
}

func libraryNameFor(platformKey string) string {
	if platformKey == "darwin-arm64" {
		return "libchtypes.dylib"
	}
	return "libchtypes.so"
}

// fakeHex derives a deterministic hex string of length n from seed — never
// a real commit or build hash, just something digest-shaped and stable
// across regenerations.
func fakeHex(seed string, n int) string {
	h := sha256Hex([]byte(seed))
	for len(h) < n {
		h += sha256Hex([]byte(h))
	}
	return h[:n]
}

// PlatformArtifact is the result of building one platform's manifest:
// everything a case or an index builder needs to reference it.
type PlatformArtifact struct {
	PlatformKey  string
	ManifestDesc Descriptor
	LayerDesc    Descriptor
	ConfigDesc   Descriptor
	Predicate    Predicate
	BundleDigest string // the signature bundle's own blob digest (for lock fixtures)
}

// ArtifactOptions deviates the happy-path recipe for one platform manifest.
// The zero value is the happy path.
type ArtifactOptions struct {
	TarMode            string // "" (plain) | "multiframe" | "oversizewindow" | "bomb" | bad tar kinds below
	BadTarKind         *badTarKind
	LibraryContentSeed string // defaults to platform+version+build when empty
	// LibraryBytesOverride forces the PREDICATE's library_sha256/library_bytes
	// to those of a DIFFERENT (unused) content, while the tarball still
	// carries the real library — library-hash-mismatch's whole point.
	LibraryContentForPredicateOnly []byte
	// SkipSigning leaves the manifest with no referrer at all (no-bundle).
	SkipSigning bool
}

// buildPlatformArtifact builds one platform's manifest, layer and config,
// signs it with key (the normal, happy-path signature), and registers the
// referrer on tree. Returns everything a caller needs to reference it from
// an index or a lock fixture.
func buildPlatformArtifact(tree *Tree, key SigningKey, platformKey, version, build, channel string, opts ArtifactOptions) PlatformArtifact {
	plat := C.platform(platformKey)
	libName := libraryNameFor(platformKey)
	seed := opts.LibraryContentSeed
	if seed == "" {
		seed = platformKey + "-" + version + "-" + build
	}
	libContent := fakeLibraryContent(seed)

	var tarBytes []byte
	switch {
	case opts.BadTarKind != nil:
		tarBytes = buildBadTar(libName, libContent, *opts.BadTarKind)
	default:
		tarBytes = buildPlainTar(libName, libContent)
	}

	var layerBytes []byte
	switch opts.TarMode {
	case "multiframe":
		layerBytes = buildMultiFrameZstd(tarBytes)
	case "oversizewindow":
		layerBytes = buildOversizeWindowZstd(tarBytes)
	case "bomb":
		layerBytes = buildZipBombTarZstd(libName, C.Limits.MaxUnpackedBytes+1)
	default:
		layerBytes = zstdEncode(tarBytes)
	}
	layerDesc := tree.PutBlob(C.MediaTypes.Layer, layerBytes)

	predLibContent := libContent
	if opts.LibraryContentForPredicateOnly != nil {
		predLibContent = opts.LibraryContentForPredicateOnly
	}
	predicate := Predicate{
		ABI:               1,
		ABIFingerprint:    "sha256:" + fakeHex("abi-fingerprint-"+platformKey, 64),
		ClickHouseVersion: version,
		Channel:           channel,
		ClickHouseMinor:   minorOf(version),
		ClickHouseCommit:  fakeHex("clickhouse-commit-"+version, 40),
		OS:                plat.OS,
		Arch:              plat.Architecture,
		Build:             build,
		CoreCommit:        fakeHex("core-commit-"+version+"-"+build, 40),
		InputsSHA256:      fakeHex("inputs-"+version+"-"+build+"-"+platformKey, 64),
		Library:           libName,
		LibrarySHA256:     sha256Hex(predLibContent),
		LibraryBytes:      int64(len(predLibContent)),
	}
	if plat.OS != "darwin" {
		predicate.GlibcFloor = "2.17"
	}

	configBytes := canonicalJSON(predicate)
	configDesc := tree.PutBlob(C.MediaTypes.Config, configBytes)

	manifest := ImageManifest{
		SchemaVersion: 2,
		MediaType:     ociManifestMediaType,
		ArtifactType:  C.MediaTypes.ArtifactType,
		Config:        configDesc,
		Layers:        []Descriptor{layerDesc},
	}
	manifestBytes := canonicalJSON(manifest)
	manifestDesc := tree.PutManifest("", ociManifestMediaType, manifestBytes)
	manifestDesc.ArtifactType = C.MediaTypes.ArtifactType

	result := PlatformArtifact{
		PlatformKey:  platformKey,
		ManifestDesc: manifestDesc,
		LayerDesc:    layerDesc,
		ConfigDesc:   configDesc,
		Predicate:    predicate,
	}

	if !opts.SkipSigning {
		subjectName := libName + "-" + platformKey + ".tar.zst"
		statement := buildStatement(subjectName, digestHexPart(layerDesc.Digest), C.PredicateTypes.Artifact, predicate)
		bundle := signBundle(key.KeyID, key.Private, statement)
		result.BundleDigest = attachSignatureReferrer(tree, manifestDesc, bundle)
	}

	return result
}

// attachSignatureReferrer stores bundleJSON as a blob, wraps it in a
// referrer manifest whose subject is subjectDesc, and registers it on
// tree. Returns the bundle blob's own digest.
func attachSignatureReferrer(tree *Tree, subjectDesc Descriptor, bundleJSON []byte) string {
	bundleDesc := tree.PutBlob(C.MediaTypes.Bundle, bundleJSON)
	referrerDesc := buildReferrerManifest(tree, subjectDesc, C.MediaTypes.Bundle, bundleDesc)
	tree.AddReferrer(subjectDesc.Digest, referrerDesc)
	return bundleDesc.Digest
}

// buildReferrerManifest wraps layerDesc (the bundle, or the goldens
// content) as an OCI referrer manifest of subjectDesc, with the empty
// config OCI's referrers convention expects, and stores it by digest
// (never by tag — a referrer is found only by being listed for its
// subject, exactly like the real API).
func buildReferrerManifest(tree *Tree, subjectDesc Descriptor, artifactType string, layerDesc Descriptor) Descriptor {
	emptyConfigDesc := Descriptor{
		MediaType: C.MediaTypes.EmptyConfig,
		Digest:    digestOf(emptyConfigBlob),
		Size:      int64(len(emptyConfigBlob)),
		Data:      base64.StdEncoding.EncodeToString(emptyConfigBlob),
	}
	manifest := ImageManifest{
		SchemaVersion: 2,
		MediaType:     ociManifestMediaType,
		ArtifactType:  artifactType,
		Config:        emptyConfigDesc,
		Layers:        []Descriptor{layerDesc},
		Subject:       &Descriptor{MediaType: subjectDesc.MediaType, Digest: subjectDesc.Digest, Size: subjectDesc.Size},
	}
	manifestBytes := canonicalJSON(manifest)
	desc := tree.PutManifest("", ociManifestMediaType, manifestBytes)
	desc.ArtifactType = artifactType
	return desc
}

// minorOf returns the first two dot-separated components of a four-part
// version ("26.8.15.10" -> "26.8").
func minorOf(version string) string {
	dots := 0
	for i, r := range version {
		if r == '.' {
			dots++
			if dots == 2 {
				return version[:i]
			}
		}
	}
	return version
}
