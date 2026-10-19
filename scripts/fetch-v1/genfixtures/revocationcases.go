package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// revocationcases.go — build-level revocation (docs/guides/fetch-v1.md,
// "Build-level revocation"; public issue #625). A revocation is a signed
// referrer of the revoked platform manifest; a binding reads it from the
// listing its signature lookup reads, verifies it like the artifact statement,
// refuses the build with CHTYPES_ARTIFACT_REVOKED, and stores it in the build's
// cache entry. A 410 whose body's errors[0].detail.revoked is an object is the
// same code. A status that cannot be read fails open with one warning, or, in
// strict mode, is the transport's error.
//
// These cases are generation 2's alone: buildRevocationCases writes them into
// the fetch-v2 corpus only, because the v1 contract has none of this (the
// fetch-v1 corpus is its specification, and its frozen-warm and
// frozen-no-discovery cases keep their v1 meaning; lockcases.go gives the
// fetch-v2 copies of those two their generation-2 expectations). Case ids stay
// unique across both corpora; nothing requires the two to carry the same set.

// revocationCorpus reports whether this run writes the generation-2 corpus,
// the only one that holds revocation cases.
func revocationCorpus() bool { return fixtureABI == 2 }

// The fixed text a case checks a warning for: the start of each warning the
// guide defines, up to and including the digests it names, never the
// binding's own cause text.
func notCheckedWarning(manifest string) string {
	return "revocation status not checked for manifest " + manifest
}

func ignoredWarning(referrer, manifest string) string {
	return "revocation " + referrer + " for manifest " + manifest + " does not verify and is ignored"
}

const allowingWarning = "allowing a revoked build: "

const revokedTableRelPath = "tests/fixtures/revoked-410/cases.json"

// fixtureAdvisory is one fixture advisory: a GHSA-shaped id that is plainly a
// fixture's, never a real advisory's.
type fixtureAdvisory struct {
	ID, URL, Class, Summary, SupersededBy, RevokedAt string
}

func advisoryN(n int, class, supersededBy string) fixtureAdvisory {
	id := fmt.Sprintf("GHSA-fx01-test-%04d", 100+n)
	return fixtureAdvisory{
		ID:           id,
		URL:          "https://github.com/Wave-RF/chtypes/security/advisories/" + id,
		Class:        class,
		Summary:      fmt.Sprintf("fixture revocation %d: a filter over-accepts a value the server refuses", n),
		SupersededBy: supersededBy,
		RevokedAt:    "2026-10-19T00:00:00Z",
	}
}

// revocationPredicateFor is the predicate a revocation of art carries: the
// revoked build's own identity, copied from its signed predicate, and the
// advisory.
func revocationPredicateFor(art PlatformArtifact, adv fixtureAdvisory) map[string]any {
	return map[string]any{
		"manifest":           art.ManifestDesc.Digest,
		"abi":                art.Predicate.ABI,
		"abi_fingerprint":    art.Predicate.ABIFingerprint,
		"os":                 art.Predicate.OS,
		"arch":               art.Predicate.Arch,
		"clickhouse_version": art.Predicate.ClickHouseVersion,
		"build":              art.Predicate.Build,
		"advisory":           map[string]any{"id": adv.ID, "url": adv.URL},
		"class":              adv.Class,
		"summary":            adv.Summary,
		"superseded_by":      adv.SupersededBy,
		"revoked_at":         adv.RevokedAt,
	}
}

// revocationVariant deviates one revocation from the verified recipe: signed by
// key with the revocation predicateType, over art's layer, with the predicate
// revocationPredicateFor gives. The zero value (key unset) is the verified one.
type revocationVariant struct {
	key           *SigningKey
	predicateType string                 // "" is the revocation predicateType
	subjectHex    string                 // "" is art's layer digest
	mutate        func(p map[string]any) // nil changes nothing
	rawBundle     []byte                 // non-nil replaces the whole bundle (malformed)
	payload       []byte                 // non-nil is signed in place of the statement (malformed)
}

