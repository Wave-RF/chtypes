package main

// lockcases.go — §6's lock/frozen/offline/update case group
// (docs/guides/fetch-v1.md §6, spec/fetch-v1/schema/lock3.schema.json).
//
// Decided here, lane 0B: locks/ splits into locks/inputs/ (what a case's
// setup.lock fixture names — a few of these are DELIBERATELY not
// lock3-schema-valid: frozen-lock-abi-2 and lock-schema-2-refused exist
// specifically to prove a wrong-ABI or pre-v1 lock is refused, so their
// input fixtures carry a `invalid-` filename prefix and
// scripts/fetch-v1/schema_check.py validates locks/expected/**, never
// locks/inputs/invalid-*) and locks/expected/ (what expect.lock_after
// names — always schema-valid, because it is what a correct write
// produces).

type Lock3 struct {
	Schema    int                           `json:"schema"`
	ABI       int                           `json:"abi"`
	Platforms []string                      `json:"platforms"`
	Requests  map[string]map[string]LockPin `json:"requests"`
}

type LockPin struct {
	Version  string `json:"version"`
	Build    string `json:"build"`
	Manifest string `json:"manifest"`
	Layer    string `json:"layer"`
	Bundle   string `json:"bundle"`
	Index    string `json:"index,omitempty"`
}

func pinFor(art PlatformArtifact, bundleDigest, indexDigest string) LockPin {
	return LockPin{
		Version:  art.Predicate.ClickHouseVersion,
		Build:    art.Predicate.Build,
		Manifest: art.ManifestDesc.Digest,
		Layer:    art.LayerDesc.Digest,
		Bundle:   bundleDigest,
		Index:    indexDigest,
	}
}

func putInputLock(fs *FileSet, name string, lock Lock3) {
	fs.Put("locks/inputs/"+name+".json", canonicalJSON(lock))
}

func putExpectedLock(fs *FileSet, name string, lock Lock3) {
	fs.Put("locks/expected/"+name+".json", canonicalJSON(lock))
}

