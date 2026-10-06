package main

import (
	"os"
	"path/filepath"

	"github.com/klauspost/compress/zstd"
)

// selftest.go — proves --check actually catches drift, the same discipline
// every gate in this repository follows (scripts/lint-public.sh,
// scripts/lint-spelling.sh, ...): a checker nobody has seen fail is not a
// checker. Runs entirely in a throwaway temp directory; never touches the
// real tests/fixtures/fetch-v1/ tree.
func runSelftest(root string) {
	fs := buildAll()
	if len(fs.Paths()) == 0 {
		fatalf("selftest: buildAll produced zero files")
	}

	selftestOnlineTagsExist()
	selftestCacheLayoutsExist()
	selftestLocationHeaders()
	selftestBase2ScriptsCanSucceed()
	selftestCompareTwoRealVersions()
	selftestZstdWindowDeclaration()

	tmp, err := os.MkdirTemp("", "genfixtures-selftest-*")
	if err != nil {
		fatalf("selftest: %v", err)
	}
	defer os.RemoveAll(tmp)

	if err := fs.WriteTo(tmp); err != nil {
		fatalf("selftest: writing to temp dir: %v", err)
	}

	// 1. A fresh write must diff clean against itself.
	clean := fs.Diff(tmp, managedPrefixes)
	if len(clean) != 0 {
		fatalf("selftest: a fresh --write is not clean against its own --check:\n  %v", clean)
	}

	// 2. Flipping one byte of one file must be caught.
	victim := fs.Paths()[0]
	victimPath := filepath.Join(tmp, victim)
	content, err := os.ReadFile(victimPath)
	if err != nil {
		fatalf("selftest: %v", err)
	}
	flipped := append([]byte(nil), content...)
	flipped[0] ^= 0xFF
	if err := os.WriteFile(victimPath, flipped, 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	problems := fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: flipping a byte of %s was not caught by --check", victim)
	}

	// 3. Restore it, delete a different file entirely (an "extra file on
	// disk" the generator no longer produces must also be caught — this is
	// what protects a deleted case from leaving its old fixture behind).
	if err := os.WriteFile(victimPath, content, 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	// Always under trees/ (never beside victim, which may itself be
	// cases.json at the tree root — a prefix Diff walks as a single named
	// file, not a directory, so a sibling placed next to it would never be
	// scanned by any managed-prefix walk).
	extraPath := filepath.Join(tmp, "trees", "unexpected-extra-file.json")
	if err := os.WriteFile(extraPath, []byte("{}"), 0o644); err != nil {
		fatalf("selftest: %v", err)
	}
	problems = fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: an extra file under a managed prefix was not caught by --check")
	}
	foundExtra := false
	for _, p := range problems {
		if len(p) >= len("extra on disk") && p[:len("extra on disk")] == "extra on disk" {
			foundExtra = true
		}
	}
	if !foundExtra {
		fatalf("selftest: the extra-file problem was not reported as such:\n  %v", problems)
	}

	// 4. Deleting a file the generator DOES produce must be caught as missing.
	if err := os.Remove(extraPath); err != nil {
		fatalf("selftest: %v", err)
	}
	if err := os.Remove(victimPath); err != nil {
		fatalf("selftest: %v", err)
	}
	problems = fs.Diff(tmp, managedPrefixes)
	if len(problems) == 0 {
		fatalf("selftest: a missing generated file was not caught by --check")
	}
}

