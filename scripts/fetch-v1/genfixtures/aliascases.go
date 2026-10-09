package main

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// aliascases.go — the dev channel's alias step (docs/guides/fetch-v1.md §3,
// "The dev channel's alias step"). Beside every tag a dev publish writes, the
// registry serves `<tag>--fp-<64 hex>`: the newest build OF THAT FINGERPRINT.
// A dev SDK resolves its own fingerprint's alias first and falls back to the
// tag only when no base has the alias; any other failure of the alias request
// is that failure, never a fallback. A listing never shows an alias. And the
// dev SDK's cache lookups see only records of its own fingerprint: in a cache
// shared with a dev SDK of another fingerprint, the other's builds are never
// returned, never kept by the monotonic rule, and never touched.
//
// Every case here carries request.own_fingerprint: the runner hands that
// fixture fingerprint to its binding's seam (the dev channel takes its own from
// the generated constant instead), or, when it is null, runs the case with no
// alias step and no record filter at all, as the v1 contract does. Each verdict is what the
// tree served and what the request log shows: an expected manifest is the one
// the served tag or alias names, and "the tag was never requested" is a
// none_matching pattern anchored on the tag's own path.

const aliasSeparator = "--fp-"

// The fixture fingerprints: what a dev SDK under test calls its own (A), one
// no alias in any tree names (B), and the newer fingerprint every floating tag
// here has moved on to (N). Fixture values, never a real ABI's.
var (
	fixtureFingerprintA = fakeHex("fetch-v1 fixture abi fingerprint A", 64)
	fixtureFingerprintB = fakeHex("fetch-v1 fixture abi fingerprint B", 64)
	fixtureFingerprintN = fakeHex("fetch-v1 fixture abi fingerprint N (newer)", 64)
)

func aliasOf(tag, fingerprint string) string { return tag + aliasSeparator + fingerprint }

// tagPathPattern is the none_matching pattern for "a GET of exactly this tag",
// anchored at the end so that the tag's own alias (which starts with the tag)
// never matches it. Every binding's runner reads it the same way: Go, Python
// and TypeScript as a regular expression, Rust's fixed vocabulary as a suffix
// ("GET .*<path>$").
func tagPathPattern(tag string) string { return "GET .*/manifests/" + tag + "$" }

// buildAliasIndex builds one platform manifest per platform key, each signed
// with abi_fingerprint sha256:<fingerprint>, wires them into one index and
// registers that index under every tag given.
func buildAliasIndex(t *Tree, version, build, fingerprint string, tags, platformKeys []string) map[string]PlatformArtifact {
	arts := map[string]PlatformArtifact{}
	var descs []Descriptor
	for _, pk := range platformKeys {
		art := buildPlatformArtifact(t, testKey, pk, version, build, "stable", ArtifactOptions{ABIFingerprint: "sha256:" + fingerprint})
		arts[pk] = art
		descs = append(descs, platformDescriptor(art.ManifestDesc, pk))
	}
	content := canonicalJSON(ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: descs})
	t.PutManifest("", ociIndexMediaType, content)
	for _, tag := range tags {
		t.PutManifest(tag, ociIndexMediaType, content)
	}
	return arts
}

func buildAliasCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("dev-alias")
	all := C.platformKeys()

	// Line 26.8: the floating tag names the newest build (fingerprint N); the
	// alias for A names an OLDER build of the same line, signed under A.
	floating268 := buildAliasIndex(tree, "26.8.15.10", "20261005.120000", fixtureFingerprintN, []string{"26.8"}, all)
	alias268 := buildAliasIndex(tree, "26.8.15.9", "20260920.120000", fixtureFingerprintA, []string{aliasOf("26.8", fixtureFingerprintA)}, all)

	// The exact version 26.8.14.2, built twice: the exact tag floats to the
	// newer build (N), its alias for A names the older one.
	buildAliasIndex(tree, "26.8.14.2", "20260915.000000", fixtureFingerprintN, []string{"26.8.14.2"}, all)
	aliasExact := buildAliasIndex(tree, "26.8.14.2", "20260901.000000", fixtureFingerprintA, []string{aliasOf("26.8.14.2", fixtureFingerprintA)}, all)

	// Line 26.7: the alias for A answers 200 with an index that has no
	// linux-arm64 build (no build of this platform at that fingerprint),
	// while the floating tag offers every platform.
	buildAliasIndex(tree, "26.7.3.1", "20260910.000000", fixtureFingerprintN, []string{"26.7"}, all)
	buildAliasIndex(tree, "26.7.2.1", "20260901.000000", fixtureFingerprintA, []string{aliasOf("26.7", fixtureFingerprintA)},
		[]string{"linux-amd64", "darwin-arm64"})

	// Line 26.6: the alias for A names a build of ANOTHER line (26.5), signed
	// as such, while the floating tag is good. The alias is a pointer, never a
	// credential: the signed version outside the request is refused with
	// today's code, and the tag is not tried.
	buildAliasIndex(tree, "26.6.1.1", "20260801.000000", fixtureFingerprintN, []string{"26.6"}, all)
	buildAliasIndex(tree, "26.5.9.1", "20260725.000000", fixtureFingerprintA, []string{aliasOf("26.6", fixtureFingerprintA)}, all)
	// A build of 26.8 signed under B, this SDK's own in the expired-alias case
	// below (public issue #581): older than the tag's, named by no tag or alias
	// of this tree, so it exists only in the cache that case installs it into.
	cachedOwn268 := buildAliasIndex(tree, "26.8.15.8", "20260910.120000", fixtureFingerprintB, nil, all)
	flushTrees(fs, tree)

	want := func(c *Case, art PlatformArtifact) {
		c.Expect.Version = strp(art.Predicate.ClickHouseVersion)
		c.Expect.Build = strp(art.Predicate.Build)
		c.Expect.Manifest = strp(art.ManifestDesc.Digest)
		c.Expect.LibrarySHA256 = strp(art.Predicate.LibrarySHA256)
	}
	refused := func(c *Case, code string) {
		c.Expect.OK = false
		c.Expect.Code = strp(code)
	}
	newAliasCase := func(id, spelling string, fingerprint *string, transports ...string) Case {
		c := newCase(id, "dev-alias", transports...)
		c.Request.Spelling = spelling
		c.Request.OwnFingerprint = fingerprint
		return c
	}

	// 1. The alias exists and names an older build than the tag: the alias's
	// build is installed, and the tag itself is never requested.
	present := newAliasCase("dev-alias-line-present", "26.8", strp(fixtureFingerprintA), "file", "http")
	want(&present, alias268["linux-arm64"])
	present.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, present)

	// The same for an exact version: its alias names the older build.
	exact := newAliasCase("dev-alias-exact-present", "26.8.14.2", strp(fixtureFingerprintA), "file", "http")
	want(&exact, aliasExact["linux-arm64"])
	exact.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8.14.2")}
	cases = append(cases, exact)

	// 2. No alias for this fingerprint (a 404), and the tag names a build of
	// this SDK's own fingerprint (N): the tag answers, as today.
	absent := newAliasCase("dev-alias-absent", "26.8", strp(fixtureFingerprintN), "file", "http")
	want(&absent, floating268["linux-arm64"])
	cases = append(cases, absent)

	// An SDK ahead of the registry (public issue #578): no alias for this
	// SDK's fingerprint (B) on any base, and the tag names a build signed for
	// another (N). Refused as a request no build answers, with a message
	// naming both fingerprints and the tag build's id, from the signed
	// statement: the layer is never requested, so nothing is installed.
	aheadMessage := "no published build for this SDK's fingerprint " + fixtureFingerprintB +
		"; newest published on this channel is " + fixtureFingerprintN + " (build " + floating268["linux-arm64"].Predicate.Build + ")"
	ahead := newAliasCase("fp-ahead-tag-other-fp", "26.8", strp(fixtureFingerprintB), "file", "http")
	refused(&ahead, "CHTYPES_ARTIFACT_UNPUBLISHED")
	ahead.Expect.MessageContains = strp(aheadMessage)
	ahead.Expect.Requests.NoneMatching = []string{"GET .*/blobs/" + floating268["linux-arm64"].LayerDesc.Digest}
	cases = append(cases, ahead)

	// 6. The fetch-v1 channel (no alias fingerprint) never requests an alias,
	// from a registry that serves them: the tag answers, and no request in the
	// log names an alias.
	never := newAliasCase("dev-alias-v1-channel-never", "26.8", nil, "file", "http")
	want(&never, floating268["linux-arm64"])
	never.Expect.Requests.NoneMatching = []string{aliasSeparator}
	cases = append(cases, never)

	// The alias answers 200, but its index has no build of this platform at
	// that fingerprint: the error the tag path gives for a missing platform,
	// and no fallback (the tag names another fingerprint, which this SDK
	// would refuse anyway). One request: the alias.
	noPlatform := newAliasCase("dev-alias-platform-absent", "26.7", strp(fixtureFingerprintA), "file", "http")
	refused(&noPlatform, "CHTYPES_ARTIFACT_UNPUBLISHED")
	noPlatform.Expect.Requests.Max = intp(1)
	noPlatform.Expect.Requests.NoneMatching = []string{tagPathPattern("26.7")}
	cases = append(cases, noPlatform)

	// The alias names a build whose signed version lies outside the request:
	// refused exactly as from the tag (§4), and the tag is never tried.
	wrongVersion := newAliasCase("dev-alias-signed-version-outside", "26.6", strp(fixtureFingerprintA), "file", "http")
	refused(&wrongVersion, "CHTYPES_ARTIFACT_CORRUPT")
	wrongVersion.Expect.Requests.NoneMatching = []string{tagPathPattern("26.6")}
	cases = append(cases, wrongVersion)

	// --- scripted: the alias request's own failures, and more than one base.
	aliasPath := "/v2/chtypes/v1/manifests/" + aliasOf("26.8", fixtureFingerprintA)
	tagPath := "/v2/chtypes/v1/manifests/26.8"
	putScript := func(id string, routes, secondOriginRoutes []httpRoute) {
		validateLocationHeaders(id, routes)
		validateLocationHeaders(id, secondOriginRoutes)
		fs.Put("http/"+id+".json", canonicalJSON(HTTPScript{
			Schema: 1, ID: id, Tree: "dev-alias",
			Routes:             routes,
			SecondOriginRoutes: secondOriginRoutesOrEmpty(secondOriginRoutes),
		}))
	}
	fiveFailures := []httpResponse{{Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}}

	// 3. The alias request fails with a 5xx on every attempt: the error the
	// tag would get for that fault (the retry table, then
	// CHTYPES_SOURCE_UNREACHABLE), and no fallback: five requests, all of
	// them the alias.
	putScript("dev-alias-5xx-no-fallback", []httpRoute{{Method: "GET", Path: aliasPath, Responses: fiveFailures}}, nil)
	fault := newAliasCase("dev-alias-5xx-no-fallback", "26.8", strp(fixtureFingerprintA), "http")
	fault.HTTPScript = strp("dev-alias-5xx-no-fallback")
	refused(&fault, "CHTYPES_SOURCE_UNREACHABLE")
	fault.Expect.Sleeps = []float64{4, 8, 16, 32}
	fault.Expect.Requests.Max = intp(5)
	fault.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, fault)

	// 4. The alias is absent on the first base and present on the second:
	// the alias wins, and the first base's tag is never requested.
	putScript("dev-alias-second-base", []httpRoute{
		{Method: "GET", Path: aliasPath, Responses: []httpResponse{{Status: intp(404)}}},
	}, []httpRoute{
		{Method: "GET", Path: aliasPath, Responses: []httpResponse{{FromTree: true}}},
	})
	secondBase := newAliasCase("dev-alias-second-base", "26.8", strp(fixtureFingerprintA), "http")
	secondBase.HTTPScript = strp("dev-alias-second-base")
	secondBase.Request.Bases = []string{"{base}", "{base2}"}
	want(&secondBase, alias268["linux-arm64"])
	secondBase.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, secondBase)

	// Only a 404 on EVERY base is "not found": the first base fails the alias
	// with a 5xx and the second answers it 404, so the request fails with the
	// first base's fault and the tag is never requested on either base (both
	// bases serve the tag, so a fallback would visibly succeed).
	putScript("dev-alias-5xx-then-404-no-fallback", []httpRoute{
		{Method: "GET", Path: aliasPath, Responses: fiveFailures},
	}, []httpRoute{
		{Method: "GET", Path: aliasPath, Responses: []httpResponse{{Status: intp(404)}}},
		{Method: "GET", Path: tagPath, Responses: []httpResponse{{FromTree: true}}},
	})
	mixed := newAliasCase("dev-alias-5xx-then-404-no-fallback", "26.8", strp(fixtureFingerprintA), "http")
	mixed.HTTPScript = strp("dev-alias-5xx-then-404-no-fallback")
	mixed.Request.Bases = []string{"{base}", "{base2}"}
	refused(&mixed, "CHTYPES_SOURCE_UNREACHABLE")
	mixed.Expect.Sleeps = []float64{4, 8, 16, 32}
	mixed.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, mixed)

	// --- a cache shared with a dev SDK of another fingerprint. Two layouts:
	// the newest build of 26.8 (fingerprint N, a newer dev SDK's) installed
	// alone, and that beside this SDK's own (fingerprint A, older). This SDK
	// sees only its own record, and the other stays byte for byte as it was
	// (records_intact: the runner snapshots its unpacked/ directory before
	// the call and compares after).
	foreign := floating268["linux-arm64"]
	own := alias268["linux-arm64"]
	foreignOnly := NewLayout("dev-alias-foreign-newer")
	copyArtifactIntoLayout(foreignOnly, tree, foreign, "26.8")
	foreignOnly.SetInstalled(foreign.ManifestDesc.Digest)
	ownBeside := NewLayout("dev-alias-own-beside-foreign-newer")
	copyArtifactIntoLayout(ownBeside, tree, own, "26.8")
	copyArtifactIntoLayout(ownBeside, tree, foreign, "26.8")
	ownBeside.SetInstalled(own.ManifestDesc.Digest, foreign.ManifestDesc.Digest)
	foreignOnly.Flush(fs)
	ownBeside.Flush(fs)

	// (a) Only the other fingerprint's newer build is installed: online, this
	// SDK fetches its own alias's build (the only manifest naming it is the
	// alias's), and the monotonic rule does not keep the foreign one over it.
	fetchesOwn := newAliasCase("dev-alias-foreign-newer-fetches-own", "26.8", strp(fixtureFingerprintA), "file", "http")
	fetchesOwn.Setup.Cache = "dev-alias-foreign-newer"
	want(&fetchesOwn, own)
	fetchesOwn.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	fetchesOwn.Expect.RecordsIntact = []string{foreign.ManifestDesc.Digest}
	cases = append(cases, fetchesOwn)

	// ... and the lookup alone (what a registry open asks first) sees nothing.
	foreignMiss := newAliasCase("resolve-installed-dev-alias-foreign-newer", "26.8", strp(fixtureFingerprintA), "file", "http")
	foreignMiss.Setup.Cache = "dev-alias-foreign-newer"
	refused(&foreignMiss, "CHTYPES_ARTIFACT_MISSING")
	foreignMiss.Expect.Requests.Max = intp(0)
	foreignMiss.Expect.RecordsIntact = []string{foreign.ManifestDesc.Digest}
	cases = append(cases, foreignMiss)

	// (b) This SDK's own build is installed beside the other's newer one: the
	// lookup and the offline answer are its own, with no request at all.
	ownLookup := newAliasCase("resolve-installed-dev-alias-own-beside-foreign-newer", "26.8", strp(fixtureFingerprintA), "file", "http")
	ownLookup.Setup.Cache = "dev-alias-own-beside-foreign-newer"
	want(&ownLookup, own)
	ownLookup.Expect.Requests.Max = intp(0)
	ownLookup.Expect.RecordsIntact = []string{foreign.ManifestDesc.Digest}
	cases = append(cases, ownLookup)

	ownOffline := newAliasCase("dev-alias-own-beside-foreign-newer-offline", "26.8", strp(fixtureFingerprintA), "file", "http")
	ownOffline.Setup.Cache = "dev-alias-own-beside-foreign-newer"
	ownOffline.Request.Offline = true
	want(&ownOffline, own)
	ownOffline.Expect.Requests.Max = intp(0)
	ownOffline.Expect.RecordsIntact = []string{foreign.ManifestDesc.Digest}
	cases = append(cases, ownOffline)

	// (c) The tag's build of another fingerprint (N) is already installed, and
	// this SDK's (B) has no alias: the same refusal as with an empty cache,
	// never "already installed", and the install stays as it was.
	aheadCached := newAliasCase("fp-ahead-cached-other-fp", "26.8", strp(fixtureFingerprintB), "file", "http")
	aheadCached.Setup.Cache = "dev-alias-foreign-newer"
	refused(&aheadCached, "CHTYPES_ARTIFACT_UNPUBLISHED")
	aheadCached.Expect.MessageContains = strp(aheadMessage)
	aheadCached.Expect.Requests.NoneMatching = []string{"GET .*/blobs/" + foreign.LayerDesc.Digest}
	aheadCached.Expect.RecordsIntact = []string{foreign.ManifestDesc.Digest}
	cases = append(cases, aheadCached)

	// (d) The alias has expired but the cache still holds a build of this
	// SDK's own fingerprint (B) for the request (public issue #581): the same
	// refusal, same code, and the message adds that the cached build is still
	// usable by open. Nothing is installed or changed; records_intact holds on
	// the cached build.
	ownCached := cachedOwn268["linux-arm64"]
	cachedOwnLayout := NewLayout("dev-alias-own-cached-alias-expired")
	copyArtifactIntoLayout(cachedOwnLayout, tree, ownCached, "26.8")
	cachedOwnLayout.SetInstalled(ownCached.ManifestDesc.Digest)
	cachedOwnLayout.Flush(fs)
	aheadOwnCached := newAliasCase("fp-ahead-cached-own-fp", "26.8", strp(fixtureFingerprintB), "file", "http")
	aheadOwnCached.Setup.Cache = "dev-alias-own-cached-alias-expired"
	refused(&aheadOwnCached, "CHTYPES_ARTIFACT_UNPUBLISHED")
	aheadOwnCached.Expect.MessageContains = strp(aheadMessage + "; a cached build for this fingerprint (build " +
		ownCached.Predicate.Build + ") is still usable by open; upgrade the SDK to get newer builds")
	aheadOwnCached.Expect.Requests.NoneMatching = []string{"GET .*/blobs/" + floating268["linux-arm64"].LayerDesc.Digest}
	aheadOwnCached.Expect.RecordsIntact = []string{ownCached.ManifestDesc.Digest}
	cases = append(cases, aheadOwnCached)

	// The control: with no own fingerprint (the fetch-v1 channel), the same
	// cache answers with the newest build of the line, whoever's it is.
	v1Lookup := newAliasCase("resolve-installed-dev-alias-v1-channel-sees-all", "26.8", nil, "file", "http")
	v1Lookup.Setup.Cache = "dev-alias-own-beside-foreign-newer"
	want(&v1Lookup, foreign)
	v1Lookup.Expect.Requests.Max = intp(0)
	cases = append(cases, v1Lookup)

	// 5. A registry whose tags/list carries aliases: the listing is that list
	// less every alias, unchanged from a registry that serves none. Every
	// non-alias tag here is a two-part line, which every binding's listing
	// keeps, so the four give one answer.
	listTree := NewTree("dev-alias-list")
	buildAliasIndex(listTree, "26.8.15.10", "20261005.120000", fixtureFingerprintN, []string{"26.8"}, all)
	buildAliasIndex(listTree, "26.8.15.9", "20260920.120000", fixtureFingerprintA, []string{aliasOf("26.8", fixtureFingerprintA)}, all)
	buildAliasIndex(listTree, "26.9.1.1", "20261006.000000", fixtureFingerprintN, []string{"26.9", aliasOf("26.9", fixtureFingerprintN)}, all)
	buildAliasIndex(listTree, "26.9.0.4", "20260930.000000", fixtureFingerprintA, []string{aliasOf("26.9", fixtureFingerprintA)}, all)
	flushTrees(fs, listTree)
	listing := newCase("list-tags-dev-alias", "dev-alias-list", "file", "http")
	listing.Request.OwnFingerprint = strp(fixtureFingerprintA)
	listing.Expect.Tags = listingWithoutAliases(listTree)
	cases = append(cases, listing)

	checkAliasCases(cases)
	return cases
}

