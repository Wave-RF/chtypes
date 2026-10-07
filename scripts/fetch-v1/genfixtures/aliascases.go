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
// is that failure, never a fallback. A listing never shows an alias.
//
// Every case here carries request.alias_fingerprint: the runner hands that
// fixture fingerprint to its binding's alias seam (the dev channel takes its
// own from the generated constant instead), or, when it is null, runs the case
// with no alias step at all, as the v1 contract does. Each verdict is what the
// tree served and what the request log shows: an expected manifest is the one
// the served tag or alias names, and "the tag was never requested" is a
// none_matching pattern anchored on the tag's own path.

const aliasSeparator = "--fp-"

// The fixture fingerprints: what a dev SDK under test calls its own (A), one
// no alias in any tree names (B), and the newer fingerprint every floating tag
// here has moved on to (N). Fixture values, never a real ABI's.
var (
	aliasFingerprintA = fakeHex("fetch-v1 fixture abi fingerprint A", 64)
	aliasFingerprintB = fakeHex("fetch-v1 fixture abi fingerprint B", 64)
	aliasFingerprintN = fakeHex("fetch-v1 fixture abi fingerprint N (newer)", 64)
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
	floating268 := buildAliasIndex(tree, "26.8.15.10", "20261005.120000", aliasFingerprintN, []string{"26.8"}, all)
	alias268 := buildAliasIndex(tree, "26.8.15.9", "20260920.120000", aliasFingerprintA, []string{aliasOf("26.8", aliasFingerprintA)}, all)

	// The exact version 26.8.14.2, built twice: the exact tag floats to the
	// newer build (N), its alias for A names the older one.
	buildAliasIndex(tree, "26.8.14.2", "20260915.000000", aliasFingerprintN, []string{"26.8.14.2"}, all)
	aliasExact := buildAliasIndex(tree, "26.8.14.2", "20260901.000000", aliasFingerprintA, []string{aliasOf("26.8.14.2", aliasFingerprintA)}, all)

	// Line 26.7: the alias for A answers 200 with an index that has no
	// linux-arm64 build (no build of this platform at that fingerprint),
	// while the floating tag offers every platform.
	buildAliasIndex(tree, "26.7.3.1", "20260910.000000", aliasFingerprintN, []string{"26.7"}, all)
	buildAliasIndex(tree, "26.7.2.1", "20260901.000000", aliasFingerprintA, []string{aliasOf("26.7", aliasFingerprintA)},
		[]string{"linux-amd64", "darwin-arm64"})

	// Line 26.6: the alias for A names a build of ANOTHER line (26.5), signed
	// as such, while the floating tag is good. The alias is a pointer, never a
	// credential: the signed version outside the request is refused with
	// today's code, and the tag is not tried.
	buildAliasIndex(tree, "26.6.1.1", "20260801.000000", aliasFingerprintN, []string{"26.6"}, all)
	buildAliasIndex(tree, "26.5.9.1", "20260725.000000", aliasFingerprintA, []string{aliasOf("26.6", aliasFingerprintA)}, all)
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
		c.Request.AliasFingerprint = fingerprint
		return c
	}

	// 1. The alias exists and names an older build than the tag: the alias's
	// build is installed, and the tag itself is never requested.
	present := newAliasCase("dev-alias-line-present", "26.8", strp(aliasFingerprintA), "file", "http")
	want(&present, alias268["linux-arm64"])
	present.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, present)

	// The same for an exact version: its alias names the older build.
	exact := newAliasCase("dev-alias-exact-present", "26.8.14.2", strp(aliasFingerprintA), "file", "http")
	want(&exact, aliasExact["linux-arm64"])
	exact.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8.14.2")}
	cases = append(cases, exact)

	// 2. No alias for this fingerprint (a 404): the tag answers, as today.
	absent := newAliasCase("dev-alias-absent", "26.8", strp(aliasFingerprintB), "file", "http")
	want(&absent, floating268["linux-arm64"])
	cases = append(cases, absent)

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
	noPlatform := newAliasCase("dev-alias-platform-absent", "26.7", strp(aliasFingerprintA), "file", "http")
	refused(&noPlatform, "CHTYPES_ARTIFACT_UNPUBLISHED")
	noPlatform.Expect.Requests.Max = intp(1)
	noPlatform.Expect.Requests.NoneMatching = []string{tagPathPattern("26.7")}
	cases = append(cases, noPlatform)

	// The alias names a build whose signed version lies outside the request:
	// refused exactly as from the tag (§4), and the tag is never tried.
	wrongVersion := newAliasCase("dev-alias-signed-version-outside", "26.6", strp(aliasFingerprintA), "file", "http")
	refused(&wrongVersion, "CHTYPES_ARTIFACT_CORRUPT")
	wrongVersion.Expect.Requests.NoneMatching = []string{tagPathPattern("26.6")}
	cases = append(cases, wrongVersion)

	// --- scripted: the alias request's own failures, and more than one base.
	aliasPath := "/v2/chtypes/v1/manifests/" + aliasOf("26.8", aliasFingerprintA)
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
	fault := newAliasCase("dev-alias-5xx-no-fallback", "26.8", strp(aliasFingerprintA), "http")
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
	secondBase := newAliasCase("dev-alias-second-base", "26.8", strp(aliasFingerprintA), "http")
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
	mixed := newAliasCase("dev-alias-5xx-then-404-no-fallback", "26.8", strp(aliasFingerprintA), "http")
	mixed.HTTPScript = strp("dev-alias-5xx-then-404-no-fallback")
	mixed.Request.Bases = []string{"{base}", "{base2}"}
	refused(&mixed, "CHTYPES_SOURCE_UNREACHABLE")
	mixed.Expect.Sleeps = []float64{4, 8, 16, 32}
	mixed.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, mixed)

	// 5. A registry whose tags/list carries aliases: the listing is that list
	// less every alias, unchanged from a registry that serves none. Every
	// non-alias tag here is a two-part line, which every binding's listing
	// keeps, so the four give one answer.
	listTree := NewTree("dev-alias-list")
	buildAliasIndex(listTree, "26.8.15.10", "20261005.120000", aliasFingerprintN, []string{"26.8"}, all)
	buildAliasIndex(listTree, "26.8.15.9", "20260920.120000", aliasFingerprintA, []string{aliasOf("26.8", aliasFingerprintA)}, all)
	buildAliasIndex(listTree, "26.9.1.1", "20261006.000000", aliasFingerprintN, []string{"26.9", aliasOf("26.9", aliasFingerprintN)}, all)
	buildAliasIndex(listTree, "26.9.0.4", "20260930.000000", aliasFingerprintA, []string{aliasOf("26.9", aliasFingerprintA)}, all)
	flushTrees(fs, listTree)
	listing := newCase("list-tags-dev-alias", "dev-alias-list", "file", "http")
	listing.Request.AliasFingerprint = strp(aliasFingerprintA)
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
		fp := c.Request.AliasFingerprint
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
