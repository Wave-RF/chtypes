package chtypes

// unbuildable_test.go — a (ClickHouse line, platform) pairing the artifact
// producer has declared will NEVER get a build past this SDK's own ABI
// revision (chtypes#150) — darwin-arm64/24.8 today. That pairing's only
// served artifact refuses to load under a revision-5 build, correctly, and
// no amount of retrying or re-fetching fixes it: it is a permanent, by-design
// gap, not a registry that has not caught up yet.
//
// The set comes from $CHTYPES_UNBUILDABLE, "<platform>/<line>"
// (comma-separated, e.g. "darwin-arm64/24.8") — scripts/check-standalone.sh
// reads the release's own served `unbuildable` array (index.json) and sets
// it; nothing here hand-types a pairing or infers one from a missing
// directory. Unset or empty excludes nothing, which is every mode that never
// reaches a live index (--no-artifacts, or any run check-standalone.sh did
// not drive) and every other test file's behavior before this one existed.
//
// Every helper below drops ONLY an exact (platform, line) match, and does so
// LOUDLY — a t.Logf naming the line and the platform in this test's own
// output, or (usableVersions) a t.Skipf when nothing usable is left. A
// pairing $CHTYPES_UNBUILDABLE does not name is never touched: if IT cannot
// open, that is a real failure and stays one — this file only ever narrows
// scope, never widens what counts as success.

import (
	"os"
	"strings"
	"testing"
)

// unbuildableByDesign parses $CHTYPES_UNBUILDABLE into the set of excluded
// "<platform>/<line>" keys.
func unbuildableByDesign() map[string]bool {
	set := map[string]bool{}
	for _, pair := range strings.Split(os.Getenv("CHTYPES_UNBUILDABLE"), ",") {
		pair = strings.TrimSpace(pair)
		if pair != "" {
			set[pair] = true
		}
	}
	return set
}

// excludeUnbuildable drops any Installed entry whose (Platform, Line)
// $CHTYPES_UNBUILDABLE names.
func excludeUnbuildable(t *testing.T, installed []Installed) []Installed {
	t.Helper()
	bad := unbuildableByDesign()
	if len(bad) == 0 {
		return installed
	}
	out := installed[:0:0]
	for _, inst := range installed {
		if bad[inst.Platform+"/"+inst.Line] {
			t.Logf("chtypes#150: excluding %s on %s from this run — the release's own served "+
				"exclusion list says no build past this SDK's ABI revision will ever exist for "+
				"this pairing", inst.Line, inst.Platform)
			continue
		}
		out = append(out, inst)
	}
	return out
}

// filterUnbuildableVersions is excludeUnbuildable for a bare version list
// (r.Versions()), matched against HostPlatform() — every version such a list
// names is necessarily this host's own platform.
func filterUnbuildableVersions(t *testing.T, versions []string) []string {
	t.Helper()
	bad := unbuildableByDesign()
	if len(bad) == 0 {
		return versions
	}
	out := versions[:0:0]
	for _, v := range versions {
		if bad[HostPlatform()+"/"+v] {
			t.Logf("chtypes#150: excluding %s on %s from this run — the release's own served "+
				"exclusion list says no build past this SDK's ABI revision will ever exist for "+
				"this pairing", v, HostPlatform())
			continue
		}
		out = append(out, v)
	}
	return out
}

// usableVersions is r.Versions() filtered through filterUnbuildableVersions.
// Registry.Versions() lists every line construction discovered, whether or
// not it was preloaded (see its own doc comment) — so filtering what goes
// into WithPreload does not remove an excluded line from THIS list. Every
// test that means "every version this registry knows, and each one opens"
// uses this instead of r.Versions() directly. Skips loudly, by name, if
// nothing usable is left, so a caller may always index [0] on what comes
// back without a nil-slice check.
func usableVersions(t *testing.T, r *Registry) []string {
	t.Helper()
	out := filterUnbuildableVersions(t, r.Versions())
	if len(out) == 0 {
		t.Skipf("chtypes#150: every version this registry discovered under %s is excluded by "+
			"design — nothing left to test with", strings.Join(r.SearchPath(), ", "))
	}
	return out
}
