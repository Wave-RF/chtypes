package main

import (
	"fmt"
	"strings"
)

// httpcases.go — the HTTP-transport-only case group: retries, redirects,
// the anonymous token flow, download-token auth, mirror failover
// (docs/guides/fetch-v1.md §2, §7; plan §3.2). Every http_script here
// scripts the response(s) to exactly one route — `GET
// /v2/chtypes/v1/manifests/<tag>` — and falls through to the "http-basic"
// tree for everything else (the layer/bundle/config blobs a successful
// retry eventually needs). scripts/fetch-v1/server.py serves the schema's
// four response shapes generically; "anon-token-flow" is the one script id
// server.py also special-cases (the schema has no way to express "gate a
// response on a header", so the real bearer-token exchange is server.py's
// own logic, documented there and in docs/guides/fetch-v1.md §10).

func buildHTTPCases(fs *FileSet) []Case {
	var cases []Case
	tree := NewTree("http-basic")
	art := buildFullIndex(tree, "26.12.1.1", "20261201.000001", "lts", []string{"26.12", "26.12.1", "26.12.1.1"})
	live := art["linux-arm64"]
	flushTrees(fs, tree)

	manifestPath := "/v2/chtypes/v1/manifests/26.12"

	putScript := func(id string, routes []httpRoute, secondOriginRoutes []httpRoute) {
		validateLocationHeaders(id, routes)
		validateLocationHeaders(id, secondOriginRoutes)
		fs.Put("http/"+id+".json", canonicalJSON(HTTPScript{
			Schema: 1, ID: id, Tree: "http-basic",
			Routes:             routes,
			SecondOriginRoutes: secondOriginRoutesOrEmpty(secondOriginRoutes),
		}))
	}

	addCase := func(id string, sleeps []float64, ok bool, code string, extraRequests ...string) Case {
		c := newCase(id, "http-basic", "http")
		c.HTTPScript = strp(id)
		c.Request.Spelling = "26.12"
		c.Expect.Sleeps = sleeps
		c.Expect.OK = ok
		if ok {
			c.Expect.Version = strp(live.Predicate.ClickHouseVersion)
			c.Expect.Build = strp(live.Predicate.Build)
			c.Expect.Manifest = strp(live.ManifestDesc.Digest)
			c.Expect.LibrarySHA256 = strp(live.Predicate.LibrarySHA256)
		} else {
			c.Expect.Code = strp(code)
		}
		c.Expect.Requests.NoneMatching = append([]string{}, extraRequests...)
		return c
	}

	// --- retry-5xx: two 503s, then the tree's real content. sleeps [4,8].
	putScript("retry-5xx", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(503)}, {Status: intp(503)}, {FromTree: true},
		}},
	}, nil)
	cases = append(cases, addCase("retry-5xx", []float64{4, 8}, true, ""))

	// --- retry-408: one 408, then success. sleep [4].
	putScript("retry-408", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(408)}, {FromTree: true},
		}},
	}, nil)
	cases = append(cases, addCase("retry-408", []float64{4}, true, ""))

	// --- retry-429-no-ra: a 429 with no Retry-After falls back to the
	// normal backoff table. sleep [4].
	putScript("retry-429-no-ra", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(429)}, {FromTree: true},
		}},
	}, nil)
	cases = append(cases, addCase("retry-429-no-ra", []float64{4}, true, ""))

	// --- retry-after-seconds: Retry-After: 2 overrides the table's 4s.
	putScript("retry-after-seconds", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(429), Headers: map[string]string{"Retry-After": "2"}}, {FromTree: true},
		}},
	}, nil)
	cases = append(cases, addCase("retry-after-seconds", []float64{2}, true, ""))

	// --- retry-after-date: Retry-After as an HTTP-date, 3s after the
	// response's own Date header (server.py computes the date itself; see
	// its own comment on this script id).
	putScript("retry-after-date", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(503), Headers: map[string]string{"Retry-After": "@date+3"}}, {FromTree: true},
		}},
	}, nil)
	cases = append(cases, addCase("retry-after-date", []float64{3}, true, ""))

	// --- retry-after-over-budget: Retry-After names more than the
	// remaining retry budget could ever sleep through; refused rather than
	// shortened, zero sleeps.
	putScript("retry-after-over-budget", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(429), Headers: map[string]string{"Retry-After": "999999"}},
		}},
	}, nil)
	cases = append(cases, addCase("retry-after-over-budget", []float64{}, false, "CHTYPES_SOURCE_UNREACHABLE"))

	// --- retry-exhausted: every one of retry.attempts attempts fails;
	// sleeps [4,8,16,32] (one fewer than attempts), then give up.
	putScript("retry-exhausted", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)},
		}},
	}, nil)
	cases = append(cases, addCase("retry-exhausted", []float64{4, 8, 16, 32}, false, "CHTYPES_SOURCE_UNREACHABLE"))

	// --- no-retry-400: a plain 400 is not in retry_statuses; refused
	// immediately, zero sleeps.
	putScript("no-retry-400", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(400)}}},
	}, nil)
	cases = append(cases, addCase("no-retry-400", []float64{}, false, "CHTYPES_SOURCE_UNREACHABLE"))

	// --- no-retry-tag-404: a genuine "not published" 404 on the tag is
	// UNPUBLISHED, never retried as a transient error.
	putScript("no-retry-tag-404", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(404)}}},
	}, nil)
	cases = append(cases, addCase("no-retry-tag-404", []float64{}, false, "CHTYPES_ARTIFACT_UNPUBLISHED"))

	// --- anon-token-flow: 401 + WWW-Authenticate, a token GET, then the
	// retry carries the token. server.py's own logic (not the generic
	// response-sequence engine) gates the second manifest GET on the
	// Authorization header it issued.
	putScript("anon-token-flow", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(401), Headers: map[string]string{
				"WWW-Authenticate": `Bearer realm="http://{server}/s-anon-token-flow/chtypes/v1/token"`,
			}},
			{FromTree: true}, // served only once server.py has seen a matching Authorization header
		}},
		{Method: "GET", Path: "/v2/chtypes/v1/token", Responses: []httpResponse{
			{Status: intp(200), Headers: map[string]string{"Content-Type": "application/json"}, BodyFromTree: false},
		}},
	}, nil)
	cases = append(cases, addCase("anon-token-flow", []float64{}, true, ""))

	// --- download-token-sent / 401 / 403: CHTYPES_DOWNLOAD_TOKEN sent as a
	// static bearer; the server checks it directly (server.py logic again,
	// same reason as anon-token-flow: a gate on a header value).
	putScript("download-token-sent", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{FromTree: true}}},
	}, nil)
	downloadTokenSent := addCase("download-token-sent", []float64{}, true, "")
	downloadTokenSent.Env = map[string]string{"CHTYPES_DOWNLOAD_TOKEN": "test-static-token"}
	cases = append(cases, downloadTokenSent)

	putScript("download-token-401", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(401)}}},
	}, nil)
	downloadToken401 := addCase("download-token-401", []float64{}, false, "CHTYPES_SOURCE_UNAUTHORIZED")
	downloadToken401.Env = map[string]string{"CHTYPES_DOWNLOAD_TOKEN": "wrong-token"}
	cases = append(cases, downloadToken401)

	putScript("download-token-403", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(403)}}},
	}, nil)
	downloadToken403 := addCase("download-token-403", []float64{}, false, "CHTYPES_SOURCE_FORBIDDEN")
	downloadToken403.Env = map[string]string{"CHTYPES_DOWNLOAD_TOKEN": "forbidden-token"}
	cases = append(cases, downloadToken403)

	// --- redirect-cross-origin-drops-auth: a redirect to the second origin
	// must not carry Authorization across it.
	//
	// The Location template below was wrong twice over (lane 0B, measured
	// by the Go and Python fetch lanes against cases.json on v1,
	// 2026-10-02): `{second-origin}` already expands to a full
	// `http://host:port` (server.py's own docstring and selftest fixture
	// spell it with no scheme prefix of their own), so prepending a literal
	// `http://` doubled the scheme; and the path was missing the `/v2`
	// every client-facing URL in this server carries ahead of
	// `s-<case-id>` (server.py's own routing comment: a request path is
	// always `/v2/s-<case-id>/...`).
	putScript("redirect-cross-origin-drops-auth", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{
			{Status: intp(302), Headers: map[string]string{"Location": "{second-origin}/v2/s-redirect-cross-origin-drops-auth/chtypes/v1/manifests/26.12"}},
		}},
	}, []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{FromTree: true}}},
	})
	redirectDrops := addCase("redirect-cross-origin-drops-auth", []float64{}, true, "")
	redirectDrops.Env = map[string]string{"CHTYPES_DOWNLOAD_TOKEN": "test-static-token"}
	redirectDrops.Expect.Requests.AuthOnSecondOrigin = false
	cases = append(cases, redirectDrops)

	// --- redirect-limit: more redirect hops than limits.max_redirects.
	//
	// Two bugs here, measured by the Go and Python fetch lanes against
	// cases.json on v1 (2026-10-02): `"http://{origin}" + next` doubled the
	// scheme (`{origin}` already carries it), and `next` — the script
	// ROUTE's own path, which is matched AFTER server.py strips
	// `/v2/s-<case-id>/` from the incoming request — was reused verbatim
	// as the Location HEADER value a real client follows, which never had
	// that prefix stripped from it and so 404s on hop 2 (the router cannot
	// find `s-redirect-limit` in it). A route's `.Path` and a Location
	// header's value are two different representations of the same hop —
	// one server-relative, one client-facing absolute — and must not share
	// one variable.
	const redirectLimitID = "redirect-limit"
	var loopRoutes []httpRoute
	scriptPath := manifestPath // "/v2/chtypes/v1/manifests/26.12" — what server.py's router matches on
	for i := 0; i < C.Limits.MaxRedirects+2; i++ {
		nextScriptPath := manifestPath + "-hop" + itoa(i+1)
		nextClientPath := "/v2/s-" + redirectLimitID + nextScriptPath[len("/v2"):]
		loopRoutes = append(loopRoutes, httpRoute{Method: "GET", Path: scriptPath, Responses: []httpResponse{
			{Status: intp(302), Headers: map[string]string{"Location": "{origin}" + nextClientPath}},
		}})
		scriptPath = nextScriptPath
	}
	putScript(redirectLimitID, loopRoutes, nil)
	cases = append(cases, addCase(redirectLimitID, []float64{}, false, "CHTYPES_SOURCE_UNREACHABLE"))

	// --- mirror-failover-5xx / mirror-failover-digest-404: base[0] fails
	// (transiently, or by 404), base[1] serves the real content.
	//
	// Both originally used `bases: ["{base}", "{base}"]` — two IDENTICAL
	// URLs. Measured by the Go and Python fetch lanes against cases.json on
	// v1 (2026-10-02): the script cursor was keyed by (case id, method,
	// path) alone, so "failing over" to base[1] re-read the SAME,
	// already-exhausted cursor as base[0] and kept getting the scripted
	// failure's own last entry repeated forever — a mirror that never
	// succeeds is not a failover case. `server.py`'s cursor now also keys
	// on origin (see its own comment), and `{base2}` (decided here, lane
	// 0B — documented in docs/guides/fetch-v1.md) is a SECOND, genuinely
	// distinct base for exactly this: the http transport's own second
	// origin (`server.py`'s LISTENING line's second port), same case id
	// and repository path, with its own `second_origin_routes` script
	// entry that succeeds immediately.
	putScript("mirror-failover-5xx", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}, {Status: intp(503)}}},
	}, []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{FromTree: true}}},
	})
	failoverCase := addCase("mirror-failover-5xx", []float64{4, 8, 16, 32}, true, "")
	failoverCase.Request.Bases = []string{"{base}", "{base2}"}
	cases = append(cases, failoverCase)

	putScript("mirror-failover-digest-404", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Status: intp(404)}}},
	}, []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{FromTree: true}}},
	})
	failoverDigest := addCase("mirror-failover-digest-404", []float64{}, true, "")
	failoverDigest.Request.Bases = []string{"{base}", "{base2}"}
	cases = append(cases, failoverDigest)

	// mirror-no-failover-on-verify-fail: base[0] serves a TAMPERED layer
	// (verifies the manifest/trust fine, but the layer digest mismatches);
	// base[1] (never contacted) would serve the honest bytes. A
	// verification failure is never a reason to shop for another base.
	verifyFailTree := NewTree("http-verify-fail")
	vfArt := buildPlatformArtifact(verifyFailTree, testKey, "linux-arm64", "26.12.2.1", "20261201.000002", "lts", ArtifactOptions{LibraryContentSeed: "mirror-no-failover"})
	tamperedBytes := append([]byte(nil), mustGetBlob(verifyFailTree, vfArt.LayerDesc.Digest)...)
	tamperedBytes[0] ^= 0xFF
	verifyFailTree.PutTamperedBlobByDigest(vfArt.LayerDesc.Digest, tamperedBytes)
	vfIdx := ImageIndex{SchemaVersion: 2, MediaType: ociIndexMediaType, Manifests: []Descriptor{platformDescriptor(vfArt.ManifestDesc, "linux-arm64")}}
	verifyFailTree.PutManifest("26.12.2.1", ociIndexMediaType, canonicalJSON(vfIdx))
	flushTrees(fs, verifyFailTree)
	noFailover := newCase("mirror-no-failover-on-verify-fail", "http-verify-fail", "http")
	noFailover.Request.Spelling = "26.12.2.1"
	noFailover.Request.Bases = []string{"{base}", "{base}-never-contacted"}
	noFailover.Expect.OK = false
	noFailover.Expect.Code = strp("CHTYPES_ARTIFACT_CORRUPT")
	noFailover.Expect.Requests.NoneMatching = []string{"never-contacted"}
	cases = append(cases, noFailover)

	// --- connection-refused-then-next-base: base[0] is a port nothing
	// listens on; base[1] is the real server.
	refused := newCase("connection-refused-then-next-base", "http-basic", "http")
	refused.Request.Spelling = "26.12"
	refused.Request.Bases = []string{"http://127.0.0.1:1/chtypes/v1", "{base}"}
	refused.Expect.OK = true
	refused.Expect.Version = strp(live.Predicate.ClickHouseVersion)
	refused.Expect.Build = strp(live.Predicate.Build)
	refused.Expect.Manifest = strp(live.ManifestDesc.Digest)
	refused.Expect.LibrarySHA256 = strp(live.Predicate.LibrarySHA256)
	cases = append(cases, refused)

	// --- stall-timeout-retried: the first attempt hangs past the
	// test-only idle timeout; the retry succeeds.
	putScript("stall-timeout-retried", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{Stall: true}, {FromTree: true}}},
	}, nil)
	cases = append(cases, addCase("stall-timeout-retried", []float64{4}, true, ""))

	// --- manifest-accept-header: every manifest GET, by tag AND by digest,
	// must carry `Accept: application/vnd.oci.image.index.v1+json,
	// application/vnd.oci.image.manifest.v1+json` (the distribution spec's
	// SHOULD; a mirror may refuse to negotiate without it). No http_script:
	// server.py gates BOTH the tag resolve and the by-digest manifest GET
	// on this header directly (see its own comment on this case id), so a
	// client that omits it gets a 400 instead of silently passing. The
	// script below is never consulted for its own responses (server.py's
	// gate runs first and, once the header checks out, falls through to
	// the tree directly) — it exists only so this case has the same
	// discoverable http/<id>.json shape every other http-transport case
	// does.
	putScript("manifest-accept-header", []httpRoute{
		{Method: "GET", Path: manifestPath, Responses: []httpResponse{{FromTree: true}}},
	}, nil)
	cases = append(cases, addCase("manifest-accept-header", []float64{}, true, ""))

	return cases
}

