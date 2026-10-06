package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
)

func thisFile() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		panic("genfixtures: runtime.Caller failed")
	}
	return file
}

// cases.go — assembles every tree, http-script, lock and case built by the
// category files (resolve.go, trust.go, bytescases.go, cachecases.go,
// lockcases.go, httpcases.go, genericcases.go) into the final FileSet and
// cases.json, per the v1 fetch-layer plan's §3.2/§3.3.

// allCases accumulates across every category file via collectCases; each
// category file's own Build*Cases function returns its slice and this
// file concatenates them in a fixed, readable order (also the order the
// job summary table prints them in via parity.py).
func buildAll() *FileSet {
	fs := NewFileSet()

	var cases []Case
	cases = append(cases, buildResolveCases(fs)...)
	cases = append(cases, buildTrustCases(fs)...)
	cases = append(cases, buildBytesCases(fs)...)
	cases = append(cases, buildCacheCases(fs)...)
	cases = append(cases, buildRecordCases(fs)...)
	cases = append(cases, buildLockCases(fs)...)
	cases = append(cases, buildHTTPCases(fs)...)
	cases = append(cases, buildGenericCases(fs)...)

	checkUniqueIDs(cases)
	checkOnlineTagsExist(cases)
	checkCacheLayoutsExist(cases, fs)
	checkBase2ScriptsCanSucceed(cases, fs)

	cf := CasesFile{
		Schema: 1,
		Source: Source{
			GeneratorCommit: generatorFingerprint(),
			GoSumSHA256:     goSumSHA256(),
		},
		Cases: cases,
	}
	fs.Put("cases.json", canonicalJSON(cf))
	return fs
}

func checkUniqueIDs(cases []Case) {
	seen := map[string]bool{}
	for _, c := range cases {
		if seen[c.ID] {
			panic("genfixtures: duplicate case id " + c.ID)
		}
		seen[c.ID] = true
	}
}

// fullVersionSpellingRe matches an EXACT four-part version
// (major.minor.patch.build) — the same digit-group shape as
// spec/fetch-v1/constants.json's spelling.regex, narrowed to require all
// four parts, since a floating (two- or three-part) request is resolved
// against the tag it names literally and is not in scope here.
var fullVersionSpellingRe = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

// checkOnlineTagsExist is genfixtures' own cross-check, decided here (not
// part of lane 0A's frozen schema): every case that resolves ONLINE
// (neither offline nor frozen — a frozen case resolves by digest and may
// legitimately have no tag at all, e.g. frozen-no-discovery) against an
// EXACT four-part version, and does not already expect a refusal, must
// find a tag of that exact spelling in its own tree — a case whose
// request and whose fixture disagree about which tag exists would
// otherwise 404 against any real server, and only be caught once some
// binding's runner actually tried it.
//
// This exists because of a real defect (found by the Rust lane's own
// conformance run against `v1`, 2026-10-02): existing-install-noop and
// index-race-reapply were both published under a "c-<id>"-shaped tag
// instead of their own version spelling, so index-race-reapply could
// never succeed and existing-install-noop failed for any client that
// actually resolves online rather than trusting the fixture's prose.
func checkOnlineTagsExist(cases []Case) {
	for _, c := range cases {
		if c.Request.Offline || c.Request.Frozen || !c.Expect.OK {
			continue
		}
		if !fullVersionSpellingRe.MatchString(c.Request.Spelling) {
			continue
		}
		tree, ok := allTrees[c.Tree]
		if !ok {
			panic(fmt.Sprintf("genfixtures: case %q names tree %q, which was never built", c.ID, c.Tree))
		}
		if _, ok := tree.tags[c.Request.Spelling]; !ok {
			panic(fmt.Sprintf(
				"genfixtures: case %q resolves online against the exact version %q, but tree %q has no "+
					"tag of that name (it would 404 against a real server) — tag the manifest under its "+
					"own spelling, or mark the case offline/frozen/expecting a refusal if that is deliberate",
				c.ID, c.Request.Spelling, c.Tree))
		}
	}
}