var twoPartLine = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

// listingWithoutAliases is the tree's own tags/list less every alias, in
// version order. It panics unless every tag it keeps is a two-part line, the
// one shape every binding's listing keeps (Go lists lines only).
func listingWithoutAliases(t *Tree) []string {
	var out []string
	for _, tag := range t.tagOrder {
		if strings.Contains(tag, aliasSeparator) {
			continue
		}
		if !twoPartLine.MatchString(tag) {
			panic(fmt.Sprintf("genfixtures: tree %s lists %q, which is not a two-part line: the four bindings' listings would differ", t.Name, tag))
		}
		out = append(out, tag)
	}
	sort.Slice(out, func(i, j int) bool { return versionLessGen(out[i], out[j]) })
	if len(out) == 0 || len(out) == len(t.tagOrder) {
		panic(fmt.Sprintf("genfixtures: tree %s's tags/list must hold both lines and aliases", t.Name))
	}
	return out
}

func versionLessGen(a, b string) bool {
	ap, bp := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(ap) && i < len(bp); i++ {
		x, _ := strconv.Atoi(ap[i])
		y, _ := strconv.Atoi(bp[i])
		if x != y {
			return x < y
		}
	}
	return len(ap) < len(bp)
}

// checkAliasCases is this file's own cross-check: every alias a case's tree is
// asked for is at most 128 characters (OCI's tag limit), and every alias case
// that expects a build expects the build its alias (or, with no alias, its
// tag) names in the tree, not one computed some other way.
func checkAliasCases(cases []Case) {
	for _, c := range cases {
		fp := c.Request.OwnFingerprint
		if fp != nil {
			if n := len(aliasOf(c.Request.Spelling, *fp)); n > 128 {
				panic(fmt.Sprintf("genfixtures: case %q's alias is %d characters, over OCI's 128", c.ID, n))
			}
		}
		if c.Expect.Manifest == nil || c.HTTPScript != nil {
			continue
		}
		tree := allTrees[c.Tree]
		ref := c.Request.Spelling
		if fp != nil {
			if _, ok := tree.tags[aliasOf(ref, *fp)]; ok {
				ref = aliasOf(ref, *fp)
			}
		}
		indexDigest, ok := tree.tags[ref]
		if !ok {
			panic(fmt.Sprintf("genfixtures: case %q resolves %q, which tree %s does not serve", c.ID, ref, c.Tree))
		}
		if !strings.Contains(string(tree.manifestByDigest[indexDigest]), *c.Expect.Manifest) {
			panic(fmt.Sprintf("genfixtures: case %q expects %s, which the index tree %s serves at %q does not name", c.ID, *c.Expect.Manifest, c.Tree, ref))
		}
	}
}
