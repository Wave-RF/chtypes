package main

// layout.go — OCI image layouts under tests/fixtures/fetch-v1/layouts/<name>/
// (plan §3.2): oci-layout, index.json, blobs/sha256/<hex> — for the cache,
// pre-seed and system-dir cases. The shape matches what a real
// `oras copy -r --to-oci-layout` produces (lane 0B's own
// layouts/oras-preseed/ fixture is the real thing, from the v1-oras-preseed
// job; every OTHER layout here is this generator's synthetic equivalent of
// the same shape, built from the SAME tree content a live resolve would
// find, so "already have these exact bytes locally" is literally true, not
// approximated).
//
// Decided here, lane 0B (docs/guides/fetch-v1.md §10 records this), and
// amended by the cache-record lane: a layout carries no pre-populated
// "already unpacked" directory EXCEPT in the cache-record-* and upgrade-0x-*
// layouts, which exist to test exactly that. `verified.json` is one
// canonical record every binding reads and writes (§1,
// spec/fetch-v1/schema/verified.schema.json), so a layout may now carry the
// canonical record (every binding must read it), a foreign or schema-2
// record (every binding must treat it as absent and re-verify from the
// blobs), or a 0.x leftover (never read as verified). Every OTHER layout
// here is scoped to be read exactly like a real pre-seed: a binding
// verifies-then-unpacks from these local blobs on first access, with zero
// network calls either way. The cases that need something to already be
// INSTALLED rather than merely PRESENT (monotonic-warning, and the two
// offline-newest-* cases) say so via `installed.json` at the layout root — a
// TEST-SETUP instruction the conformance RUNNER reads before the case begins
// ("call your own ensure() against these manifest digests, offline, from
// these local blobs, before starting the clock"), never a file any binding's
// shipped cache code parses. See docs/guides/fetch-v1.md §10.

type Layout struct {
	Name            string
	blobs           map[string][]byte // "sha256:<hex>" -> content
	roots           []Descriptor
	installedMarker []string
	files           map[string][]byte // extra files, by path relative to the layout root
	raw             bool              // no OCI layout at all: only files (a 0.x registry directory)
}

func NewLayout(name string) *Layout {
	return &Layout{Name: name, blobs: map[string][]byte{}}
}

func (l *Layout) addBlob(digest string, content []byte) {
	if existing, ok := l.blobs[digest]; ok && string(existing) != string(content) {
		panic("genfixtures: layout " + l.Name + ": digest collision on " + digest)
	}
	l.blobs[digest] = content
}

// AddRoot registers desc as a root entry of this layout's index.json,
// tagged with the OCI "ref.name" annotation refName names the pull used
// (oras's own convention, so layouts/oras-preseed/ and this generator's
// synthetic layouts share one shape).
func (l *Layout) AddRoot(desc Descriptor, refName string) {
	d := desc
	ann := map[string]string{}
	for k, v := range desc.Annotations {
		ann[k] = v
	}
	if refName != "" {
		ann["org.opencontainers.image.ref.name"] = refName
	}
	if len(ann) > 0 {
		d.Annotations = ann
	}
	l.roots = append(l.roots, d)
}

// AddFile adds one extra file at rel (a path relative to the layout root):
// an unpacked directory's library or verified.json, or a 0.x registry file.
func (l *Layout) AddFile(rel string, content []byte) {
	if l.files == nil {
		l.files = map[string][]byte{}
	}
	if existing, ok := l.files[rel]; ok && string(existing) != string(content) {
		panic("genfixtures: layout " + l.Name + ": file " + rel + " added twice with different content")
	}
	l.files[rel] = content
}

// Raw marks the layout as no OCI image layout at all: Flush writes only the
// files AddFile added, with no oci-layout, no index.json and no blobs. That
// is exactly what a 0.x registry directory is.
func (l *Layout) Raw() { l.raw = true }