// checkCacheLayoutsExist is genfixtures' own cross-check for the defect
// class preseed-oras shipped with (found by three fetch lanes
// independently, 2026-10-02): a case naming setup.cache = X must be backed
// by a REAL layouts/X directory — either one this generator writes itself,
// or, for the externally-provided exemptions in excludedFromManagement
// (main.go), one that is physically present on disk right now.
//
// "empty" is newCase's own default (model.go) meaning "no pre-populated
// layout at all" and never names a layouts/ directory; it is exempt from
// this check entirely, not merely allowed to be absent.
func checkCacheLayoutsExist(cases []Case, fs *FileSet) {
	const layoutsPrefix = "layouts/"

	writtenLayouts := map[string]bool{}
	for _, p := range fs.Paths() {
		if !strings.HasPrefix(p, layoutsPrefix) {
			continue
		}
		rest := p[len(layoutsPrefix):]
		if i := strings.Index(rest, "/"); i >= 0 {
			writtenLayouts[rest[:i]] = true
		}
	}

	exemptNames := map[string]bool{}
	for _, p := range excludedFromManagement {
		if strings.HasPrefix(p, layoutsPrefix) {
			exemptNames[p[len(layoutsPrefix):]] = true
		}
	}

	for _, c := range cases {
		name := c.Setup.Cache
		if name == "" || name == "empty" {
			continue
		}
		if writtenLayouts[name] {
			continue
		}
		if exemptNames[name] {
			if layoutPresentOnDisk(name) {
				continue
			}
			panic(fmt.Sprintf(
				"genfixtures: case %q names setup.cache %q, which excludedFromManagement (main.go) says is "+
					"externally provided, but layouts/%s is not present on disk right now — a case that "+
					"depends on an externally-provided layout must be GATED on its presence (see "+
					"cachecases.go's preseed-oras), never emitted unconditionally",
				c.ID, name, name))
		}
		panic(fmt.Sprintf(
			"genfixtures: case %q names setup.cache %q, but layouts/%s is neither written by this generator "+
				"nor declared in excludedFromManagement (main.go) — a conformance runner would look for a "+
				"directory that will never exist",
			c.ID, name, name))
	}
}

// checkBase2ScriptsCanSucceed is genfixtures' own cross-check for the exact
// defect class mirror-failover-5xx and mirror-failover-digest-404 both
// shipped with (measured, the Go and Python fetch lanes against cases.json
// on v1, 2026-10-02): a case whose request.bases names "{base2}" — the
// http transport's second, genuinely distinct origin — must have an
// http_script whose second_origin_routes can actually serve success. The
// original defect used two IDENTICAL "{base}" bases, so "failing over"
// re-read the same exhausted script cursor and never succeeded; even after
// introducing {base2}, a script with second_origin_routes that are
// themselves all failures (or missing entirely) would reproduce the same
// bug one level down.
func checkBase2ScriptsCanSucceed(cases []Case, fs *FileSet) {
	for _, c := range cases {
		usesBase2 := false
		for _, b := range c.Request.Bases {
			if b == "{base2}" {
				usesBase2 = true
			}
		}
		if !usesBase2 {
			continue
		}
		if c.HTTPScript == nil {
			panic(fmt.Sprintf("genfixtures: case %q uses {base2} but has no http_script to carry second_origin_routes", c.ID))
		}
		raw, ok := fs.files["http/"+*c.HTTPScript+".json"]
		if !ok {
			panic(fmt.Sprintf("genfixtures: case %q names http_script %q, which was never written", c.ID, *c.HTTPScript))
		}
		var script HTTPScript
		if err := json.Unmarshal(raw, &script); err != nil {
			panic(fmt.Sprintf("genfixtures: case %q's script %q does not parse as an HTTPScript: %v", c.ID, *c.HTTPScript, err))
		}
		if len(script.SecondOriginRoutes) == 0 {
			panic(fmt.Sprintf(
				"genfixtures: case %q uses {base2} but script %q has no second_origin_routes at all — "+
					"the second base would never serve anything",
				c.ID, *c.HTTPScript))
		}
		canSucceed := false
		for _, route := range script.SecondOriginRoutes {
			for _, resp := range route.Responses {
				if resp.FromTree || (resp.Status != nil && *resp.Status >= 200 && *resp.Status < 300) {
					canSucceed = true
				}
			}
		}
		if !canSucceed {
			panic(fmt.Sprintf(
				"genfixtures: case %q uses {base2}, and script %q has second_origin_routes, but none of "+
					"their responses can ever succeed (no from_tree, no 2xx status) — a second base that "+
					"never actually serves content is not a failover target",
				c.ID, *c.HTTPScript))
		}
	}
}

// generatorFingerprint is a 40-hex-character fingerprint of this
// generator's own source (every *.go file in this directory, sorted by
// name) — cases.schema.json's `source.generator_commit` field is shaped
// like a git commit (pattern ^[0-9a-f]{40}$), but a LITERAL git HEAD would
// make cases.json "stale" on every unrelated commit to this repository,
// not just a commit that touches this generator. Decided here, lane 0B:
// use a content fingerprint instead, so the field only ever changes when
// the generator that produced it changes — exactly the staleness this
// field exists to detect (docs/guides/fetch-v1.md §10 records this).
func generatorFingerprint() string {
	dir := filepath.Dir(thisFile())
	entries, err := os.ReadDir(dir)
	if err != nil {
		panic(err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".go" {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	var all []byte
	for _, n := range names {
		b, err := os.ReadFile(filepath.Join(dir, n))
		if err != nil {
			panic(err)
		}
		all = append(all, []byte(n+"\x00")...)
		all = append(all, b...)
		all = append(all, 0)
	}
	return fakeHex(string(all), 40)
}

func goSumSHA256() string {
	dir := filepath.Dir(thisFile())
	b, err := os.ReadFile(filepath.Join(dir, "go.sum"))
	if err != nil {
		panic(err)
	}
	return sha256Hex(b)
}