// attachRevocation signs a revocation of art and registers it as a referrer
// of art's platform manifest, after its signature. It returns the referrer
// manifest's digest (what a warning names) and the bundle blob's digest (what
// a stored record holds byte for byte).
func attachRevocation(tree *Tree, art PlatformArtifact, adv fixtureAdvisory, v revocationVariant) (referrer, bundleDigest string) {
	key := testKey
	if v.key != nil {
		key = *v.key
	}
	predicateType := C.PredicateTypes.Revocation
	if v.predicateType != "" {
		predicateType = v.predicateType
	}
	subject := digestHexPart(art.LayerDesc.Digest)
	if v.subjectHex != "" {
		subject = v.subjectHex
	}
	pred := revocationPredicateFor(art, adv)
	if v.mutate != nil {
		v.mutate(pred)
	}
	var bundle []byte
	switch {
	case v.rawBundle != nil:
		bundle = v.rawBundle
	case v.payload != nil:
		bundle = signBundle(key.KeyID, key.Private, v.payload)
	default:
		statement := buildStatement(libraryNameFor(art.PlatformKey)+"-"+art.PlatformKey+".tar.zst", subject, predicateType, pred)
		bundle = signBundle(key.KeyID, key.Private, statement)
	}
	bundleDesc := tree.PutBlob(C.MediaTypes.Bundle, bundle)
	referrerDesc := buildReferrerManifest(tree, art.ManifestDesc, C.MediaTypes.RevocationArtifactType, bundleDesc)
	tree.AddReferrer(art.ManifestDesc.Digest, referrerDesc)
	return referrerDesc.Digest, bundleDesc.Digest
}

// revokedMessage is the guide's message for a verified revocation of art:
// "<version> <platform> build <build> (manifest <digest>) is revoked: <id>
// <url> (<class>); superseded by build <superseded_by>: <summary>".
func revokedMessage(art PlatformArtifact, adv fixtureAdvisory) string {
	msg := fmt.Sprintf("%s %s build %s (manifest %s) is revoked: %s %s",
		art.Predicate.ClickHouseVersion, art.PlatformKey, art.Predicate.Build, art.ManifestDesc.Digest, adv.ID, adv.URL)
	if adv.Class != "" {
		msg += " (" + adv.Class + ")"
	}
	if adv.SupersededBy != "" {
		msg += "; superseded by build " + adv.SupersededBy
	}
	if adv.Summary != "" {
		msg += ": " + adv.Summary
	}
	return msg
}

