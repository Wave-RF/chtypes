package main

import "fmt"

// tree.go — a route tree: exactly the URL paths a client requests, laid
// out as static files (plan §3.2): manifests/<tag>, manifests/sha256:<hex>,
// manifests/sha256-<hex> (the referrer fallback tag), blobs/sha256:<hex>,
// referrers/sha256:<hex>, tags/list. A `file://` base serves these
// directly; scripts/fetch-v1/server.py serves the same files over HTTP.

const defaultRepoPath = "v2/chtypes/v1" // the primary repository every ordinary tree serves

// fixturesRepoPath is the SDK's own fetch-fixtures repository
// (registry.fixtures_repository_suffix appended to the chtypes/v1 base) —
// used by exactly one case (fixtures-digest-pin-no-tag-fallback) to prove
// the generic signed-artifact fetch never tries a tag for it.
func fixturesRepoPath() string {
	return "v2/chtypes/v1" + C.Registry.FixturesRepositorySuffix
}

// referrersMode controls what GET /v2/<repo>/referrers/<digest> returns for
// a subject digest that has at least one referrer added:
//   - "normal": a populated index (the ordinary case).
//   - "empty":  a 200 with an empty `manifests` array — R2/Workers Cache
//     can serve this for up to five minutes right after a push (measured
//     on the staging host; see referrers-empty-*). A client must not treat
//     it as "this host has no referrers API" (that is "absent", below) —
//     it must still try the fallback tag.
//   - "absent": no referrers/<digest> route at all (404) — simulates a
//     host or mirror that does not implement the referrers API, so the
//     client must fall back to the sha256-<hex> tag entirely on its own.
type referrersMode string

const (
	referrersNormal referrersMode = "normal"
	referrersEmpty  referrersMode = "empty"
	referrersAbsent referrersMode = "absent"
)

type fallbackTagMode string

const (
	fallbackNormal fallbackTagMode = "normal" // the sha256-<hex> tag is served, with whatever referrers were added
	fallbackAbsent fallbackTagMode = "absent" // the sha256-<hex> tag does not exist either
)

// Tree is one named fixture tree under tests/fixtures/fetch-v1/trees/<name>/.
// ReferrersMode/FallbackTagMode are the tree-wide default; SetReferrerServing
// overrides them for one specific subject digest, so one tree can hold both
// ordinary manifests and the handful of cases that need the referrers API
// or the fallback tag to behave abnormally for just one of them.
type Tree struct {
	Name            string
	RepoPath        string
	ReferrersMode   referrersMode
	FallbackTagMode fallbackTagMode
	NoTagsList      bool // frozen-no-discovery: no tags/list route at all
	EmptyList       bool // an existing, empty repository: tags/list answers {"tags":[]}

	blobs            map[string][]byte
	manifestByDigest map[string][]byte
	tags             map[string]string
	referrersOf      map[string][]Descriptor
	referrersOrder   []string
	tagOrder         []string

	modeOverride     map[string]referrersMode
	fallbackOverride map[string]fallbackTagMode
}

// NewTree starts a tree over the primary chtypes/v1 repository, serving
// both the referrers API and the fallback tag normally by default.
func NewTree(name string) *Tree {
	return newTreeWithRepo(name, defaultRepoPath)
}

// NewFixturesRepoTree starts a tree over the SDK's own fetch-fixtures
// repository (chtypes/v1/sdk-fetch-fixtures) instead of chtypes/v1 itself.
func NewFixturesRepoTree(name string) *Tree {
	return newTreeWithRepo(name, fixturesRepoPath())
}

// allTrees is every tree any case-building function has started, keyed by
// name — so a cross-cutting check (checkOnlineTagsExist, in cases.go) can
// look up "the tree a given case names" without every category file having
// to thread its own trees back out to buildAll(). Registration happens
// once per tree, in newTreeWithRepo; nothing ever removes an entry except
// the selftest's own cleanup of its throwaway fake tree.
var allTrees = map[string]*Tree{}

func newTreeWithRepo(name, repoPath string) *Tree {
	t := &Tree{
		Name:             name,
		RepoPath:         repoPath,
		ReferrersMode:    referrersNormal,
		FallbackTagMode:  fallbackNormal,
		blobs:            map[string][]byte{},
		manifestByDigest: map[string][]byte{},
		tags:             map[string]string{},
		referrersOf:      map[string][]Descriptor{},
		modeOverride:     map[string]referrersMode{},
		fallbackOverride: map[string]fallbackTagMode{},
	}
	if _, exists := allTrees[name]; exists {
		panic("genfixtures: tree name " + name + " is already in use")
	}
	allTrees[name] = t
	return t
}

// SetReferrerServing overrides how referrers of subjectDigest are served,
// independent of every other subject in this tree.
func (t *Tree) SetReferrerServing(subjectDigest string, rm referrersMode, fm fallbackTagMode) {
	t.modeOverride[subjectDigest] = rm
	t.fallbackOverride[subjectDigest] = fm
}