// selftestOnlineTagsExist proves checkOnlineTagsExist (cases.go) actually
// catches the exact defect class it exists for: an online, non-frozen,
// non-refusal case whose request spelling names a tag its own tree does
// not have — exactly the bug existing-install-noop and index-race-reapply
// shipped with before this generator checked for it. Never touches a real
// tree: the planted mismatch is a throwaway fake, deregistered afterward.
func selftestOnlineTagsExist() {
	fake := NewTree("selftest-online-tags-exist-fake")
	fake.PutManifest("1.2.3.4", ociIndexMediaType, []byte("{}\n"))
	defer delete(allTrees, fake.Name)

	bad := newCase("selftest-planted-mismatch", fake.Name, "file")
	bad.Request.Spelling = "9.9.9.9" // deliberately not "1.2.3.4", the only tag `fake` has

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkOnlineTagsExist did not catch a planted online-case/tag mismatch")
			}
		}()
		checkOnlineTagsExist([]Case{bad})
	}()

	// And the inverse: a case whose spelling DOES match a real tag must
	// never be flagged — a checker that fires on everything is as useless
	// as one that fires on nothing.
	good := newCase("selftest-planted-match", fake.Name, "file")
	good.Request.Spelling = "1.2.3.4"
	checkOnlineTagsExist([]Case{good})

	// Every exemption this check grants must actually exempt: offline,
	// frozen, and an explicit refusal, each paired with a spelling that
	// still would not resolve, so a checker that forgot to look at these
	// fields would otherwise panic here.
	offlineCase := newCase("selftest-planted-offline", fake.Name, "file")
	offlineCase.Request.Spelling = "9.9.9.9"
	offlineCase.Request.Offline = true
	checkOnlineTagsExist([]Case{offlineCase})

	frozenCase := newCase("selftest-planted-frozen", fake.Name, "file")
	frozenCase.Request.Spelling = "9.9.9.9"
	frozenCase.Request.Frozen = true
	checkOnlineTagsExist([]Case{frozenCase})

	refusalCase := newCase("selftest-planted-refusal", fake.Name, "file")
	refusalCase.Request.Spelling = "9.9.9.9"
	refusalCase.Expect.OK = false
	checkOnlineTagsExist([]Case{refusalCase})
}

// selftestCacheLayoutsExist proves checkCacheLayoutsExist (cases.go)
// actually catches the exact defect class preseed-oras shipped with: a
// case naming setup.cache for a layouts/ directory nothing backs. Never
// touches a real tree or the real excludedFromManagement list (it is
// restored before returning).
func selftestCacheLayoutsExist() {
	fs := NewFileSet()
	fs.Put("layouts/written-layout/oci-layout", []byte("{}\n"))

	good := newCase("selftest-cache-good", "selftest-cache-tree", "file")
	good.Setup.Cache = "written-layout"
	checkCacheLayoutsExist([]Case{good}, fs)

	// A planted mismatch: setup.cache names a directory nobody wrote and
	// which is not declared externally-provided either.
	bad := newCase("selftest-cache-bad", "selftest-cache-tree", "file")
	bad.Setup.Cache = "nonexistent-layout"
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkCacheLayoutsExist did not catch a planted missing-layout case")
			}
		}()
		checkCacheLayoutsExist([]Case{bad}, fs)
	}()

	// newCase's own default, "empty" (model.go), names no layouts/
	// directory at all and must never be flagged.
	emptyCase := newCase("selftest-cache-empty", "selftest-cache-tree", "file")
	checkCacheLayoutsExist([]Case{emptyCase}, fs)

	// An externally-provided exemption (excludedFromManagement) that is NOT
	// actually present on disk must still be caught — a case depending on
	// one must be GATED on its presence (cachecases.go's preseed-oras is
	// the real example), never emitted unconditionally. A fake, never-real
	// name proves this without depending on whether the real
	// layouts/oras-preseed/ happens to have landed in this checkout.
	const fakeExemptName = "selftest-fake-externally-provided-layout"
	savedExemptions := excludedFromManagement
	excludedFromManagement = append(append([]string{}, savedExemptions...), "layouts/"+fakeExemptName)
	defer func() { excludedFromManagement = savedExemptions }()

	exemptButAbsent := newCase("selftest-cache-exempt-absent", "selftest-cache-tree", "file")
	exemptButAbsent.Setup.Cache = fakeExemptName
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkCacheLayoutsExist did not catch an externally-provided layout absent on disk")
			}
		}()
		checkCacheLayoutsExist([]Case{exemptButAbsent}, fs)
	}()
}

