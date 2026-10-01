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
	"strconv"
	"strings"
	"sync/atomic"
)

// FetchABIRevision is the ABI revision fetch selects release rows at in place
// of chtypes.ABIRevision, when it is non-zero (docs/guides/fetch.md §2). It
// changes WHICH rows are eligible and nothing else: the default registry
// directory stays abi<ABIRevision>/, and the loader still refuses an artifact
// of another revision.
//
// The fetch-fixture suites set it to FixturesABIRevision's answer for their
// fixture set, so a binding whose own revision has moved ahead of the
// fixtures still exercises the whole chain, and to the two-revisions/
// fixture's low and high revisions to pin the filter itself. Nothing else may
// set it; the command's `list` reads it too, so what it shows agrees with
// what fetch would install.
var FetchABIRevision int

// FixturesABIRevision is the ABI revision the fixture set under dir (the
// tests/fixtures/fetch tree) DECLARES in its expected.json, as
// `fixtures_abi_revision`: the revision every fixture's rows carry except
// two-revisions/, which deliberately spans two.
//
// It is read from the fixtures' own declaration, never typed into a test: a
// suite that hand-set it would be testing its author's belief about the
// fixtures, not the fetch. A missing or non-integer field is an error.
func FixturesABIRevision(dir string) (int, error) {
	path := filepath.Join(dir, "expected.json")
	blob, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(blob, &doc); err != nil {
		return 0, fmt.Errorf("%s: %w", path, err)
	}
	raw, ok := doc["fixtures_abi_revision"]
	if !ok {
		return 0, fmt.Errorf("%s declares no fixtures_abi_revision — regenerate the fixtures from the artifact producer's current set", path)
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		return 0, fmt.Errorf("%s: fixtures_abi_revision is %s, not an integer", path, raw)
	}
	return n, nil
}

// NativeKind names which C release function a NativeFreed observer saw.
type NativeKind int

const (
	FreedSchema NativeKind = iota + 1 // chs_schema_free
	FreedFilter                       // chs_filter_free
	FreedBlock                        // chs_block_free
)

// NativeFreed holds an optional observer of native frees: when it holds a
// function, the chtypes package calls it once for every schema, filter and
// block handle it frees, with the handle's address, right after the C call
// returns and still under the lock that guarded it. It is how the GC tests
// count C frees instead of timing them: an abandoned schema is reclaimed
// exactly when its handle, and each of its open children's, has been seen
// here.
//
// Nil (the default) costs the free path one atomic load. A test installs an
// observer for its own duration and removes it after; the observer must not
// call back into chtypes.
var NativeFreed atomic.Pointer[func(kind NativeKind, handle uintptr)]