// validateLocationHeaders is genfixtures' own self-check for the exact
// defect class redirect-cross-origin-drops-auth and redirect-limit both
// shipped with (measured, the Go and Python fetch lanes against cases.json
// on v1, 2026-10-02): a Location header built from the {origin}/
// {second-origin} placeholder, either with a literal scheme pasted in front
// of it (server.py's own docstring and selftest fixture already carry the
// scheme in the substituted value — doubling it) or missing the router's
// own "/v2/s-<script-id>/" prefix that every client-facing path in this
// server needs (server.py strips exactly that prefix before matching a
// route, so a Location value without it 404s on the next hop). Called from
// putScript for every route list this file builds, so a future case cannot
// ship the same shape of bug unnoticed.
func validateLocationHeaders(scriptID string, routes []httpRoute) {
	wantPrefix := "/v2/s-" + scriptID + "/"
	for _, route := range routes {
		for _, resp := range route.Responses {
			loc, ok := resp.Headers["Location"]
			if !ok {
				continue
			}
			if strings.Contains(loc, "://{origin}") || strings.Contains(loc, "://{second-origin}") {
				panic(fmt.Sprintf(
					"genfixtures: script %q's Location header %q has a literal scheme pasted in front of "+
						"{origin}/{second-origin}, which already expands to a full http://host:port — "+
						"doubled scheme", scriptID, loc))
			}
			rest, found := strings.CutPrefix(loc, "{origin}")
			if !found {
				rest, found = strings.CutPrefix(loc, "{second-origin}")
			}
			if !found {
				continue // not a templated redirect target
			}
			if !strings.HasPrefix(rest, wantPrefix) {
				panic(fmt.Sprintf(
					"genfixtures: script %q's Location header %q does not continue with the router's own "+
						"%q prefix right after the origin placeholder — a real client 404s on this hop "+
						"(server.py strips exactly that prefix before route-matching)",
					scriptID, loc, wantPrefix))
			}
		}
	}
}

func secondOriginRoutesOrEmpty(r []httpRoute) []httpRoute {
	if r == nil {
		return []httpRoute{}
	}
	return r
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	if neg {
		b = append([]byte{'-'}, b...)
	}
	return string(b)
}
