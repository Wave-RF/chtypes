package main

import "encoding/json"

// recordcases.go — the cache-record and upgrade cache cases
// (docs/guides/fetch-v1.md §1, §10). verified.json is one canonical record
// every binding reads and writes (spec/fetch-v1/schema/verified.schema.json);
// these cases prove the READER half in every binding from layouts this
// generator writes, and the cross-binding interop job
// (`v1-cache-interop`, .github/workflows/v1.yml) proves the WRITER half.
//
//   - cache-record-canonical: an unpacked directory holding the canonical
//     record, and NO blobs and NO index.json. A binding that does not read
//     the record has nothing else to answer from, so the case passes only
//     if the record was read and trusted.
//   - cache-record-foreign-*: an unpacked directory whose record is
//     unparsable, in the old flat 0.x-era shape, or of schema 2 (carrying a
//     build no real artifact has, so trusting it would be visible). Blobs and
//     an index.json entry are present: every binding must treat the record as
//     ABSENT, re-verify from the blobs, and answer with the real artifact.
//   - upgrade-0x-registry-*: CHTYPES_CACHE points at a 0.x registry
//     directory (`<minor>/manifest.json` plus a library, `patches/...`, no
//     oci-layout, no index.json). It is an empty foreign layout: offline is
//     the ordinary not-installed code, and an online fetch installs beside the
//     old files.
//   - upgrade-0x-unpacked-*: an unpacked/sha256/<hex>/ directory holding a
//     0.x-style manifest.json and no verified.json. Without blobs it is
//     not-installed; with blobs it is re-verified and replaced.

type recordDigestsDoc struct {
	Index          *string `json:"index"`
	Manifest       string  `json:"manifest"`
	Layer          string  `json:"layer"`
	Bundle         *string `json:"bundle"`
	BundleManifest *string `json:"bundle_manifest"`
}

type recordDoc struct {
	Schema        int              `json:"schema"`
	Platform      string           `json:"platform"`
	Version       string           `json:"version"`
	Channel       *string          `json:"channel"`
	Build         string           `json:"build"`
	Library       string           `json:"library"`
	LibrarySHA256 string           `json:"library_sha256"`
	LibraryBytes  int64            `json:"library_bytes"`
	Digests       recordDigestsDoc `json:"digests"`
	SignedBy      *string          `json:"signed_by"`
	Predicate     Predicate        `json:"predicate"`
}

// canonicalRecordFor is the record an installer would write for art, built
// from the artifact's own digests and the test key that signed it.
func canonicalRecordFor(tree *Tree, art PlatformArtifact) recordDoc {
	channel := art.Predicate.Channel
	bundle := art.BundleDigest
	bundleManifest := tree.referrersOf[art.ManifestDesc.Digest][0].Digest
	signedBy := testKey.KeyID
	return recordDoc{
		Schema:        fixtureRecordSchema,
		Platform:      art.PlatformKey,
		Version:       art.Predicate.ClickHouseVersion,
		Channel:       &channel,
		Build:         art.Predicate.Build,
		Library:       art.Predicate.Library,
		LibrarySHA256: art.Predicate.LibrarySHA256,
		LibraryBytes:  art.Predicate.LibraryBytes,
		Digests: recordDigestsDoc{
			Manifest:       art.ManifestDesc.Digest,
			Layer:          art.LayerDesc.Digest,
			Bundle:         &bundle,
			BundleManifest: &bundleManifest,
		},
		SignedBy:  &signedBy,
		Predicate: art.Predicate,
	}
}

func unpackedPath(art PlatformArtifact, name string) string {
	return "unpacked/sha256/" + digestHexPart(art.ManifestDesc.Digest) + "/" + name
}

func buildRecordCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("cache-record")
	var layouts []*Layout

	newArtifact := func(version, build, seed string) (PlatformArtifact, string) {
		return buildPlatformArtifact(tree, testKey, "linux-arm64", version, build, "lts", ArtifactOptions{LibraryContentSeed: seed}), seed
	}
	expectArt := func(c *Case, art PlatformArtifact) {
		c.Expect.OK = true
		c.Expect.Version = strp(art.Predicate.ClickHouseVersion)
		c.Expect.Build = strp(art.Predicate.Build)
		c.Expect.LibrarySHA256 = strp(art.Predicate.LibrarySHA256)
	}
	expectMissing := func(c *Case) {
		c.Expect.OK = false
		c.Expect.Code = strp("CHTYPES_ARTIFACT_MISSING")
	}
	// Each layout is exercised twice, once through ensure with offline and
	// once through resolve_installed (the `resolve-installed-` id prefix,
	// docs/guides/fetch-v1.md §10).
	offlinePair := func(slug, layout, spelling string, art *PlatformArtifact) {
		for _, mode := range []string{"offline", "resolve"} {
			id := slug + "-offline"
			if mode == "resolve" {
				id = "resolve-installed-" + slug
			}
			c := newCase(id, "cache-record", "file", "http")
			c.Request.Spelling = spelling
			c.Request.Offline = true
			c.Setup.Cache = layout
			c.Expect.Requests.Max = intp(0)
			if art != nil {
				expectArt(&c, *art)
			} else {
				expectMissing(&c)
			}
			cases = append(cases, c)
		}
	}

	// --- cache-record-canonical ---------------------------------------------
	artCanon, seedCanon := newArtifact("26.2.3.4", "20260203.000004", "record-canonical")
	layoutCanon := NewLayout("cache-record-canonical")
	layoutCanon.AddFile(unpackedPath(artCanon, artCanon.Predicate.Library), fakeLibraryContent(seedCanon))
	layoutCanon.AddFile(unpackedPath(artCanon, "verified.json"), canonicalJSON(canonicalRecordFor(tree, artCanon)))
	layouts = append(layouts, layoutCanon)
	offlinePair("cache-record-canonical", "cache-record-canonical", "26.2", &artCanon)

	// --- cache-record-foreign-*: three kinds of record no binding may trust ---
	artForeign, seedForeign := newArtifact("26.2.5.6", "20260205.000006", "record-foreign")
	canon := canonicalRecordFor(tree, artForeign)
	schema2 := canon
	schema2.Schema = foreignRecordSchema
	schema2.Build = "99999999.999999"
	schema2.Version = "26.2.99.99"
	flat, err := json.Marshal(map[string]any{
		"platform":        artForeign.PlatformKey,
		"version":         artForeign.Predicate.ClickHouseVersion,
		"build":           "99999999.999999",
		"channel":         "lts",
		"manifest_digest": artForeign.ManifestDesc.Digest,
		"layer_digest":    artForeign.LayerDesc.Digest,
		"bundle_digest":   artForeign.BundleDigest,
		"signed_by":       testKey.KeyID,
		"library":         artForeign.Predicate.Library,
		"library_sha256":  artForeign.Predicate.LibrarySHA256,
		"library_bytes":   artForeign.Predicate.LibraryBytes,
		"predicate":       artForeign.Predicate,
	})
	if err != nil {
		panic(err)
	}
	for _, kind := range []struct {
		slug   string
		record []byte
	}{
		{"unparsable", []byte(`{"schema": 1, "platform": "linux-arm64", "version": `)},
		{"flat", append(flat, '\n')},
		{"schema2", canonicalJSON(schema2)},
	} {
		name := "cache-record-foreign-" + kind.slug
		l := NewLayout(name)
		copyArtifactIntoLayout(l, tree, artForeign, "26.2")
		l.AddFile(unpackedPath(artForeign, artForeign.Predicate.Library), fakeLibraryContent(seedForeign))
		l.AddFile(unpackedPath(artForeign, "verified.json"), kind.record)
		layouts = append(layouts, l)
		offlinePair(name, name, "26.2", &artForeign)
	}

	// --- upgrade-0x-registry: a 0.x directory is an empty foreign layout ------
	art0x := func() (PlatformArtifact, string) {
		return newArtifact("26.1.10.10", "20260110.000010", "upgrade-0x-online")
	}
	artOnline, _ := art0x()
	idx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(artOnline.ManifestDesc, "linux-arm64")}}
	tree.PutManifest("26.1.10.10", ociIndexMediaType, canonicalJSON(idx))
	layout0x := NewLayout("upgrade-0x-registry")
	layout0x.Raw()
	old0xLib := []byte("chtypes 0.x registry library (not a real binary)\n")
	layout0x.AddFile("26.1/manifest.json", []byte("{\"schema\": 1, \"line\": \"26.1\", \"version\": \"26.1.3.4\", \"library\": \"libchtypes.so\"}\n"))
	layout0x.AddFile("26.1/libchtypes.so", old0xLib)
	layout0x.AddFile("patches/26.1.3.4/manifest.json", []byte("{\"schema\": 1, \"version\": \"26.1.3.4\", \"library\": \"libchtypes.so\"}\n"))
	layout0x.AddFile("patches/26.1.3.4/libchtypes.so", old0xLib)
	layouts = append(layouts, layout0x)
	offlinePair("upgrade-0x-registry", "upgrade-0x-registry", "26.1", nil)
	online := newCase("upgrade-0x-registry-online", "cache-record", "file", "http")
	online.Request.Spelling = "26.1.10.10"
	online.Setup.Cache = "upgrade-0x-registry"
	expectArt(&online, artOnline)
	cases = append(cases, online)

	// --- upgrade-0x-unpacked: a 0.x manifest.json where verified.json belongs --
	artU, seedU := newArtifact("26.3.7.8", "20260307.000008", "upgrade-0x-unpacked")
	old0xManifest := []byte("{\"schema\": 1, \"line\": \"26.3\", \"version\": \"26.3.7.8\", \"library\": \"libchtypes.so\"}\n")
	layoutNoBlobs := NewLayout("upgrade-0x-unpacked-noblobs")
	layoutNoBlobs.AddFile(unpackedPath(artU, "manifest.json"), old0xManifest)
	layoutNoBlobs.AddFile(unpackedPath(artU, artU.Predicate.Library), fakeLibraryContent(seedU))
	layoutReverify := NewLayout("upgrade-0x-unpacked-reverify")
	copyArtifactIntoLayout(layoutReverify, tree, artU, "26.3")
	layoutReverify.AddFile(unpackedPath(artU, "manifest.json"), old0xManifest)
	layouts = append(layouts, layoutNoBlobs, layoutReverify)
	offlinePair("upgrade-0x-unpacked-noblobs", "upgrade-0x-unpacked-noblobs", "26.3", nil)
	offlinePair("upgrade-0x-unpacked-reverify", "upgrade-0x-unpacked-reverify", "26.3", &artU)

	// --- root-order-*: the cache and the system dirs are one search
	// (docs/guides/fetch-v1.md §1, one root order; public issue #486). Two
	// layouts, each holding one canonical record and its library and nothing
	// else, one build of 26.2 older than the other. Whichever root holds the
	// newer build, it answers: a binding that stops at the first root with a
	// match, or never reads a system dir's records, answers with the older.
	artOlder, seedOlder := newArtifact("26.2.7.1", "20260207.000001", "root-order-older")
	artNewer, seedNewer := newArtifact("26.2.8.1", "20260208.000001", "root-order-newer")
	for _, v := range []struct {
		name string
		art  PlatformArtifact
		seed string
	}{{"root-order-older", artOlder, seedOlder}, {"root-order-newer", artNewer, seedNewer}} {
		l := NewLayout(v.name)
		l.AddFile(unpackedPath(v.art, v.art.Predicate.Library), fakeLibraryContent(v.seed))
		l.AddFile(unpackedPath(v.art, "verified.json"), canonicalJSON(canonicalRecordFor(tree, v.art)))
		layouts = append(layouts, l)
	}
	for _, v := range []struct{ slug, cache, system string }{
		{"root-order-system-newer", "root-order-older", "root-order-newer"},
		{"root-order-cache-newer", "root-order-newer", "root-order-older"},
	} {
		for _, id := range []string{v.slug + "-offline", "resolve-installed-" + v.slug} {
			c := newCase(id, "cache-record", "file", "http")
			c.Request.Spelling = "26.2"
			c.Request.Offline = true
			c.Setup.Cache = v.cache
			c.Setup.SystemDirs = []string{v.system}
			c.Expect.Requests.Max = intp(0)
			expectArt(&c, artNewer)
			cases = append(cases, c)
		}
	}

	flushTrees(fs, tree)
	for _, l := range layouts {
		l.Flush(fs)
	}
	return cases
}