func buildLockCases(fs *FileSet) []Case {
	var cases []Case

	// --- frozen-no-discovery: a tree with NO tags, NO referrers API and NO
	// tags/list — every request --frozen makes is by digest (R11). --------
	noDiscoveryTree := NewTree("lock-no-discovery")
	noDiscoveryTree.NoTagsList = true
	ndArt := buildPlatformArtifact(noDiscoveryTree, testKey, "linux-arm64", "26.10.1.1", "20261010.000001", "lts", ArtifactOptions{LibraryContentSeed: "frozen-no-discovery"})
	noDiscoveryTree.SetReferrerServing(ndArt.ManifestDesc.Digest, referrersAbsent, fallbackAbsent)
	ndIndex := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(ndArt.ManifestDesc, "linux-arm64")}}
	ndIndexDesc := noDiscoveryTree.PutManifest("", ociIndexMediaType, canonicalJSON(ndIndex)) // by digest only, no tag

	ndLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"26.10.1.1": {"linux-arm64": pinFor(ndArt, ndArt.BundleDigest, ndIndexDesc.Digest)},
		}}
	putInputLock(fs, "frozen-no-discovery", ndLock)
	frozenNoDiscovery := newCase("frozen-no-discovery", "lock-no-discovery", "file", "http", "registry")
	frozenNoDiscovery.Request.Spelling = "26.10.1.1"
	frozenNoDiscovery.Request.Frozen = true
	frozenNoDiscovery.Setup.Lock = strp("frozen-no-discovery")
	frozenNoDiscovery.Expect.OK = true
	frozenNoDiscovery.Expect.Version = strp(ndArt.Predicate.ClickHouseVersion)
	frozenNoDiscovery.Expect.Build = strp(ndArt.Predicate.Build)
	frozenNoDiscovery.Expect.Manifest = strp(ndArt.ManifestDesc.Digest)
	frozenNoDiscovery.Expect.LibrarySHA256 = strp(ndArt.Predicate.LibrarySHA256)
	cases = append(cases, frozenNoDiscovery)

	// --- frozen-unpinned: a lock exists but names no entry for this
	// request's spelling. -------------------------------------------------
	basicLockTree := NewTree("lock-basic")
	lbArt := buildFullIndex(basicLockTree, "26.10.2.1", "20261010.000002", "lts", []string{"26.10.2.1", "26.10.2"})
	otherPinnedArt := lbArt["linux-arm64"]
	unpinnedLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"26.9.9.9": {"linux-arm64": pinFor(otherPinnedArt, otherPinnedArt.BundleDigest, "")},
		}}
	putInputLock(fs, "frozen-unpinned", unpinnedLock)
	frozenUnpinned := newCase("frozen-unpinned", "lock-basic", "file", "http")
	frozenUnpinned.Request.Spelling = "26.10.2.1"
	frozenUnpinned.Request.Frozen = true
	frozenUnpinned.Setup.Lock = strp("frozen-unpinned")
	frozenUnpinned.Expect.OK = false
	frozenUnpinned.Expect.Code = strp("CHTYPES_ARTIFACT_PINNED")
	cases = append(cases, frozenUnpinned)

	// --- frozen-lock-abi-2: the lock names a different ABI generation.
	// Deliberately NOT lock3-schema-valid (abi must be 1) — this fixture
	// exists to prove that exact refusal, so it lives under an
	// `invalid-` name schema_check.py skips. ------------------------------
	abi2Lock := Lock3{Schema: 3, ABI: wrongABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{
			"26.10.2.1": {"linux-arm64": pinFor(otherPinnedArt, otherPinnedArt.BundleDigest, "")},
		}}
	putInputLock(fs, "invalid-abi-2", abi2Lock)
	frozenAbi2 := newCase("frozen-lock-abi-2", "lock-basic", "file", "http")
	frozenAbi2.Request.Spelling = "26.10.2.1"
	frozenAbi2.Request.Frozen = true
	frozenAbi2.Setup.Lock = strp("invalid-abi-2")
	frozenAbi2.Expect.OK = false
	frozenAbi2.Expect.Code = strp("CHTYPES_ARTIFACT_PINNED")
	cases = append(cases, frozenAbi2)

	// --- lock-schema-2-refused: a v0-shaped lock (schema 2) is refused
	// outright, never reinterpreted. Also deliberately not lock3-valid. ---
	schema2Lock := map[string]any{
		"schema": 2, "abi_revision": 1,
		"entries": map[string]any{"26.10.2.1": map[string]any{"sha256": "deadbeef"}},
	}
	fs.Put("locks/inputs/invalid-schema-2.json", canonicalJSON(schema2Lock))
	schema2Case := newCase("lock-schema-2-refused", "lock-basic", "file", "http")
	schema2Case.Request.Spelling = "26.10.2.1"
	schema2Case.Request.Frozen = true
	schema2Case.Setup.Lock = strp("invalid-schema-2")
	schema2Case.Expect.OK = false
	schema2Case.Expect.Code = strp("CHTYPES_ARTIFACT_PINNED")
	cases = append(cases, schema2Case)

	// --- lock-write-all-platforms: every index platform is locked; every
	// platform's bundle is verified, but the layer is fetched for the
	// host's platform ONLY. -------------------------------------------------
	lockWriteTree := NewTree("lock-write-all-platforms")
	lwArts := buildFullIndex(lockWriteTree, "26.10.3.1", "20261010.000003", "lts", []string{"26.10.3.1"})
	lwPins := map[string]LockPin{}
	for _, pk := range C.platformKeys() {
		art := lwArts[pk]
		lwPins[pk] = pinFor(art, art.BundleDigest, "")
	}
	lwExpected := Lock3{Schema: 3, ABI: fixtureABI, Platforms: C.platformKeys(),
		Requests: map[string]map[string]LockPin{"26.10.3.1": lwPins}}
	putExpectedLock(fs, "lock-write-all-platforms", lwExpected)
	lockWriteAll := newCase("lock-write-all-platforms", "lock-write-all-platforms", "file", "http", "registry")
	lockWriteAll.Request.Spelling = "26.10.3.1"
	lockWriteAll.Request.Platform = "linux-arm64"
	lockWriteAll.Request.LockWrite = true
	lockWriteAll.Expect.OK = true
	lockWriteAll.Expect.Version = strp(lwArts["linux-arm64"].Predicate.ClickHouseVersion)
	lockWriteAll.Expect.Build = strp(lwArts["linux-arm64"].Predicate.Build)
	lockWriteAll.Expect.Manifest = strp(lwArts["linux-arm64"].ManifestDesc.Digest)
	lockWriteAll.Expect.LibrarySHA256 = strp(lwArts["linux-arm64"].Predicate.LibrarySHA256)
	for _, pk := range C.platformKeys() {
		if pk == "linux-arm64" {
			continue
		}
		lockWriteAll.Expect.Requests.NoneMatching = append(lockWriteAll.Expect.Requests.NoneMatching,
			"GET .*/blobs/"+lwArts[pk].LayerDesc.Digest)
	}
	lockWriteAll.Expect.LockAfter = strp("lock-write-all-platforms")
	cases = append(cases, lockWriteAll)

	// --- update-re-resolves: an existing (older) lock is re-resolved and
	// overwritten, never merged. --------------------------------------------
	updateTree := NewTree("lock-update")
	updArtOld := buildPlatformArtifact(updateTree, testKey, "linux-arm64", "26.10.4.1", "20261010.000004", "lts", ArtifactOptions{LibraryContentSeed: "update-old"})
	updArtNew := buildPlatformArtifact(updateTree, testKey, "linux-arm64", "26.10.4.2", "20261010.000005", "lts", ArtifactOptions{LibraryContentSeed: "update-new"})
	updateIdx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(updArtNew.ManifestDesc, "linux-arm64")}}
	updateTree.PutManifest("26.10.4", ociIndexMediaType, canonicalJSON(updateIdx)) // the live tag now serves the NEW build
	updateInputLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.4": {"linux-arm64": pinFor(updArtOld, updArtOld.BundleDigest, "")}}}
	putInputLock(fs, "update-re-resolves-before", updateInputLock)
	updateExpectedLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.4": {"linux-arm64": pinFor(updArtNew, updArtNew.BundleDigest, "")}}}
	putExpectedLock(fs, "update-re-resolves", updateExpectedLock)
	updateCase := newCase("update-re-resolves", "lock-update", "file", "http")
	updateCase.Request.Spelling = "26.10.4"
	updateCase.Request.Update = true
	updateCase.Setup.Lock = strp("update-re-resolves-before")
	updateCase.Expect.OK = true
	updateCase.Expect.Version = strp(updArtNew.Predicate.ClickHouseVersion)
	updateCase.Expect.Build = strp(updArtNew.Predicate.Build)
	updateCase.Expect.Manifest = strp(updArtNew.ManifestDesc.Digest)
	updateCase.Expect.LibrarySHA256 = strp(updArtNew.Predicate.LibrarySHA256)
	updateCase.Expect.LockAfter = strp("update-re-resolves")
	cases = append(cases, updateCase)

	// --- frozen-mirror: base[0] names a repository root that does not
	// exist at all (every request against it fails outright, the same
	// shape a dead mirror host produces); base[1] is a digest-only mirror
	// that has exactly what the lock names, no tags at all. A single tree
	// serves base[1] — base[0] is a deliberately bad path templated off
	// the SAME `{base}`, which keeps this case within cases.schema.json's
	// one-tree-per-case shape rather than needing a second tree field.
	mirrorTree := NewTree("lock-mirror-digest-only")
	mirrorTree.NoTagsList = true
	mirrorArt := buildPlatformArtifact(mirrorTree, testKey, "linux-arm64", "26.10.5.1", "20261010.000006", "lts", ArtifactOptions{LibraryContentSeed: "frozen-mirror"})
	mirrorTree.SetReferrerServing(mirrorArt.ManifestDesc.Digest, referrersAbsent, fallbackAbsent)
	mirrorIdx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(mirrorArt.ManifestDesc, "linux-arm64")}}
	mirrorIdxDesc := mirrorTree.PutManifest("", ociIndexMediaType, canonicalJSON(mirrorIdx))
	mirrorLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.5.1": {"linux-arm64": pinFor(mirrorArt, mirrorArt.BundleDigest, mirrorIdxDesc.Digest)}}}
	putInputLock(fs, "frozen-mirror", mirrorLock)
	frozenMirror := newCase("frozen-mirror", "lock-mirror-digest-only", "file", "http")
	frozenMirror.Request.Spelling = "26.10.5.1"
	frozenMirror.Request.Frozen = true
	frozenMirror.Request.Bases = []string{"{base}/does-not-exist", "{base}"}
	frozenMirror.Setup.Lock = strp("frozen-mirror")
	frozenMirror.Expect.OK = true
	frozenMirror.Expect.Version = strp(mirrorArt.Predicate.ClickHouseVersion)
	frozenMirror.Expect.Build = strp(mirrorArt.Predicate.Build)
	frozenMirror.Expect.Manifest = strp(mirrorArt.ManifestDesc.Digest)
	frozenMirror.Expect.LibrarySHA256 = strp(mirrorArt.Predicate.LibrarySHA256)
	cases = append(cases, frozenMirror)

	// --- offline-frozen: a valid lock plus a cache holding exactly its
	// digests; zero requests, purely local. ---------------------------------
	offlineFrozenTree := NewTree("lock-offline-frozen")
	ofArt := buildPlatformArtifact(offlineFrozenTree, testKey, "linux-arm64", "26.10.6.1", "20261010.000007", "lts", ArtifactOptions{LibraryContentSeed: "offline-frozen"})
	ofIdx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(ofArt.ManifestDesc, "linux-arm64")}}
	offlineFrozenTree.PutManifest("26.10.6.1", ociIndexMediaType, canonicalJSON(ofIdx))
	ofLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.6.1": {"linux-arm64": pinFor(ofArt, ofArt.BundleDigest, "")}}}
	putInputLock(fs, "offline-frozen", ofLock)
	offlineFrozenLayout := NewLayout("offline-frozen")
	copyArtifactIntoLayout(offlineFrozenLayout, offlineFrozenTree, ofArt, "26.10.6.1")
	offlineFrozenLayout.Flush(fs)
	offlineFrozen := newCase("offline-frozen", "lock-offline-frozen", "file", "http")
	offlineFrozen.Request.Spelling = "26.10.6.1"
	offlineFrozen.Request.Offline = true
	offlineFrozen.Setup.Lock = strp("offline-frozen")
	offlineFrozen.Setup.Cache = "offline-frozen"
	offlineFrozen.Expect.OK = true
	offlineFrozen.Expect.Version = strp(ofArt.Predicate.ClickHouseVersion)
	offlineFrozen.Expect.Build = strp(ofArt.Predicate.Build)
	offlineFrozen.Expect.Manifest = strp(ofArt.ManifestDesc.Digest)
	offlineFrozen.Expect.LibrarySHA256 = strp(ofArt.Predicate.LibrarySHA256)
	offlineFrozen.Expect.Requests.Max = intp(0)
	cases = append(cases, offlineFrozen)

	// --- frozen-warm-cache-zero-requests, frozen-offline-lock-match and
	// frozen-offline-lock-mismatch and frozen-offline-lock-not-installed (public issue #414): a build installed
	// under exactly the lock's pinned manifest, layer and bundle digests
	// answers --frozen with ZERO requests, --frozen --offline verifies the
	// installed build against the lock with zero network, and an installed
	// build that is not the pinned one is CHTYPES_ARTIFACT_PINNED. ---------
	warmTree := NewTree("lock-frozen-warm")
	warmArt := buildPlatformArtifact(warmTree, testKey, "linux-arm64", "26.10.7.1", "20261010.000008", "lts", ArtifactOptions{LibraryContentSeed: "frozen-warm"})
	otherArt := buildPlatformArtifact(warmTree, testKey, "linux-arm64", "26.10.7.1", "20261010.000009", "lts", ArtifactOptions{LibraryContentSeed: "frozen-warm-other"})
	warmLayout := NewLayout("frozen-warm")
	copyArtifactIntoLayout(warmLayout, warmTree, warmArt, "26.10.7.1")
	warmLayout.SetInstalled(warmArt.ManifestDesc.Digest)
	warmLayout.Flush(fs)
	warmLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.7.1": {"linux-arm64": pinFor(warmArt, warmArt.BundleDigest, "")}}}
	putInputLock(fs, "frozen-warm", warmLock)
	// not-installed: the lock pins a build the cache does not hold.
	otherLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.7.1": {"linux-arm64": pinFor(otherArt, otherArt.BundleDigest, "")}}}
	putInputLock(fs, "frozen-warm-not-installed", otherLock)
	// mismatch: the pinned manifest IS installed, but its record's bundle
	// digest differs from the pin's.
	mismatchLock := Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.10.7.1": {"linux-arm64": pinFor(warmArt, otherArt.BundleDigest, "")}}}
	putInputLock(fs, "frozen-warm-mismatch", mismatchLock)
	for _, w := range []struct {
		id, lock string
		offline  bool
		ok       bool
	}{
		{"frozen-warm-cache-zero-requests", "frozen-warm", false, true},
		{"frozen-offline-lock-match", "frozen-warm", true, true},
		{"frozen-offline-lock-mismatch", "frozen-warm-mismatch", true, false},
		{"frozen-offline-lock-not-installed", "frozen-warm-not-installed", true, false},
	} {
		c := newCase(w.id, "lock-frozen-warm", "file", "http")
		c.Request.Spelling = "26.10.7.1"
		c.Request.Frozen = true
		c.Request.Offline = w.offline
		c.Setup.Cache = "frozen-warm"
		c.Setup.Lock = strp(w.lock)
		c.Expect.OK = w.ok
		c.Expect.Requests.Max = intp(0)
		if w.ok {
			c.Expect.Version = strp(warmArt.Predicate.ClickHouseVersion)
			c.Expect.Build = strp(warmArt.Predicate.Build)
			c.Expect.Manifest = strp(warmArt.ManifestDesc.Digest)
			c.Expect.LibrarySHA256 = strp(warmArt.Predicate.LibrarySHA256)
		} else {
			c.Expect.Code = strp("CHTYPES_ARTIFACT_PINNED")
			if w.id == "frozen-offline-lock-not-installed" {
				c.Expect.Code = strp("CHTYPES_ARTIFACT_MISSING")
			}
		}
		cases = append(cases, c)
	}

	flushTrees(fs, noDiscoveryTree, basicLockTree, lockWriteTree, updateTree, mirrorTree, offlineFrozenTree, warmTree)
	return cases
}