// selftestLocationHeaders proves validateLocationHeaders (httpcases.go)
// actually catches the exact defect class redirect-cross-origin-drops-auth
// and redirect-limit both shipped with: a doubled scheme, and a Location
// value missing the router's own "/v2/s-<id>/" prefix.
func selftestLocationHeaders() {
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: validateLocationHeaders did not catch a doubled scheme")
			}
		}()
		validateLocationHeaders("selftest-case", []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{
			{Status: intp(302), Headers: map[string]string{"Location": "http://{origin}/v2/s-selftest-case/x"}},
		}}})
	}()

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: validateLocationHeaders did not catch a Location missing the /v2/s-<id>/ prefix")
			}
		}()
		validateLocationHeaders("selftest-case", []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{
			{Status: intp(302), Headers: map[string]string{"Location": "{origin}/v2/chtypes/v1/manifests/x"}},
		}}})
	}()

	// Well-formed Location headers, on both placeholders, must never be
	// flagged — nor may a response with no Location header at all (most of
	// them).
	validateLocationHeaders("selftest-case", []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{
		{Status: intp(302), Headers: map[string]string{"Location": "{origin}/v2/s-selftest-case/chtypes/v1/manifests/x"}},
		{Status: intp(302), Headers: map[string]string{"Location": "{second-origin}/v2/s-selftest-case/chtypes/v1/manifests/x"}},
		{Status: intp(503)},
	}}})
}

// selftestBase2ScriptsCanSucceed proves checkBase2ScriptsCanSucceed
// (cases.go) actually catches the exact defect class mirror-failover-5xx
// and mirror-failover-digest-404 both shipped with: a {base2} case whose
// second origin cannot ever actually serve success (missing entirely, or
// present but all failures).
func selftestBase2ScriptsCanSucceed() {
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkBase2ScriptsCanSucceed did not catch a {base2} case with no http_script")
			}
		}()
		noScript := newCase("selftest-base2-no-script", "selftest-tree", "http")
		noScript.Request.Bases = []string{"{base}", "{base2}"}
		checkBase2ScriptsCanSucceed([]Case{noScript}, NewFileSet())
	}()

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkBase2ScriptsCanSucceed did not catch empty second_origin_routes")
			}
		}()
		fs := NewFileSet()
		fs.Put("http/selftest-base2-empty.json", canonicalJSON(HTTPScript{
			Schema: 1, ID: "selftest-base2-empty", Tree: "selftest-tree",
			Routes:             []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{{FromTree: true}}}},
			SecondOriginRoutes: []httpRoute{},
		}))
		c := newCase("selftest-base2-empty", "selftest-tree", "http")
		c.HTTPScript = strp("selftest-base2-empty")
		c.Request.Bases = []string{"{base}", "{base2}"}
		checkBase2ScriptsCanSucceed([]Case{c}, fs)
	}()

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: checkBase2ScriptsCanSucceed did not catch all-failure second_origin_routes")
			}
		}()
		fs := NewFileSet()
		fs.Put("http/selftest-base2-allfail.json", canonicalJSON(HTTPScript{
			Schema: 1, ID: "selftest-base2-allfail", Tree: "selftest-tree",
			Routes:             []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{{Status: intp(503)}}}},
			SecondOriginRoutes: []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{{Status: intp(503)}}}},
		}))
		c := newCase("selftest-base2-allfail", "selftest-tree", "http")
		c.HTTPScript = strp("selftest-base2-allfail")
		c.Request.Bases = []string{"{base}", "{base2}"}
		checkBase2ScriptsCanSucceed([]Case{c}, fs)
	}()

	// The inverse: a genuinely succeeding second_origin_routes must pass.
	fsGood := NewFileSet()
	fsGood.Put("http/selftest-base2-good.json", canonicalJSON(HTTPScript{
		Schema: 1, ID: "selftest-base2-good", Tree: "selftest-tree",
		Routes:             []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{{Status: intp(503)}}}},
		SecondOriginRoutes: []httpRoute{{Method: "GET", Path: "/x", Responses: []httpResponse{{FromTree: true}}}},
	}))
	good := newCase("selftest-base2-good", "selftest-tree", "http")
	good.HTTPScript = strp("selftest-base2-good")
	good.Request.Bases = []string{"{base}", "{base2}"}
	checkBase2ScriptsCanSucceed([]Case{good}, fsGood)

	// A case that does not use {base2} at all must never be flagged, even
	// with no script and no fs entries.
	plain := newCase("selftest-base2-not-used", "selftest-tree", "http")
	checkBase2ScriptsCanSucceed([]Case{plain}, NewFileSet())
}