// PutBlob stores content addressed by its own digest and returns the
// descriptor a manifest or index should reference it with.
func (t *Tree) PutBlob(mediaType string, content []byte) Descriptor {
	d := descriptorFor(mediaType, content)
	hex := d.Digest
	if existing, ok := t.blobs[hex]; ok && string(existing) != string(content) {
		panic(fmt.Sprintf("genfixtures: tree %s: digest collision on %s", t.Name, hex))
	}
	t.blobs[hex] = content
	return d
}

// PutManifest stores a manifest or index's bytes addressable by digest,
// and (when tag != "") also by that floating or exact tag. desc is the
// descriptor OTHER objects should cite to point at it.
func (t *Tree) PutManifest(tag string, mediaType string, content []byte) Descriptor {
	d := descriptorFor(mediaType, content)
	t.manifestByDigest[d.Digest] = content
	if tag != "" {
		if _, exists := t.tags[tag]; !exists {
			t.tagOrder = append(t.tagOrder, tag)
		}
		t.tags[tag] = d.Digest
	}
	return d
}

// PutTamperedManifestByDigest writes content at a digest path that does
// NOT match sha256(content) — tampered-manifest's whole fixture: the
// index's descriptor names the honest digest, but a GET of that digest
// returns different bytes.
func (t *Tree) PutTamperedManifestByDigest(claimedDigest string, content []byte) {
	t.manifestByDigest[claimedDigest] = content
}

// PutTamperedBlobByDigest is PutTamperedManifestByDigest's blob-side
// counterpart — tampered-layer's fixture: a layer GET returns bytes that
// do not hash to the digest it is served under (which the manifest, and
// the signed predicate, both still cite honestly).
func (t *Tree) PutTamperedBlobByDigest(claimedDigest string, content []byte) {
	t.blobs[claimedDigest] = content
}

// AddReferrer records that referrerDesc refers to subjectDigest. Flush
// turns this into the referrers API response and/or the fallback tag's
// index, depending on ReferrersMode/FallbackTagMode (or a per-subject
// override set via SetReferrerServing).
func (t *Tree) AddReferrer(subjectDigest string, referrerDesc Descriptor) {
	if _, ok := t.referrersOf[subjectDigest]; !ok {
		t.referrersOrder = append(t.referrersOrder, subjectDigest)
	}
	t.referrersOf[subjectDigest] = append(t.referrersOf[subjectDigest], referrerDesc)
}

// Flush renders every route this tree serves into fs under
// trees/<name>/<RepoPath>/.
func (t *Tree) Flush(fs *FileSet) {
	base := "trees/" + t.Name + "/" + t.RepoPath

	for digest, content := range t.blobs {
		fs.Put(base+"/blobs/"+digest, content)
	}
	for digest, content := range t.manifestByDigest {
		fs.Put(base+"/manifests/"+digest, content)
	}
	for tag, digest := range t.tags {
		fs.Put(base+"/manifests/"+tag, t.manifestByDigest[digest])
	}

	if !t.NoTagsList {
		tl := TagsList{Name: repoName(t.RepoPath), Tags: append([]string(nil), t.tagOrder...)}
		if t.EmptyList {
			// A repository that exists and holds nothing answers {"tags":[]}, not null.
			tl.Tags = []string{}
		}
		fs.Put(base+"/tags/list", canonicalJSON(tl))
	}

	for _, subjectDigest := range t.referrersOrder {
		refs := t.referrersOf[subjectDigest]

		rm := t.ReferrersMode
		if override, ok := t.modeOverride[subjectDigest]; ok {
			rm = override
		}
		fm := t.FallbackTagMode
		if override, ok := t.fallbackOverride[subjectDigest]; ok {
			fm = override
		}

		switch rm {
		case referrersNormal:
			idx := Referrers{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: refs}
			fs.Put(base+"/referrers/"+subjectDigest, canonicalJSON(idx))
		case referrersEmpty:
			idx := Referrers{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{}}
			fs.Put(base+"/referrers/"+subjectDigest, canonicalJSON(idx))
		case referrersAbsent:
			// no route written: a GET 404s, as from a host without the API
		}

		if fm == fallbackNormal {
			fallbackTag := "sha256-" + digestHexPart(subjectDigest)
			idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: refs}
			content := canonicalJSON(idx)
			d := descriptorFor(ociIndexMediaType, content)
			t.manifestByDigest[d.Digest] = content
			fs.Put(base+"/manifests/"+fallbackTag, content)
		}
	}
}

func repoName(repoPath string) string {
	const prefix = "v2/"
	if len(repoPath) > len(prefix) {
		return repoPath[len(prefix):]
	}
	return repoPath
}

const ociIndexMediaType = "application/vnd.oci.image.index.v1+json"
const ociManifestMediaType = "application/vnd.oci.image.manifest.v1+json"

// digestHexPart strips the "sha256:" prefix a descriptor digest always
// carries, for building a "sha256-<hex>" fallback tag name or a
// referrers/<hex> route.
func digestHexPart(digest string) string {
	const prefix = "sha256:"
	if len(digest) > len(prefix) && digest[:len(prefix)] == prefix {
		return digest[len(prefix):]
	}
	panic("genfixtures: digest " + digest + " has no sha256: prefix")
}
