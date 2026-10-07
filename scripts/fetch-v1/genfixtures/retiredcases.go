package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// retiredcases.go — a 410 Gone is permanent (docs/guides/fetch-v1.md §2, "A
// retired repository"; public issue #571). The registry's operator answers a
// retired repository's every route with 410 and a short error document
// (measured on staging chtypes/v1, 2026-10-07: `cache-control: no-store`,
// `{"errors":[{"code":"DENIED","message":…,"detail":{…}}]}`). A binding
// reports it as CHTYPES_SOURCE_RETIRED with the registry's own message, made
// safe to print, and the URL that answered; never retries it; never asks the
// next base; and, under the dev channel, never falls back from its alias to
// the plain tag.
//
// Every case here is scripted, so its verdict is what the fixture server
// served and logged (requests.max, none_matching), and its expected message
// comes from the one shared table every binding's own unit test reads
// (tests/fixtures/retired-message/cases.json): the conformance case and the
// unit tests cannot disagree about what the sanitized text is.

const retiredMessageTableRelPath = "tests/fixtures/retired-message/cases.json"

// retiredTableCase is one row of the shared table: a 410 body and the message
// the binding's sanitizer must return (nil: no message).
type retiredTableCase struct {
	Name     string  `json:"name"`
	BodyText *string `json:"body_text"`
	BodyHex  *string `json:"body_hex"`
	Message  *string `json:"message"`
}

// retiredTableRow returns the named row, which must carry its body as text
// (a scripted response body is text) and expect a message.
func retiredTableRow(name string) (body, message string) {
	path := filepath.Join(repoRoot(), retiredMessageTableRelPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		panic("genfixtures: reading " + path + ": " + err.Error())
	}
	var doc struct {
		Cases []retiredTableCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		panic("genfixtures: parsing " + path + ": " + err.Error())
	}
	for _, c := range doc.Cases {
		if c.Name != name {
			continue
		}
		if c.BodyText == nil || c.Message == nil {
			panic(fmt.Sprintf("genfixtures: %s row %q must carry body_text and a message", retiredMessageTableRelPath, name))
		}
		return *c.BodyText, *c.Message
	}
	panic(fmt.Sprintf("genfixtures: %s has no row %q", retiredMessageTableRelPath, name))
}

// goneResponse is the registry's 410 for a retired repository: its own
// headers, and body as given ("" sends none).
func goneResponse(body string) httpResponse {
	r := httpResponse{Status: intp(410), Headers: map[string]string{"Cache-Control": "no-store"}}
	if body != "" {
		r.Headers["Content-Type"] = "application/json"
		r.Body = strp(body)
	}
	return r
}