// selftestCompareTwoRealVersions proves mustCompareTwoRealVersions
// (trust.go) actually catches the exact defect class predicate-wrong-version
// shipped with: a symbolic (non-version) request spelling, a symbolic
// predicate version, and the two being equal (no actual mismatch).
func selftestCompareTwoRealVersions() {
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: mustCompareTwoRealVersions did not catch a symbolic (non-version) request spelling")
			}
		}()
		mustCompareTwoRealVersions("selftest-case", "t-wrong-version", "26.7.1.1")
	}()

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: mustCompareTwoRealVersions did not catch a symbolic predicate version")
			}
		}()
		mustCompareTwoRealVersions("selftest-case", "26.8.1.5", "not-a-version")
	}()

	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: mustCompareTwoRealVersions did not catch two equal versions")
			}
		}()
		mustCompareTwoRealVersions("selftest-case", "26.8.1.5", "26.8.1.5")
	}()

	// The inverse: two distinct, real versions must never be flagged.
	mustCompareTwoRealVersions("selftest-case", "26.8.1.5", "26.7.1.1")
}

// selftestZstdWindowDeclaration proves mustDeclareOversizeWindow
// (tarzstd.go) actually catches the exact defect class zstd-window-too-large
// shipped with: a frame whose Window_Descriptor byte does NOT declare a
// windowLog above limits.zstd_window_log_max (because zstd.WithWindowSize
// does not raise the declared window for small content), and the
// Single_Segment case where there is no Window_Descriptor byte at all.
func selftestZstdWindowDeclaration() {
	base := zstdEncode([]byte("selftest payload, deliberately tiny"), zstd.WithSingleSegment(false))

	// A planted TOO-SMALL window (exponent 5 => windowLog 15, under the
	// cap) must be caught.
	tooSmall := append([]byte(nil), base...)
	patchZstdWindowDescriptor(tooSmall, 5)
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: mustDeclareOversizeWindow did not catch a planted too-small window")
			}
		}()
		mustDeclareOversizeWindow(tooSmall)
	}()

	// The inverse: a genuinely oversized declaration must never be flagged.
	oversize := append([]byte(nil), base...)
	patchZstdWindowDescriptor(oversize, 25)
	mustDeclareOversizeWindow(oversize)

	// A Single_Segment frame has no Window_Descriptor byte at all — must be
	// caught too, not panic on a nil dereference or silently pass.
	singleSegment := zstdEncode([]byte("selftest payload, deliberately tiny"), zstd.WithSingleSegment(true))
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: mustDeclareOversizeWindow did not catch a Single_Segment frame (no Window_Descriptor to check)")
			}
		}()
		mustDeclareOversizeWindow(singleSegment)
	}()

	// And patchZstdWindowDescriptor itself must refuse to silently corrupt a
	// byte that is not actually a Window_Descriptor.
	func() {
		defer func() {
			if r := recover(); r == nil {
				fatalf("selftest: patchZstdWindowDescriptor did not refuse a Single_Segment frame")
			}
		}()
		patchZstdWindowDescriptor(append([]byte(nil), singleSegment...), 25)
	}()
}
