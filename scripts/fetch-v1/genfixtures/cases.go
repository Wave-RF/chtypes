package main

import (
	"os"
	"path/filepath"
	"runtime"
	"sort"
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
	cases = append(cases, buildLockCases(fs)...)
	cases = append(cases, buildHTTPCases(fs)...)
	cases = append(cases, buildGenericCases(fs)...)

	checkUniqueIDs(cases)

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