// revoked410Row returns the named row of the shared 410 table: its body, and
// for a revoked row the text after "<url> answered 410 Gone: " (nil for a
// retired one). The conformance cases and every binding's unit test read the
// same table, so they cannot disagree about the message.
func revoked410Row(name string) (body, code string, text *string) {
	path := filepath.Join(repoRoot(), revokedTableRelPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		panic("genfixtures: reading " + path + ": " + err.Error())
	}
	var doc struct {
		Cases []struct {
			Name     string  `json:"name"`
			BodyText *string `json:"body_text"`
			Code     string  `json:"code"`
			Revoked  *struct {
				Text string `json:"text"`
			} `json:"revoked"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		panic("genfixtures: parsing " + path + ": " + err.Error())
	}
	for _, c := range doc.Cases {
		if c.Name != name {
			continue
		}
		if c.BodyText == nil {
			panic(fmt.Sprintf("genfixtures: %s row %q must carry body_text", revokedTableRelPath, name))
		}
		if c.Revoked != nil {
			t := c.Revoked.Text
			text = &t
		}
		return *c.BodyText, c.Code, text
	}
	panic(fmt.Sprintf("genfixtures: %s has no row %q", revokedTableRelPath, name))
}

// devAliasTag268 is the dev-alias tree's 26.8 tag, built at fingerprint N
// (aliascases.go), which the all-revoked alias case falls back to.
var devAliasTag268 map[string]PlatformArtifact

func buildRevocationCases(fs *FileSet) []Case {
	if !revocationCorpus() {
		return nil
	}
	var cases []Case
	tree := NewTree("revocation")
	var layouts []*Layout

	build := func(version, buildID string) PlatformArtifact {
		return buildPlatformArtifact(tree, testKey, "linux-arm64", version, buildID, "lts", ArtifactOptions{LibraryContentSeed: "revocation-" + version + "-" + buildID})
	}
	tag := func(t *Tree, spelling string, arts ...PlatformArtifact) {
		var descs []Descriptor
		for _, a := range arts {
			descs = append(descs, platformDescriptor(a.ManifestDesc, a.PlatformKey))
		}
		t.PutManifest(spelling, ociIndexMediaType, canonicalJSON(ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: descs}))
	}
	installed := func(name string, art PlatformArtifact, ref string) {
		l := NewLayout(name)
		copyArtifactIntoLayout(l, tree, art, ref)
		l.SetInstalled(art.ManifestDesc.Digest)
		layouts = append(layouts, l)
	}
	pinLock := func(name, spelling string, arts ...PlatformArtifact) {
		lock := Lock3{Schema: 3, ABI: fixtureABI, Requests: map[string]map[string]LockPin{spelling: {}}}
		for _, a := range arts {
			lock.Platforms = append(lock.Platforms, a.PlatformKey)
			lock.Requests[spelling][a.PlatformKey] = pinFor(a, a.BundleDigest, "")
		}
		putInputLock(fs, name, lock)
	}
	putScript := func(id, treeName string, routes []httpRoute) {
		validateLocationHeaders(id, routes)
		fs.Put("http/"+id+".json", canonicalJSON(HTTPScript{Schema: 1, ID: id, Tree: treeName, Routes: routes, SecondOriginRoutes: []httpRoute{}}))
	}
	want := func(c *Case, art PlatformArtifact) {
		c.Expect.OK = true
		c.Expect.Version = strp(art.Predicate.ClickHouseVersion)
		c.Expect.Build = strp(art.Predicate.Build)
		c.Expect.Manifest = strp(art.ManifestDesc.Digest)
		c.Expect.LibrarySHA256 = strp(art.Predicate.LibrarySHA256)
	}
	refused := func(c *Case, code string, contains string) {
		c.Expect.OK = false
		c.Expect.Code = strp(code)
		c.Expect.MessageContains = strp(contains)
	}
	noLayer := func(c *Case, arts ...PlatformArtifact) {
		for _, a := range arts {
			c.Expect.Requests.NoneMatching = append(c.Expect.Requests.NoneMatching, "GET .*/blobs/"+a.LayerDesc.Digest)
		}
	}
	listingPaths := func(art PlatformArtifact) []string {
		return []string{
			"/v2/chtypes/v1/referrers/" + art.ManifestDesc.Digest,
			"/v2/chtypes/v1/manifests/sha256-" + digestHexPart(art.ManifestDesc.Digest),
		}
	}
	unreachable := func(art PlatformArtifact) []httpRoute {
		var routes []httpRoute
		for _, p := range listingPaths(art) {
			routes = append(routes, httpRoute{Method: "GET", Path: p, Responses: []httpResponse{{Status: intp(503)}}})
		}
		return routes
	}
	opens := func(c *Case, n int) { c.Request.Opens = intp(n) }

	// --- 1. An install (item 1): the tag names a build its listing revokes. The
	// revocation is read from the trust listing, verified, and the build is
	// refused before its layer is requested: nothing is downloaded or stored.
	artInstall := build("26.13.1.1", "20261013.000001")
	advInstall := advisoryN(1, "over-accept", "20261019.000001")
	attachRevocation(tree, artInstall, advInstall, revocationVariant{})
	tag(tree, "26.13.1.1", artInstall)
	c := newCase("install-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.1.1"
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artInstall, advInstall))
	noLayer(&c, artInstall)
	cases = append(cases, c)

	// --- 2. A revoked build under --frozen (item 2), cold and warm; the
	// override (item 9); and the record a warm call stores (item 6), read back
	// offline, --frozen --offline and by an open, each with no request.
	artPinned := build("26.13.2.1", "20261013.000002")
	advPinned := advisoryN(2, "crash", "20261019.000002")
	_, pinnedBundle := attachRevocation(tree, artPinned, advPinned, revocationVariant{})
	tag(tree, "26.13.2.1", artPinned)
	pinLock("revocation-pinned", "26.13.2.1", artPinned)
	installed("revocation-pinned-installed", artPinned, "26.13.2.1")
	stored := []StoredRevocation{{Manifest: artPinned.ManifestDesc.Digest, Bundle: pinnedBundle}}

	c = newCase("frozen-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.2.1"
	c.Request.Frozen = true
	c.Setup.Lock = strp("revocation-pinned")
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artPinned, advPinned))
	noLayer(&c, artPinned)
	cases = append(cases, c)

	c = newCase("frozen-warm-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.2.1"
	c.Request.Frozen = true
	c.Setup.Lock = strp("revocation-pinned")
	c.Setup.Cache = "revocation-pinned-installed"
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artPinned, advPinned))
	c.Expect.Requests.Max = intp(3) // the listing, the revocation's manifest and its bundle
	c.Expect.StoredRevocations = stored
	cases = append(cases, c)

	c = newCase("allow-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.2.1"
	c.Request.Frozen = true
	c.Request.AllowRevoked = true
	c.Setup.Lock = strp("revocation-pinned")
	c.Setup.Cache = "revocation-pinned-installed"
	want(&c, artPinned)
	c.Expect.Warnings = []string{allowingWarning + revokedMessage(artPinned, advPinned)}
	c.Expect.WarningsMax = intp(1)
	c.Expect.Requests.Max = intp(3)
	c.Expect.StoredRevocations = stored
	cases = append(cases, c)

	// The stored record (item 6): an entry holding the canonical verified.json,
	// the library and the revocation's bundle byte for byte, with no blobs.
	// Every call that would answer with it refuses with no request, offline
	// included (item 7 only says an offline call with NO record cannot know).
	storedLayout := NewLayout("revocation-stored")
	storedLayout.AddFile(unpackedPath(artPinned, artPinned.Predicate.Library), fakeLibraryContent("revocation-26.13.2.1-20261013.000002"))
	storedLayout.AddFile(unpackedPath(artPinned, "verified.json"), canonicalJSON(canonicalRecordFor(tree, artPinned)))
	storedLayout.AddFile(unpackedPath(artPinned, C.Cache.RevocationRecord), mustGetBlob(tree, pinnedBundle))
	layouts = append(layouts, storedLayout)
	for _, s := range []struct {
		id              string
		offline, frozen bool
		opens           bool
	}{
		{"offline-revoked-record", true, false, false},
		{"frozen-offline-revoked-record", true, true, false},
		{"open-revoked-record", false, false, true},
	} {
		c = newCase(s.id, "revocation", "file", "http")
		c.Request.Spelling = "26.13.2.1"
		c.Request.Offline = s.offline
		c.Request.Frozen = s.frozen
		if s.frozen {
			c.Setup.Lock = strp("revocation-pinned")
		}
		if s.opens {
			opens(&c, 1)
		}
		c.Setup.Cache = "revocation-stored"
		refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artPinned, advPinned))
		c.Expect.Requests.Max = intp(0)
		cases = append(cases, c)
	}

	// --- 3. Revocations that do not verify (item 4) are each ignored, with one
	// warning naming it: a wrong key, a malformed bundle, a malformed
	// statement, and each checked field disagreeing. The warm --frozen fetch
	// succeeds.
	artUnverifiable := build("26.13.3.1", "20261013.000003")
	advUnverifiable := advisoryN(3, "other", "20261019.000003")
	other := artInstall // another build: its manifest, layer and identity disagree
	set := func(key string, value any) func(map[string]any) {
		return func(p map[string]any) { p[key] = value }
	}
	variants := []revocationVariant{
		{key: &otherKey},
		{rawBundle: []byte("this is not a Sigstore bundle\n")},
		{payload: []byte("{\"_type\": \"https://in-toto.io/Statement/v1\", \"subject\": [")},
		{predicateType: C.PredicateTypes.Artifact},
		{subjectHex: digestHexPart(other.LayerDesc.Digest)},
		{mutate: set("manifest", other.ManifestDesc.Digest)},
		{mutate: set("abi", wrongABI)},
		{mutate: set("abi_fingerprint", "sha256:"+fakeHex("revocation another fingerprint", 64))},
		{mutate: set("os", "darwin")},
		{mutate: set("arch", "amd64")},
		{mutate: set("clickhouse_version", "26.13.3.2")},
		{mutate: set("build", "20261013.999999")},
		{mutate: func(p map[string]any) { delete(p, "advisory") }},
	}
	var ignored []string
	for _, v := range variants {
		ref, _ := attachRevocation(tree, artUnverifiable, advUnverifiable, v)
		ignored = append(ignored, ignoredWarning(ref, artUnverifiable.ManifestDesc.Digest))
	}
	pinLock("revocation-unverifiable", "26.13.3.1", artUnverifiable)
	installed("revocation-unverifiable-installed", artUnverifiable, "26.13.3.1")
	c = newCase("revocation-unverifiable-ignored", "revocation", "file", "http")
	c.Request.Spelling = "26.13.3.1"
	c.Request.Frozen = true
	c.Setup.Lock = strp("revocation-unverifiable")
	c.Setup.Cache = "revocation-unverifiable-installed"
	want(&c, artUnverifiable)
	c.Expect.Warnings = ignored
	c.Expect.WarningsMax = intp(len(ignored))
	noLayer(&c, artUnverifiable)
	cases = append(cases, c)

	// --- 4. The status cannot be read (item 3): the listing answers 503 on
	// the API and the fallback tag. A warm --frozen fetch spends the retry
	// table on each, then succeeds with the one warning; in strict mode it is
	// the transport's error.
	artUnreachable := build("26.13.4.1", "20261013.000004")
	attachRevocation(tree, artUnreachable, advisoryN(4, "over-accept", "20261019.000004"), revocationVariant{})
	pinLock("revocation-unreachable", "26.13.4.1", artUnreachable)
	installed("revocation-unreachable-installed", artUnreachable, "26.13.4.1")
	retryTable := []float64{4, 8, 16, 32, 4, 8, 16, 32}
	for _, strict := range []bool{false, true} {
		id := "revocation-unreachable-warns"
		if strict {
			id = "revocation-strict-fails"
		}
		putScript(id, "revocation", unreachable(artUnreachable))
		c = newCase(id, "revocation", "http")
		c.HTTPScript = strp(id)
		c.Request.Spelling = "26.13.4.1"
		c.Request.Frozen = true
		c.Request.RequireRevocationCheck = strict
		c.Setup.Lock = strp("revocation-unreachable")
		c.Setup.Cache = "revocation-unreachable-installed"
		c.Expect.Sleeps = retryTable
		c.Expect.Requests.Max = intp(10)
		if strict {
			refused(&c, "CHTYPES_SOURCE_UNREACHABLE", notCheckedWarning(artUnreachable.ManifestDesc.Digest))
		} else {
			want(&c, artUnreachable)
			c.Expect.Warnings = []string{notCheckedWarning(artUnreachable.ManifestDesc.Digest)}
			c.Expect.WarningsMax = intp(1)
		}
		cases = append(cases, c)
	}

	// --- 5. --lock and update refuse to pin a revoked build (item 1): the
	// host's own build installs, and linux-amd64's, which a lock would pin
	// too, is revoked. No layer of the revoked platform is requested.
	lockArts := map[string]PlatformArtifact{}
	var lockDescs []Descriptor
	for _, pk := range C.platformKeys() {
		a := buildPlatformArtifact(tree, testKey, pk, "26.13.5.1", "20261013.000005", "lts", ArtifactOptions{})
		lockArts[pk] = a
		lockDescs = append(lockDescs, platformDescriptor(a.ManifestDesc, pk))
	}
	lockIndex := canonicalJSON(ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: lockDescs})
	tree.PutManifest("26.13.5.1", ociIndexMediaType, lockIndex)
	tree.PutManifest("26.13.5", ociIndexMediaType, lockIndex)
	advLock := advisoryN(5, "over-accept", "20261019.000005")
	attachRevocation(tree, lockArts["linux-amd64"], advLock, revocationVariant{})
	c = newCase("lock-refuses-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.5.1"
	c.Request.LockWrite = true
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(lockArts["linux-amd64"], advLock))
	noLayer(&c, lockArts["linux-amd64"])
	cases = append(cases, c)

	// update re-resolves both platforms its lock names; the live tag's
	// linux-amd64 build is revoked.
	oldArm := buildPlatformArtifact(tree, testKey, "linux-arm64", "26.13.5.0", "20261012.000005", "lts", ArtifactOptions{})
	oldAmd := buildPlatformArtifact(tree, testKey, "linux-amd64", "26.13.5.0", "20261012.000005", "lts", ArtifactOptions{})
	putInputLock(fs, "revocation-update-before", Lock3{Schema: 3, ABI: fixtureABI, Platforms: []string{"linux-amd64", "linux-arm64"},
		Requests: map[string]map[string]LockPin{"26.13.5": {
			"linux-amd64": pinFor(oldAmd, oldAmd.BundleDigest, ""),
			"linux-arm64": pinFor(oldArm, oldArm.BundleDigest, ""),
		}}})
	c = newCase("update-refuses-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.5"
	c.Request.Update = true
	c.Setup.Lock = strp("revocation-update-before")
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(lockArts["linux-amd64"], advLock))
	noLayer(&c, lockArts["linux-amd64"])
	cases = append(cases, c)

	// --- 6. The 410 on a tag (item 8). detail.revoked an object is REVOKED,
	// one request and no retry, for an exact, a patch and a line tag, and for a
	// platform whose own manifest is not revoked (the complete-set rule);
	// every other 410 is RETIRED, detail.retired's value never read.
	revokedBody, _, revokedText := revoked410Row("contract-revoked")
	retiredBody, retiredMessage := retiredTableRow("contract-body")
	neitherBody, _, _ := revoked410Row("neither")
	notObjectBody, _, _ := revoked410Row("revoked-not-object-string")
	for _, t := range []struct {
		id, spelling, platform, body, code, contains string
	}{
		{"tag-410-revoked", "26.13.6.1", "linux-arm64", revokedBody, "CHTYPES_ARTIFACT_REVOKED", "answered 410 Gone: " + *revokedText},
		{"tag-410-revoked-patch", "26.13.6", "linux-arm64", revokedBody, "CHTYPES_ARTIFACT_REVOKED", "answered 410 Gone: " + *revokedText},
		{"tag-410-revoked-other-platform", "26.13.6.1", "darwin-arm64", revokedBody, "CHTYPES_ARTIFACT_REVOKED", "answered 410 Gone: " + *revokedText},
		{"tag-410-retired-not-revoked", "26.13.6.1", "linux-arm64", retiredBody, "CHTYPES_SOURCE_RETIRED", retiredMessage},
		{"tag-410-neither", "26.13.6.1", "linux-arm64", neitherBody, "CHTYPES_SOURCE_RETIRED", "the repository is retired"},
		{"tag-410-revoked-not-object", "26.13.6.1", "linux-arm64", notObjectBody, "CHTYPES_SOURCE_RETIRED", "the repository is retired"},
	} {
		putScript(t.id, "revocation", []httpRoute{
			{Method: "GET", Path: "/v2/chtypes/v1/manifests/" + t.spelling, Responses: []httpResponse{goneResponse(t.body), {FromTree: true}}},
		})
		c = newCase(t.id, "revocation", "http")
		c.HTTPScript = strp(t.id)
		c.Request.Spelling = t.spelling
		c.Request.Platform = t.platform
		refused(&c, t.code, t.contains)
		c.Expect.Requests.Max = intp(1)
		cases = append(cases, c)
	}
	// The tags these 410s replace exist in the tree, so a client that retried
	// or read past the 410 would visibly succeed.
	art6 := map[string]PlatformArtifact{}
	for _, pk := range C.platformKeys() {
		art6[pk] = buildPlatformArtifact(tree, testKey, pk, "26.13.6.1", "20261013.000006", "lts", ArtifactOptions{})
	}
	tag(tree, "26.13.6.1", art6["linux-amd64"], art6["linux-arm64"], art6["darwin-arm64"])
	tag(tree, "26.13.6", art6["linux-amd64"], art6["linux-arm64"], art6["darwin-arm64"])

	// A line tag that revocation emptied: tags/list omits it, and a direct
	// fetch of it answers the revoked 410 (the third serving behavior).
	listing := NewTree("revocation-listing")
	buildFullIndex(listing, "26.13.9.1", "20261013.000009", "lts", []string{"26.13"})
	buildFullIndex(listing, "26.15.1.1", "20261015.000001", "lts", []string{"26.15"})
	c = newCase("list-tags-omits-revoked", "revocation-listing", "file", "http")
	c.Expect.Tags = []string{"26.13", "26.15"}
	cases = append(cases, c)
	putScript("tag-410-revoked-line", "revocation-listing", []httpRoute{
		{Method: "GET", Path: "/v2/chtypes/v1/manifests/26.14", Responses: []httpResponse{goneResponse(revokedBody)}},
	})
	c = newCase("tag-410-revoked-line", "revocation-listing", "http")
	c.HTTPScript = strp("tag-410-revoked-line")
	c.Request.Spelling = "26.14"
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", "answered 410 Gone: "+*revokedText)
	c.Expect.Requests.Max = intp(1)
	cases = append(cases, c)

	// The complete-set rule's other half: linux-amd64's manifest of a build is
	// revoked and the exact tag answers the revoked 410, but a --frozen pin of
	// linux-arm64's manifest, which is not revoked, keeps verifying. The tag
	// is never requested.
	art7 := map[string]PlatformArtifact{}
	for _, pk := range C.platformKeys() {
		art7[pk] = buildPlatformArtifact(tree, testKey, pk, "26.13.7.1", "20261013.000007", "lts", ArtifactOptions{})
	}
	tag(tree, "26.13.7.1", art7["linux-amd64"], art7["linux-arm64"], art7["darwin-arm64"])
	attachRevocation(tree, art7["linux-amd64"], advisoryN(7, "over-accept", "20261019.000007"), revocationVariant{})
	pinLock("revocation-unrevoked-platform", "26.13.7.1", art7["linux-arm64"])
	putScript("frozen-unrevoked-platform-ok", "revocation", []httpRoute{
		{Method: "GET", Path: "/v2/chtypes/v1/manifests/26.13.7.1", Responses: []httpResponse{goneResponse(revokedBody)}},
	})
	c = newCase("frozen-unrevoked-platform-ok", "revocation", "file", "http")
	c.HTTPScript = strp("frozen-unrevoked-platform-ok")
	c.Request.Spelling = "26.13.7.1"
	c.Request.Frozen = true
	c.Setup.Lock = strp("revocation-unrevoked-platform")
	want(&c, art7["linux-arm64"])
	c.Expect.Requests.NoneMatching = []string{tagPathPattern("26.13.7.1")}
	cases = append(cases, c)

	// A fingerprint alias whose builds are all revoked answers the ordinary 404
	// (MANIFEST_UNKNOWN), never the 410: the client handles it exactly as a
	// missing alias, falls back to the tag, and here refuses the tag's build of
	// another fingerprint as an SDK ahead of the registry (§3).
	notFound := `{"errors":[{"code":"MANIFEST_UNKNOWN","message":"manifest unknown"}]}`
	putScript("dev-alias-all-revoked-404", "dev-alias", []httpRoute{
		{Method: "GET", Path: "/v2/chtypes/v1/manifests/" + aliasOf("26.8", fixtureFingerprintA), Responses: []httpResponse{
			{Status: intp(404), Headers: map[string]string{"Content-Type": "application/json"}, Body: strp(notFound)},
		}},
	})
	c = newCase("dev-alias-all-revoked-404", "dev-alias", "http")
	c.HTTPScript = strp("dev-alias-all-revoked-404")
	c.Request.Spelling = "26.8"
	c.Request.OwnFingerprint = strp(fixtureFingerprintA)
	tagBuild := devAliasTag268["linux-arm64"]
	refused(&c, "CHTYPES_ARTIFACT_UNPUBLISHED", "no published build for this SDK's fingerprint "+fixtureFingerprintA+
		"; newest published on this channel is "+fixtureFingerprintN+" (build "+tagBuild.Predicate.Build+")")
	noLayer(&c, tagBuild)
	cases = append(cases, c)

	// --- 7. The installed-build check (item 12), through the registry-open
	// driver and an ensure that finds the build installed. It reads one
	// listing, at most once per build per process, with its own budget: one
	// attempt per base, no backoff (so no sleep), and one deadline.
	artOpenRevoked := build("26.13.10.1", "20261013.000010")
	advOpen := advisoryN(10, "over-accept", "20261019.000010")
	_, openBundle := attachRevocation(tree, artOpenRevoked, advOpen, revocationVariant{})
	tag(tree, "26.13.10.1", artOpenRevoked)
	installed("revocation-open-revoked", artOpenRevoked, "26.13.10.1")
	openStored := []StoredRevocation{{Manifest: artOpenRevoked.ManifestDesc.Digest, Bundle: openBundle}}

	// Two opens of a revoked build: both refuse, and the second makes no
	// request (the kept outcome, or the record the first stored).
	c = newCase("open-warm-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.10.1"
	opens(&c, 2)
	c.Setup.Cache = "revocation-open-revoked"
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artOpenRevoked, advOpen))
	c.Expect.Requests.Max = intp(3)
	c.Expect.StoredRevocations = openStored
	cases = append(cases, c)

	// Offline skips the check (item 7): a revocation the cache has not stored
	// cannot be known, and the open answers with no request.
	c = newCase("open-warm-offline-zero", "revocation", "file", "http")
	c.Request.Spelling = "26.13.10.1"
	c.Request.Offline = true
	opens(&c, 1)
	c.Setup.Cache = "revocation-open-revoked"
	want(&c, artOpenRevoked)
	c.Expect.Requests.Max = intp(0)
	c.Expect.WarningsMax = intp(0)
	cases = append(cases, c)

	// An ensure that finds the build installed checks it too: no layer is
	// requested, and the revocation is stored.
	c = newCase("fetch-installed-revoked", "revocation", "file", "http")
	c.Request.Spelling = "26.13.10.1"
	c.Setup.Cache = "revocation-open-revoked"
	refused(&c, "CHTYPES_ARTIFACT_REVOKED", revokedMessage(artOpenRevoked, advOpen))
	noLayer(&c, artOpenRevoked)
	c.Expect.StoredRevocations = openStored
	cases = append(cases, c)

	// Two opens of a build nobody revoked: one listing, and nothing else.
	artOpenOK := build("26.13.11.1", "20261013.000011")
	installed("revocation-open-ok", artOpenOK, "26.13.11.1")
	c = newCase("open-warm-checks-once", "revocation", "file", "http")
	c.Request.Spelling = "26.13.11.1"
	opens(&c, 2)
	c.Setup.Cache = "revocation-open-ok"
	want(&c, artOpenOK)
	c.Expect.Requests.Max = intp(1)
	c.Expect.WarningsMax = intp(0)
	cases = append(cases, c)

	// The listing answers 503 on the API and the fallback tag: one attempt
	// each, no sleep, success with the one warning, and a second open in the
	// same process neither requests nor warns again; in strict mode, the
	// transport's error.
	artOpenDown := build("26.13.12.1", "20261013.000012")
	installed("revocation-open-unreachable", artOpenDown, "26.13.12.1")
	for _, o := range []struct {
		id     string
		n      int
		strict bool
	}{
		{"open-warm-unreachable-warns", 1, false},
		{"open-warm-warns-once", 2, false},
		{"open-warm-strict-fails", 1, true},
	} {
		putScript(o.id, "revocation", unreachable(artOpenDown))
		c = newCase(o.id, "revocation", "http")
		c.HTTPScript = strp(o.id)
		c.Request.Spelling = "26.13.12.1"
		c.Request.RequireRevocationCheck = o.strict
		opens(&c, o.n)
		c.Setup.Cache = "revocation-open-unreachable"
		c.Expect.Requests.Max = intp(2)
		if o.strict {
			refused(&c, "CHTYPES_SOURCE_UNREACHABLE", notCheckedWarning(artOpenDown.ManifestDesc.Digest))
		} else {
			want(&c, artOpenDown)
			c.Expect.Warnings = []string{notCheckedWarning(artOpenDown.ManifestDesc.Digest)}
			c.Expect.WarningsMax = intp(1)
		}
		cases = append(cases, c)
	}

	flushTrees(fs, tree, listing)
	for _, l := range layouts {
		l.Flush(fs)
	}
	checkRevocationCases(cases)
	return cases
}

// checkRevocationCases is this file's own cross-check: every case that drives
// the registry-open path is an `open-` case and every `open-` case does, and a
// case that expects REVOKED says what its message must contain.
func checkRevocationCases(cases []Case) {
	for _, c := range cases {
		if (c.Request.Opens != nil) != strings.HasPrefix(c.ID, "open-") {
			panic(fmt.Sprintf("genfixtures: revocation case %q: request.opens and the open- id prefix must go together", c.ID))
		}
		if c.Expect.Code != nil && *c.Expect.Code == "CHTYPES_ARTIFACT_REVOKED" && c.Expect.MessageContains == nil {
			panic(fmt.Sprintf("genfixtures: revocation case %q expects REVOKED with no message_contains", c.ID))
		}
		for _, s := range c.Expect.StoredRevocations {
			if !strings.HasPrefix(s.Bundle, "sha256:") || len(s.Bundle) != len("sha256:")+hex.EncodedLen(32) {
				panic(fmt.Sprintf("genfixtures: revocation case %q names a malformed bundle digest %q", c.ID, s.Bundle))
			}
		}
	}
}