func (l *Layout) Flush(fs *FileSet) {
	base := "layouts/" + l.Name
	for rel, content := range l.files {
		fs.Put(base+"/"+rel, content)
	}
	if l.raw {
		return
	}
	fs.Put(base+"/oci-layout", canonicalJSON(ociLayoutMarker))
	for digest, content := range l.blobs {
		fs.Put(base+"/blobs/sha256/"+digestHexPart(digest), content)
	}
	idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: l.roots}
	fs.Put(base+"/index.json", canonicalJSON(idx))
	if len(l.installedMarker) > 0 {
		fs.Put(base+"/installed.json", canonicalJSON(InstalledMarker{Installed: l.installedMarker}))
	}
}

// copyArtifactIntoLayout copies every blob a resolved platform artifact
// needs (config, layer, manifest, and — unless the bundle is intentionally
// withheld, see offline-miss — its signature bundle and referrer wrapper)
// from tree into layout, and registers the platform manifest ITSELF as a
// root entry under refName — the shape a real `oras copy --platform <p>
// --to-oci-layout` produces (one platform resolved, no index). Every
// cache/pre-seed layout in cachecases.go/lockcases.go wants exactly this.
func copyArtifactIntoLayout(layout *Layout, tree *Tree, art PlatformArtifact, refName string) {
	copyArtifactBlobsIntoLayout(layout, tree, art)
	layout.AddRoot(platformDescriptor(art.ManifestDesc, art.PlatformKey), refName)
}

// copyArtifactBlobsIntoLayout copies one platform artifact's config,
// layer, manifest and every referrer's own blobs into layout, WITHOUT
// adding a root entry — the building block copyArtifactIntoLayout and
// copyIndexIntoLayout both share.
func copyArtifactBlobsIntoLayout(layout *Layout, tree *Tree, art PlatformArtifact) {
	layout.addBlob(art.ConfigDesc.Digest, mustGetBlob(tree, art.ConfigDesc.Digest))
	layout.addBlob(art.LayerDesc.Digest, mustGetBlob(tree, art.LayerDesc.Digest))
	layout.addBlob(art.ManifestDesc.Digest, mustGetManifest(tree, art.ManifestDesc.Digest))

	for _, ref := range tree.referrersOf[art.ManifestDesc.Digest] {
		layout.addBlob(ref.Digest, mustGetManifest(tree, ref.Digest))
		var refManifest ImageManifest
		if err := unmarshalJSON(mustGetManifest(tree, ref.Digest), &refManifest); err != nil {
			panic(err)
		}
		// The referrer's own config (always the empty `{}` blob for every
		// referrer this generator builds) must exist as a real blob too,
		// never only as the descriptor's embedded `data` field — measured
		// against a real registry host, 2026-10-02: a manifest PUT whose
		// config digest has no backing blob is refused 400 BLOB_UNKNOWN.
		layout.addBlob(refManifest.Config.Digest, mustGetBlob(tree, refManifest.Config.Digest))
		for _, l := range refManifest.Layers {
			layout.addBlob(l.Digest, mustGetBlob(tree, l.Digest))
		}
	}
}

// copyIndexIntoLayout copies a full multi-platform image index — every
// platform artifact's own blobs, PLUS the index's own bytes as the one
// root entry — the real shape a registry push publishes (one index per
// tag) and what `oras copy -r --from-oci-layout` expects to find when
// resolving a tag that should fan out to several platforms. Used only by
// layouts/basic/, the one layout the v1-network job pushes into
// registry:2 whole, rather than one platform at a time.
func copyIndexIntoLayout(layout *Layout, tree *Tree, indexDesc Descriptor, arts map[string]PlatformArtifact, refName string) {
	layout.addBlob(indexDesc.Digest, mustGetManifest(tree, indexDesc.Digest))
	for _, art := range arts {
		copyArtifactBlobsIntoLayout(layout, tree, art)
	}
	layout.AddRoot(indexDesc, refName)
}

// InstalledMarker is layouts/<name>/installed.json's shape: the manifest
// digests the conformance runner must treat as already fully installed
// (unpacked and verified by the binding's OWN code, from these local
// blobs, offline) before the case begins.
type InstalledMarker struct {
	Installed []string `json:"installed"`
}

func (l *Layout) SetInstalled(manifestDigests ...string) {
	l.installedMarker = append([]string(nil), manifestDigests...)
}
