package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// oci.go — the small subset of the OCI image-spec and distribution-spec
// this generator needs: descriptors, image manifests, image indexes, and
// canonical (deterministic) JSON encoding. Map values only ever appear as
// map[string]string (annotations), and encoding/json always sorts those
// keys, so nothing here depends on Go's randomized map iteration order.

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func digestOf(b []byte) string {
	return "sha256:" + sha256Hex(b)
}

// canonicalJSON renders v as indented JSON with a trailing newline. It is
// "canonical" only in the sense this generator needs: the same Go value
// always renders to the same bytes, run to run — struct field order is
// fixed by the struct definition, and the one place this codebase uses
// maps (annotations) encoding/json already key-sorts.
func canonicalJSON(v any) []byte {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		panic(err)
	}
	return append(b, '\n')
}

// marshalCompact renders v as compact (no added whitespace) JSON with a
// trailing newline — used for DSSE payloads, where the exact bytes become
// what is signed and later re-parsed, never read as a route-tree file in
// its own right.
func marshalCompact(v any) ([]byte, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

func unmarshalJSON(b []byte, v any) error {
	return json.Unmarshal(b, v)
}

type Platform struct {
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
}

type Descriptor struct {
	MediaType    string            `json:"mediaType"`
	Digest       string            `json:"digest"`
	Size         int64             `json:"size"`
	ArtifactType string            `json:"artifactType,omitempty"`
	Platform     *Platform         `json:"platform,omitempty"`
	Annotations  map[string]string `json:"annotations,omitempty"`
	Data         string            `json:"data,omitempty"`
}

func descriptorFor(mediaType string, content []byte) Descriptor {
	return Descriptor{
		MediaType: mediaType,
		Digest:    digestOf(content),
		Size:      int64(len(content)),
	}
}

type ImageIndex struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Manifests     []Descriptor `json:"manifests"`
}

type ImageManifest struct {
	SchemaVersion int               `json:"schemaVersion"`
	MediaType     string            `json:"mediaType"`
	ArtifactType  string            `json:"artifactType,omitempty"`
	Config        Descriptor        `json:"config"`
	Layers        []Descriptor      `json:"layers"`
	Subject       *Descriptor       `json:"subject,omitempty"`
	Annotations   map[string]string `json:"annotations,omitempty"`
}

// emptyConfigBlob is the `{}` config blob every referrer manifest (a
// signature bundle, a goldens artifact) uses in place of a real config —
// OCI's "empty descriptor" convention (media_types.empty_config).
var emptyConfigBlob = []byte("{}")

// OCILayout is the oci-layout marker file every OCI image layout directory
// carries at its root.
type OCILayout struct {
	ImageLayoutVersion string `json:"imageLayoutVersion"`
}

var ociLayoutMarker = OCILayout{ImageLayoutVersion: "1.0.0"}

// Referrers is the GET /v2/<repo>/referrers/<digest> response shape: an
// OCI image index whose manifests[] are the referrer descriptors.
type Referrers = ImageIndex

// TagsList is the GET /v2/<repo>/tags/list response shape.
type TagsList struct {
	Name string   `json:"name"`
	Tags []string `json:"tags"`
}
