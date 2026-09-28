// Package testhook holds the test-only overrides the chtypes package reads.
// Go's internal/ rule limits its importers to this module's own packages, so
// no consumer can reach it: it is not part of the public contract, and
// docs/guides/fetch.md documents no such knob.
//
// It is a package rather than an unexported variable in chtypes because the
// command's tests (go/cmd/chtypes) drive fetch through the CLI and need the
// same override; they cannot reach an unexported name in another package.
package testhook

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// FetchABIRevision is the ABI revision fetch selects release rows at in place
// of chtypes.ABIRevision, when it is non-zero (docs/guides/fetch.md §2). It
// changes WHICH rows are eligible and nothing else: the default registry
// directory stays abi<ABIRevision>/, and the loader still refuses an artifact
// of another revision.
//
// The fetch-fixture suites set it to FixtureABIRevision's answer for their
// fixture set, so a binding whose own revision has moved ahead of the
// fixtures still exercises the whole chain. Nothing else may set it; the
// command's `list` reads it too, so what it shows agrees with what fetch
// would install.
var FetchABIRevision int

// FixtureABIRevision reads every fixture release under dir — the
// tests/fixtures/fetch tree, one release per subdirectory holding an
// index.json — and returns the one abi_revision all of their rows carry.
//
// The revision is DERIVED from the fixtures' own bytes, never typed into a
// test: a suite that hand-set it would be testing its author's belief about
// the fixtures, not the fetch. So anything short of one unambiguous answer is
// an error — no release found, a row with no integer abi_revision, or rows
// that disagree.
func FixtureABIRevision(dir string) (int, error) {
	indexes, err := filepath.Glob(filepath.Join(dir, "*", "index.json"))
	if err != nil {
		return 0, err
	}
	sort.Strings(indexes)
	if len(indexes) == 0 {
		return 0, fmt.Errorf("no <release>/index.json under %s", dir)
	}
	seen := map[int][]string{}
	for _, path := range indexes {
		blob, err := os.ReadFile(path)
		if err != nil {
			return 0, err
		}
		var doc struct {
			Artifacts []map[string]json.RawMessage `json:"artifacts"`
		}
		if err := json.Unmarshal(blob, &doc); err != nil {
			return 0, fmt.Errorf("%s: %w", path, err)
		}
		if len(doc.Artifacts) == 0 {
			return 0, fmt.Errorf("%s lists no artifacts", path)
		}
		for i, row := range doc.Artifacts {
			raw, ok := row["abi_revision"]
			n, err := strconv.Atoi(strings.TrimSpace(string(raw)))
			if !ok || err != nil {
				return 0, fmt.Errorf("%s: row %d carries no integer abi_revision (%s)", path, i, raw)
			}
			rel, _ := filepath.Rel(dir, path)
			if list := seen[n]; len(list) == 0 || list[len(list)-1] != rel {
				seen[n] = append(list, rel)
			}
		}
	}
	if len(seen) != 1 {
		var parts []string
		for n, files := range seen {
			parts = append(parts, fmt.Sprintf("%d in %s", n, strings.Join(files, ", ")))
		}
		sort.Strings(parts)
		return 0, fmt.Errorf("the fixture releases under %s disagree on abi_revision (%s); there is no one revision to fetch them at",
			dir, strings.Join(parts, "; "))
	}
	for n := range seen {
		return n, nil
	}
	panic("unreachable")
}