func buildRetiredCases(fs *FileSet) []Case {
	var cases []Case

	contractBody, contractMessage := retiredTableRow("contract-body")
	sanitizedBody, sanitizedMessage := retiredTableRow("sanitized-and-capped")

	// One tree that serves a complete build, so a client that retried or
	// moved on would visibly succeed; and the same build under a second
	// repository path, the mirror a no-fallthrough case names as its second
	// base. Base 1's 410 is scripted, so it needs no content of its own.
	tree := NewTree("http-retired")
	art := buildFullIndex(tree, "26.12.1.1", "20261201.000001", "lts", []string{"26.12", "26.12.1", "26.12.1.1"})
	live := art["linux-arm64"]
	mirror := newTreeWithRepo("http-retired-mirror", defaultRepoPath+"/mirror")
	buildFullIndex(mirror, "26.12.1.1", "20261201.000001", "lts", []string{"26.12", "26.12.1", "26.12.1.1"})
	flushTrees(fs, tree, mirror)

	manifestPath := "/v2/chtypes/v1/manifests/26.12"

	putScript := func(id, treeName string, routes []httpRoute) {
		validateLocationHeaders(id, routes)
		fs.Put("http/"+id+".json", canonicalJSON(HTTPScript{
			Schema: 1, ID: id, Tree: treeName,
			Routes:             routes,
			SecondOriginRoutes: []httpRoute{},
		}))
	}
	retired := func(id, treeName string) Case {
		c := newCase(id, treeName, "http")
		c.HTTPScript = strp(id)
		c.Request.Spelling = "26.12"
		c.Expect.OK = false
		c.Expect.Code = strp("CHTYPES_SOURCE_RETIRED")
		return c
	}

	// 1. The tag answers 410 with the registry's own document: RETIRED, the
	// registry's message verbatim, one request and no sleep.
	putScript("retired-410-message", "http-retired", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{goneResponse(contractBody), {FromTree: true}}},
	})
	message := retired("retired-410-message", "http-retired")
	message.Expect.MessageContains = strp(contractMessage)
	message.Expect.Requests.Max = intp(1)
	cases = append(cases, message)

	// 2. Base 1 answers 410 and base 2, a mirror under another repository
	// path, serves the whole build: a 410 is never a reason to try the next
	// base, so nothing is requested from the mirror and the call fails.
	putScript("retired-410-no-fallthrough", "http-retired-mirror", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{goneResponse(contractBody)}},
	})
	noFallthrough := retired("retired-410-no-fallthrough", "http-retired-mirror")
	noFallthrough.Request.Bases = []string{"{base}", "{base}/mirror"}
	noFallthrough.Expect.MessageContains = strp(contractMessage)
	noFallthrough.Expect.Requests.Max = intp(1)
	noFallthrough.Expect.Requests.NoneMatching = []string{"GET .*/mirror/"}
	cases = append(cases, noFallthrough)

	// 3. Under the dev channel, the alias answers 410 while the plain tag
	// would serve a build: the alias's 410 is the answer, and the tag is never
	// requested (§3: only a 404 on every base falls back).
	aliasPath := "/v2/chtypes/v1/manifests/" + aliasOf("26.8", fixtureFingerprintA)
	putScript("retired-410-dev-alias", "dev-alias", []httpRoute{
		{Method: "GET", Path: aliasPath, Responses: []httpResponse{goneResponse(contractBody), {FromTree: true}}},
	})
	alias := retired("retired-410-dev-alias", "dev-alias")
	alias.Request.Spelling = "26.8"
	alias.Request.OwnFingerprint = strp(fixtureFingerprintA)
	alias.Expect.MessageContains = strp(contractMessage)
	alias.Expect.Requests.Max = intp(1)
	alias.Expect.Requests.NoneMatching = []string{tagPathPattern("26.8")}
	cases = append(cases, alias)

	// 4. The listing's tags/list answers 410. The id keeps §10's `list-tags-`
	// prefix, which is how every runner knows to call its listing.
	putScript("list-tags-retired-410", "http-retired", []httpRoute{
		{Method: "GET", Path: "/v2/chtypes/v1/tags/list", Responses: []httpResponse{goneResponse(contractBody), {FromTree: true}}},
	})
	listing := retired("list-tags-retired-410", "http-retired")
	listing.Expect.MessageContains = strp(contractMessage)
	listing.Expect.Requests.Max = intp(1)
	cases = append(cases, listing)

	// 5. Everything resolves and verifies, then the layer blob answers 410
	// once and would serve the real bytes after: a retry would succeed, so
	// RETIRED with no sleep is the proof that it was not retried.
	putScript("retired-410-blob", "http-retired", []httpRoute{
		{Method: "GET", Path: "/v2/chtypes/v1/blobs/" + live.LayerDesc.Digest, Responses: []httpResponse{goneResponse(contractBody), {FromTree: true}}},
	})
	blob := retired("retired-410-blob", "http-retired")
	blob.Expect.MessageContains = strp(contractMessage)
	cases = append(cases, blob)

	// 6. The registry's message carries an escape sequence and a bidi
	// override and runs past the cap: the error carries the sanitized, capped
	// text the shared table names.
	putScript("retired-410-sanitized", "http-retired", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{goneResponse(sanitizedBody)}},
	})
	sanitized := retired("retired-410-sanitized", "http-retired")
	sanitized.Expect.MessageContains = strp(sanitizedMessage)
	sanitized.Expect.Requests.Max = intp(1)
	cases = append(cases, sanitized)

	// 7. A 410 with no body: still RETIRED, the error names the URL that
	// answered, and nothing stands in for the missing message.
	putScript("retired-410-no-body", "http-retired", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{goneResponse("")}},
	})
	noBody := retired("retired-410-no-body", "http-retired")
	noBody.Expect.MessageContains = strp("/s-retired-410-no-body/chtypes/v1/manifests/26.12")
	noBody.Expect.MessageExcludes = []string{"null", "undefined", "None", "<nil>"}
	noBody.Expect.Requests.Max = intp(1)
	cases = append(cases, noBody)

	checkRetiredCases(cases)
	return cases
}

// checkRetiredCases is this file's own cross-check: every case expects
// RETIRED with something its error must contain, and none of them names a
// message that still holds a character the sanitizer removes.
func checkRetiredCases(cases []Case) {
	for _, c := range cases {
		if c.Expect.OK || c.Expect.Code == nil || *c.Expect.Code != "CHTYPES_SOURCE_RETIRED" || c.Expect.MessageContains == nil {
			panic(fmt.Sprintf("genfixtures: retired case %q must expect CHTYPES_SOURCE_RETIRED with message_contains", c.ID))
		}
		if strings.ContainsFunc(*c.Expect.MessageContains, func(r rune) bool {
			return r < 0x20 || (r >= 0x7f && r <= 0x9f) || r == 0x200e || r == 0x200f ||
				(r >= 0x202a && r <= 0x202e) || (r >= 0x2066 && r <= 0x2069)
		}) {
			panic(fmt.Sprintf("genfixtures: retired case %q expects a message holding a character the sanitizer removes", c.ID))
		}
	}
}
